"""Un seul service, trois façades sur le même moteur :
  POST /decide  POST /outcome  POST /execute  GET /lessons/<ns>  GET /health      (JSON, gate universel)
  POST /mcp     JSON-RPC MCP (initialize, tools/list, tools/call), stateless        (n8n MCP Client, Claude, Cursor...)
  POST /hooks/claude   hook http de Claude Code : PreToolUse → decide, SessionStart/UserPromptSubmit → leçons
Stdlib uniquement (ThreadingHTTPServer).
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .core import ESTIMATE_OUTCOME_SCHEMA, ESTIMATE_SCHEMA, EXECUTE_SCHEMA, OUTCOME_SCHEMA, REQUEST_SCHEMA, Engine
from .llm import LLMError

MAX_BODY = 1_000_000
MAX_TOOL_INPUT_CHARS = 4000
MCP_TOOLS = [
    {"name": "decide", "description": "Prend une décision bornée pour une automatisation (règles → modèle → humain). "
                                      "Retourne action, confiance, besoin d'humain.", "inputSchema": REQUEST_SCHEMA},
    {"name": "outcome", "description": "Rapporte le résultat réel d'une décision (success|failure|unknown) pour l'apprentissage.",
     "inputSchema": OUTCOME_SCHEMA},
    {"name": "execute", "description": "Exécute une tâche de texte/extraction avec le modèle exécuteur et les leçons du namespace.",
     "inputSchema": EXECUTE_SCHEMA},
    {"name": "estimate", "description": "Estimation probabiliste calibrée (binaire, catégorielle, ordinale, quantité) avec source et preuves.",
     "inputSchema": ESTIMATE_SCHEMA},
    {"name": "estimate_outcome", "description": "Résout une estimation (issue réelle) pour la scorer (Brier, log-loss) et créditer les leçons.",
     "inputSchema": ESTIMATE_OUTCOME_SCHEMA},
]
SCOPE = {"decide": "decide", "outcome": "outcome", "execute": "execute", "estimate": "estimate", "estimate_outcome": "estimate"}


class Forbidden(Exception):
    """Jeton valide mais sans droit sur ce scope ou ce namespace (distinct d'un PermissionError du système de fichiers)."""


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def load_config(path: str | None = None) -> dict:
    p = Path(path or os.environ.get("REASON_CONFIG", "reason.config.json"))
    cfg = json.loads(p.read_text()) if p.exists() else {}
    cfg.setdefault("token", os.environ.get("REASON_TOKEN"))
    return cfg


class Handler(BaseHTTPRequestHandler):
    engine: Engine
    token: str | None = None          # jeton administrateur (REASON_TOKEN) : tous les namespaces, tous les scopes
    tokens: dict = {}                 # config["tokens"] : {secret: {"namespaces": ["acme/*"], "scopes": ["decide", ...]}}
    insecure: bool = False            # aucun jeton : seulement avec serve --insecure
    server_version = f"Reason/{__version__}"
    timeout = 30   # un client qui annonce un Content-Length et n'envoie rien ne tient pas un thread indéfiniment

    def log_message(self, fmt, *args):  # journal compact
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # --- plomberie
    def _json(self, code: int, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("corps trop volumineux")
        raw = self.rfile.read(n) if n else b"{}"
        obj = json.loads(raw or b"{}")
        if not isinstance(obj, dict):
            raise ValueError("objet JSON attendu")
        return obj

    def _principal(self) -> dict | None:
        """Droits du porteur du jeton, ou None. Comparaison à temps constant sur chaque jeton connu."""
        table = dict(self.tokens or {})
        if self.token:
            table[self.token] = {"namespaces": ["*"], "scopes": ["*"]}
        if not table:
            return {"namespaces": ["*"], "scopes": ["*"]} if self.insecure else None
        got = (self.headers.get("Authorization") or "").encode("utf-8", "surrogateescape")
        found = None
        for secret, rights in table.items():       # octets : compare_digest refuse les str non ASCII
            if hmac.compare_digest(got, f"Bearer {secret}".encode("utf-8")):
                found = rights
        return found

    def _authed(self) -> bool:
        return self._principal() is not None

    def _check(self, scope: str, ns: str | None) -> None:
        """Lève PermissionError si le jeton n'a pas le scope sur ce namespace (namespace inconnu : jeton global requis)."""
        pr = self._principal() or {}
        scopes, nss = pr.get("scopes", []), pr.get("namespaces", [])
        if "*" not in scopes and scope not in scopes:
            raise Forbidden(f"scope {scope} non accordé à ce jeton")
        if "*" in nss:
            return
        if not ns or not any(fnmatch.fnmatchcase(ns, pat) for pat in nss):
            raise Forbidden("namespace non accordé à ce jeton")

    def _ns_of(self, kind: str, body: dict) -> str | None:
        if kind in ("decide", "execute", "estimate"):
            return body.get("namespace") if isinstance(body.get("namespace"), str) else None
        if kind == "outcome":
            self.engine.refresh()
            rec = self.engine._by_id.get(body.get("decision_id"))
            return rec["request"]["namespace"] if rec else None
        if kind == "estimate_outcome":
            rec = self.engine._estimates.get(body.get("estimate_id"))
            return rec["estimate"]["namespace"] if rec else None
        return None

    def _origin_ok(self) -> bool:
        """Anti DNS rebinding (MCP) : un Origin présent doit être local ou dans REASON_ALLOWED_ORIGINS."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = (urlparse(origin).hostname or "").lower()
        allowed = {h.strip().lower() for h in os.environ.get("REASON_ALLOWED_ORIGINS", "").split(",") if h.strip()}
        return host in {"localhost", "127.0.0.1", "::1"} | allowed

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "version": __version__})
        if self.path == "/mcp":
            return self._json(405, {"error": "pas de flux SSE : utiliser POST"})
        if not self._authed():
            return self._json(401, {"error": "non autorisé"})
        if self.insecure and not self.token and not self.tokens:     # sans jeton : anti DNS rebinding sur l'en-tête Host
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in LOOPBACK:
                return self._json(403, {"error": "hôte refusé"})
        if self.path.startswith(("/lessons/", "/pending/")):
            try:
                self._check("read", self.path.split("/", 2)[2])
            except Forbidden as e:
                return self._json(403, {"error": str(e)})
        if self.path.startswith("/lessons/"):
            ns = self.path[len("/lessons/"):]
            try:
                pb = self.engine.playbook(ns)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, {"namespace": ns, "lessons": pb.active(), "pending": pb.pending(), "warnings": pb.warnings})
        if self.path.startswith("/pending/"):
            ns = self.path[len("/pending/"):]
            try:
                return self._json(200, {"namespace": ns, "pending": self.engine.pending(ns)})
            except ValueError as e:
                return self._json(400, {"error": str(e)})
        self._json(404, {"error": "inconnu"})

    def do_POST(self):
        if not self._authed():
            return self._json(401, {"error": "non autorisé"})
        if not self._origin_ok():
            return self._json(403, {"error": "origine refusée"})
        try:
            body = self._body()
            route = {"/decide": "decide", "/outcome": "outcome", "/execute": "execute", "/estimate": "estimate",
                     "/estimate/outcome": "estimate_outcome", "/estimate_outcome": "estimate_outcome"}.get(self.path)
            if route:
                self._check(SCOPE[route], self._ns_of(route, body))
                return self._json(200, getattr(self.engine, route)(body))
            if self.path == "/mcp":
                return self._mcp(body)
            if self.path == "/hooks/claude":
                return self._json(200, self._claude_hook(body))
            return self._json(404, {"error": "inconnu"})
        except Forbidden as e:
            return self._json(403, {"error": str(e)})
        except (ValueError, KeyError) as e:
            return self._json(400, {"error": str(e)})
        except LLMError as e:
            return self._json(502, {"error": f"modèle: {e}"})
        except Exception:  # noqa: BLE001  jamais de 500 silencieux
            traceback.print_exc()
            return self._json(500, {"error": "erreur interne"})

    # --- façade MCP (JSON-RPC 2.0 sur POST, stateless : aucune session requise)
    def _mcp(self, msg: dict):
        rid, method, params = msg.get("id"), msg.get("method"), msg.get("params")
        if not isinstance(method, str):
            return self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": "requête JSON-RPC invalide"}})
        if method.startswith("notifications/"):
            self.send_response(202); self.end_headers(); return
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "params doit être un objet"}})
        try:
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2025-11-25"),
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "reason", "version": __version__}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": MCP_TOOLS}
            elif method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                fn = getattr(self.engine, name) if name in SCOPE else None
                if fn is None or not isinstance(args, dict):
                    return self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"outil inconnu ou arguments invalides: {name}"}})
                try:
                    self._check(SCOPE[name], self._ns_of(name, args))
                    out = fn(args)
                    result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                              "structuredContent": out, "isError": False}
                except (ValueError, KeyError, LLMError, Forbidden) as e:
                    result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
            else:
                return self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"méthode inconnue {method}"}})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}})
        return self._json(200, {"jsonrpc": "2.0", "id": rid, "result": result})

    # --- hook http Claude Code : le gate « décider avant l'action »
    def _claude_hook(self, h: dict) -> dict:
        ev = h.get("hook_event_name")
        ns = os.environ.get("REASON_HOOK_NAMESPACE") or "claude/" + hook_namespace(h.get("cwd"))
        self._check("decide" if ev == "PreToolUse" else "read", ns)
        if ev == "PreToolUse":
            tool_input = json.dumps(h.get("tool_input"), ensure_ascii=False)[:MAX_TOOL_INPUT_CHARS]
            # le point de décision inclut l'empreinte de l'entrée : l'anti-boucle ne vise que la répétition du même appel
            point = f"tool:{h.get('tool_name')}:{hashlib.sha1(tool_input.encode()).hexdigest()[:8]}"
            dec = self.engine.decide({
                "namespace": ns, "run_id": h.get("session_id"), "decision_point": point,
                "event": {"type": "tool_call", "tool_name": h.get("tool_name"), "tool_input": tool_input},
                "state": {"cwd": h.get("cwd"), "permission_mode": h.get("permission_mode")},
                "candidate_actions": [{"name": "allow", "schema": {"type": "object", "additionalProperties": False}},
                                      {"name": "deny", "schema": {"type": "object", "additionalProperties": False}}],
                "idempotency_key": h.get("tool_use_id")})
            name = dec["action"]["name"]
            decision = {"allow": "allow", "deny": "deny"}.get(name, "ask")
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                           "permissionDecisionReason": f"Reason[{dec['tier']}] {dec['rationale']}"[:500]}}
        if ev in ("SessionStart", "UserPromptSubmit"):
            text, _ = self.engine.playbook(ns).render(self.engine.thresholds["lessons_max_chars"])
            if not text:
                return {}
            return {"hookSpecificOutput": {"hookEventName": ev, "additionalContext": "Leçons Reason pour ce projet :\n" + text}}
        return {}


def hook_namespace(cwd: str | None) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(cwd or "").name).lstrip("._-")
    return name or "default"


def serve(config: dict, host: str = "127.0.0.1", port: int = 8787) -> None:
    Handler.token = config.get("token")
    Handler.tokens = config.get("tokens") or {}
    Handler.insecure = bool(config.get("insecure"))
    if not Handler.token and not Handler.tokens and not Handler.insecure:
        sys.exit("refus de démarrer sans authentification : définir REASON_TOKEN ou config.tokens (ou serve --insecure en local)")
    if Handler.insecure and not Handler.token and not Handler.tokens and host not in LOOPBACK:
        sys.exit("--insecure n'est permis que sur une adresse locale (127.0.0.1, localhost, ::1)")
    engine = Engine(config)
    # un seul serveur par data_dir : l'idempotence des décisions vit en mémoire de ce processus
    lock = open(engine.data_dir / ".serve.lock", "a")
    try:
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:      # pragma: no cover
        pass
    except OSError:
        sys.exit(f"un autre `reason serve` utilise déjà {engine.data_dir}")
    Handler.engine = engine
    srv = ThreadingHTTPServer((host, port), Handler)
    mode = "jeton administrateur" if Handler.token else ("jetons par namespace" if Handler.tokens else "AUCUNE (--insecure)")
    print(f"Reason {__version__} sur http://{host}:{port}  rôles={engine.roles}  auth={mode}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
