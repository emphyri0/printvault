"""MQTT-listener voor Bambu Lab-printers (alleen lezen).

Verbindt per printer (met IP, serienummer en access code uit de printerinstellingen)
met mqtts://<ip>:8883, abonneert op device/<serienummer>/report en publiceert nooit iets.
Volgt print.gcode_state en houdt de prints in de database bij. De live status staat in
het geheugen en is als JSON beschikbaar op http://<LIVE_HOST>:<LIVE_PORT>/live.

Draaien:  python mqtt_listener.py
"""
import json
import re
import logging
import os
import sqlite3
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DB_PATH = os.path.join(DATA_DIR, "printvault.db")
TZ = ZoneInfo(os.environ.get("PRINTVAULT_TZ", "Europe/Brussels"))
LIVE_HOST = os.environ.get("LIVE_HOST", "127.0.0.1")
LIVE_PORT = int(os.environ.get("LIVE_PORT", "8101"))
RELOAD_SECONDS = int(os.environ.get("MQTT_RELOAD_SECONDS", "60"))

ACTIVE = {"PREPARE", "RUNNING", "PAUSE"}
DONE = {"FINISH": "gelukt", "FAILED": "mislukt"}
CANCELLED = 0x0300400C  # print_error als je de print zelf stopt

log = logging.getLogger("printvault.mqtt")


def now_local():
    return datetime.now(TZ).replace(microsecond=0)


def deep_merge(dst, src):
    """Voegt een (eventueel gedeeltelijk) statusbericht samen met de bekende status."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def error_code(err):
    """50348044 → '0300-400C' (notatie zoals in Bambu Studio en de Bambu-wiki)."""
    try:
        err = int(err)
    except (TypeError, ValueError):
        return None
    return f"{err >> 16:04X}-{err & 0xFFFF:04X}" if err else None


def ams_slots(state):
    slots = []
    for unit in (state.get("ams") or {}).get("ams") or []:
        for tray in unit.get("tray") or []:
            if tray.get("tray_type"):
                color = str(tray.get("tray_color") or "")
                slots.append((str(unit.get("id", "")), str(tray.get("id", "")), str(tray["tray_type"])[:40],
                              color.upper() if re.fullmatch(r"[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", color) else ""))
    return slots


@contextmanager
def connect_db(db_path):
    """Verbinding die bij succes commit en altijd gesloten wordt."""
    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    finally:
        db.close()


class PrinterTracker:
    """Volgt de status van één printer. Losstaand van MQTT zodat het testbaar is."""

    def __init__(self, printer_id, printer_name, db_path, clock=now_local):
        self.printer_id = printer_id
        self.printer_name = printer_name
        self.db_path = db_path
        self.clock = clock
        self.state = {}
        self.gcode_state = None   # laatst geziene gcode_state (None = nog niets ontvangen)
        self.print_id = None      # het 'bezig'-record dat we volgen
        self.task_id = None
        self.connected = False
        self.last_message = None
        self.lock = threading.Lock()

    # ---------- berichten ----------

    def handle(self, payload):
        report = payload.get("print")
        if not isinstance(report, dict):
            return
        with self.lock:
            deep_merge(self.state, report)
            self.last_message = self.clock()
            new = self.state.get("gcode_state")
            task = str(self.state.get("task_id") or "") or None
            if new != self.gcode_state:
                prev, self.gcode_state = self.gcode_state, new
                log.info("printer %s: %s -> %s", self.printer_id, prev, new)
                self._transition(prev, new, task)
            elif new in ACTIVE and self.print_id and task and self.task_id and task != self.task_id:
                # Nieuwe opdracht zonder dat we het einde van de vorige zagen (bv. tijdens een verbindingsonderbreking).
                self._close_stale(self.print_id)
                self.print_id = None
                self._start(task, first_seen=False)

    def _transition(self, prev, new, task):
        if new in ACTIVE:
            if self.print_id is None:
                self._start(task, first_seen=prev is None)
        elif new in DONE:
            target = self.print_id
            if target is None and prev is None:
                # Listener start terwijl de print al klaar is: open record van deze opdracht afsluiten.
                target = self._find_running(task)
            if target is not None:
                self._finish(target, DONE[new], seen_live=prev is not None)
            self.print_id = None
            self.task_id = None

    # ---------- database ----------

    def _find_running(self, task):
        """Zoekt het 'bezig'-record van deze printer. Een record van een andere opdracht wordt afgesloten."""
        with connect_db(self.db_path) as db:
            rows = db.execute("SELECT id, task_id FROM prints WHERE printer_id = ? AND status = 'bezig' "
                              "ORDER BY id DESC", (self.printer_id,)).fetchall()
        match = None
        for r in rows:
            if match is None and (not task or not r["task_id"] or r["task_id"] == task):
                match = r["id"]
            else:
                self._close_stale(r["id"])
        return match

    def _start(self, task, first_seen):
        existing = self._find_running(task)
        now = self.clock()
        name = (self.state.get("subtask_name") or "").strip()[:200] or "Naamloze print"
        with connect_db(self.db_path) as db:
            if existing is not None:
                self.print_id = existing
                db.execute("UPDATE prints SET task_id = COALESCE(task_id, ?) WHERE id = ?", (task, existing))
                log.info("printer %s: hervat print %s", self.printer_id, existing)
            else:
                notes = ("Start niet live gezien: listener startte tijdens de print, starttijd en duur zijn bij benadering."
                         if first_seen and self.state.get("gcode_state") != "PREPARE" else "")
                cur = db.execute(
                    "INSERT INTO prints (name, status, printer_id, printed_on, started_at, task_id, source, notes) "
                    "VALUES (?, 'bezig', ?, ?, ?, ?, 'mqtt', ?)",
                    (name, self.printer_id, now.date().isoformat(), now.isoformat(), task, notes))
                self.print_id = cur.lastrowid
                log.info("printer %s: nieuwe print %s (%s)", self.printer_id, self.print_id, name)
            self._save_ams(db, self.print_id)
        self.task_id = task

    def _finish(self, print_id, status, seen_live=True):
        now = self.clock()
        err = self.state.get("print_error") if status == "mislukt" else None
        with connect_db(self.db_path) as db:
            row = db.execute("SELECT started_at, notes FROM prints WHERE id = ?", (print_id,)).fetchone()
            if row is None:
                return
            minutes = None
            if row["started_at"]:
                try:
                    start = datetime.fromisoformat(row["started_at"])
                    minutes = max(0, round((now - start).total_seconds() / 60))
                except ValueError:
                    pass
            notes = row["notes"] or ""
            if not seen_live:
                extra = "Einde niet live gezien: eindtijd is het moment waarop de listener startte."
                notes = f"{notes}\n{extra}".strip()
            fail_note = None
            if status == "mislukt":
                code = error_code(err)
                if err and int(err) == CANCELLED:
                    fail_note = "Geannuleerd op de printer."
                elif code:
                    fail_note = f"Printerfout {code}"
            db.execute(
                "UPDATE prints SET status = ?, finished_at = ?, print_time_min = ?, print_error = ?, "
                "fail_note = COALESCE(?, fail_note), notes = ? WHERE id = ?",
                (status, now.isoformat(), minutes, int(err) if err else None, fail_note, notes, print_id))
            self._save_ams(db, print_id)
        log.info("printer %s: print %s %s", self.printer_id, print_id, status)

    def _close_stale(self, print_id):
        note = "Einde niet waargenomen (listener was niet verbonden). Controleer de status zelf."
        with connect_db(self.db_path) as db:
            db.execute("UPDATE prints SET status = 'opnieuw', "
                       "notes = TRIM(COALESCE(notes, '') || char(10) || ?) WHERE id = ? AND status = 'bezig'",
                       (note, print_id))
        log.warning("printer %s: print %s had geen einde, gemarkeerd als 'opnieuw'", self.printer_id, print_id)

    def _save_ams(self, db, print_id):
        slots = ams_slots(self.state)
        if not slots:
            return
        db.execute("DELETE FROM print_ams WHERE print_id = ?", (print_id,))
        db.executemany("INSERT INTO print_ams (print_id, ams_id, tray_id, tray_type, tray_color) VALUES (?,?,?,?,?)",
                       [(print_id, *s) for s in slots])

    # ---------- live status ----------

    def live(self):
        with self.lock:
            s = self.state
            def as_int(v):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return None
            return {
                "printer_id": self.printer_id,
                "printer": self.printer_name,
                "connected": self.connected,
                "gcode_state": self.gcode_state,
                "name": s.get("subtask_name") or None,
                "percent": as_int(s.get("mc_percent")),
                "remaining_min": as_int(s.get("mc_remaining_time")),
                "layer": as_int(s.get("layer_num")),
                "total_layers": as_int(s.get("total_layer_num")),
                "print_id": self.print_id,
                "error": error_code(s.get("print_error")),
                "updated_at": self.last_message.isoformat() if self.last_message else None,
            }


class PrinterConnection:
    """Eén MQTT-verbinding (alleen subscribe) met automatisch herverbinden door paho."""

    def __init__(self, cfg, tracker):
        import paho.mqtt.client as mqtt
        self.cfg = cfg
        self.tracker = tracker
        self.topic = f"device/{cfg['serial']}/report"
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"printvault-{cfg['id']}-{os.getpid()}")
        c.username_pw_set("bblp", cfg["access_code"])
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # de printer gebruikt een zelfondertekend certificaat
        c.tls_set_context(ctx)
        c.reconnect_delay_set(min_delay=2, max_delay=60)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self.client = c

    def start(self):
        log.info("printer %s: verbinden met %s:8883", self.cfg["id"], self.cfg["ip"])
        self.client.connect_async(self.cfg["ip"], 8883, keepalive=60)
        self.client.loop_start()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()
        self.tracker.connected = False

    def _on_connect(self, client, _u, _flags, reason, _props=None):
        if reason.is_failure:
            log.warning("printer %s: verbinding geweigerd (%s)", self.cfg["id"], reason)
            return
        log.info("printer %s: verbonden", self.cfg["id"])
        self.tracker.connected = True
        client.subscribe(self.topic, qos=0)

    def _on_disconnect(self, _c, _u, _flags, reason, _props=None):
        self.tracker.connected = False
        log.warning("printer %s: verbinding verbroken (%s), opnieuw proberen…", self.cfg["id"], reason)

    def _on_message(self, _c, _u, msg):
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return
        try:
            self.tracker.handle(payload)
        except Exception:
            log.exception("printer %s: fout bij verwerken bericht", self.cfg["id"])


class Supervisor:
    """Leest periodiek de printerinstellingen en start/stopt verbindingen."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.connections = {}   # printer_id -> (config-sleutel, PrinterConnection)
        self.trackers = {}      # printer_id -> PrinterTracker

    def configs(self):
        with connect_db(self.db_path) as db:
            rows = db.execute("SELECT id, name, ip, serial, access_code FROM printers WHERE mqtt_enabled = 1 "
                              "AND COALESCE(ip, '') != '' AND COALESCE(serial, '') != '' "
                              "AND COALESCE(access_code, '') != ''").fetchall()
        return {r["id"]: dict(r) for r in rows}

    def sync(self):
        wanted = self.configs()
        for pid in list(self.connections):
            key, conn = self.connections[pid]
            cfg = wanted.get(pid)
            if cfg is None or (cfg["ip"], cfg["serial"], cfg["access_code"]) != key:
                conn.stop()
                del self.connections[pid]
                if cfg is None:
                    self.trackers.pop(pid, None)
        for pid, cfg in wanted.items():
            tracker = self.trackers.get(pid)
            if tracker is None:
                tracker = self.trackers[pid] = PrinterTracker(pid, cfg["name"], self.db_path)
            tracker.printer_name = cfg["name"]
            if pid not in self.connections:
                conn = PrinterConnection(cfg, tracker)
                self.connections[pid] = ((cfg["ip"], cfg["serial"], cfg["access_code"]), conn)
                conn.start()

    def live(self):
        return [t.live() for t in list(self.trackers.values())]

    def run(self):
        while True:
            try:
                self.sync()
            except Exception:
                log.exception("printerinstellingen lezen mislukt")
            time.sleep(RELOAD_SECONDS)


def serve_live(supervisor, host=LIVE_HOST, port=LIVE_PORT):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/live":
                self.send_error(404)
                return
            body = json.dumps({"printers": supervisor.live()}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="live-http").start()
    log.info("live status op http://%s:%d/live", host, port)
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from app import init_db   # zorgt dat de tabellen/kolommen bestaan
    init_db()
    sup = Supervisor()
    serve_live(sup)
    sup.run()


if __name__ == "__main__":
    main()
