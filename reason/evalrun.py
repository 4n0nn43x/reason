"""Évaluation : rejeu de régression (pass@1, pass^k), bilan d'issues sur cohorte mûre, rapport de calibration par source
et par leçon (observationnel et contrefactuel), empreinte de modèle pour détecter un changement silencieux."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import calib
from .core import META_ACTIONS, Engine, base_result
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


def _dataset(engine: Engine, ns: str, after=None, limit: int | None = None) -> list[dict]:
    rows = _rows(engine.ns_dir(ns) / "dataset.jsonl")
    if after is not None:                 # jeu tenu à l'écart : décisions postérieures à la date (ex. création des leçons)
        a = parse_instant(after)
        if a is None:
            raise ValueError("--after doit être une date ISO 8601")
        rows = [r for r in rows if (parse_instant(r["request"].get("now")) or a) >= a]
    return rows[:limit] if limit else rows


def replay(engine: Engine, ns: str, k: int = 1, limit: int | None = None, lessons="on", after=None) -> dict:
    """Rejoue le jeu de régression en dry_run (rien n'est journalisé). lessons : "on", "off", ou liste de textes
    (leçons substituées, par exemple volontairement erronées). Chaque exécution est inscrite au registre des essais.
    Limite : le jeu est stocké après masquage (redact_paths) ; un cas qui dépend d'un champ masqué est signalé."""
    rows = _dataset(engine, ns, after, limit)
    override = None if lessons == "on" else lessons
    results = []
    for i, row in enumerate(rows):
        ok_all, got = True, []
        for j in range(k):
            req = {**row["request"], "idempotency_key": None, "run_id": f"eval-{time.time_ns()}-{i}-{j}"}
            try:
                dec = engine.decide(req, dry_run=True, lessons_override=override)
                if dec.get("model_errors") and dec.get("proposed") is None:   # panne de fournisseur, pas une décision
                    raise LLMError(dec["rationale"][:200])
                got.append(dec["action"]["name"])
                ok_all &= dec["action"]["name"] == row["expected_action"]
            except (LLMError, ValueError) as e:
                got.append(f"error:{e}"); ok_all = False
        results.append({"expected": row["expected_action"], "got": got, "pass_k": ok_all,
                        "redacted": "[redacted]" in json.dumps(row["request"])})
    n = len(results)
    pass1 = sum(r["got"][0] == r["expected"] for r in results if r["got"]) / n if n else None
    passk = sum(r["pass_k"] for r in results) / n if n else None
    mode = lessons if isinstance(lessons, str) else f"substituted:{len(lessons)}"
    engine._append(ns, "trials.jsonl", {"ts": time.time(), "kind": "replay", "lessons": mode, "after": after, "k": k,
                                        "cases": n, "pass_1": pass1, "pass_k": passk})
    return {"namespace": ns, "cases": n, "k": k, "lessons": mode, "after": after, "pass_1": pass1, "pass_k": passk,
            "errors": sum(any(str(g).startswith("error:") for g in r["got"]) for r in results),
            "redacted_cases": sum(r["redacted"] for r in results), "results": results}


def compare_lessons(engine: Engine, ns: str, wrong_lessons: list[str] | None = None, after=None, limit: int | None = None) -> dict:
    """Ablation appariée de la mémoire : mêmes cas, leçons on / off / erronées. Test du signe sur les paires discordantes."""
    arms = {"on": replay(engine, ns, lessons="on", after=after, limit=limit),
            "off": replay(engine, ns, lessons="off", after=after, limit=limit)}
    if wrong_lessons:
        arms["wrong"] = replay(engine, ns, lessons=wrong_lessons, after=after, limit=limit)
    ok = {a: [r["got"][0] == r["expected"] for r in res["results"]] for a, res in arms.items()}
    err = {a: [str(r["got"][0]).startswith("error:") for r in res["results"]] for a, res in arms.items()}
    def paired(a, b):
        keep = [i for i in range(len(ok[a])) if not err[a][i] and not err[b][i]]   # une panne n'est pas une victoire
        wins = sum(ok[a][i] and not ok[b][i] for i in keep); losses = sum(ok[b][i] and not ok[a][i] for i in keep)
        return {"better": wins, "worse": losses, "pairs": len(keep), "excluded_errors": len(ok[a]) - len(keep),
                "p_value": calib.sign_test(wins, losses)}
    out = {"namespace": ns, "cases": arms["on"]["cases"], "pass_1": {a: r["pass_1"] for a, r in arms.items()},
           "on_vs_off": paired("on", "off")}
    if "wrong" in arms:
        out["on_vs_wrong"] = paired("on", "wrong")
    out["note"] = ("les leçons aident si on_vs_off.better > worse avec p < 0,05 ; sous ~30 paires discordantes, ne pas conclure. "
                   "Évaluer sur des décisions postérieures à la création des leçons (--after).")
    return out


def fit_decision_calibration(engine: Engine, ns: str, target_accuracy: float = 0.9, min_n: int = 30, split: float = 0.7) -> dict:
    """Calibre la confiance du décideur sur les issues réelles (decision_scores.jsonl, dernière étiquette par décision).
    Découpage chronologique : Platt et seuils appris sur les 70 % anciens, précision et couverture RAPPORTÉES sur les 30 %
    récents (pas de fuite). Seuil = plus petite confiance dont la borne de Wilson atteint la précision visée. Écriture
    atomique, fusionnée avec la calibration existante (un point qui repasse sous min_n garde son ancien seuil)."""
    epoch0 = datetime.fromtimestamp(0, tz=timezone.utc)
    rows = _rows(engine.ns_dir(ns) / "decision_scores.jsonl")
    last: dict[str, dict] = {}
    for r in rows:                           # l'ordre du fichier = l'ordre des révisions : la dernière ligne gagne
        last[r["decision_id"]] = r
    rows = sorted(last.values(), key=lambda r: parse_instant(r.get("created_now")) or epoch0)
    current = engine.decision_calibration(ns)
    out_cal: dict = {"platt": dict(current.get("platt") or {}), "thresholds": {k: dict(v) for k, v in (current.get("thresholds") or {}).items()},
                     "target_accuracy": target_accuracy, "fitted_at": time.time()}
    report: dict = {"namespace": ns, "decisions": len(rows), "tiers": {}, "thresholds": {}}
    for tier in sorted({r["tier"] for r in rows}):
        rs = [r for r in rows if r["tier"] == tier]
        k = int(len(rs) * split)
        xs, ys = [r["confidence_eff"] for r in rs], [r["correct"] for r in rs]
        fit = calib.fit_if_better(xs, ys, min_n=min_n, split=split)
        if fit and fit["method"] == "platt":
            out_cal["platt"][tier] = {"a": fit["a"], "b": fit["b"], "fitted_on": fit["fitted_on"]}
        elif fit:
            out_cal["platt"].pop(tier, None)
        cal = [calib.platt_apply(x, fit["a"], fit["b"]) for x in xs] if fit and fit["method"] == "platt" else xs
        report["tiers"][tier] = {"n": len(rs), "accuracy": calib.mean([float(y) for y in ys]),
                                 "auroc": calib.auroc(xs, ys), "aurc_test": calib.aurc(cal[k:], ys[k:]),
                                 "mean_confidence": calib.mean(xs), "platt": fit}
        for dp in sorted({r["decision_point"] for r in rs}):
            tr = [i for i in range(k) if rs[i]["decision_point"] == dp]
            te = [i for i in range(k, len(rs)) if rs[i]["decision_point"] == dp]
            if len(tr) < min_n:
                report["thresholds"].setdefault(dp, {})[tier] = f"moins de {min_n} issues d'apprentissage (seuil existant conservé)"
                continue
            t = calib.selective_threshold([cal[i] for i in tr], [ys[i] for i in tr], target_accuracy)
            t = 1.01 if t is None else round(t, 4)       # aucune confiance n'atteint la cible : escalader (les propositions
            out_cal["thresholds"].setdefault(dp, {})[tier] = t   # escaladées restent scorées, le seuil peut redescendre)
            kept = [i for i in te if cal[i] >= t]
            report["thresholds"].setdefault(dp, {})[tier] = {
                "threshold": t, "train_n": len(tr), "test_n": len(te),
                "test_coverage": len(kept) / len(te) if te else None,
                "test_accuracy": calib.mean([float(ys[i]) for i in kept]) if kept else None}
    path = engine.ns_dir(ns) / "decision_calibration.json"
    tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(out_cal, indent=1)); os.replace(tmp, path)
    report["note"] = ("auroc proche de 0,5 : la confiance ne distingue pas les erreurs ; test_accuracy est mesurée sur des "
                      "décisions non vues ; un seuil appris ne descend jamais sous le seuil configuré, et expire après "
                      "calibration_max_age_days")
    return report


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
    path = engine.ns_dir(ns) / "calibration.json"   # écriture atomique : `estimate` le lit pendant qu'on écrit
    tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(current, indent=1)); os.replace(tmp, path)
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


def _settled(engine: Engine, ns: str) -> list[tuple[dict, dict | None]]:
    """(enregistrement, issue ou None) des décisions du namespace, dans l'ordre chronologique."""
    engine.refresh()
    with engine._lock:
        items = [(r, engine._outcomes.get(did)) for did, r in engine._by_id.items() if r["request"]["namespace"] == ns]
    return sorted(items, key=lambda x: x[0]["ts"])


def _expected(dec: dict, o: dict | None) -> str | None:
    """Action correcte connue : correction humaine, sinon l'action prise si l'issue est un succès."""
    if not o or base_result(o) == "unknown":
        return None
    hf = o.get("human_feedback") or {}
    exp = hf.get("correct_action") or (dec["action"]["name"] if base_result(o) == "success" else None)
    return None if exp in META_ACTIONS else exp


def simulate_rules(engine: Engine, ns: str, candidate: list[dict], examples: int = 20) -> dict:
    """Rejoue des règles candidates sur les requêtes journalisées, sans appel modèle. Pour chaque décision : action
    forcée par les nouvelles règles, action prise désormais interdite (escalade), ou règle forcée retirée (le modèle
    serait consulté, issue inconnue). Sur les décisions réglées : erreurs évitées, erreurs introduites, escalades."""
    from .core import validate
    from .rules import Rules
    if not isinstance(candidate, list):
        raise ValueError("les règles candidates doivent être une liste JSON")
    old, new = engine.rules(ns), Rules(candidate)
    counts = {"decisions": 0, "changed": 0, "newly_forced": 0, "newly_blocked": 0, "released_to_model": 0,
              "errors_avoided": 0, "errors_introduced": 0, "new_escalations_on_success": 0,
              "escalations_automated_right": 0, "escalations_automated_wrong": 0}
    ex = []
    for rec, o in _settled(engine, ns):
        req, dec = rec["request"], rec["decision"]
        taken = dec["action"]["name"]
        if dec["tier"] == "loop":                      # l'anti-boucle passe avant les règles : rien ne changerait
            counts["decisions"] += 1
            continue
        forced, forb = new.decide(req), new.forbidden(req)
        cand = {a["name"]: a for a in req["candidate_actions"]}
        if forced and forced["name"] != "ask_human" and (     # ignorée par le moteur : abstain, non candidate,
                forced["name"] not in cand or forced["name"] in forb   # interdite, ou paramètres hors schéma
                or validate(forced.get("params", {}), cand[forced["name"]].get("schema", {"type": "object"}))):
            forced = None
        elif forced and forced["name"] in forb:
            forced = None
        if forced and forced["name"] != taken:
            pred, kind = forced["name"], "newly_forced"
        elif taken in forb and taken not in old.forbidden(req):
            pred, kind = "ask_human", "newly_blocked"
        elif dec["tier"] == "rules" and not forced:
            pred, kind = None, "released_to_model"
        else:
            counts["decisions"] += 1
            continue
        counts["decisions"] += 1
        counts["changed"] += 1
        counts[kind] += 1
        exp = _expected(dec, o)
        settled = o is not None and base_result(o) != "unknown"
        was_right = None if taken in META_ACTIONS or not settled else (taken == exp if exp else False)
        now_right = None if pred in (None, "ask_human") else (pred == exp if exp else None)
        if was_right is False and (pred == "ask_human" or now_right):
            counts["errors_avoided"] += 1
        elif was_right and now_right is False:
            counts["errors_introduced"] += 1
        elif was_right and pred == "ask_human":
            counts["new_escalations_on_success"] += 1
        elif was_right is None and now_right is not None:          # escalade d'hier tranchée par l'humain, automatisée demain
            counts["escalations_automated_right" if now_right else "escalations_automated_wrong"] += 1
        if len(ex) < examples:
            ex.append({"decision_id": dec["decision_id"], "kind": kind, "taken": taken, "predicted": pred,
                       "result": o.get("result") if o else None, "expected": exp})
    return {"namespace": ns, "lint": new.errors, **counts, "examples": ex,
            "note": "requêtes telles que journalisées (champs masqués par redact_paths compris) ; une action interdite "
                    "est comptée comme escalade alors que le palier suivant pourrait conclure ; released_to_model : issue inconnue ; "
                    "comparaison aux règles actuelles, les décisions passées ont pu suivre une version antérieure"}


def shadow_report(engine: Engine, ns: str) -> dict:
    """Champion contre challenger (mode ombre) sur les mêmes requêtes. Paires étiquetées : correction humaine, sinon
    l'action du champion si elle a réussi, ce qui avantage le champion (une autre action aurait pu réussir aussi)."""
    rows: dict[str, dict] = {}
    for r in _rows(engine.ns_dir(ns) / "shadow.jsonl"):
        rows[r["decision_id"]] = r
    by_id = {rec["decision"]["decision_id"]: (rec, o) for rec, o in _settled(engine, ns)}
    ok = [r for r in rows.values() if "challenger" in r]
    out = {"namespace": ns, "shadowed": len(rows), "errors": len(rows) - len(ok),
           "agreement": calib.mean([float(r["agree"]) for r in ok]),
           "challenger_escalation_rate": calib.mean([float(r["challenger"]["action"] in META_ACTIONS) for r in ok]),
           "champion_escalation_rate": calib.mean([float(r["champion"]["action"] in META_ACTIONS) for r in ok])}
    wins = losses = both_right = both_wrong = auto_right = auto_wrong = esc_on_error = esc_on_right = 0
    for r in ok:
        rec, o = by_id.get(r["decision_id"], (None, None))
        exp = _expected(rec["decision"], o) if rec else None
        if exp is None:
            continue
        c, h = r["champion"]["action"], r["challenger"]["action"]
        if h in META_ACTIONS:                      # le challenger escalade : erreur évitée ou automatisation perdue
            esc_on_error += c not in META_ACTIONS and c != exp
            esc_on_right += c == exp
            continue
        if c in META_ACTIONS:                      # le champion a escaladé, l'humain a tranché : le challenger aurait-il eu raison ?
            auto_right += h == exp
            auto_wrong += h != exp
            continue
        cr, hr = c == exp, h == exp
        wins += hr and not cr
        losses += cr and not hr
        both_right += cr and hr
        both_wrong += not (cr or hr)
    out["paired"] = {"n": wins + losses + both_right + both_wrong, "challenger_wins": wins, "challenger_losses": losses,
                     "both_right": both_right, "both_wrong": both_wrong, "p_sign": calib.sign_test(wins, losses)}
    out["champion_escalations_challenger_would_automate"] = {"right": auto_right, "wrong": auto_wrong}
    out["challenger_escalations"] = {"on_champion_errors": esc_on_error, "on_champion_successes": esc_on_right}
    out["note"] = ("étiquette par défaut = action du champion quand elle a réussi : biais en faveur du champion ; seules "
                   "les corrections humaines départagent sans biais ; quelques centaines de paires discordantes avant de conclure")
    return out
