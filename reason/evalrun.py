"""Rejeu de régression (pass@1 et pass^k) et empreinte de modèle pour détecter un changement silencieux."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .core import Engine
from .llm import LLMError

FINGERPRINT_PROMPTS = [
    "Réponds exactement par le mot OUI.",
    "Donne la liste des trois premiers nombres premiers séparés par des virgules, rien d'autre.",
    "Classe ce message en SUPPORT, VENTE ou AUTRE, réponds par un seul mot : « ma facture est fausse »",
    "Écris le JSON {\"ok\": true} et rien d'autre.",
    "Réponds par un seul mot : quelle est la capitale de la France ?",
]


def replay(engine: Engine, ns: str, k: int = 1, limit: int | None = None) -> dict:
    path = engine.ns_dir(ns) / "dataset.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []
    rows = rows[:limit] if limit else rows
    results = []
    for i, row in enumerate(rows):
        req = {**row["request"], "idempotency_key": None, "run_id": f"eval-{int(time.time())}-{i}"}
        ok_all, got = True, []
        for _ in range(k):
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


def fingerprint(engine: Engine, store: Path | None = None) -> dict:
    """Signature déterministe (température 0) de chaque modèle configuré ; compare au dernier relevé."""
    store = store or engine.data_dir / "fingerprints.json"
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
    return {"report": report, "fingerprints": cur, "previous": prev}
