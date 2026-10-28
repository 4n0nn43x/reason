"""Client HTTP minimal pour un service Reason (stdlib). Délai d'attente, nouvelles tentatives, repli sûr.

    from reason.client import Client
    rc = Client("http://127.0.0.1:8787", token=os.environ["REASON_TOKEN"])
    dec = rc.decide(request, fallback="hold")      # service injoignable : décision de repli marquée degraded
    rc.outcome({"decision_id": dec["decision_id"], "result": "success", "value": 0.012})

Nouvelles tentatives : sur connexion refusée (la requête n'est pas partie) pour toutes les routes ; sur délai dépassé,
409 ou 5xx seulement pour les routes idempotentes. /decide le devient : une idempotency_key est générée si la requête
n'en a pas, donc un renvoi rend la même décision. /estimate et /execute (appels modèle facturés) ne sont jamais renvoyés
après un délai dépassé. Une erreur 4xx (requête invalide, jeton refusé) n'est jamais masquée par le repli.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid

IDEMPOTENT = {"/decide", "/outcome", "/estimate/outcome", "/estimate/record"}


class ReasonError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Client:
    def __init__(self, url: str = "http://127.0.0.1:8787", token: str | None = None, timeout: float = 30,
                 retries: int = 2, backoff: float = 0.5):
        self.url, self.token, self.timeout, self.retries, self.backoff = url.rstrip("/"), token, timeout, retries, backoff

    def decide(self, request: dict, fallback: str | None = None) -> dict:
        """fallback : action renvoyée si le service est injoignable (jamais sur une erreur 4xx)."""
        request = {**request, "idempotency_key": request.get("idempotency_key") or "c_" + uuid.uuid4().hex}
        try:
            return self.post("/decide", request)
        except ReasonError as e:
            if fallback is None or (e.status is not None and e.status < 500 and e.status != 409):
                raise
            return {"decision_id": None, "namespace": request.get("namespace"), "action": {"name": fallback, "params": {}},
                    "tier": "client_fallback", "confidence": 0.0, "degraded": True, "rationale": f"Reason injoignable : {e}",
                    "needs_human": {"required": False}, "intent_key": None}

    def outcome(self, o: dict) -> dict:
        return self.post("/outcome", o)

    def estimate(self, x: dict) -> dict:
        return self.post("/estimate", x)

    def record_estimate(self, x: dict) -> dict:
        return self.post("/estimate/record", x)

    def estimate_outcome(self, o: dict) -> dict:
        return self.post("/estimate/outcome", o)

    def execute(self, x: dict) -> dict:
        return self.post("/execute", x)

    def lessons(self, ns: str) -> dict:
        return self._call("GET", f"/lessons/{ns}", None)

    def health(self) -> dict:
        return self._call("GET", "/health", None)

    def post(self, path: str, body: dict) -> dict:
        return self._call("POST", path, body)

    def _call(self, method: str, path: str, body: dict | None) -> dict:
        safe = method == "GET" or path in IDEMPOTENT
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.token}"} if self.token else {})}
        last: ReasonError | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.backoff * attempt)
            req = urllib.request.Request(self.url + path, method=method, headers=headers,
                                         data=None if body is None else json.dumps(body).encode())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                msg = e.read()[:500].decode(errors="replace")
                last = ReasonError(f"HTTP {e.code}: {msg}", e.code)
                if not (safe and (e.code >= 500 or e.code == 409)):
                    raise last from None
            except urllib.error.URLError as e:
                last = ReasonError(f"connexion impossible : {e.reason}")
                refused = isinstance(e.reason, ConnectionRefusedError)
                if not (safe or refused):
                    raise last from None
            except (TimeoutError, OSError) as e:
                last = ReasonError(f"délai dépassé ou coupure : {e}")
                if not safe:
                    raise last from None
        raise last or ReasonError("échec sans réponse")
