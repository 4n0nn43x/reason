"""Un seul service, trois façades sur le même moteur :
  POST /decide  POST /outcome  POST /execute  GET /lessons/<ns>  GET /health      (JSON, gate universel)
  POST /mcp     JSON-RPC MCP (initialize, tools/list, tools/call), stateless        (n8n MCP Client, Claude, Cursor...)
  POST /hooks/claude   hook http de Claude Code : PreToolUse → decide, SessionStart/UserPromptSubmit → leçons
  POST /hooks/claude/answer   réponse humaine à un appel d'outil escaladé (scope approve)
  POST /decisions/answer      réponse humaine à une décision escaladée (scope approve) ; GET /decisions/<id> (scope read)
  POST /chat                  conversation en lecture seule (config chat, scope read sur tous les namespaces)
  POST /telegram/callback     clic sur un bouton Reason relayé par l'application qui lit le bot partagé (scope approve)
Stdlib uniquement (ThreadingHTTPServer).
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .core import ESTIMATE_OUTCOME_SCHEMA, ESTIMATE_RECORD_SCHEMA, ESTIMATE_SCHEMA, EXECUTE_SCHEMA, OUTCOME_SCHEMA, REQUEST_SCHEMA, Engine, InProgress
from .llm import LLMError

MAX_BODY = 1_000_000
MAX_TOOL_INPUT_CHARS = 4000


def _tool(name: str, title: str, description: str, schema: dict, destructive: bool, idempotent: bool, open_world: bool) -> dict:
    """Outil MCP avec ses annotations (spec 2025-11-25, ToolAnnotations). Aucun outil n'est en lecture seule : tous
    journalisent. destructive = met à jour un état existant (compteurs de leçons, qui peuvent archiver une leçon) au lieu
    d'ajouter seulement ; idempotent = même appel, aucun effet de plus ; open_world = appelle un fournisseur de modèle.
    `title` est doublé dans annotations pour les clients antérieurs à la spec 2025-06-18. Indices non fiables par nature :
    les garde-fous restent côté serveur (jetons, scopes, règles)."""
    return {"name": name, "title": title, "description": description, "inputSchema": schema,
            "annotations": {"title": title, "readOnlyHint": False, "destructiveHint": destructive,
                            "idempotentHint": idempotent, "openWorldHint": open_world}}


MCP_TOOLS = [
    # decide : sans idempotency_key, un second appel identique produit une nouvelle décision
    _tool("decide", "Décider", "Prend une décision bornée pour une automatisation (règles → modèle → humain). "
                               "Retourne action, confiance, besoin d'humain.", REQUEST_SCHEMA,
          destructive=False, idempotent=False, open_world=True),
    # outcome : met à jour les compteurs de leçons ; un renvoi identique est reconnu (source_ref) et sans effet
    _tool("outcome", "Rapporter une issue", "Rapporte le résultat réel d'une décision (success|failure|unknown) pour l'apprentissage.",
          OUTCOME_SCHEMA, destructive=True, idempotent=True, open_world=True),
    _tool("execute", "Exécuter une tâche", "Exécute une tâche de texte/extraction avec le modèle exécuteur et les leçons du namespace.",
          EXECUTE_SCHEMA, destructive=False, idempotent=False, open_world=True),
    _tool("estimate", "Estimer une probabilité", "Estimation probabiliste calibrée (binaire, catégorielle, ordinale, quantité) avec source et preuves.",
          ESTIMATE_SCHEMA, destructive=False, idempotent=False, open_world=True),
    # record_estimate : aucun appel modèle ; un renvoi pour le même (estimator, correlation_key) rend la première estimation
    _tool("record_estimate", "Enregistrer une estimation externe",
          "Enregistre une probabilité produite ailleurs (watcher, vault, statique) sous un canal `estimator`, pour la scorer et la calibrer par canal.",
          ESTIMATE_RECORD_SCHEMA, destructive=False, idempotent=True, open_world=False),
    # estimate_outcome : aucun appel modèle ; une seconde résolution est un doublon sans effet
    _tool("estimate_outcome", "Résoudre une estimation", "Résout une estimation (issue réelle) pour la scorer (Brier, log-loss) et créditer les leçons.",
          ESTIMATE_OUTCOME_SCHEMA, destructive=True, idempotent=True, open_world=False),
]
META_VERSION = "io.modelcontextprotocol/protocolVersion"
MODERN_VERSIONS = ("2026-07-28",)          # versions sans initialize ; les clients 2025-11-25 passent par initialize
SCOPE = {"decide": "decide", "outcome": "outcome", "execute": "execute", "estimate": "estimate", "estimate_outcome": "estimate",
         "record_estimate": "estimate"}


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
        # RFC 9110 §8.6 / RFC 9112 §6.3 : Content-Length = 1*DIGIT, une seule valeur ; sinon 400 et connexion fermée.
        # int() accepterait « -1 » (rfile.read(-1) lit jusqu'à EOF, sans limite), « +5 » ou « 1_0 ».
        vals = self.headers.get_all("Content-Length") or []
        v = vals[0].strip() if len(vals) == 1 else ("" if vals else "0")
        if not (v.isascii() and v.isdigit()) or self.headers.get("Transfer-Encoding"):   # chunked non géré
            self.close_connection = True
            raise ValueError("Content-Length invalide")
        n = int(v)
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
        if kind in ("decide", "execute", "estimate", "record_estimate"):
            return body.get("namespace") if isinstance(body.get("namespace"), str) else None
        if kind == "outcome":
            self.engine.refresh()
            rec = self.engine._by_id.get(body.get("decision_id"))
            return rec["request"]["namespace"] if rec else None
        if kind in ("answer", "telegram_callback"):
            self.engine.refresh()
            did = body.get("decision_id")
            if kind == "telegram_callback":
                parts = str(body.get("data") or "").split(":")
                did = parts[1] if len(parts) == 3 else None
            rec = self.engine._by_id.get(did) if isinstance(did, str) else None
            return rec["request"]["namespace"] if rec else None
        if kind == "estimate_outcome":
            self.engine.refresh()
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
        if self.path.startswith("/decisions/"):
            did = self.path[len("/decisions/"):]
            self.engine.refresh()
            rec = self.engine._by_id.get(did)
            try:
                self._check("read", rec["request"]["namespace"] if rec else None)
            except Forbidden as e:
                return self._json(403, {"error": str(e)})
            if not rec:
                return self._json(404, {"error": "décision inconnue"})
            return self._json(200, self.engine.decision(did))
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
                     "/estimate/outcome": "estimate_outcome", "/estimate_outcome": "estimate_outcome",
                     "/estimate/record": "record_estimate"}.get(self.path)
            if route:
                self._check(SCOPE[route], self._ns_of(route, body))
                return self._json(200, getattr(self.engine, route)(body))
            if self.path == "/mcp":
                return self._mcp(body)
            if self.path == "/hooks/claude":
                return self._json(200, self._claude_hook(body))
            if self.path == "/hooks/claude/answer":
                return self._json(200, self._hook_answer(body))
            if self.path == "/chat":
                if not self.engine.config.get("chat"):
                    return self._json(404, {"error": "chat non configuré"})
                self._check("read", "*")             # lit plusieurs namespaces : jeton administrateur ou namespaces ["*"]
                if not isinstance(body.get("message"), str):
                    raise ValueError("message (texte) requis")
                from .chat import Chat
                return self._json(200, Chat(self.engine).ask(body["message"], body.get("history") if isinstance(body.get("history"), list) else None))
            if self.path == "/decisions/answer":
                self._check("approve", self._ns_of("answer", body))
                return self._json(200, self.engine.answer(body))
            if self.path == "/telegram/callback":
                if not self.engine.telegram:
                    return self._json(404, {"error": "telegram non configuré"})
                self._check("approve", self._ns_of("telegram_callback", body))
                return self._json(200, self.engine.telegram.handle_callback(body.get("data"), str(body.get("by") or "")))
            return self._json(404, {"error": "inconnu"})
        except Forbidden as e:
            return self._json(403, {"error": str(e)})
        except (ValueError, KeyError) as e:
            return self._json(400, {"error": str(e)})
        except InProgress as e:
            return self._json(409, {"error": str(e)})
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
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        modern = META_VERSION in meta or self.headers.get("MCP-Protocol-Version") in MODERN_VERSIONS
        if modern:                     # MCP 2026-07-28 : sans état, version et capacités portées par chaque requête
            bad = self._mcp_modern_error(method, params, meta)
            if bad:
                status, code, message, data = bad
                err = {"code": code, "message": message, **({"data": data} if data else {})}
                return self._json(status, {"jsonrpc": "2.0", "id": rid, "error": err})
        try:
            if method == "server/discover":
                result = {"supportedVersions": list(MODERN_VERSIONS), "capabilities": {"tools": {}},
                          "instructions": "Reason : décisions bornées (règles, puis modèle typé, puis humain) pour des "
                                          "automatisations. Appeler decide avant toute action irréversible, puis outcome.",
                          "ttlMs": 0, "cacheScope": "public"}
            elif method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2025-11-25"),
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "reason", "version": __version__}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": MCP_TOOLS, "ttlMs": 0, "cacheScope": "public"}   # liste identique pour tous les jetons
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
        result["resultType"] = "complete"          # exigé en 2026-07-28, ignoré par les clients antérieurs
        if modern:
            result["_meta"] = {"io.modelcontextprotocol/serverInfo": {"name": "reason", "version": __version__}}
        return self._json(200, {"jsonrpc": "2.0", "id": rid, "result": result})

    def _mcp_modern_error(self, method: str, params: dict, meta: dict):
        """Validation d'une requête MCP 2026-07-28 (basic/index « _meta », transports/streamable-http « Server Validation »).
        Retourne (statut HTTP, code JSON-RPC, message, data) ou None."""
        version = meta.get(META_VERSION)
        if not isinstance(version, str) or not isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict):
            return 400, -32602, "_meta : protocolVersion et clientCapabilities sont requis", None
        if self.headers.get("MCP-Protocol-Version") != version:
            return 400, -32020, "en-tête MCP-Protocol-Version absent ou différent de _meta", None
        if version not in MODERN_VERSIONS:
            return 400, -32022, f"version de protocole non prise en charge : {version}", \
                {"supported": list(MODERN_VERSIONS), "requested": version}
        if self.headers.get("Mcp-Method") != method:
            return 400, -32020, "en-tête Mcp-Method absent ou différent de method", None
        name = _header_value(self.headers.get("Mcp-Name"))
        if method == "tools/call" and (name is None or name != params.get("name")):
            return 400, -32020, "en-tête Mcp-Name absent ou différent de params.name", None
        if method not in ("server/discover", "tools/list", "tools/call"):   # initialize et ping n'existent plus
            return 404, -32601, f"méthode inconnue {method}", None
        return None

    # --- hook http Claude Code : le gate « décider avant l'action »
    def _claude_hook(self, h: dict) -> dict:
        ev = h.get("hook_event_name")
        ns = os.environ.get("REASON_HOOK_NAMESPACE") or "claude/" + hook_namespace(h.get("cwd"))
        self._check("decide" if ev == "PreToolUse" else "read", ns)
        if ev == "PreToolUse":
            tool_input = json.dumps(h.get("tool_input"), ensure_ascii=False)[:MAX_TOOL_INPUT_CHARS]
            # le point de décision inclut l'empreinte de l'entrée : l'anti-boucle ne vise que la répétition du même appel
            point = f"tool:{h.get('tool_name')}:{hashlib.sha1(tool_input.encode()).hexdigest()[:8]}"
            full = hashlib.sha256(json.dumps({"tool_name": h.get("tool_name"), "tool_input": h.get("tool_input")},
                                             ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            try:
                dec = self.engine.decide({
                    "namespace": ns, "run_id": h.get("session_id"), "decision_point": point,
                    "event": {"type": "tool_call", "tool_name": h.get("tool_name"), "tool_input": tool_input,
                              "tool_input_sha256": full, "hook_mac": self._hook_mac(ns, h.get("session_id"), full)},
                    "state": {"cwd": h.get("cwd"), "permission_mode": h.get("permission_mode")},
                    "candidate_actions": [{"name": "allow", "schema": {"type": "object", "additionalProperties": False}},
                                          {"name": "deny", "schema": {"type": "object", "additionalProperties": False}}],
                    "idempotency_key": h.get("tool_use_id")})
            except InProgress:              # un statut non 2xx ne bloquerait pas l'outil : répondre ask en 200
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                               "permissionDecisionReason": "Reason: décision déjà en cours pour cet appel"}}
            name = dec["action"]["name"]
            decision = {"allow": "allow", "deny": "deny"}.get(name, "ask")
            out = {"hookEventName": "PreToolUse", "permissionDecision": decision,
                   "permissionDecisionReason": f"Reason[{dec['tier']}] {dec['rationale']}"[:500]}
            # réponse humaine (POST /hooks/claude/answer) sur cette décision, ou sur le même appel de la même session
            # (empreinte sha256 de l'entrée complète) : la plus récente gagne ; deny l'emporte sur tout, allow ne
            # tranche qu'une escalade (jamais un refus de règle ou de modèle)
            ans = next((r for r in reversed(self._answers(ns)) if r.get("decision_id") == dec["decision_id"]
                        or (h.get("session_id") and r.get("session_id") == h.get("session_id")
                            and r.get("tool_input_sha256") == full)), None)
            if ans and (ans["action"] == "deny" or name == "ask_human"):
                out["permissionDecision"] = ans["action"]
                out["permissionDecisionReason"] = "Reason[human] " + (ans.get("note") or "réponse humaine")[:480]
                if ans["action"] == "allow" and isinstance(ans.get("updated_input"), dict):
                    out["updatedInput"] = ans["updated_input"]        # remplace l'entrée entière (spec des hooks)
            elif name == "ask_human":
                out["permissionDecisionReason"] = out["permissionDecisionReason"][:460] + f" [decision {dec['decision_id']}]"
                if os.environ.get("REASON_HOOK_DEFER") == "1":
                    out["permissionDecision"] = "defer"            # claude -p seulement ; ignoré en interactif
            return {"hookSpecificOutput": out}
        if ev in ("SessionStart", "UserPromptSubmit"):
            text, _ = self.engine.playbook(ns).render(self.engine.thresholds["lessons_max_chars"])
            if not text:
                return {}
            return {"hookSpecificOutput": {"hookEventName": ev, "additionalContext": "Leçons Reason pour ce projet :\n" + text}}
        return {}


    def _answers(self, ns: str) -> list[dict]:
        """Réponses valides seulement : une ligne illisible ne doit jamais faire tomber le gate (non 2xx = outil exécuté)."""
        p = self.engine.ns_dir(ns) / "hook_answers.jsonl"      # ponytail: relu à chaque appel, indexer si le fichier grossit
        try:
            with self.engine._ns_lock(ns):
                lines = p.read_text().splitlines() if p.exists() else []
        except (OSError, UnicodeDecodeError):
            return []
        out = []
        for l in lines:
            try:
                r = json.loads(l)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("action") in ("allow", "deny"):
                out.append(r)
        return out

    def _hook_mac(self, ns: str, session: str | None, sha: str) -> str:
        """Prouve qu'une décision vient du hook : /decide accepte n'importe quel event et ne connaît pas la clé."""
        kp = self.engine.data_dir / "hook.key"
        if not kp.exists():
            try:
                fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(os.urandom(32))
            except FileExistsError:
                pass
        key = kp.read_bytes()
        return hmac.new(key, f"{ns}\n{session}\n{sha}".encode(), hashlib.sha256).hexdigest()

    def _hook_answer(self, b: dict) -> dict:
        """Réponse humaine à un appel d'outil escaladé : scope approve (distinct d'outcome : approuver ou réécrire une
        commande est un privilège). Ni issue, ni score, ni leçon : l'issue réelle s'enregistre ensuite via /outcome."""
        self.engine.refresh()
        did = b.get("decision_id")
        rec = self.engine._by_id.get(did) if isinstance(did, str) else None
        ns = rec["request"]["namespace"] if rec else None
        self._check("approve", ns)
        ev = rec["request"].get("event") or {} if rec else {}
        sha = ev.get("tool_input_sha256")
        if not (rec and isinstance(sha, str) and isinstance(ev.get("hook_mac"), str) and hmac.compare_digest(
                ev["hook_mac"], self._hook_mac(ns, rec["request"].get("run_id"), sha))):
            raise KeyError("décision de hook inconnue")
        if b.get("action") not in ("allow", "deny"):
            raise ValueError("action doit valoir allow ou deny")
        if "updated_input" in b and (b["action"] != "allow" or not isinstance(b["updated_input"], dict)):
            raise ValueError("updated_input : un objet, avec allow seulement")
        row = {"decision_id": rec["decision"]["decision_id"], "session_id": rec["request"].get("run_id"),
               "tool_input_sha256": sha, "action": b["action"],
               "note": str(b.get("note") or "")[:480], "ts": time.time(),
               **({"updated_input": b["updated_input"]} if "updated_input" in b else {})}
        self.engine._append(ns, "hook_answers.jsonl", row)
        return {"recorded": True, "decision_id": row["decision_id"], "action": row["action"]}


def _header_value(v: str | None) -> str | None:
    """Décode la forme sentinelle =?base64?…?= (streamable-http « Value Encoding ») ; None si absent ou illisible."""
    if v and v.startswith("=?base64?") and v.endswith("?="):
        try:
            return base64.b64decode(v[9:-2], validate=True).decode("utf-8")
        except ValueError:
            return None
    return v


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
    if engine.telegram and engine.telegram.enabled and engine.telegram.poll:
        threading.Thread(target=engine.telegram.poll_forever, daemon=True).start()
        print("Reason : lecture des réponses Telegram (bot dédié)", file=sys.stderr)
    srv = ThreadingHTTPServer((host, port), Handler)
    mode = "jeton administrateur" if Handler.token else ("jetons par namespace" if Handler.tokens else "AUCUNE (--insecure)")
    print(f"Reason {__version__} sur http://{host}:{port}  rôles={engine.roles}  auth={mode}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
