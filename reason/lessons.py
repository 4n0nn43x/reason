"""Playbook de leçons par namespace, à la ACE : unités atomiques avec compteurs utile/nuisible, jamais réécrit en bloc.

v2 : tags, portée (decision_points), fenêtre de validité (valid_from, valid_until), statut, provenance, score de
calibration (brier_n, brier_sum), tri par borne de Wilson, injection bornée (K leçons, budget de caractères), fusion
avec les leçons d'un vault markdown (lecture seule, compteurs gardés ici sous `counters`).

Fichier JSON : {"lessons": [...], "pending": [...], "counters": {id: {...}}, "next_id": n}
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager

try:
    import fcntl
except ImportError:      # pragma: no cover  (Windows : verrou de processus seulement)
    fcntl = None
from datetime import datetime, timezone
from pathlib import Path

from .calib import wilson_lower
from .rules import parse_instant

_WORD = re.compile(r"\w+", re.UNICODE)
_NEG = {"ne", "pas", "jamais", "non", "sans", "aucun", "aucune", "not", "never", "no", "don't", "without"}
MAX_LESSON_CHARS = 300
DEFAULT_MAX_LESSONS = 6
COUNTER_KEYS = ("helpful", "harmful", "brier_n", "brier_sum", "last_used_at", "archived")


def _tokens(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text) if len(w) > 2}


def _similar(a: str, b: str, threshold: float = 0.8) -> bool:
    """Doublon = même sens : Jaccard sur les mots ET mêmes marqueurs de négation (« approuver » vs « ne pas approuver »)."""
    ta, tb = _tokens(a), _tokens(b)
    wa, wb = {w.lower() for w in _WORD.findall(a)}, {w.lower() for w in _WORD.findall(b)}
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= threshold and (wa & _NEG) == (wb & _NEG)


def _instant(v, end_of_day: bool = False) -> datetime | None:
    """Instant ISO 8601 ou epoch (via rules.parse_instant) ; une date seule vaut minuit, ou 23:59:59 pour une fin de validité."""
    if isinstance(v, datetime) or v is None or v == "":
        return v or None
    d = parse_instant(v)
    if d is not None and end_of_day and isinstance(v, str) and len(v.strip()) == 10:
        d = d.replace(hour=23, minute=59, second=59)
    return d


def _new_lesson(lid: str, text: str, source, helpful: int = 0, **extra) -> dict:
    l = {"id": lid, "text": " ".join(text.split())[:MAX_LESSON_CHARS], "helpful": helpful, "harmful": 0,
         "brier_n": 0, "brier_sum": 0.0, "created": time.time(), "source": source, "archived": False,
         "status": "approved", "tags": [], "decision_points": [], "valid_from": None, "valid_until": None,
         "last_used_at": None}
    l.update({k: v for k, v in extra.items() if v is not None})
    return l


class Playbook:
    def __init__(self, path: Path, external_loader=None, max_lessons: int = DEFAULT_MAX_LESSONS, external_signature=None):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = 0
        self.max_lessons = max_lessons
        self.external_loader = external_loader      # () -> (lessons, warnings) ; leçons du vault, lecture seule
        self.external_signature = external_signature  # () -> str ; on ne relit le vault que si elle change
        self._external: list[dict] = []
        self._external_sig = None
        self.warnings: list[str] = []
        self.data = {"lessons": [], "pending": [], "counters": {}, "next_id": 1}
        self._refresh()

    # --- persistance
    def _refresh(self) -> None:
        """Recharge si le fichier a changé (CLI approve/reject pendant que le serveur tourne) et relit le vault."""
        try:
            m = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            m = None
        if m is not None and m != self._mtime:
            data = json.loads(self.path.read_text())
            data.setdefault("pending", []); data.setdefault("counters", {})
            data.setdefault("next_id", 1 + len(data["lessons"]) + len(data["pending"]))
            for l in data["lessons"] + data["pending"]:
                for k, v in _new_lesson("x", "x", None).items():
                    l.setdefault(k, v if k not in ("id", "text", "source") else l.get(k))
            self.data, self._mtime = data, m
        if self.external_loader is not None:
            sig = self.external_signature() if self.external_signature else None
            if sig is None or sig != self._external_sig:
                try:
                    lessons, warnings = self.external_loader()
                except Exception as e:  # noqa: BLE001  un vault cassé ne doit pas bloquer les décisions
                    lessons, warnings = [], [f"vault illisible: {e}"]
                local_ids = {l["id"] for l in self.data["lessons"] + self.data["pending"]}
                for e in lessons:
                    if e["id"] in local_ids:
                        warnings.append(f"{e.get('path')}: id {e['id']} déjà utilisé par une leçon locale, ignoré (préfixer les ids du vault, ex. V-)")
                self._external = [e for e in lessons if e["id"] not in local_ids]
                self.warnings, self._external_sig = warnings, sig

    @contextmanager
    def _locked(self):
        """Lecture-modification-écriture atomique entre threads ET entre processus (serve, poll, CLI) : verrou fcntl sur
        un fichier .lock, relecture forcée du playbook sous verrou, puis écriture par remplacement atomique."""
        with self._lock:
            with open(self.path.with_suffix(".lock"), "a") as fd:
                if fcntl:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    self._mtime = -1          # force la relecture : un autre processus a pu écrire
                    self._refresh()
                    yield
                finally:
                    if fcntl:
                        fcntl.flock(fd, fcntl.LOCK_UN)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime_ns

    def _next_id(self) -> str:
        taken = {e["id"] for e in self._external}
        while True:                                   # jamais un id déjà porté par une leçon du vault
            n = self.data["next_id"]; self.data["next_id"] = n + 1
            if f"L-{n:03d}" not in taken:
                return f"L-{n:03d}"

    # --- vue fusionnée (fichier + vault), compteurs du vault lus dans data["counters"]
    def _all(self) -> list[dict]:
        local_ids = {l["id"] for l in self.data["lessons"]}
        merged = [dict(l) for l in self.data["lessons"]]
        for e in self._external:
            if e["id"] in local_ids:
                continue
            c = self.data["counters"].get(e["id"], {})
            merged.append({**_new_lesson(e["id"], e["text"], e.get("source")), **e,
                           **{k: c.get(k, {"last_used_at": None, "archived": False}.get(k, 0)) for k in COUNTER_KEYS}})
        return merged

    def _is_active(self, l: dict, now: datetime | None) -> bool:
        if l.get("archived") or l.get("status", "approved") != "approved":
            return False
        now = now or datetime.now(timezone.utc)
        vf, vu = _instant(l.get("valid_from")), _instant(l.get("valid_until"), end_of_day=True)
        return not (vf and now < vf) and not (vu and now > vu)

    @staticmethod
    def score(l: dict) -> float:
        """Borne inférieure de Wilson ; une leçon approuvée jamais évaluée reçoit le prior 1/1 (comme une approbation locale),
        pour qu'une doctrine écrite par un humain ne soit jamais affamée par des leçons déjà notées."""
        h, n = l.get("helpful", 0), l.get("helpful", 0) + l.get("harmful", 0)
        return wilson_lower(h, n) if n else wilson_lower(1, 1)

    def active(self, now: datetime | None = None) -> list[dict]:
        with self._lock:
            self._refresh()
            acts = [l for l in self._all() if self._is_active(l, now)]
            return sorted(acts, key=lambda l: (self.score(l), l.get("last_used_at") or 0, l.get("created", 0)), reverse=True)

    def pending(self) -> list[dict]:
        with self._lock:
            self._refresh()
            return [dict(p) for p in self.data["pending"]] + [dict(e) for e in self._external if e.get("status") == "pending"]

    def render(self, max_chars: int = 4000, decision_point: str | None = None, tags: list[str] | None = None,
               now=None, exclude: tuple = ()) -> tuple[str, list[str]]:
        """Texte à injecter et ids montrés : filtrer (statut, fenêtre, portée) → trier (Wilson) → couper (K, budget)."""
        now_dt = _instant(now) if now is not None and not isinstance(now, datetime) else now
        lines, ids, size = [], [], 0
        for l in self.active(now_dt):
            if l["id"] in exclude:
                continue
            if decision_point and l.get("decision_points") and decision_point not in l["decision_points"]:
                continue
            if l.get("tags") and tags is not None and not set(l["tags"]) & set(tags):
                continue
            line = f"- [{l['id']}] {l['text']}"
            if size + len(line) > max_chars or len(ids) >= self.max_lessons:
                break
            lines.append(line); ids.append(l["id"]); size += len(line) + 1
        return "\n".join(lines), ids

    # --- écriture
    def add(self, text: str, source="manual", helpful: int = 1, **extra) -> dict:
        text = " ".join(text.split())[:MAX_LESSON_CHARS]
        with self._locked():
            for l in self.data["lessons"]:
                if _similar(l["text"], text):          # doublon : on renforce au lieu d'ajouter
                    l["helpful"] += 1; l["archived"] = False
                    self._save(); return l
            l = _new_lesson(self._next_id(), text, source, helpful, **extra)
            self.data["lessons"].append(l); self._save()
            return l

    def _counters(self, lid: str) -> dict | None:
        for l in self.data["lessons"]:
            if l["id"] == lid:
                return l
        if any(e["id"] == lid for e in self._external):
            return self.data["counters"].setdefault(lid, {"helpful": 0, "harmful": 0, "brier_n": 0, "brier_sum": 0.0, "last_used_at": None, "archived": False})
        return None

    def feedback(self, ids: list[str], helpful: bool, brier: float | None = None, weight: int = 1) -> None:
        """weight = 1 pour appliquer, -1 pour annuler (révision d'une issue)."""
        with self._locked():
            for lid in ids:
                c = self._counters(lid)
                if c is None:
                    continue
                c["helpful" if helpful else "harmful"] = max(0, c.get("helpful" if helpful else "harmful", 0) + weight)
                if brier is not None:
                    c["brier_n"] = max(0, c.get("brier_n", 0) + weight)
                    c["brier_sum"] = max(0.0, c.get("brier_sum", 0.0) + weight * brier)
                # ponytail: seuil fixe d'élagage ; passer en ratio pondéré par l'âge si le playbook dépasse ~200 leçons
                c["archived"] = c.get("harmful", 0) >= 3 and c["harmful"] >= c.get("helpful", 0) + 3   # recalculé : une révision peut désarchiver
            self._save()

    def touch(self, ids: list[str]) -> None:
        with self._locked():
            for lid in ids:
                c = self._counters(lid)
                if c is not None:
                    c["last_used_at"] = time.time()
            self._save()

    def propose(self, texts: list[str], source: str, **extra) -> list[str]:
        """Propositions en attente de validation humaine. Retourne les ids créés."""
        ids = []
        with self._locked():
            for t in texts:
                t = " ".join(t.split())[:MAX_LESSON_CHARS]
                if not t or any(_similar(p["text"], t) for p in self.data["pending"] + self._all()):
                    continue
                p = _new_lesson(self._next_id(), t, source, 0, **extra)
                p["status"] = "pending"
                self.data["pending"].append(p); ids.append(p["id"])
            self._save()
        return [p["id"] for p in self.data["pending"] if p["id"] in ids]

    def get(self, lid: str) -> dict | None:
        with self._lock:
            self._refresh()
            return next((dict(l) for l in self._all() + self.data["pending"] if l["id"] == lid), None)

    def approve(self, lid: str) -> dict | None:
        with self._locked():
            for i, p in enumerate(self.data["pending"]):
                if p["id"] == lid:
                    self.data["pending"].pop(i); p["helpful"] = 1; p["status"] = "approved"
                    self.data["lessons"].append(p); self._save(); return p
        return None

    def reject(self, lid: str) -> bool:
        with self._locked():
            before = len(self.data["pending"])
            self.data["pending"] = [p for p in self.data["pending"] if p["id"] != lid]
            self._save(); return len(self.data["pending"]) < before

    def review(self, min_n: int = 30, promote_wilson: float = 0.7, demote_wilson: float = 0.3) -> dict:
        """Candidats à la promotion en règle P0 et à la mise en revue. Reason signale, l'humain décide."""
        promote, demote = [], []
        for l in self.active():
            n = l["helpful"] + l["harmful"]
            w = self.score(l)
            bmean = (l["brier_sum"] / l["brier_n"]) if l.get("brier_n") else None
            row = {"id": l["id"], "text": l["text"], "n": n, "wilson": round(w, 3), "brier_mean": bmean}
            if n >= min_n and w >= promote_wilson:
                promote.append(row)
            elif (n >= 10 and w < demote_wilson):
                demote.append(row)
        return {"promote_to_rule": promote, "review": demote}
