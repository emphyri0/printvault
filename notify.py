"""Meldingen via ntfy (https://ntfy.sh of een eigen ntfy-server).

De instellingen staan in de tabel `settings` en zijn in de app aan te passen (Instellingen).
Er wordt gepubliceerd als JSON naar de basis-URL van de server, met het topic in de body.
"""
import json
import logging
import re
import sqlite3
import threading
import urllib.request
from contextlib import closing

log = logging.getLogger("printvault.notify")

# soort melding -> instelling die hem aan/uit zet
KINDS = {"finish": "notify_finish", "failed": "notify_failed", "maintenance": "notify_maintenance"}
SETTING_KEYS = ("ntfy_url", "ntfy_topic", "ntfy_token", *KINDS.values())
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
URL_RE = re.compile(r"^https?://[^\s/?#]+(/[^\s?#]*)?$", re.I)


class NotifyError(Exception):
    pass


def load_settings(db):
    return {row[0]: row[1] for row in db.execute("SELECT key, value FROM settings")}


def save_settings(db, values):
    db.executemany("INSERT INTO settings (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", list(values.items()))


def configured(cfg):
    return bool(cfg.get("ntfy_url") and cfg.get("ntfy_topic"))


def enabled(cfg, kind):
    return configured(cfg) and cfg.get(KINDS[kind]) == "1"


def post(cfg, title, message, tags=(), priority=None, urlopen=None, timeout=10):
    """Verstuurt één melding. Geeft een NotifyError bij een fout."""
    if not configured(cfg):
        raise NotifyError("ntfy is niet ingesteld (URL en topic zijn nodig).")
    body = {"topic": cfg["ntfy_topic"], "title": title[:250], "message": message[:4000]}
    if tags:
        body["tags"] = list(tags)
    if priority:
        body["priority"] = int(priority)
    req = urllib.request.Request(cfg["ntfy_url"].rstrip("/"), data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "printvault"})
    if cfg.get("ntfy_token"):
        req.add_header("Authorization", f"Bearer {cfg['ntfy_token']}")
    try:
        with (urlopen or urllib.request.urlopen)(req, timeout=timeout) as resp:
            resp.read(4096)
    except Exception as e:   # HTTPError, URLError, timeout, …
        raise NotifyError(f"ntfy-melding versturen mislukt: {e}") from None


class Notifier:
    """Leest bij elke melding de actuele instellingen en verstuurt (standaard op de achtergrond),
    zodat een trage of onbereikbare ntfy-server de listener niet ophoudt."""

    def __init__(self, db_path, urlopen=None, background=True):
        self.db_path = db_path
        self.urlopen = urlopen
        self.background = background

    def settings(self):
        with closing(sqlite3.connect(self.db_path, timeout=30)) as db:
            return load_settings(db)

    def __call__(self, kind, title, message, tags=(), priority=None):
        cfg = self.settings()
        if not enabled(cfg, kind):
            return False
        if self.background:
            threading.Thread(target=self._send, args=(cfg, title, message, tags, priority),
                             daemon=True, name="ntfy").start()
        else:
            self._send(cfg, title, message, tags, priority)
        return True

    def _send(self, cfg, title, message, tags, priority):
        try:
            post(cfg, title, message, tags, priority, urlopen=self.urlopen)
            log.info("ntfy-melding verstuurd: %s", title)
        except NotifyError as e:
            log.warning("%s", e)
