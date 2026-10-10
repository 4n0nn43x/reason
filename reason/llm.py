"""Client minimal pour toute API « chat completions » compatible OpenAI (OpenAI, DeepSeek, OpenRouter, Together, LiteLLM, vLLM).

Stdlib uniquement. Une seule méthode utile : chat(). Les sorties JSON passent par response_format
json_schema strict quand le fournisseur le supporte, sinon json_object + validation côté code.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, base_url: str, model: str, api_key: str | None = None, api_key_env: str | None = None,
                 json_schema: bool = True, reasoning_effort: str | None = None, timeout: int = 90,
                 extra: dict | None = None, max_tokens: int = 1024, name: str = "llm"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or (os.environ.get(api_key_env) if api_key_env else None)
        self.json_schema = json_schema
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.extra = extra or {}
        self.max_tokens = max_tokens
        self.name = name

    @classmethod
    def from_config(cls, name: str, cfg: dict) -> "LLM":
        return cls(name=name, **{k: v for k, v in cfg.items() if k in {
            "base_url", "model", "api_key", "api_key_env", "json_schema", "reasoning_effort",
            "timeout", "extra", "max_tokens"}})

    def chat(self, messages: list[dict], schema: dict | None = None, temperature: float = 0.0,
             max_tokens: int | None = None, retries: int = 1) -> tuple[dict | str, dict]:
        """Retourne (contenu, usage). Si schema est fourni, contenu est un dict validé par le fournisseur ou parsé."""
        body: dict = {"model": self.model, "messages": list(messages), "temperature": temperature,
                      "max_tokens": max_tokens or self.max_tokens, **self.extra}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if schema is not None:
            if self.json_schema:
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "decision", "strict": True, "schema": schema}}
            else:
                body["response_format"] = {"type": "json_object"}
                body["messages"] = messages + [{"role": "system", "content":
                    "Réponds uniquement avec un objet JSON valide respectant ce schéma JSON :\n" + json.dumps(schema)}]
        last = None
        for attempt in range(retries + 1):
            try:
                raw = self._post(body)
                content = raw["choices"][0]["message"].get("content") or ""
                usage = raw.get("usage") or {}
                if schema is None:
                    return content, usage
                return _parse_json(content), usage
            except (LLMError, ValueError, KeyError, IndexError) as e:  # réponse mal formée : on retente une fois
                last = e
                if attempt < retries:
                    time.sleep(0.5)
        raise LLMError(f"{self.name}: {last}")

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise LLMError(f"HTTP {e.code}: {e.read()[:500].decode(errors='replace')}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError(str(e)) from None


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"pas de JSON dans la réponse : {text[:200]!r}")
    return json.loads(text[start:end + 1])
