"""Règles P0 v2 : tables JSON déterministes qui forcent (`decide`) ou interdisent (`forbid`) une action.

Grammaire :
  Rule := {"id", "if": Cond | [Leaf, ...], "decide": {...} | "forbid": [...], "reason"}
  Cond := Leaf | {"all": [Cond]} | {"any": [Cond]} | {"not": Cond}
  Leaf := {"path", "op", "value"} | {"path", "op", "path2"}
Opérateurs : eq ne gt gte lt lte in nin contains exists regex before after hours_until_gte hours_until_lte within_hours.
Instants : ISO 8601 (offset ou Z) ou epoch ; naïf = UTC. `now` est lu dans la requête (jamais l'horloge) : rejouable.
Regex : motifs écrits par l'auteur des règles (jamais pris dans la requête), ≤ 200 caractères, lint anti-quantificateur
imbriqué, texte tronqué et optionnellement normalisé (`norm: true`). `reason` interpole {match} et {chemin.vers.champ}.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

OPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "nin", "contains", "exists", "regex",
       "before", "after", "hours_until_gte", "hours_until_lte", "within_hours"}
TIME_OPS = {"before", "after", "hours_until_gte", "hours_until_lte", "within_hours"}
MAX_PATTERN = 200
MAX_TEXT = 20_000
_INTERP = re.compile(r"\{([A-Za-z0-9_.]+)\}")


def get_path(obj, path: str):
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)] if int(part) < len(obj) else None
        else:
            return None
    return obj


def parse_instant(v) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str):
        s = v.strip()
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def normalize_text(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(s.casefold().split())


def _risky_group(p: str) -> bool:
    """Vrai si un groupe quantifié (+, *, {n,}) contient un quantificateur, une alternance ou un sous-groupe :
    (a+)+ , (a|aa)+ , ((\\w+)\\s?)+ . Analyse par pile, échappements et classes [...] respectés."""
    stack: list[dict] = []
    i, n, in_class = 0, len(p), False
    while i < n:
        c = p[i]
        if c == "\\":
            i += 2; continue
        if in_class:
            in_class = c != "]"; i += 1; continue
        if c == "[":
            in_class = True
        elif c == "(":
            if stack:
                stack[-1]["risky"] = True
            stack.append({"risky": False})
        elif c == ")" and stack:
            g = stack.pop()
            nxt = p[i + 1] if i + 1 < n else ""
            if g["risky"] and nxt and nxt in "+*{":
                return True
            if stack and nxt and nxt in "+*{?":
                stack[-1]["risky"] = True
        elif c in "+*{|" and stack:
            stack[-1]["risky"] = True
        i += 1
    return False


def lint_pattern(p) -> str | None:
    if not isinstance(p, str):
        return "motif regex non textuel"
    if len(p) > MAX_PATTERN:
        return f"motif regex > {MAX_PATTERN} caractères"
    if _risky_group(p):
        return "quantificateur imbriqué (risque ReDoS)"
    try:
        re.compile(p)
    except re.error as e:
        return f"regex invalide: {e}"
    return None


def lint_rules(rules: list) -> list[str]:
    """Erreurs statiques. Vide = OK."""
    errs: list[str] = []
    if not isinstance(rules, list):
        return ["rules.json doit être une liste"]
    for i, r in enumerate(rules):
        rid = r.get("id", f"#{i}") if isinstance(r, dict) else f"#{i}"
        if not isinstance(r, dict):
            errs.append(f"{rid}: règle non objet"); continue
        if "if" not in r:
            errs.append(f"{rid}: clé if manquante (utiliser \"if\": [] pour une règle toujours vraie)")
        if ("decide" in r) == ("forbid" in r):
            errs.append(f"{rid}: exactement un de decide / forbid")
        if "decide" in r and not (isinstance(r["decide"], dict) and isinstance(r["decide"].get("name") or r["decide"].get("action"), str)):
            errs.append(f"{rid}: decide.name manquant")
        if "forbid" in r and not (isinstance(r["forbid"], list) and all(isinstance(x, str) for x in r["forbid"])):
            errs.append(f"{rid}: forbid doit être une liste de noms")
        errs += [f"{rid}: {e}" for e in _lint_cond(r.get("if", []))]
    return errs


def _lint_cond(c) -> list[str]:
    if isinstance(c, list):
        return [e for x in c for e in _lint_cond(x)]
    if not isinstance(c, dict):
        return ["condition non objet"]
    if "all" in c or "any" in c:
        k = "all" if "all" in c else "any"
        if not isinstance(c[k], list):
            return [f"{k} doit être une liste"]
        return [e for x in c[k] for e in _lint_cond(x)]
    if "not" in c:
        return _lint_cond(c["not"])
    errs = []
    if not isinstance(c.get("path"), str):
        errs.append("path manquant")
    op = c.get("op", "eq")
    if op not in OPS:
        errs.append(f"op inconnu {op!r}")
    if "value" in c and "path2" in c:
        errs.append("value et path2 sont exclusifs")
    if op == "regex" and "path2" in c:
        errs.append("regex : le motif doit être écrit dans la règle (value), jamais lu dans la requête (path2)")
    if op in ("regex",):
        pats = c.get("value")
        for p in (pats if isinstance(pats, list) else [pats]):
            e = lint_pattern(p)
            if e:
                errs.append(e)
    if op in ("hours_until_gte", "hours_until_lte", "within_hours") and not isinstance(c.get("value"), (int, float)):
        errs.append(f"{op} attend une valeur numérique en heures")
    return errs


def _cmp(v, ref, op: str) -> bool:
    try:
        if op == "eq":
            return v == ref
        if op == "ne":
            return v != ref
        if op in ("gt", "gte", "lt", "lte"):
            if v is None or ref is None:
                return False
            return {"gt": v > ref, "gte": v >= ref, "lt": v < ref, "lte": v <= ref}[op]
        if op == "in":
            return v in (ref or [])
        if op == "nin":
            return v not in (ref or [])
        if op == "contains":
            return ref in (v or ())
        if op == "exists":
            return (v is not None) == bool(ref)
    except TypeError:
        return False
    return False


def eval_leaf(req: dict, c: dict, ctx: dict) -> bool:
    op = c.get("op", "eq")
    v = get_path(req, c["path"])
    ref = get_path(req, c["path2"]) if "path2" in c else c.get("value")
    if op == "regex":
        if v is None or "path2" in c:
            return False
        text = str(v)[:MAX_TEXT]
        if c.get("norm"):
            text = normalize_text(text)
        for p in (ref if isinstance(ref, list) else [ref]):
            if lint_pattern(p):
                continue
            m = re.search(p, text)
            if m:
                ctx["match"] = m.group(0)[:200]
                return True
        return False
    if op in TIME_OPS:
        t = parse_instant(v)
        if t is None:
            return False
        if op in ("before", "after"):
            t2 = parse_instant(ref)
            return t2 is not None and (t < t2 if op == "before" else t > t2)
        now = parse_instant(req.get("now"))
        if now is None or not isinstance(ref, (int, float)):
            return False
        hours = (t - now).total_seconds() / 3600
        return {"hours_until_gte": hours >= ref, "hours_until_lte": hours <= ref,
                "within_hours": abs(hours) <= ref}[op]
    return _cmp(v, ref, op)


def eval_cond(req: dict, c, ctx: dict) -> bool:
    if isinstance(c, list):
        return all(eval_cond(req, x, ctx) for x in c)
    if not isinstance(c, dict):
        return False
    if "all" in c:
        return isinstance(c["all"], list) and all(eval_cond(req, x, ctx) for x in c["all"])
    if "any" in c:
        return isinstance(c["any"], list) and any(eval_cond(req, x, ctx) for x in c["any"])
    if "not" in c:
        return not eval_cond(req, c["not"], ctx)
    try:
        return eval_leaf(req, c, ctx)
    except (KeyError, TypeError, re.error):
        return False


def interpolate(reason: str | None, req: dict, ctx: dict) -> str | None:
    if not reason:
        return reason
    def sub(m):
        key = m.group(1)
        val = ctx.get("match") if key == "match" else get_path(req, key)
        return str(val)[:200] if val is not None else m.group(0)
    return _INTERP.sub(sub, reason)


class Rules:
    def __init__(self, rules: list[dict] | None = None):
        self.rules = rules if isinstance(rules, list) else []
        self.errors = lint_rules(self.rules)
        # une règle qui ne passe pas le lint n'est jamais exécutée (ex. regex lue dans la requête, motif ReDoS)
        self.active = [r for r in self.rules if isinstance(r, dict) and not lint_rules([r])]

    @classmethod
    def load(cls, path: Path) -> "Rules":
        return cls(json.loads(path.read_text()) if path.exists() else [])

    def matching(self, req: dict) -> list[tuple[dict, dict]]:
        out = []
        for r in self.active:
            ctx: dict = {}
            if eval_cond(req, r.get("if", []), ctx):
                out.append((r, ctx))
        return out

    def decide(self, req: dict) -> dict | None:
        for r, ctx in self.matching(req):
            if "decide" in r:
                d = r["decide"]
                return {"name": d.get("name") or d.get("action"), "params": d.get("params", {}), "rule": r.get("id"),
                        "reason": interpolate(r.get("reason"), req, ctx)}
        return None

    def forbidden(self, req: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for r, ctx in self.matching(req):
            for name in r.get("forbid", []):
                out.setdefault(name, interpolate(r.get("reason"), req, ctx) or r.get("id") or "règle")
        return out

    def test(self, cases: list[dict]) -> dict:
        """cases: [{"name", "request", "expect": {"decide": name|None, "forbid": [names]}}]. Couverture et contradictions."""
        hit: set[str] = set()
        results = []
        for case in cases:
            req = case.get("request", {})
            matched = self.matching(req)
            for r, _ in matched:
                hit.add(r.get("id"))
            d = self.decide(req)
            f = self.forbidden(req)
            exp = case.get("expect", {})
            ok = True
            if "decide" in exp:
                ok &= (d["name"] if d else None) == exp["decide"]
            if "forbid" in exp:
                ok &= set(f) == set(exp["forbid"])
            contradiction = bool(d and d["name"] in f)
            results.append({"name": case.get("name"), "ok": ok, "decide": d["name"] if d else None,
                            "forbid": sorted(f), "contradiction": contradiction})
        return {"lint": self.errors, "cases": results, "passed": sum(r["ok"] for r in results),
                "failed": [r["name"] for r in results if not r["ok"]],
                "never_matched": [r.get("id") for r in self.rules if isinstance(r, dict) and r.get("id") not in hit],
                "contradictions": [r["name"] for r in results if r["contradiction"]]}
