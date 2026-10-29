"""Connecteur Telegram (stdlib) : prévient un humain quand une décision l'attend et recueille sa réponse par bouton.

config["telegram"] :
  {"bot_token_env": "REASON_TELEGRAM_TOKEN", "chat_id_env": "REASON_TELEGRAM_CHAT_ID",   (ou bot_token / chat_id en clair)
   "namespaces": ["*"], "poll": true, "callback_prefix": "rsn",
   "quiet_hours": {"start": 21, "end": 7, "utc_offset": 1}}

Messages envoyés : décision escaladée vers l'humain (avec un bouton par action jouable sans paramètre) et décision de repli
en mode dégradé (information) ; budget épuisé signalé une fois par namespace et par jour. L'envoi ne bloque jamais une
décision : il part dans un thread, une erreur Telegram est seulement journalisée sur stderr.

Deux façons de recevoir les clics (callback_data = "<prefix>:<decision_id>:<n° d'option>") :
  - poll: true : Reason lit lui-même les mises à jour de SON bot (bot dédié). Un seul lecteur par token : Telegram refuse
    deux getUpdates simultanés, donc jamais poll: true sur le token d'une application qui lit déjà ses mises à jour ;
  - poll: false (bot partagé) : Reason ne fait qu'envoyer ; l'application qui lit les mises à jour transmet les callbacks
    qui commencent par le préfixe à POST /telegram/callback {"data": ..., "by": ...} et affiche le `text` rendu.
Seul le chat configuré est écouté.
"""
from __future__ import annotations

import fnmatch
import html
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .core import validate

MAX_TEXT = 3500


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, cfg: dict, engine):
        self.engine = engine
        self.token = cfg.get("bot_token") or os.environ.get(cfg.get("bot_token_env", "REASON_TELEGRAM_TOKEN"), "")
        self.chat_id = str(cfg.get("chat_id") or os.environ.get(cfg.get("chat_id_env", "REASON_TELEGRAM_CHAT_ID"), ""))
        self.namespaces = cfg.get("namespaces") or ["*"]
        self.poll = bool(cfg.get("poll"))
        self.prefix = str(cfg.get("callback_prefix") or "rsn")
        self.quiet = cfg.get("quiet_hours")
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            print("reason: telegram désactivé (token ou chat_id absent)", file=sys.stderr)
        if len(self.prefix) > 16 or ":" in self.prefix:
            raise ValueError("telegram.callback_prefix : 16 caractères au plus, sans « : »")
        self._budget_sent: set[tuple[str, str]] = set()

    # --- API Bot
    def call(self, method: str, payload: dict, timeout: float = 15) -> dict:
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = {"ok": False, "description": f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}"}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TelegramError(f"{method}: {e}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description')}")   # jamais le token dans le message
        return body.get("result")

    def send(self, text: str, buttons: list[list[dict]] | None = None) -> dict:
        payload = {"chat_id": self.chat_id, "text": text[:MAX_TEXT], "parse_mode": "HTML",
                   "disable_web_page_preview": True, "disable_notification": self._quiet_now()}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return self.call("sendMessage", payload)

    def _quiet_now(self) -> bool:
        if not self.quiet:
            return False
        h = (datetime.now(timezone.utc) + timedelta(hours=float(self.quiet.get("utc_offset", 0)))).hour
        s, e = int(self.quiet["start"]), int(self.quiet["end"])
        return s <= h < e if s < e else (h >= s or h < e)

    # --- envoi
    def decision(self, req: dict, dec: dict) -> None:
        """Appelé après chaque décision servie ; n'attend pas Telegram."""
        if not self.enabled or not any(fnmatch.fnmatchcase(req["namespace"], g) for g in self.namespaces):
            return
        msg = self.message(req, dec)
        if msg is None:
            return
        threading.Thread(target=self._send_quietly, args=msg, daemon=True).start()

    def _send_quietly(self, text: str, buttons) -> None:
        try:
            self.send(text, buttons)
        except TelegramError as e:
            print(f"reason: telegram {e}", file=sys.stderr)

    def message(self, req: dict, dec: dict) -> tuple[str, list | None] | None:
        """(texte HTML, boutons) pour une décision qui concerne un humain, sinon None."""
        human = (dec.get("needs_human") or {}).get("required")
        if not human and not dec.get("degraded"):
            return None
        if not human:                      # repli déjà joué : une info, et le budget épuisé une fois par jour
            if dec.get("budget_exhausted"):
                key = (req["namespace"], datetime.now(timezone.utc).date().isoformat())
                if key in self._budget_sent:
                    return None
                self._budget_sent.add(key)
        h = html.escape
        lines = [("🔔 <b>Décision requise</b>" if human else "⚠️ <b>Mode dégradé</b> : repli joué"),
                 f"<b>{h(req['namespace'])}</b> · {h(req['decision_point'])}"]
        if not human:
            lines.append(f"Action : <b>{h(dec['action']['name'])}</b>")
        if dec.get("budget_exhausted"):
            lines.append("Budget du jour épuisé : aucun modèle appelé.")
        elif dec.get("model_errors"):
            lines.append(f"Modèles indisponibles ({dec['model_errors']} erreur(s)).")
        p = dec.get("proposed")
        if p:
            lines.append(f"Proposition du modèle : <b>{h(p['action'])}</b> (confiance {p['confidence']:.2f})")
        if dec.get("rationale"):
            lines.append(f"<i>{h(str(dec['rationale'])[:600])}</i>")
        facts = (req.get("state") or {}).get("facts") if isinstance(req.get("state"), dict) else None
        if isinstance(facts, dict) and facts:
            lines.append("<pre>" + h(json.dumps(facts, ensure_ascii=False, indent=1)[:1200]) + "</pre>")
        lines.append(f"<code>{h(dec['decision_id'])}</code>")
        buttons = None
        if human:
            opts = self.options(req)
            buttons = [[{"text": name, "callback_data": f"{self.prefix}:{dec['decision_id']}:{i}"}] for i, name in enumerate(opts)]
            missing = [a["name"] for a in req["candidate_actions"] if a["name"] not in opts]
            if missing:
                lines.append("Avec paramètres, à répondre par l'API : " + ", ".join(h(m) for m in missing))
        return "\n".join(lines), buttons

    @staticmethod
    def options(req: dict) -> list[str]:
        """Actions jouables d'un bouton : candidates valides sans paramètre, puis abstain."""
        return [a["name"] for a in req["candidate_actions"] if not validate({}, a.get("schema", {"type": "object"}))] + ["abstain"]

    # --- réponses
    def handle_callback(self, data: str, by: str = "") -> dict:
        """callback_data d'un bouton Reason -> réponse enregistrée. Retourne {"text", "recorded"} à afficher."""
        parts = data.split(":") if isinstance(data, str) else []
        if len(parts) != 3 or parts[0] != self.prefix or not parts[2].isdigit():
            raise ValueError("callback Reason invalide")
        rec = self.engine.decision(parts[1])
        req = self.engine._by_id[parts[1]]["request"]
        opts = self.options(req)
        if int(parts[2]) >= len(opts):
            raise ValueError("option inconnue")
        r = self.engine.answer({"decision_id": parts[1], "action": opts[int(parts[2])], "by": by})
        a = r["answer"]
        text = (f"✅ Réponse enregistrée : <b>{html.escape(a['action'])}</b>" if r["recorded"]
                else f"Déjà répondu : <b>{html.escape(a['action'])}</b>") + (f" ({html.escape(a['by'])})" if a.get("by") else "")
        return {"recorded": r["recorded"], "text": text, "decision_id": rec["decision"]["decision_id"], "action": a["action"]}

    def handle_update(self, upd: dict) -> None:
        cq = upd.get("callback_query") or {}
        msg = cq.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != self.chat_id or not str(cq.get("data", "")).startswith(self.prefix + ":"):
            return                          # autre chat, ou bouton qui n'est pas à Reason : ignoré
        user = cq.get("from") or {}
        by = user.get("username") or str(user.get("id") or "")
        try:
            out = self.handle_callback(cq["data"], by)
            note, text = ("Enregistré" if out["recorded"] else "Déjà répondu"), out["text"]
        except (ValueError, KeyError) as e:
            note, text = str(e)[:180], None
        try:
            self.call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": note[:180]})
            if text:
                self.call("editMessageText", {"chat_id": self.chat_id, "message_id": msg.get("message_id"), "parse_mode": "HTML",
                                              "text": (html.escape(msg.get("text") or "")[:MAX_TEXT - 300] + "\n\n" + text)})
        except TelegramError as e:
            print(f"reason: telegram {e}", file=sys.stderr)

    def poll_forever(self, stop: threading.Event | None = None) -> None:
        """Long polling de getUpdates (bot dédié uniquement). L'offset n'est pas persisté : au redémarrage, un clic
        redélivré retombe sur une réponse déjà enregistrée (doublon sans effet)."""
        offset = None
        while not (stop and stop.is_set()):
            try:
                ups = self.call("getUpdates", {"timeout": 50, "allowed_updates": ["callback_query"],
                                               **({"offset": offset} if offset else {})}, timeout=60)
            except TelegramError as e:
                print(f"reason: telegram {e}", file=sys.stderr)
                time.sleep(5)
                continue
            for u in ups or []:
                offset = u["update_id"] + 1
                self.handle_update(u)
