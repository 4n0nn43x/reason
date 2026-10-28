"""Poller d'issues différées : lit une source de règlements depuis un watermark persisté, apparie chaque règlement à la
décision qui l'a motivé (clé de corrélation fournie au moment de la décision, fenêtre temporelle), poste l'issue v2 de
façon idempotente (source_ref), et déclare `unknown` les décisions dont l'horizon est dépassé sans règlement.

Sources (config["poller"]["sources"][i]) :
  {"name", "namespace", "type": "jsonl" | "http_json" | "graphql", "match_ttl_hours": 720, "horizon_hours": 168,
   "path" (jsonl) | "url", "headers", "query" (graphql), "items": "chemin.vers.liste", "cursor": "champ",
   "fields": {"correlation_key": "...", "result": "...", "result_detail": "...", "value": "...", "payout_fraction": "...",
              "settled_at": "...", "ref": "...", "resolution_text": "...", "label_confidence": 0.9},
   "result_map": {"win": "success", "loss": "failure", "refund": "void", "no_outcome": "no_outcome"},
   "ignore": ["BUY", "SELL"]}
Avec "settles": "estimates", la source règle les estimations (POST /estimate/record ou /estimate avec correlation_key)
au lieu des décisions : `fields.winning` donne l'issue gagnante (label ou index, ou la valeur pour kind=quantity), un
résultat void ou no_outcome annule l'estimation. Chaque règlement résout toutes les estimations de la clé, tous canaux
confondus, ce qui permet de scorer aussi les marchés non tradés.
Un type d'événement qui n'est ni un résultat valide, ni dans `result_map`, ni dans `ignore` n'est JAMAIS converti en
`unknown` (cela exclurait silencieusement des pertes du score, biais du survivant) : il est gardé de côté, compté dans le
rapport du tick (`unmapped`), signalé sur stderr, et retraité à chaque tick avec la configuration courante.
Chaque entrée de `fields` est un chemin dans l'item source, ou une constante si la valeur n'est pas une chaîne.
Stdlib seulement. Pas de broker, pas de base : l'état tient dans poller_state.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .rules import get_path, parse_instant


def _val(item: dict, spec):
    return get_path(item, spec) if isinstance(spec, str) else spec


def _head(path: Path, n: int) -> str | None:
    """Empreinte des n premiers octets déjà lus (n ≤ 256) : stable quand on ajoute, change si le fichier est remplacé."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha1(f.read(min(n, 256))).hexdigest()[:16]
    except OSError:
        return None


def read_jsonl(path: Path, offset: int):
    """Lignes complètes à partir d'un offset d'octets ; retourne (événements, nouvel offset)."""
    if not path.exists():
        return [], offset
    if path.stat().st_size < offset:                  # tronqué
        offset = 0
    out = []
    with open(path, "rb") as f:
        f.seek(offset)
        while True:
            pos = f.tell()
            line = f.readline()
            if not line:
                break
            if not line.endswith(b"\n"):      # ligne partielle en cours d'écriture : on la relira
                f.seek(pos); break
            try:
                out.append((json.loads(line), f.tell()))
            except ValueError:
                continue
        return out, f.tell() if out else offset


MAX_HTTP = 5_000_000
MAX_UNMATCHED = 2000


def read_http(src: dict, cursor):
    url = src["url"]
    if not url.startswith("https://") and not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
        print(f"avertissement: source {src['name']} hors https, en-têtes transmis en clair", file=sys.stderr)
    body = None
    if src.get("type") == "graphql":
        body = json.dumps({"query": src["query"], "variables": {"cursor": cursor}}).encode()
    elif cursor is not None and "{cursor}" in url:
        url = url.replace("{cursor}", str(cursor))
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **src.get("headers", {})})
    with urllib.request.urlopen(req, timeout=src.get("timeout", 30)) as r:
        raw = r.read(MAX_HTTP + 1)
        if len(raw) > MAX_HTTP:
            raise ValueError(f"réponse > {MAX_HTTP} octets")
        data = json.loads(raw.decode())
    items = get_path(data, src["items"]) if src.get("items") else data
    return [(it, _val(it, src.get("cursor"))) for it in (items or []) if isinstance(it, dict)]


def normalize(src: dict, item: dict, position) -> dict | None:
    f = src.get("fields", {})
    key = _val(item, f.get("correlation_key", "correlation_key"))
    if key is None:
        return None
    raw = _val(item, f.get("result", "result"))
    if raw is not None and str(raw) in {str(x) for x in src.get("ignore", [])}:
        return None
    result = (src.get("result_map") or {}).get(str(raw), raw)
    if result not in VALID_RESULTS:
        return {"_unmapped": str(raw)}
    detail = _val(item, f.get("result_detail")) or (str(raw) if raw is not None else None)
    ref = _val(item, f.get("ref")) or (f"{src['name']}:{position}" if position is not None else
                                         f"{src['name']}:" + hashlib.sha1(json.dumps(item, sort_keys=True).encode()).hexdigest()[:16])
    settled = _val(item, f.get("settled_at", "settled_at"))
    out = {"correlation_key": str(key), "result": result,
           "result_detail": detail, "value": _val(item, f.get("value")), "payout_fraction": _val(item, f.get("payout_fraction")),
           "settled_at": settled, "observed_at": datetime.now(timezone.utc).isoformat(),
           "source": src["name"], "source_ref": str(ref), "label_confidence": _val(item, f.get("label_confidence", 1.0)),
           "resolution_text": _val(item, f.get("resolution_text")), "version": _val(item, f.get("version")),
           "winning": _val(item, f.get("winning"))}
    return {k: v for k, v in out.items() if v is not None}


VALID_RESULTS = {"success", "failure", "unknown", "partial", "void", "no_outcome"}   # = core.RESULTS (pas d'import circulaire)


def _newer(a, b) -> bool:
    """a > b, en numérique si les deux le sont (sinon « 9 » > « 10 » lexicographiquement)."""
    try:
        return float(a) > float(b)
    except (TypeError, ValueError):
        return str(a) > str(b)


def matches(decision: dict, outcome: dict, ttl_hours: float) -> bool:
    if decision.get("correlation_key") != outcome["correlation_key"]:
        return False
    settled = parse_instant(outcome.get("settled_at"))
    decided = parse_instant(decision.get("decided_at"))
    if settled is None or decided is None:
        return True
    return decided <= settled <= decided + timedelta(hours=ttl_hours)


def tick(engine, src: dict, state_path: Path, now: datetime | None = None) -> dict:
    """Un passage : lit depuis le watermark, poste les issues appariées, expire les décisions hors horizon."""
    now = now or datetime.now(timezone.utc)
    ns = src["namespace"]
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    st = state.setdefault(src["name"], {"watermark": None, "last_run": None})
    estimates = src.get("settles", "decisions") == "estimates"
    pending = engine.pending_estimates(ns) if estimates else engine.pending(ns)
    posted, skipped, errors = [], [], []
    unmapped: dict[str, int] = {}
    if src["type"] == "jsonl":
        path = Path(src["path"])
        offset = int(st.get("watermark") or 0)
        if offset and st.get("head") and _head(path, offset) != st["head"]:   # fichier remplacé ou réécrit : on repart du début
            offset = 0
        events, new_wm = read_jsonl(path, offset)
        st["head"] = _head(path, new_wm) if new_wm else None
    else:
        events = read_http(src, st.get("watermark")); new_wm = st.get("watermark")
    ttl = src.get("match_ttl_hours", 720)
    # événements déjà lus mais pas encore appariés (décision prise plus tard, ou autre processus) : réessayés jusqu'au TTL
    queue = [(u["outcome"], u["seen"]) for u in st.get("unmatched", [])]
    # événements de type inconnu : retraités avec la configuration courante (une correction de result_map les récupère)
    held = st.get("unmapped_events", [])
    still_held = []
    for item, pos in [(h["item"], h["pos"]) for h in held] + [(item, pos) for item, pos in events]:
        o = normalize(src, item, pos)
        if o is None:
            skipped.append(pos)
        elif "_unmapped" in o:
            unmapped[o["_unmapped"]] = unmapped.get(o["_unmapped"], 0) + 1
            still_held.append({"item": item, "pos": pos})
        else:
            queue.append((o, now.isoformat()))
    st["unmapped_events"] = still_held[-MAX_UNMATCHED:]
    if unmapped:
        print(f"avertissement: source {src['name']} : types d'événement sans correspondance (ni result_map ni ignore), "
              f"non comptés : {unmapped}", file=sys.stderr)
    for item, pos in events:
        if src["type"] == "jsonl":
            new_wm = pos                                   # offset d'octets : les types inconnus sont gardés dans l'état
        elif pos is not None and (new_wm is None or _newer(pos, new_wm)):
            new_wm = pos
    unmatched = []
    for o, seen in queue:
        hits = [d for d in pending if matches(d, o, ttl)]
        for d in hits:
            if estimates:
                try:
                    r = engine.settle_estimate(d["estimate_id"], o)
                    posted.append({"estimate_id": d["estimate_id"], "source_ref": o["source_ref"], "recorded": r.get("recorded"),
                                   "duplicate": r.get("duplicate", False)})
                except (ValueError, KeyError) as e:
                    errors.append(f"{d['estimate_id']}: {e}")
                continue
            try:
                r = engine.outcome({**{k: v for k, v in o.items() if v is not None}, "decision_id": d["decision_id"]})
                posted.append({"decision_id": d["decision_id"], "source_ref": o["source_ref"], "recorded": r.get("recorded"), "duplicate": r.get("duplicate", False)})
            except (ValueError, KeyError) as e:
                errors.append(f"{d['decision_id']}: {e}")
        first = parse_instant(seen) or now
        if not hits and (now - first).total_seconds() / 3600 <= ttl:
            unmatched.append({"outcome": o, "seen": seen})
    st["unmatched"] = unmatched[-MAX_UNMATCHED:]
    horizon = src.get("horizon_hours")
    expired = []
    if horizon and not estimates:
        for d in engine.pending(ns):
            decided = parse_instant(d.get("decided_at"))
            if not d.get("expired") and decided and (now - decided).total_seconds() / 3600 > horizon:
                try:
                    engine.outcome({"decision_id": d["decision_id"], "result": "unknown", "source": "poller",
                                    "source_ref": f"horizon:{d['decision_id']}", "label_confidence": 0.0,
                                    "observed_at": now.isoformat(), "detail": "horizon dépassé sans règlement"})
                    expired.append(d["decision_id"])
                except (ValueError, KeyError) as e:
                    errors.append(f"{d['decision_id']}: {e}")
    st["watermark"], st["last_run"] = new_wm, now.isoformat()
    tmp = state_path.with_suffix(".tmp"); tmp.write_text(json.dumps(state, indent=1)); os.replace(tmp, state_path)
    return {"source": src["name"], "namespace": ns, "events": len(events), "posted": posted, "expired": expired,
            "skipped": len(skipped), "unmatched": len(st["unmatched"]), "unmapped": unmapped, "errors": errors,
            "watermark": new_wm}


def run(engine, config: dict, once: bool = True, interval_s: int = 300) -> list[dict]:
    srcs = (config.get("poller") or {}).get("sources") or []
    state_path = Path(engine.data_dir) / "poller_state.json"
    while True:
        results = []
        for src in srcs:
            try:
                results.append(tick(engine, src, state_path))
            except Exception as e:  # noqa: BLE001  une source en panne ne bloque pas les autres
                results.append({"source": src.get("name"), "error": str(e)})
        if once:
            return results
        print(json.dumps(results, ensure_ascii=False), file=sys.stderr)
        time.sleep(interval_s)
