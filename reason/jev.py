"""Adaptateur Jev (TypeSafe AI) : modèle de décision typée, pas un LLM. Même interface chat() que LLM,
donc utilisable sur n'importe quel rôle via {"provider": "jev"}. Même format chez TypeSafe, OpenRouter
(base_url https://openrouter.ai/api, model jev-latest) et Vercel (base_url https://ai-gateway.vercel.sh/typesafe, model typesafe-ai/jev).

Limites connues : Jev ne produit pas de paramètres d'action (params = {}), donc une action qui exige des paramètres
sera escaladée au raisonneur ; il n'explique pas (rationale = probabilités) ; state texte, 32k tokens max.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from .core import META_ACTIONS
from .llm import LLMError


class JevDecider:
    def __init__(self, base_url: str = "https://api.typesafe.ai", model: str = "jev-latest", api_key: str | None = None,
                 api_key_env: str | None = "TYPESAFE_API_KEY", timeout: int = 20, name: str = "jev", **_ignored):
        self.base_url, self.model, self.timeout, self.name = base_url.rstrip("/"), model, timeout, name
        self.api_key = api_key or (os.environ.get(api_key_env) if api_key_env else None)

    def chat(self, messages: list[dict], schema: dict | None = None, temperature: float = 0.0,
             max_tokens: int | None = None, retries: int = 1) -> tuple[dict | str, dict]:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        user = "\n".join(m["content"] for m in messages if m["role"] == "user")
        if schema is None:  # empreinte / usage libre : une question noul sur le texte
            ans, usage = self._ask(user, {"q": {"type": "noul", "instructions": "The statement in the state is true."}})
            return str(ans["q"].get("noul")), usage
        names = [n for n in schema["properties"]["action"]["enum"] if n not in META_ACTIONS]
        try:
            req = json.loads(user)
            criteria = {a["name"]: a.get("description") for a in req.get("candidate_actions", []) if a["name"] in names}
        except (ValueError, TypeError, KeyError):
            criteria = {}
        criteria = {n: criteria.get(n) for n in names}
        criteria["abstain"] = "None of the actions clearly applies, or more information is needed"
        ans, usage = self._ask(user, {"action": {"type": "choice", "instructions": system[:8000], "criteria": criteria}})
        a = ans["action"]
        probs = a.get("probabilities", {})
        return {"action": a["choice"], "params": {}, "confidence": float(a.get("confidence", 0.0)),
                "rationale": "jev " + json.dumps(probs, ensure_ascii=False), "lessons_used": []}, usage

    def _ask(self, state: str, questions: dict) -> tuple[dict, dict]:
        body = json.dumps({"model": self.model, "state": state, "questions": questions}).encode()
        req = urllib.request.Request(self.base_url + "/v1/systemone", data=body, headers={
            "Content-Type": "application/json", **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        last = None
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    out = json.loads(r.read().decode())
                return out["answers"], out.get("usage", {})
            except urllib.error.HTTPError as e:
                last = LLMError(f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")
                if e.code not in (429, 529, 502, 503) or attempt:
                    break
                time.sleep(1.0)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as e:
                last = LLMError(str(e)); break
        raise LLMError(f"{self.name}: {last}")
