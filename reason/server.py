"""Un seul service, trois façades sur le même moteur :
  POST /decide  POST /outcome  POST /execute  GET /lessons/<ns>  GET /health      (JSON, gate universel)
  POST /mcp     JSON-RPC MCP (initialize, tools/list, tools/call), stateless        (n8n MCP Client, Claude, Cursor...)
  POST /hooks/claude   hook http de Claude Code : PreToolUse → decide, SessionStart/UserPromptSubmit → leçons
Stdlib uniquement (ThreadingHTTPServer).
"""
from __future__ import annotations

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
from .core import EXECUTE_SCHEMA, OUTCOME_SCHEMA, REQUEST_SCHEMA, Engine
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
]


def load_config(path: str | None = None) -> dict:
    p = Path(path or os.environ.get("REASON_CONFIG", "reason.config.json"))
    cfg = json.loads(p.read_text()) if p.exists() else {}
    cfg.setdefault("token", os.environ.get("REASON_TOKEN"))
    return cfg


class Handler(BaseHTTPRequestHandler):
    engine: Engine
    token: str | None
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

    def _authed(self) -> bool:
        if not self.token:
            return True
        return hmac.compare_digest(self.headers.get("Authorization") or "", f"Bearer {self.token}")

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
        if self.path.startswith("/lessons/"):
            ns = self.path[len("/lessons/"):]
            try:
                pb = self.engine.playbook(ns)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, {"namespace": ns, "lessons": pb.active(), "pending": pb.pending()})
        self._json(404, {"error": "inconnu"})

    def do_POST(self):
        if not self._authed():
            return self._json(401, {"error": "non autorisé"})
        if not self._origin_ok():
            return self._json(403, {"error": "origine refusée"})
        try:
            body = self._body()
            if self.path == "/decide":
                return self._json(200, self.engine.decide(body))
            if self.path == "/outcome":
                return self._json(200, self.engine.outcome(body))
            if self.path == "/execute":
                return self._json(200, self.engine.execute(body))
            if self.path == "/mcp":
                return self._mcp(body)
            if self.path == "/hooks/claude":
                return self._json(200, self._claude_hook(body))
            return self._json(404, {"error": "inconnu"})
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
                fn = {"decide": self.engine.decide, "outcome": self.engine.outcome, "execute": self.engine.execute}.get(name)
                if fn is None or not isinstance(args, dict):
                    return self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"outil inconnu ou arguments invalides: {name}"}})
                try:
                    out = fn(args)
                    result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                              "structuredContent": out, "isError": False}
                except (ValueError, KeyError, LLMError) as e:
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
    Handler.engine = Engine(config)
    Handler.token = config.get("token")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Reason {__version__} sur http://{host}:{port}  rôles={Handler.engine.roles}  "
          f"auth={'oui' if Handler.token else 'non (REASON_TOKEN non défini)'}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
