"""CLI : reason serve | decide | outcome | execute | lessons | eval | fingerprint | init"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import Engine
from .evalrun import fingerprint, replay
from .server import load_config, serve

EXAMPLE_RULES = [
    {"id": "amount-cap", "if": [{"path": "state.facts.amount", "op": "gt", "value": 5000}],
     "forbid": ["approve"], "reason": "montant > 5000 : jamais d'approbation automatique"},
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
    l = sub.add_parser("lessons"); l.add_argument("namespace"); l.add_argument("action", choices=["list", "pending", "approve", "reject", "add"])
    l.add_argument("value", nargs="?")
    e = sub.add_parser("eval"); e.add_argument("namespace"); e.add_argument("-k", type=int, default=1); e.add_argument("--limit", type=int)
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
    elif a.cmd == "lessons":
        pb = eng.playbook(a.namespace)
        if a.action in ("approve", "reject", "add") and not a.value:
            ap.error(f"lessons {a.action} attend une valeur (id ou texte)")
        out = {"list": lambda: pb.active(), "pending": lambda: pb.pending(),
               "approve": lambda: pb.approve(a.value), "reject": lambda: pb.reject(a.value),
               "add": lambda: pb.add(a.value, source="manual")}[a.action]()
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
