"""Playbook de leçons par namespace, à la ACE : unités atomiques avec compteurs utile/nuisible, jamais réécrit en bloc.

Fichier JSON : {"lessons":[{"id","text","helpful","harmful","created","source","archived"}], "pending":[...]}
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

_WORD = re.compile(r"\w+", re.UNICODE)
_NEG = {"ne", "pas", "jamais", "non", "sans", "aucun", "aucune", "not", "never", "no", "don't", "without"}
MAX_LESSON_CHARS = 300


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text) if len(w) > 2}


def _similar(a: str, b: str, threshold: float = 0.8) -> bool:
    """Doublon = même sens : Jaccard sur les mots ET mêmes marqueurs de négation (« approuver » vs « ne pas approuver »)."""
    ta, tb = _tokens(a), _tokens(b)
    wa, wb = {w.lower() for w in _WORD.findall(a)}, {w.lower() for w in _WORD.findall(b)}
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= threshold and (wa & _NEG) == (wb & _NEG)


class Playbook:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = 0.0
        self.data = {"lessons": [], "pending": [], "next_id": 1}
        self._refresh()

    def _refresh(self) -> None:
        """Recharge si le fichier a changé (CLI approve/reject pendant que le serveur tourne)."""
        try:
            m = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return
        if m != self._mtime:
            data = json.loads(self.path.read_text())
            data.setdefault("pending", []); data.setdefault("next_id", 1 + len(data["lessons"]) + len(data["pending"]))
            self.data, self._mtime = data, m

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime_ns

    def _next_id(self) -> str:
        n = self.data["next_id"]; self.data["next_id"] = n + 1
        return f"L-{n:03d}"

    def active(self) -> list[dict]:
        with self._lock:
            self._refresh()
            return sorted((dict(l) for l in self.data["lessons"] if not l.get("archived")),
                          key=lambda l: (l["helpful"] - l["harmful"], l["helpful"]), reverse=True)

    def pending(self) -> list[dict]:
        with self._lock:
            self._refresh()
            return [dict(p) for p in self.data["pending"]]

    def render(self, max_chars: int = 4000) -> tuple[str, list[str]]:
        """Texte à injecter dans le prompt et liste des ids montrés, dans la limite de max_chars."""
        lines, ids, size = [], [], 0
        for l in self.active():
            line = f"- [{l['id']}] {l['text']}"
            if size + len(line) > max_chars:
                break
            lines.append(line); ids.append(l["id"]); size += len(line) + 1
        return "\n".join(lines), ids

    def add(self, text: str, source: str = "manual", helpful: int = 1) -> dict:
        text = " ".join(text.split())[:MAX_LESSON_CHARS]
        with self._lock:
            self._refresh()
            for l in self.data["lessons"]:
                if _similar(l["text"], text):          # doublon : on renforce au lieu d'ajouter
                    l["helpful"] += 1; l["archived"] = False
                    self._save(); return l
            l = {"id": self._next_id(), "text": text, "helpful": helpful, "harmful": 0,
                 "created": time.time(), "source": source, "archived": False}
            self.data["lessons"].append(l); self._save()
            return l

    def feedback(self, ids: list[str], helpful: bool) -> None:
        with self._lock:
            self._refresh()
            for l in self.data["lessons"]:
                if l["id"] in ids:
                    l["helpful" if helpful else "harmful"] += 1
                    # ponytail: seuil fixe d'élagage ; passer en ratio pondéré par l'âge si le playbook dépasse ~200 leçons
                    if l["harmful"] >= 3 and l["harmful"] >= l["helpful"] + 3:
                        l["archived"] = True
            self._save()

    def propose(self, texts: list[str], source: str) -> list[str]:
        """Propositions en attente de validation humaine (ou d'eval verte). Retourne les ids créés."""
        ids = []
        with self._lock:
            self._refresh()
            for t in texts:
                t = " ".join(t.split())[:MAX_LESSON_CHARS]
                if not t or any(_similar(p["text"], t) for p in self.data["pending"] + self.data["lessons"]):
                    continue
                p = {"id": self._next_id(), "text": t, "helpful": 0, "harmful": 0, "created": time.time(),
                     "source": source, "archived": False}
                self.data["pending"].append(p); ids.append(p["id"])
            self._save()
        return ids

    def approve(self, lid: str) -> dict | None:
        with self._lock:
            self._refresh()
            for i, p in enumerate(self.data["pending"]):
                if p["id"] == lid:
                    self.data["pending"].pop(i); p["helpful"] = 1
                    self.data["lessons"].append(p); self._save(); return p
        return None

    def reject(self, lid: str) -> bool:
        with self._lock:
            self._refresh()
            before = len(self.data["pending"])
            self.data["pending"] = [p for p in self.data["pending"] if p["id"] != lid]
            self._save(); return len(self.data["pending"]) < before
