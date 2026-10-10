"""Adaptateur Jev (TypeSafe AI) : modèle de décision typée, pas un LLM. Même interface chat() que LLM,
donc utilisable comme décideur via {"provider": "jev"}. Même format chez TypeSafe, OpenRouter
(base_url https://openrouter.ai/api, model jev-latest) et Vercel (base_url https://ai-gateway.vercel.sh/typesafe, model typesafe-ai/jev).

Limites connues : Jev ne produit pas de paramètres d'action (params = {}), donc une action qui exige des paramètres
est escaladée au raisonneur ; il n'explique pas (rationale = probabilités) ; rôle décideur uniquement (pas exécuteur
ni reflector) ; state texte, 32k tokens max.
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
                 api_key_env: str | None = "TYPESAFE_API_KEY", timeout: int = 20, name: str = "jev"):
        self.base_url, self.model, self.timeout, self.name = base_url.rstrip("/"), model, timeout, name
        self.api_key = api_key or (os.environ.get(api_key_env) if api_key_env else None)

    def chat(self, messages: list[dict], schema: dict | None = None, temperature: float | None = None,
             max_tokens: int | None = None, retries: int = 1) -> tuple[dict | str, dict]:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        user = "\n".join(m["content"] for m in messages if m["role"] == "user")
        if schema is None:  # empreinte : une question noul sur le texte
            ans, usage = self._ask(user, {"q": {"type": "noul", "instructions": "The statement in the state is true."}})
            return str(ans.get("q", {}).get("noul")), usage
        try:
            names = [n for n in schema["properties"]["action"]["enum"] if n not in META_ACTIONS]
        except (KeyError, TypeError):
            raise LLMError(f"{self.name}: Jev ne sert que de décideur (schéma de décision attendu)") from None
        try:
            req = json.loads(user)
            desc = {a["name"]: a.get("description") or "" for a in req.get("candidate_actions", [])}
        except (ValueError, TypeError, KeyError, AttributeError):
            desc = {}
        criteria = {n: desc.get(n, "") for n in names}
        criteria["abstain"] = "None of the actions clearly applies, or more information is needed"
        criteria["ask_human"] = "A human must decide: high stakes, irreversible, or outside the policy"
        ans, usage = self._ask(user, {"action": {"type": "choice", "instructions": system[:8000], "criteria": criteria}})
        try:
            a = ans["action"]
            return {"action": a["choice"], "params_json": "{}", "confidence": float(a.get("confidence", 0.0)),
                    "rationale": "jev " + json.dumps(a.get("probabilities", {}), ensure_ascii=False), "lessons_used": []}, usage
        except (KeyError, TypeError, ValueError) as e:
            raise LLMError(f"{self.name}: réponse inattendue {e}") from None

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
