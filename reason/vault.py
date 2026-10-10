"""Puits markdown : Reason LIT `doctrine/` et `postmortems/` d'un vault (Obsidian ou dépôt git) et y CRÉE des propositions.
Il ne modifie jamais un fichier existant. Frontmatter YAML plat (clés scalaires, listes en ligne [a, b] ou en tirets).

Fichier de leçon :
---
id: V-042
kind: lesson
namespace: acme/invoices
status: approved          # sinon déduit du dossier : doctrine → approved, postmortems → pending
tags: [fournisseur, iban]
decision_points: [before_pay]
valid_from: 2026-10-01
valid_until: 2026-12-31
decision_id: d_...        # provenance ; absente et sans author → quarantine
author: human
---
Texte de la leçon (premier paragraphe).
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
MAX_FILE = 64 * 1024
_UNSAFE = re.compile(r"[\r\n\x00-\x1f]")


def fm_scalar(v) -> str:
    """Valeur de frontmatter sur une seule ligne : jamais de retour à la ligne (anti-injection de clés ou de ---)."""
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return json.dumps(v)
    t = _UNSAFE.sub(" ", str(v)).strip()[:300]
    return json.dumps(t, ensure_ascii=False) if (not t or re.search(r"[:#\[\]{},\"'&*!|>%@`]", t) or t[0] in "-?") else t


def fm_line(k: str, v) -> str:
    if isinstance(v, list):
        return f"{k}: [" + ", ".join(fm_scalar(re.sub(r"[,\[\]]", " ", str(x))) for x in v) + "]"
    return f"{k}: {fm_scalar(v)}"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FM.match(text)
    if not m:
        return {}, text.strip()
    meta: dict = {}
    key = None
    for raw in m.group(1).splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith(("- ", "  - ")) and key is not None:
            meta.setdefault(key, [])
            if isinstance(meta[key], list):
                meta[key].append(_scalar(line.split("- ", 1)[1]))
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        if val == "":
            meta[key] = []
        elif val.startswith("[") and val.endswith("]"):
            meta[key] = [_scalar(x) for x in val[1:-1].split(",") if x.strip()]
        elif val.startswith('"') and val.endswith('"') and len(val) >= 2:
            try:
                meta[key] = json.loads(val)
            except ValueError:
                meta[key] = _scalar(val)
        else:
            meta[key] = _scalar(val)
    body = m.group(2).strip()
    return meta, body


def _scalar(s: str):
    s = s.strip().strip('"').strip("'")
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    try:
        return int(s) if re.fullmatch(r"-?\d+", s) else float(s) if re.fullmatch(r"-?\d+\.\d+", s) else s
    except ValueError:
        return s


def norm_hash(meta: dict, body: str) -> str:
    """Hash insensible à l'ordre des clés et aux espaces (Obsidian réordonne le frontmatter)."""
    return hashlib.sha1(json.dumps(meta, sort_keys=True, ensure_ascii=False).encode() + b"\n" + " ".join(body.split()).encode()).hexdigest()[:12]


def load_lessons(vault_dir: Path, namespace: str) -> tuple[list[dict], list[str]]:
    """Leçons du vault pour un namespace. Statut effectif, surcharges expired/quarantine, doublons signalés."""
    out: list[dict] = []
    warnings: list[str] = []
    seen: dict[str, Path] = {}
    for folder, default_status in (("doctrine", "approved"), ("postmortems", "pending")):
        base = vault_dir / folder
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.md")):
            if path.name.startswith("d_"):        # post-mortems de décisions : jamais des leçons
                continue
            try:
                if path.stat().st_size > MAX_FILE:
                    warnings.append(f"{path}: > {MAX_FILE} octets, ignoré"); continue
                meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as e:
                warnings.append(f"{path}: illisible ({e})"); continue
            if meta.get("kind") != "lesson":
                continue
            ns = meta.get("namespace")
            nss = ns if isinstance(ns, list) else [ns]
            if namespace not in nss and "*" not in nss:
                continue
            lid = str(meta.get("id") or "")
            text = body.split("\n\n")[0].strip()
            if not lid or not text:
                warnings.append(f"{path}: id ou texte manquant, ignoré"); continue
            if lid in seen:
                warnings.append(f"{path}: id {lid} dupliqué avec {seen[lid]}, ignoré"); continue
            seen[lid] = path
            status = str(meta.get("status") or default_status)
            if not (meta.get("decision_id") or meta.get("author")):
                status = "quarantine"
            out.append({"id": lid, "text": " ".join(text.split())[:300], "status": status,
                        "tags": [str(t) for t in (meta.get("tags") or [])],
                        "decision_points": [str(d) for d in (meta.get("decision_points") or [])],
                        "valid_from": meta.get("valid_from"), "valid_until": meta.get("valid_until"),
                        "review_by": meta.get("review_by"), "source": {"decision_id": meta.get("decision_id"),
                                                                        "outcome_id": meta.get("outcome_id"),
                                                                        "author": meta.get("author")},
                        "path": str(path), "hash": norm_hash(meta, body), "external": True})
    return out, warnings


def dir_signature(vault_dir: Path) -> str:
    """Signature bon marché (chemins + mtime) pour détecter un changement sans relire tous les fichiers."""
    parts = []
    for folder in ("doctrine", "postmortems"):
        base = vault_dir / folder
        if base.exists():
            for p in sorted(base.rglob("*.md")):
                if p.name.startswith("d_"):
                    continue
                try:
                    parts.append(f"{p}:{p.stat().st_mtime_ns}:{p.stat().st_size}")
                except OSError:
                    continue
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:12]


_SAFE_NS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*){0,2}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")


def _check(namespace: str, ident: str) -> None:
    if not _SAFE_NS.fullmatch(namespace or "") or not _SAFE_ID.fullmatch(ident or ""):
        raise ValueError("namespace ou identifiant invalide pour le vault")


def write_proposal(vault_dir: Path, namespace: str, lesson: dict, kind: str = "lesson") -> Path | None:
    """Crée un fichier de proposition dans postmortems/<ns>/ ; ne touche jamais un fichier existant."""
    _check(namespace, lesson.get("id"))
    d = vault_dir / "postmortems" / namespace
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{kind}-{lesson['id']}.md"
    if path.exists():
        return None
    src = lesson.get("source") or {}
    fm = {"id": lesson["id"], "kind": kind, "namespace": namespace, "status": "pending",
          "tags": lesson.get("tags") or [], "decision_points": lesson.get("decision_points") or [],
          "valid_until": lesson.get("valid_until"), "decision_id": src.get("decision_id") if isinstance(src, dict) else lesson.get("source"),
          "author": "reason", "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    lines = ["---"] + [fm_line(k, v) for k, v in fm.items() if v not in (None, [])]
    lines += ["---", _UNSAFE.sub(" ", lesson["text"]).strip(), ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_postmortem(root: Path, namespace: str, decision_id: str, meta: dict, sections: dict[str, str]) -> Path:
    """Un fichier par décision réglée (nom d_*, jamais lu comme leçon) ; réécrit à chaque révision avec une section Historique."""
    _check(namespace, decision_id)
    d = root / "postmortems" / namespace
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{decision_id}.md"
    history = ""
    if path.exists():
        old = path.read_text(encoding="utf-8")
        prev = old.split("## Historique des versions", 1)[-1].strip() if "## Historique des versions" in old else ""
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        history = f"\n## Historique des versions\n\n- {stamp} : version {meta.get('version')} ({meta.get('result_detail') or meta.get('result')})\n" + (prev + "\n" if prev else "")
    lines = ["---"] + [fm_line(k, v) for k, v in meta.items() if v not in (None, [], "")] + ["---"]
    for title, text in sections.items():
        if text:
            lines += [f"## {title}", "", text.strip().replace("\n---", "\n- - -"), ""]
    path.write_text("\n".join(lines) + history, encoding="utf-8")
    return path
