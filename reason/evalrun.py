"""Évaluation : rejeu de régression (pass@1, pass^k), bilan d'issues sur cohorte mûre, rapport de calibration par source
et par leçon (observationnel et contrefactuel), empreinte de modèle pour détecter un changement silencieux."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from . import calib
from .core import Engine, base_result
from .llm import LLMError
from .rules import parse_instant

FINGERPRINT_PROMPTS = [
    "Réponds exactement par le mot OUI.",
    "Donne la liste des trois premiers nombres premiers séparés par des virgules, rien d'autre.",
    "Classe ce message en SUPPORT, VENTE ou AUTRE, réponds par un seul mot : « ma facture est fausse »",
    "Écris le JSON {\"ok\": true} et rien d'autre.",
    "Réponds par un seul mot : quelle est la capitale de la France ?",
]


def _rows(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def replay(engine: Engine, ns: str, k: int = 1, limit: int | None = None) -> dict:
    rows = _rows(engine.ns_dir(ns) / "dataset.jsonl")
    rows = rows[:limit] if limit else rows
    results = []
    for i, row in enumerate(rows):
        ok_all, got = True, []
        for j in range(k):
            req = {**row["request"], "idempotency_key": None, "run_id": f"eval-{time.time_ns()}-{i}-{j}"}
            try:
                dec = engine.decide(req)
                got.append(dec["action"]["name"])
                ok_all &= dec["action"]["name"] == row["expected_action"]
            except (LLMError, ValueError) as e:
                got.append(f"error:{e}"); ok_all = False
        results.append({"expected": row["expected_action"], "got": got, "pass_k": ok_all})
    n = len(results)
    pass1 = sum(r["got"][0] == r["expected"] for r in results if r["got"]) / n if n else None
    passk = sum(r["pass_k"] for r in results) / n if n else None
    return {"namespace": ns, "cases": n, "k": k, "pass_1": pass1, "pass_k": passk, "results": results}


def outcome_summary(engine: Engine, ns: str, horizon_hours: float | None = None, now: datetime | None = None) -> dict:
    """Bilan sur la cohorte mûre seulement (décidée il y a plus de horizon_hours) ; une issue absente n'est ni un échec
    ni un succès, et la couverture des labels est toujours affichée."""
    now = now or datetime.now(timezone.utc)
    engine.refresh()
    with engine._lock:
        items = list(engine._by_id.items())
        outcomes = dict(engine._outcomes)
    total = mature = labelled = success = failure = unknown = 0
    latencies = []
    for did, rec in items:
        req = rec["request"]
        if req.get("namespace") != ns:
            continue
        total += 1
        decided = parse_instant(req.get("now")) or datetime.fromtimestamp(rec["ts"], tz=timezone.utc)
        if horizon_hours is not None and (now - decided).total_seconds() / 3600 <= horizon_hours:
            continue
        mature += 1
        o = outcomes.get(did)
        if not o:
            continue
        labelled += 1
        base = base_result(o)
        if base == "success":
            success += 1
        elif base == "failure":
            failure += 1
        else:
            unknown += 1
        s_, obs = parse_instant(o.get("settled_at")), parse_instant(o.get("observed_at"))
        if s_ and obs:
            latencies.append((obs - s_).total_seconds())
    known = success + failure
    latencies.sort()
    return {"namespace": ns, "decisions": total, "mature": mature, "labelled": labelled,
            "coverage": labelled / mature if mature else None, "success": success, "failure": failure, "unknown": unknown,
            "success_rate_on_known": success / known if known else None,
            "poller_lag_s_median": latencies[len(latencies) // 2] if latencies else None}


def calibration_report(engine: Engine, ns: str, bins: int = 5) -> dict:
    """Par source : N, Brier brut et calibré, skill contre base et marché, Murphy (≥ 2 points par bin), couverture.
    Par leçon : Brier observationnel avec et sans la leçon (même namespace)."""
    rows = [r for r in _rows(engine.ns_dir(ns) / "estimate_scores.jsonl") if r.get("status") == "resolved" and r.get("kind") == "binary"]
    by_source: dict[str, list[dict]] = {}
    for r in rows:
        by_source.setdefault(r["source"], []).append(r)
    sources = {}
    for src, rs in by_source.items():
        ps, os_ = [r["p_raw"] for r in rs], [r["outcome"] for r in rs]
        pc = [r["p_cal"] for r in rs]
        b_raw = [calib.brier(p, o) for p, o in zip(ps, os_)]
        b_cal = [calib.brier(p, o) for p, o in zip(pc, os_)]
        base = [r["brier_base"] for r in rs if r.get("brier_base") is not None]
        mkt = [r["brier_market"] for r in rs if r.get("brier_market") is not None]
        cov = [r["covered"] for r in rs if r.get("covered") is not None]
        sources[src] = {"n": len(rs), "brier_raw": calib.mean(b_raw), "brier_cal": calib.mean(b_cal),
                        "brier_ci90": calib.bootstrap_ci(b_cal), "logloss_cal": calib.mean([r["logloss_cal"] for r in rs]),
                        "bss_vs_base": calib.skill(calib.mean(b_cal), calib.mean(base)) if len(base) == len(rs) else None,
                        "bss_vs_market": calib.skill(calib.mean(b_cal), calib.mean(mkt)) if len(mkt) == len(rs) else None,
                        "murphy": calib.murphy(pc, os_, bins) if len(rs) >= 20 * bins else None,
                        "interval_coverage": calib.mean([1.0 if c else 0.0 for c in cov]) if cov else None,
                        "calibration": engine.calibration(ns).get(src)}
    lessons: dict[str, dict] = {}
    all_b = [r["brier_cal"] for r in rows]
    for lid in {l for r in rows for l in r.get("lessons_used", [])}:
        with_l = [r["brier_cal"] for r in rows if lid in r.get("lessons_used", [])]
        without = [r["brier_cal"] for r in rows if lid not in r.get("lessons_used", [])]
        lessons[lid] = {"n_with": len(with_l), "brier_with": calib.mean(with_l), "brier_without": calib.mean(without),
                        "delta_observational": (calib.mean(without) - calib.mean(with_l)) if with_l and without else None,
                        "ci90_with": calib.bootstrap_ci(with_l)}
    return {"namespace": ns, "resolved": len(rows), "brier_all": calib.mean(all_b), "sources": sources, "lessons": lessons,
            "note": "delta_observational > 0 = la leçon accompagne de meilleures estimations ; biais de confusion possible, "
                    "le rejeu contrefactuel (calibrate --counterfactual) est la mesure causale"}


def fit_calibration(engine: Engine, ns: str, min_n: int = 30) -> dict:
    """Ajuste Platt par source (ordre chronologique, 70/30), n'écrit que les ajustements qui améliorent le test."""
    rows = [r for r in _rows(engine.ns_dir(ns) / "estimate_scores.jsonl") if r.get("status") == "resolved" and r.get("kind") == "binary"]
    epoch0 = datetime.fromtimestamp(0, tz=timezone.utc)
    rows.sort(key=lambda r: parse_instant(r.get("created_now")) or epoch0)
    current = engine.calibration(ns)
    report = {}
    for src in {r["source"] for r in rows}:
        rs = [r for r in rows if r["source"] == src]
        fit = calib.fit_if_better([r["p_raw"] for r in rs], [r["outcome"] for r in rs], min_n=min_n)
        report[src] = fit or {"method": "none", "reason": f"moins de {min_n} issues"}
        if fit and fit["method"] == "platt":
            current[src] = {**fit, "fitted_at": time.time()}
        elif src in current:
            current.pop(src)        # un ajustement qui n'améliore plus le test est retiré
    (engine.ns_dir(ns) / "calibration.json").write_text(json.dumps(current, indent=1))
    return {"namespace": ns, "fits": report, "active": current}


def counterfactual_lessons(engine: Engine, ns: str, limit: int | None = None) -> dict:
    """Rejeu « sans cette leçon » de chaque estimation binaire résolue (un appel modèle par paire estimation × leçon),
    en dry_run (rien n'est journalisé). Chaque paire n'est créditée qu'une fois (counterfactual_done.json).
    Limite connue : le rejeu est un nouvel échantillon, et le modèle peut connaître l'issue si elle est publique depuis."""
    d = engine.ns_dir(ns)
    done_path = d / "counterfactual_done.json"
    done = set(json.loads(done_path.read_text())) if done_path.exists() else set()
    scores = [r for r in _rows(d / "estimate_scores.jsonl") if r.get("status") == "resolved" and r.get("kind") == "binary"]
    deltas: dict[str, list[float]] = {}
    replays = 0
    pb = engine.playbook(ns)
    for sc in scores[:limit] if limit else scores:
        rec = engine._estimates.get(sc["estimate_id"])
        if not rec:
            continue
        for lid in sc.get("lessons_used", []):
            pair = f"{sc['estimate_id']}:{lid}"
            if pair in done:
                continue
            try:
                est = engine.estimate({**rec["request"], "exclude_lessons": [lid]}, dry_run=True)
            except (LLMError, ValueError):
                continue
            if est.get("status") != "ok":
                continue
            delta = calib.brier(est["raw"]["p"], sc["outcome"]) - calib.brier(sc["p_raw"], sc["outcome"])
            deltas.setdefault(lid, []).append(delta)
            replays += 1
            done.add(pair)
            if abs(delta) > 1e-9:                     # Δ > 0 : retirer la leçon dégrade le Brier, elle aide
                pb.feedback([lid], helpful=delta > 0)
    done_path.write_text(json.dumps(sorted(done)))
    out = {lid: {"n": len(v), "delta_brier_mean": calib.mean(v), "ci90": calib.bootstrap_ci(v)} for lid, v in deltas.items()}
    return {"namespace": ns, "replays": replays, "lessons": out,
            "note": "delta_brier_mean > 0 : retirer la leçon dégrade le Brier (elle aide) ; < 0 : elle nuit"}


def fingerprint(engine: Engine) -> dict:
    """Signature déterministe (température 0) de chaque modèle configuré ; compare au dernier relevé."""
    store = engine.data_dir / "fingerprints.json"
    prev = json.loads(store.read_text()) if store.exists() else {}
    cur, report = {}, {}
    for name, llm in engine.models.items():
        outs = []
        for p in FINGERPRINT_PROMPTS:
            try:
                txt, _ = llm.chat([{"role": "user", "content": p}], temperature=0, max_tokens=64)
            except LLMError as e:
                txt = f"error:{e}"
            outs.append(txt.strip())
        h = hashlib.sha256("\n".join(outs).encode()).hexdigest()[:16]
        cur[name] = {"hash": h, "model": llm.model, "outputs": outs, "ts": time.time()}
        old = prev.get(name)
        report[name] = ("new" if not old else "same" if old["hash"] == h else "CHANGED")
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps(cur, ensure_ascii=False, indent=1))
    return {"report": report}
