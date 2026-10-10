"""Moteur de décision : règles → décideur typé → raisonneur (escalade) → humain, avec leçons, journal, anti-boucle.

Rôles de modèles configurables (config["roles"]) : decider, reasoner, executor. Tous optionnels sauf
executor. Sans decider, l'executor décide lui-même. Une requête peut surcharger les rôles via "models".
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from .lessons import Playbook
from .llm import LLM, LLMError

META_ACTIONS = ("abstain", "ask_human")
DEFAULT_THRESHOLDS = {"decider_confidence": 0.75, "reasoner_confidence": 0.6, "loop_max_repeats": 3,
                      "lessons_max_chars": 4000}


# ---------- validation JSON schema minimale (type, enum, required, properties, additionalProperties, items, bornes)
def validate(inst, schema: dict, path: str = "$") -> list[str]:
    errs: list[str] = []
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float),
             "boolean": bool, "null": type(None)}
    if t:
        allowed = t if isinstance(t, list) else [t]
        ok = any(isinstance(inst, types[a]) and not (a in ("integer", "number") and isinstance(inst, bool))
                 for a in allowed if a in types)
        if not ok:
            return [f"{path}: attendu {t}, reçu {type(inst).__name__}"]
    if "enum" in schema and inst not in schema["enum"]:
        errs.append(f"{path}: {inst!r} hors enum {schema['enum']}")
    if isinstance(inst, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in inst:
                errs.append(f"{path}.{k}: requis")
        for k, v in inst.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}.{k}: propriété non autorisée")
    if isinstance(inst, list) and "items" in schema:
        for i, v in enumerate(inst):
            errs += validate(v, schema["items"], f"{path}[{i}]")
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in schema and inst < schema["minimum"]:
            errs.append(f"{path}: {inst} < minimum {schema['minimum']}")
        if "maximum" in schema and inst > schema["maximum"]:
            errs.append(f"{path}: {inst} > maximum {schema['maximum']}")
    return errs


# ---------- règles déterministes (P0) : tables JSON, veto possible
def _get(obj, path: str):
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)] if int(part) < len(obj) else None
        else:
            return None
    return obj


def _cond(req: dict, c: dict) -> bool:
    v, op, ref = _get(req, c["path"]), c.get("op", "eq"), c.get("value")
    try:
        return {"eq": lambda: v == ref, "ne": lambda: v != ref, "gt": lambda: v is not None and v > ref,
                "gte": lambda: v is not None and v >= ref, "lt": lambda: v is not None and v < ref,
                "lte": lambda: v is not None and v <= ref, "in": lambda: v in (ref or []),
                "nin": lambda: v not in (ref or []), "contains": lambda: ref in (v or ()),
                "exists": lambda: (v is not None) == bool(ref), "regex": lambda: v is not None and re.search(ref, str(v)) is not None,
                }[op]()
    except TypeError:
        return False


class Rules:
    """[{"id":"r1","if":[{"path":"state.facts.amount","op":"gt","value":1000}],"decide":{"name":"escalate","params":{}}},
        {"id":"r2","if":[...],"forbid":["approve"],"reason":"..."}]"""

    def __init__(self, rules: list[dict] | None = None):
        self.rules = rules or []

    @classmethod
    def load(cls, path: Path) -> "Rules":
        return cls(json.loads(path.read_text()) if path.exists() else [])

    def matching(self, req: dict) -> list[dict]:
        return [r for r in self.rules if all(_cond(req, c) for c in r.get("if", []))]

    def decide(self, req: dict) -> dict | None:
        for r in self.matching(req):
            if "decide" in r:
                return {**r["decide"], "rule": r.get("id")}
        return None

    def forbidden(self, req: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for r in self.matching(req):
            for name in r.get("forbid", []):
                out.setdefault(name, r.get("reason") or r.get("id") or "règle")
        return out


# ---------- moteur
def decision_schema(action_names: list[str]) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": ["action", "params", "confidence", "rationale", "lessons_used"],
            "properties": {
                "action": {"type": "string", "enum": action_names + list(META_ACTIONS)},
                "params": {"type": "object"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "rationale": {"type": "string"},
                "lessons_used": {"type": "array", "items": {"type": "string"}}}}


class Engine:
    def __init__(self, config: dict):
        self.config = config
        self.data_dir = Path(config.get("data_dir", "data"))
        self.thresholds = {**DEFAULT_THRESHOLDS, **config.get("thresholds", {})}
        self.models = {name: _make_model(name, cfg) for name, cfg in config.get("models", {}).items()}
        self.roles = {"decider": None, "reasoner": None, "executor": None, "reflector": None,
                      **config.get("roles", {})}
        if not self.roles["executor"] and self.models:
            self.roles["executor"] = next(iter(self.models))
        self._lock = threading.Lock()
        self._playbooks: dict[str, Playbook] = {}
        self._by_key: dict[str, dict] = {}       # idempotency_key -> decision
        self._by_id: dict[str, dict] = {}        # decision_id -> {request, decision}
        self._runs: dict[str, list[str]] = {}    # run_id -> [hash(decision_point, action)]
        self._load_journals()

    # --- rôles de modèles
    def model(self, role: str, overrides: dict | None = None) -> LLM | None:
        name = (overrides or {}).get(role) or self.roles.get(role)
        if role == "decider" and not name:            # pas de décideur dédié : l'exécuteur décide
            name = (overrides or {}).get("executor") or self.roles.get("executor")
        if role == "reflector" and not name:
            name = (overrides or {}).get("decider") or self.roles.get("decider") or self.roles.get("executor")
        if name and name not in self.models:
            raise ValueError(f"modèle inconnu pour le rôle {role}: {name}")
        return self.models.get(name) if name else None

    # --- fichiers par namespace
    def ns_dir(self, ns: str) -> Path:
        seg = r"[A-Za-z0-9][A-Za-z0-9_.-]*"          # chaque segment commence par un alphanumérique : ni "." ni ".."
        if not re.fullmatch(rf"{seg}(/{seg}){{0,2}}", ns):
            raise ValueError("namespace invalide (lettres, chiffres, . _ -, jusqu'à 3 segments séparés par /)")
        d = self.data_dir / ns
        d.mkdir(parents=True, exist_ok=True)
        return d

    def playbook(self, ns: str) -> Playbook:
        if ns not in self._playbooks:
            self._playbooks[ns] = Playbook(self.ns_dir(ns) / "playbook.json")
        return self._playbooks[ns]

    def rules(self, ns: str) -> Rules:
        return Rules.load(self.ns_dir(ns) / "rules.json")

    def policy(self, ns: str) -> str:
        p = self.ns_dir(ns) / "policy.md"
        return p.read_text() if p.exists() else ""

    def _journal(self, ns: str, event: dict) -> None:
        event = {"ts": time.time(), **event}
        with self._lock, open(self.ns_dir(ns) / "journal.jsonl", "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _load_journals(self) -> None:
        if not self.data_dir.exists():
            return
        for jp in self.data_dir.glob("**/journal.jsonl"):
            for line in jp.read_text().splitlines():
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "DecisionMade":
                    self._index(ev["request"], ev["decision"])

    def _index(self, req: dict, dec: dict) -> None:
        if req.get("idempotency_key"):
            self._by_key[req["idempotency_key"]] = dec
        self._by_id[dec["decision_id"]] = {"request": req, "decision": dec}
        if req.get("run_id"):
            self._runs.setdefault(req["run_id"], []).append(_sig(req, dec))

    # --- décision
    def decide(self, req: dict) -> dict:
        errs = validate(req, REQUEST_SCHEMA)
        if errs:
            raise ValueError("requête invalide: " + "; ".join(errs))
        ns = req["namespace"]
        self.ns_dir(ns)
        key = req.get("idempotency_key")
        if key and key in self._by_key:
            return {**self._by_key[key], "replayed": True}
        names = [a["name"] for a in req["candidate_actions"]]
        overrides = req.get("models") or {}
        constraints = req.get("constraints") or {}
        pb = self.playbook(ns)
        lessons_text, shown = pb.render(self.thresholds["lessons_max_chars"])
        rules = self.rules(ns)
        forbidden = rules.forbidden(req)
        trace: list[str] = []

        # anti-boucle : même point de décision répété N fois dans un run
        sigs = self._runs.get(req.get("run_id") or "", [])
        repeats = sum(1 for s in sigs if s.startswith(req["decision_point"] + "|"))
        if repeats >= self.thresholds["loop_max_repeats"]:
            dec = self._human(req, "loop", f"{repeats} décisions déjà prises à ce point dans ce run", shown, [])
            return self._finish(ns, req, dec)

        # P0 règles
        forced = rules.decide(req)
        if forced:
            dec = self._mk(req, "rules", forced["name"], forced.get("params", {}), 1.0,
                           f"règle {forced.get('rule')}", shown, [])
            return self._finish(ns, req, dec)

        # P1 décideur (ou exécuteur si pas de décideur)
        tiers = [("decider", self.thresholds["decider_confidence"])]
        if constraints.get("allow_reasoner", True) and self.model("reasoner", overrides):
            tiers.append(("reasoner", self.thresholds["reasoner_confidence"]))
        for role, threshold in tiers:
            llm = self.model(role, overrides)
            if llm is None:
                continue
            try:
                out = self._ask(llm, req, lessons_text, names, role)
            except LLMError as e:
                trace.append(f"{role}: erreur {e}")
                continue
            action, conf = out["action"], float(out["confidence"])
            used = [l for l in out.get("lessons_used", []) if l in shown]
            if action in forbidden:
                trace.append(f"{role}: {action} interdit par règle ({forbidden[action]})")
                continue
            if action == "ask_human":
                dec = self._human(req, role, out.get("rationale", ""), shown, used)
                return self._finish(ns, req, dec)
            if action == "abstain" or conf < threshold:
                trace.append(f"{role}: {action} confiance {conf:.2f} < {threshold}")
                continue
            cand = next(a for a in req["candidate_actions"] if a["name"] == action)
            perrs = validate(out.get("params", {}), cand.get("schema", {"type": "object"}))
            if perrs:
                trace.append(f"{role}: paramètres invalides {perrs}")
                continue
            dec = self._mk(req, role, action, out["params"], conf, out.get("rationale", ""), shown, used)
            dec["rollback"] = cand.get("rollback")
            return self._finish(ns, req, dec)

        dec = self._human(req, "escalation", " | ".join(trace) or "aucun palier n'a conclu", shown, [])
        return self._finish(ns, req, dec)

    def _ask(self, llm: LLM, req: dict, lessons_text: str, names: list[str], role: str) -> dict:
        ns = req["namespace"]
        system = (f"Tu es le décideur du module Reason pour l'automatisation « {ns} », point de décision "
                  f"« {req['decision_point']} ». Choisis UNE action parmi les candidates, ou `abstain` si tu n'es pas sûr, "
                  "ou `ask_human` si l'enjeu l'exige. Respecte le schéma de paramètres de l'action. "
                  "Indique dans lessons_used les identifiants des leçons que tu as réellement utilisées.\n\n"
                  f"## Politique\n{self.policy(ns) or '(aucune)'}\n\n## Leçons apprises\n{lessons_text or '(aucune)'}")
        user = json.dumps({k: req.get(k) for k in ("event", "state", "history", "candidate_actions")},
                          ensure_ascii=False)
        out, _ = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                          schema=decision_schema(names))
        errs = validate(out, decision_schema(names))
        if errs:
            raise LLMError(f"sortie du {role} invalide: {errs}")
        return out

    def _mk(self, req, tier, action, params, conf, rationale, shown, used) -> dict:
        return {"decision_id": "d_" + hashlib.sha1(f"{time.time_ns()}{req.get('run_id')}".encode()).hexdigest()[:12],
                "namespace": req["namespace"], "decision_point": req["decision_point"],
                "action": {"name": action, "params": params}, "tier": tier, "confidence": conf,
                "rationale": rationale, "lessons_shown": shown, "lessons_used": used,
                "needs_human": {"required": False}, "rollback": None, "ttl_s": 600}

    def _human(self, req, tier, reason, shown, used) -> dict:
        dec = self._mk(req, tier, "ask_human", {}, 0.0, reason, shown, used)
        dec["needs_human"] = {"required": True, "question": f"Décision requise au point « {req['decision_point']} » : {reason}",
                              "options": [a["name"] for a in req["candidate_actions"]],
                              "schema": {"type": "object", "properties": {"action": {"type": "string"}},
                                         "required": ["action"], "additionalProperties": False}}
        return dec

    def _finish(self, ns: str, req: dict, dec: dict) -> dict:
        with self._lock:
            self._index(req, dec)
        self._journal(ns, {"type": "DecisionMade", "request": req, "decision": dec})
        return dec

    # --- retour d'expérience
    def outcome(self, o: dict) -> dict:
        errs = validate(o, OUTCOME_SCHEMA)
        if errs:
            raise ValueError("outcome invalide: " + "; ".join(errs))
        rec = self._by_id.get(o["decision_id"])
        if not rec:
            raise KeyError(f"décision inconnue: {o['decision_id']}")
        req, dec = rec["request"], rec["decision"]
        ns = req["namespace"]
        pb = self.playbook(ns)
        success = o["result"] == "success"
        if dec["lessons_used"]:
            pb.feedback(dec["lessons_used"], helpful=success)
        hf = o.get("human_feedback") or {}
        expected = hf.get("correct_action") or (dec["action"]["name"] if success and dec["tier"] != "rules" else None)
        if expected and dec["action"]["name"] not in META_ACTIONS or hf.get("correct_action"):
            self._append(ns, "dataset.jsonl", {"request": {k: v for k, v in req.items() if k != "idempotency_key"},
                                               "expected_action": expected})
        proposals: list[str] = []
        if not success or hf.get("correct_action") or hf.get("note"):
            proposals = self.reflect(req, dec, o)
        self._journal(ns, {"type": "OutcomeRecorded", "decision_id": dec["decision_id"], "outcome": o,
                           "proposals": proposals})
        return {"decision_id": dec["decision_id"], "recorded": True, "lessons_updated": dec["lessons_used"],
                "lesson_proposals": proposals}

    def reflect(self, req: dict, dec: dict, o: dict) -> list[str]:
        llm = self.model("reflector")
        if llm is None:
            return []
        from .reflector import propose
        try:
            texts = propose(llm, req, dec, o, self.playbook(req["namespace"]).render(2000)[0])
        except LLMError:
            return []
        return self.playbook(req["namespace"]).propose(texts, source=dec["decision_id"])

    # --- exécution (les « mains ») : tâche de texte/extraction avec les leçons du namespace
    def execute(self, x: dict) -> dict:
        errs = validate(x, EXECUTE_SCHEMA)
        if errs:
            raise ValueError("execute invalide: " + "; ".join(errs))
        llm = self.model("executor", x.get("models"))
        if llm is None:
            raise ValueError("aucun modèle executor configuré")
        ns = x["namespace"]
        lessons_text, shown = self.playbook(ns).render(self.thresholds["lessons_max_chars"])
        system = (f"Tu exécutes une tâche pour l'automatisation « {ns} ». Fais exactement ce qui est demandé.\n\n"
                  f"## Politique\n{self.policy(ns) or '(aucune)'}\n\n## Leçons apprises\n{lessons_text or '(aucune)'}")
        user = x["task"] + ("\n\n## Entrée\n" + json.dumps(x.get("input"), ensure_ascii=False) if "input" in x else "")
        out, usage = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                              schema=x.get("output_schema"), max_tokens=x.get("max_tokens"))
        if x.get("output_schema"):
            perrs = validate(out, x["output_schema"])
            if perrs:
                raise LLMError(f"sortie de l'exécuteur hors schéma: {perrs}")
        self._journal(ns, {"type": "Executed", "task": x["task"], "usage": usage, "lessons_shown": shown})
        return {"output": out, "usage": usage, "lessons_shown": shown}

    def _append(self, ns: str, name: str, row: dict) -> None:
        with self._lock, open(self.ns_dir(ns) / name, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _make_model(name: str, cfg: dict):
    if cfg.get("provider") == "jev":
        from .jev import JevDecider
        return JevDecider(name=name, **{k: v for k, v in cfg.items() if k != "provider"})
    return LLM.from_config(name, cfg)


def _sig(req: dict, dec: dict) -> str:
    return req["decision_point"] + "|" + json.dumps(dec["action"], sort_keys=True)


_ACTION = {"type": "object", "required": ["name"], "additionalProperties": True,
           "properties": {"name": {"type": "string"}, "schema": {"type": "object"}, "description": {"type": "string"},
                          "rollback": {"type": ["object", "string", "null"]}}}
REQUEST_SCHEMA = {"type": "object", "required": ["namespace", "decision_point", "candidate_actions"],
                  "properties": {"namespace": {"type": "string"}, "run_id": {"type": ["string", "null"]},
                                 "decision_point": {"type": "string"}, "event": {}, "state": {},
                                 "history": {"type": "array"},
                                 "candidate_actions": {"type": "array", "items": _ACTION},
                                 "constraints": {"type": "object"}, "models": {"type": "object"},
                                 "idempotency_key": {"type": ["string", "null"]}}}
OUTCOME_SCHEMA = {"type": "object", "required": ["decision_id", "result"],
                  "properties": {"decision_id": {"type": "string"},
                                 "result": {"type": "string", "enum": ["success", "failure", "unknown"]},
                                 "metrics": {"type": "object"}, "human_feedback": {"type": "object"},
                                 "detail": {"type": "string"}}}
EXECUTE_SCHEMA = {"type": "object", "required": ["namespace", "task"],
                  "properties": {"namespace": {"type": "string"}, "task": {"type": "string"}, "input": {},
                                 "output_schema": {"type": "object"}, "models": {"type": "object"},
                                 "max_tokens": {"type": "integer"}}}
