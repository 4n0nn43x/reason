"""Client minimal pour toute API « chat completions » compatible OpenAI (OpenAI, DeepSeek, OpenRouter, Together, LiteLLM, vLLM).

Stdlib uniquement. Une seule méthode utile : chat(). Les sorties JSON passent par response_format
json_schema strict quand le fournisseur le supporte, sinon json_object + validation côté code.
Options par modèle : temperature (null pour ne pas l'envoyer, requis par les modèles de raisonnement OpenAI),
max_tokens_key ("max_tokens" ou "max_completion_tokens"), reasoning_effort, extra (champs ajoutés tels quels).
"""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, base_url: str, model: str, api_key: str | None = None, api_key_env: str | None = None,
                 json_schema: bool = True, reasoning_effort: str | None = None, timeout: int = 90,
                 extra: dict | None = None, max_tokens: int = 1024, max_tokens_key: str = "max_tokens",
                 temperature: float | None = 0.0, name: str = "llm"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or (os.environ.get(api_key_env) if api_key_env else None)
        self.json_schema = json_schema
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.extra = extra or {}
        self.max_tokens = max_tokens
        self.max_tokens_key = max_tokens_key
        self.temperature = temperature
        self.name = name

    @classmethod
    def from_config(cls, name: str, cfg: dict) -> "LLM":
        known = {"base_url", "model", "api_key", "api_key_env", "json_schema", "reasoning_effort", "timeout", "extra",
                 "max_tokens", "max_tokens_key", "temperature"}
        unknown = set(cfg) - known
        if unknown:
            raise ValueError(f"modèle {name}: options inconnues {sorted(unknown)}")
        return cls(name=name, **cfg)

    def chat(self, messages: list[dict], schema: dict | None = None, temperature: float | None = None,
             max_tokens: int | None = None, retries: int = 1) -> tuple[dict | str, dict]:
        """Retourne (contenu, usage). Si schema est fourni, contenu est un dict parsé (à valider par l'appelant)."""
        messages = [dict(m) for m in messages]
        body: dict = {"model": self.model, "messages": messages, self.max_tokens_key: max_tokens or self.max_tokens}
        temp = self.temperature if temperature is None else temperature
        if temp is not None:
            body["temperature"] = temp
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if schema is not None:
            if self.json_schema:
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "decision", "strict": True, "schema": schema}}
            else:
                body["response_format"] = {"type": "json_object"}
                note = "\n\nRéponds uniquement avec un objet JSON valide respectant ce schéma JSON :\n" + json.dumps(schema)
                sys_idx = next((i for i, m in enumerate(messages) if m["role"] == "system"), None)
                if sys_idx is None:
                    messages.insert(0, {"role": "system", "content": note.strip()})
                else:
                    messages[sys_idx]["content"] += note
        body.update(self.extra)
        body = {k: v for k, v in body.items() if v is not None}
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                raw = self._post(body)
                choice = raw["choices"][0]
                content = choice["message"].get("content") or ""
                if choice.get("finish_reason") == "length":
                    raise LLMError("réponse tronquée (finish_reason=length), augmenter max_tokens")
                usage = raw.get("usage") or {}
                if schema is None:
                    return content, usage
                if not content.strip():
                    raise LLMError("contenu vide")
                return _parse_json(content), usage
            except _Retryable as e:
                last = e
                if attempt < retries:
                    time.sleep(0.5 * (attempt + 1))
            except (LLMError, ValueError, KeyError, IndexError) as e:
                last = e
                if attempt < retries and not isinstance(e, LLMError):   # JSON mal formé : une relance
                    continue
                break
        raise LLMError(f"{self.name}: {last}")

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            msg = f"HTTP {e.code}: {e.read()[:500].decode(errors='replace')}"
            raise (_Retryable if e.code in RETRY_STATUS else LLMError)(msg) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:
            raise _Retryable(str(e)) from None


class _Retryable(LLMError):
    pass


def _parse_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"pas de JSON dans la réponse : {text[:200]!r}")
    return json.loads(text[start:end + 1])
