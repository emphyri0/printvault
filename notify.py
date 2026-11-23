"""Notifications via ntfy (https://ntfy.sh or a self-hosted ntfy server).

The settings live in the `settings` table and can be changed in the app (Settings).
Messages are published as JSON to the server's base URL, with the topic in the body.
"""
import json
import logging
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing

log = logging.getLogger("printvault.notify")

# kind of notification -> setting that turns it on/off
KINDS = {"finish": "notify_finish", "failed": "notify_failed", "maintenance": "notify_maintenance"}
SETTING_KEYS = ("ntfy_url", "ntfy_topic", "ntfy_token", *KINDS.values())
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
URL_RE = re.compile(r"^https?://[^\s/?#]+(/[^\s?#]*)?$", re.I)


class NotifyError(Exception):
    pass


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """urllib would send the Authorization header along to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirects)


def server_of(url):
    """(scheme, host:port) of a URL, to see whether two URLs are the same server."""
    parts = urllib.parse.urlsplit((url or "").lower())
    return parts.scheme, parts.netloc


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
    """Sends one notification. Raises NotifyError on failure."""
    if not configured(cfg):
        raise NotifyError("ntfy is not set up (a URL and a topic are required).")
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
        with (urlopen or _opener.open)(req, timeout=timeout) as resp:
            resp.read(4096)
    except urllib.error.HTTPError as e:
        raise NotifyError(f"Sending the ntfy notification failed: HTTP {e.code}{_reason(e)}") from None
    except Exception as e:   # URLError, timeout, …
        raise NotifyError(f"Sending the ntfy notification failed: {e}") from None


def _reason(err):
    """The error reason from the server's response. ntfy answers with JSON such as
    {"code": 40009, "http": 400, "error": "…"}. Anything else is not shown: it is not from ntfy."""
    if 300 <= err.code < 400:
        return ": the server answers with a redirect; enter the final address"
    try:
        body = err.read(2048).decode("utf-8", "replace").strip()
    except Exception:
        return f" {err.reason}" if getattr(err, "reason", None) else ""
    try:
        data = json.loads(body)
        if isinstance(data, dict) and data.get("error"):
            code = f" (ntfy code {data['code']})" if data.get("code") else ""
            return f": {str(data['error'])[:300]}{code}"
    except ValueError:
        pass
    return f" {err.reason}" + (" — is this really an ntfy server?" if body else "")


class Notifier:
    """Reads the current settings for every notification and sends it (in the background by default),
    so that a slow or unreachable ntfy server does not hold up the listener."""

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
            log.info("ntfy notification sent: %s", title)
        except NotifyError as e:
            log.warning("%s", e)
