"""CLI : reason serve | decide | outcome | execute | estimate | estimate-outcome | lessons | rules | eval | outcomes |
calibrate | poll | pending | fingerprint | init"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import Engine
from .evalrun import calibration_report, counterfactual_lessons, fingerprint, fit_calibration, outcome_summary, replay
from .poller import run as poll_run
from .rules import Rules
from .server import load_config, serve

EXAMPLE_RULES = [
    {"id": "amount-cap", "if": [{"path": "state.facts.amount", "op": "gt", "value": 5000}],
     "forbid": ["approve"], "reason": "montant {state.facts.amount} > 5000 : jamais d'approbation automatique"},
    {"id": "window-closes-before-event",
     "if": [{"path": "state.facts.window_end", "op": "before", "path2": "state.facts.event_at"}],
     "decide": {"name": "ask_human"}, "reason": "Fenêtre close ({state.facts.window_end}) avant l'événement ({state.facts.event_at})"},
    {"id": "known-small", "if": [{"path": "state.facts.vendor_known", "op": "eq", "value": True},
                                 {"path": "state.facts.amount", "op": "lte", "value": 200}],
     "decide": {"name": "approve", "params": {"limit": 200}}},
]
EXAMPLE_POLICY = "# Politique\n\n- En cas de doute, préférer `ask_human` à une action irréversible.\n- Ne jamais approuver un fournisseur inconnu au-dessus de 1000.\n"


def _read(arg: str) -> dict:
    return json.loads(Path(arg).read_text() if arg != "-" and Path(arg).exists() else (sys.stdin.read() if arg == "-" else arg))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="reason")
    ap.add_argument("--config", help="fichier de config (défaut: $REASON_CONFIG ou reason.config.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8787)
    d = sub.add_parser("decide"); d.add_argument("request", help="fichier JSON, JSON inline ou - (stdin)")
    o = sub.add_parser("outcome"); o.add_argument("outcome")
    x = sub.add_parser("execute"); x.add_argument("task")
    es = sub.add_parser("estimate"); es.add_argument("request")
    eo = sub.add_parser("estimate-outcome"); eo.add_argument("outcome")
    l = sub.add_parser("lessons"); l.add_argument("namespace")
    l.add_argument("action", choices=["list", "pending", "approve", "reject", "add", "review"])
    l.add_argument("value", nargs="?")
    r = sub.add_parser("rules"); r.add_argument("namespace"); r.add_argument("action", choices=["lint", "test"])
    r.add_argument("--cases", help="fichier JSON de cas (défaut: data/<ns>/rules.test.json)")
    e = sub.add_parser("eval"); e.add_argument("namespace"); e.add_argument("-k", type=int, default=1); e.add_argument("--limit", type=int)
    oc = sub.add_parser("outcomes"); oc.add_argument("namespace"); oc.add_argument("--horizon-hours", type=float)
    c = sub.add_parser("calibrate"); c.add_argument("namespace"); c.add_argument("--fit", action="store_true", help="ajuste Platt par source")
    c.add_argument("--counterfactual", action="store_true", help="rejoue chaque estimation sans chaque leçon utilisée (coût : appels modèle)")
    c.add_argument("--limit", type=int)
    pl = sub.add_parser("poll"); pl.add_argument("--loop", action="store_true"); pl.add_argument("--interval", type=int, default=300)
    pd = sub.add_parser("pending"); pd.add_argument("namespace")
    sub.add_parser("fingerprint")
    i = sub.add_parser("init"); i.add_argument("namespace")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.cmd == "serve":
        serve(cfg, a.host, a.port); return 0
    eng = Engine(cfg)
    if a.cmd == "decide":
        out = eng.decide(_read(a.request))
    elif a.cmd == "outcome":
        out = eng.outcome(_read(a.outcome))
    elif a.cmd == "execute":
        out = eng.execute(_read(a.task))
    elif a.cmd == "estimate":
        out = eng.estimate(_read(a.request))
    elif a.cmd == "estimate-outcome":
        out = eng.estimate_outcome(_read(a.outcome))
    elif a.cmd == "rules":
        rules = eng.rules(a.namespace)
        if a.action == "lint":
            out = {"errors": rules.errors, "count": len(rules.rules)}
        else:
            cases_path = Path(a.cases) if a.cases else eng.ns_dir(a.namespace) / "rules.test.json"
            cases = json.loads(cases_path.read_text()) if cases_path.exists() else []
            out = rules.test(cases)
            if out["failed"] or out["lint"] or out["contradictions"]:
                print(json.dumps(out, ensure_ascii=False, indent=1)); return 1
    elif a.cmd == "outcomes":
        out = outcome_summary(eng, a.namespace, a.horizon_hours)
    elif a.cmd == "calibrate":
        out = {"report": calibration_report(eng, a.namespace)}
        if a.fit:
            out["fit"] = fit_calibration(eng, a.namespace)
        if a.counterfactual:
            out["counterfactual"] = counterfactual_lessons(eng, a.namespace, a.limit)
    elif a.cmd == "poll":
        out = poll_run(eng, cfg, once=not a.loop, interval_s=a.interval)
    elif a.cmd == "pending":
        out = eng.pending(a.namespace)
    elif a.cmd == "lessons":
        pb = eng.playbook(a.namespace)
        if a.action in ("approve", "reject", "add") and not a.value:
            ap.error(f"lessons {a.action} attend une valeur (id ou texte)")
        out = {"list": lambda: pb.active(), "pending": lambda: pb.pending(),
               "approve": lambda: pb.approve(a.value), "reject": lambda: pb.reject(a.value),
               "add": lambda: pb.add(a.value, source="manual"), "review": lambda: pb.review()}[a.action]()
    elif a.cmd == "eval":
        out = replay(eng, a.namespace, k=a.k, limit=a.limit)
    elif a.cmd == "fingerprint":
        out = fingerprint(eng)
    elif a.cmd == "init":
        d = eng.ns_dir(a.namespace)
        for name, content in (("rules.json", json.dumps(EXAMPLE_RULES, ensure_ascii=False, indent=1)), ("policy.md", EXAMPLE_POLICY)):
            p = d / name
            if not p.exists():
                p.write_text(content)
        out = {"created": str(d), "files": sorted(p.name for p in d.iterdir())}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
