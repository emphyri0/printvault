"""MQTT listener for Bambu Lab printers (read-only).

Connects per printer (with the IP, serial number and access code from the printer settings)
to mqtts://<ip>:8883, subscribes to device/<serial>/report and never publishes anything.
Follows print.gcode_state and keeps the prints in the database up to date. The live status is
kept in memory and served as JSON at http://<LIVE_HOST>:<LIVE_PORT>/live.

Run:  python mqtt_listener.py
"""
import json
import re
import logging
import os
import sqlite3
import ssl
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import bambu_errors
import bambu_status
import camera
import maintenance
import notify
from bambu_errors import CANCELLED, error_code  # noqa: F401  (error_code is also used by app)

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DB_PATH = os.path.join(DATA_DIR, "printvault.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
TZ = ZoneInfo(os.environ.get("PRINTVAULT_TZ", "Europe/Brussels"))
LIVE_HOST = os.environ.get("LIVE_HOST", "127.0.0.1")
LIVE_PORT = int(os.environ.get("LIVE_PORT", "8101"))
RELOAD_SECONDS = int(os.environ.get("MQTT_RELOAD_SECONDS", "60"))

AMS_INTERVAL = timedelta(minutes=10)   # at most one stored AMS reading per unit per 10 minutes
SAMPLE_INTERVAL = timedelta(minutes=1) # time series per print: at most one point per minute (plus one at every stage change)
ACTIVE = {"PREPARE", "RUNNING", "PAUSE"}
DONE = {"FINISH": "succeeded", "FAILED": "failed"}

log = logging.getLogger("printvault.mqtt")


def now_local():
    return datetime.now(TZ).replace(microsecond=0)


def deep_merge(dst, src):
    """Merges a (possibly partial) status message into the known status."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def ams_slots(state):
    slots = []
    for unit in (state.get("ams") or {}).get("ams") or []:
        for tray in unit.get("tray") or []:
            if tray.get("tray_type"):
                color = str(tray.get("tray_color") or "")
                slots.append((str(unit.get("id", "")), str(tray.get("id", "")), str(tray["tray_type"])[:40],
                              color.upper() if re.fullmatch(r"[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", color) else ""))
    return slots


def _num(value, cast=float):
    """'24.6' → 24.6; empty, invalid or exactly 0 (sensor missing or no reading yet) → None."""
    try:
        v = cast(float(value))
    except (TypeError, ValueError):
        return None
    return v or None


def ams_units(state):
    """Humidity and temperature per AMS unit. Only newer firmware sends humidity_pct (humidity_raw);
    humidity_level is Bambu's level 1–5."""
    units = []
    for unit in (state.get("ams") or {}).get("ams") or []:
        if not isinstance(unit, dict) or unit.get("id") is None:
            continue
        u = {"id": str(unit["id"]), "humidity_pct": _num(unit.get("humidity_raw")),
             "humidity_level": _num(unit.get("humidity"), int), "temp": _num(unit.get("temp"))}
        if any(u[k] is not None for k in ("humidity_pct", "humidity_level", "temp")):
            units.append(u)
    return units


@contextmanager
def connect_db(db_path):
    """Connection that commits on success and is always closed."""
    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    finally:
        db.close()


class PrinterTracker:
    """Follows the status of one printer. Independent of MQTT so that it can be tested."""

    def __init__(self, printer_id, printer_name, db_path, clock=now_local, notifier=None):
        self.printer_id = printer_id
        self.notifier = notifier  # callable(kind, title, message, tags, priority) or None
        self.snapshotter = None   # callable(print_id, status): camera image at the end, or None
        self.printer_name = printer_name
        self.db_path = db_path
        self.clock = clock
        self.state = {}
        self.gcode_state = None   # last seen gcode_state (None = nothing received yet)
        self.print_id = None      # the 'printing' record we are following
        self.task_id = None
        self.connected = False
        self.last_message = None
        self.ams_saved = {}       # ams_id -> time of the last stored reading
        self.settings_saved = (None, frozenset())   # (print_id, fields already written to the print record)
        self.last_sample = (None, None, None)       # (print_id, time, stage) of the last data point
        self.lock = threading.Lock()

    # ---------- messages ----------

    def handle(self, payload):
        report = payload.get("print")
        if not isinstance(report, dict):
            return
        with self.lock:
            deep_merge(self.state, report)
            self.last_message = self.clock()
            self._record_ams()
            new = self.state.get("gcode_state")
            task = str(self.state.get("task_id") or "") or None
            if new != self.gcode_state:
                prev, self.gcode_state = self.gcode_state, new
                log.info("printer %s: %s -> %s", self.printer_id, prev, new)
                self._transition(prev, new, task)
            elif new in ACTIVE and self.print_id and task and self.task_id and task != self.task_id:
                # New job without having seen the previous one end (e.g. during a connection outage).
                self._close_stale(self.print_id)
                self.print_id = None
                self._start(task, first_seen=False)
            if self.print_id and self.gcode_state in ACTIVE:
                self._save_settings(self.print_id)
                self._record_sample(self.print_id)

    def _transition(self, prev, new, task):
        if new in ACTIVE:
            if self.print_id is None:
                self._start(task, first_seen=prev is None)
        elif new in DONE:
            target = self.print_id
            if target is None and prev is None:
                # Listener starts while the print has already ended: close this job's open record.
                target = self._find_running(task)
            if target is not None:
                if target == self.print_id:
                    self._record_sample(target, force=True)   # last point: the end
                self._finish(target, DONE[new], seen_live=prev is not None)
            self.print_id = None
            self.task_id = None

    # ---------- database ----------

    def _find_running(self, task):
        """Finds this printer's 'printing' record. A record of another job is closed."""
        with connect_db(self.db_path) as db:
            rows = db.execute("SELECT id, task_id FROM prints WHERE printer_id = ? AND status = 'printing' "
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
        name = (self.state.get("subtask_name") or "").strip()[:200] or "Untitled print"
        with connect_db(self.db_path) as db:
            if existing is not None:
                self.print_id = existing
                db.execute("UPDATE prints SET task_id = COALESCE(task_id, ?) WHERE id = ?", (task, existing))
                log.info("printer %s: resuming print %s", self.printer_id, existing)
            else:
                notes = ("Start not seen live: the listener started during the print, start time and duration are approximate."
                         if first_seen and self.state.get("gcode_state") != "PREPARE" else "")
                cur = db.execute(
                    "INSERT INTO prints (name, status, printer_id, printed_on, started_at, task_id, source, notes) "
                    "VALUES (?, 'printing', ?, ?, ?, ?, 'mqtt', ?)",
                    (name, self.printer_id, now.date().isoformat(), now.isoformat(), task, notes))
                self.print_id = cur.lastrowid
                log.info("printer %s: new print %s (%s)", self.printer_id, self.print_id, name)
            self._save_ams(db, self.print_id)
        self.task_id = task

    def _finish(self, print_id, status, seen_live=True):
        now = self.clock()
        err = self.state.get("print_error") if status == "failed" else None
        with connect_db(self.db_path) as db:
            row = db.execute("SELECT name, started_at, notes FROM prints WHERE id = ?", (print_id,)).fetchone()
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
                extra = "End not seen live: the end time is when the listener started."
                notes = f"{notes}\n{extra}".strip()
            fail_note = fail_reason = None
            if status == "failed":
                fail_note, fail_reason = self._fail_info(err)
            db.execute(
                "UPDATE prints SET status = ?, finished_at = ?, print_time_min = ?, print_error = ?, "
                "fail_note = COALESCE(NULLIF(fail_note, ''), ?), fail_reason = COALESCE(NULLIF(fail_reason, ''), ?), "
                "notes = ? WHERE id = ?",
                (status, now.isoformat(), minutes, int(err) if err else None, fail_note, fail_reason, notes, print_id))
            self._save_ams(db, print_id)
        log.info("printer %s: print %s %s", self.printer_id, print_id, status)
        if seen_live:   # no notification or photo for an end only noticed after a restart
            self._notify(row["name"], status, minutes, fail_note)
            self._snapshot(print_id, status)

    def _snapshot(self, print_id, status):
        if self.snapshotter is None:
            return
        try:
            self.snapshotter(print_id, status)
        except Exception:
            log.exception("printer %s: starting the camera snapshot failed", self.printer_id)

    def _notify(self, name, status, minutes, fail_note):
        if self.notifier is None:
            return
        try:
            if status == "succeeded":
                took = f" · {minutes // 60}h {minutes % 60:02d}m" if minutes is not None else ""
                self.notifier("finish", f"Print finished: {name}", f"{self.printer_name}{took}",
                              tags=["white_check_mark"])
            else:
                self.notifier("failed", f"Print failed: {name}",
                              f"{self.printer_name}\n{fail_note or 'No error code from the printer.'}",
                              tags=["x"], priority=4)
        except Exception:
            log.exception("printer %s: sending the notification failed", self.printer_id)

    def _fail_info(self, err):
        """(explanation, suggested fail reason) for a failed print, from print_error and active HMS."""
        hms = bambu_errors.describe_hms_list(self.state.get("hms"))
        lines = []
        d = bambu_errors.describe_print_error(err)
        if d and int(err) == CANCELLED:
            lines.append("Cancelled on the printer.")
        elif d:
            lines.append(f"Printer error {d['code']}: {d['text']}" if d["text"] else f"Printer error {d['code']}")
        lines += [f"HMS {bambu_errors.label(h)}" for h in hms]
        return ("\n".join(lines) or None), bambu_errors.suggest_fail_reason(err, hms)

    # Fields written to the print record at the start, but only while they are still empty.
    # If they only arrive later (e.g. the full report after a restart), they are filled in then.
    SETTINGS_COLUMNS = ("nozzle", "nozzle_type", "plate_type", "plate", "plate_count", "speed_profile",
                        "spaghetti_detection", "first_layer_inspection")

    def _save_settings(self, print_id):
        info = {k: v for k, v in bambu_status.print_settings(self.state).items() if k in self.SETTINGS_COLUMNS}
        done = self.settings_saved[1] if self.settings_saved[0] == print_id else frozenset()
        new = {k: v for k, v in info.items() if k not in done}
        if not new:
            return
        with connect_db(self.db_path) as db:
            db.execute(f"UPDATE prints SET {', '.join(f'{k} = COALESCE(NULLIF({k}, ?), ?)' for k in new)} WHERE id = ?",
                       [x for v in new.values() for x in ("", v)] + [print_id])
        self.settings_saved = (print_id, done | frozenset(new))

    def _record_sample(self, print_id, force=False):
        now = self.clock()
        point = bambu_status.sample(self.state)
        last_id, last_at, last_stage = self.last_sample
        if last_id != print_id:   # (re)start: this print's last point from the database
            with connect_db(self.db_path) as db:
                row = db.execute("SELECT recorded_at, stage FROM print_samples WHERE print_id = ? "
                                 "ORDER BY recorded_at DESC LIMIT 1", (print_id,)).fetchone()
            last_at, last_stage = (datetime.fromisoformat(row[0]), row[1]) if row else (None, None)
        if not force and last_at is not None and now - last_at < SAMPLE_INTERVAL and point["stage"] == last_stage:
            self.last_sample = (print_id, last_at, last_stage)
            return
        if all(point[k] is None for k in ("nozzle", "bed", "chamber", "stage")):
            return
        with connect_db(self.db_path) as db:
            db.execute(f"INSERT INTO print_samples (print_id, recorded_at, {', '.join(point)}) "
                       f"VALUES (?, ?, {', '.join('?' * len(point))})", (print_id, now.isoformat(), *point.values()))
        self.last_sample = (print_id, now, point["stage"])

    def _close_stale(self, print_id):
        note = "End not observed (listener was not connected). Check the status yourself."
        with connect_db(self.db_path) as db:
            db.execute("UPDATE prints SET status = 'unknown', "
                       "notes = TRIM(COALESCE(notes, '') || char(10) || ?) WHERE id = ? AND status = 'printing'",
                       (note, print_id))
        log.warning("printer %s: print %s had no end, marked as 'unknown'", self.printer_id, print_id)

    def _record_ams(self):
        units = ams_units(self.state)
        now = self.clock()
        unknown = [u["id"] for u in units if u["id"] not in self.ams_saved]
        if unknown:   # first message after a (re)start: last reading per unit from the database
            with connect_db(self.db_path) as db:
                for ams_id in unknown:
                    row = db.execute("SELECT MAX(recorded_at) FROM ams_readings WHERE printer_id = ? AND ams_id = ?",
                                     (self.printer_id, ams_id)).fetchone()
                    self.ams_saved[ams_id] = datetime.fromisoformat(row[0]) if row[0] else None
        due = [u for u in units if self.ams_saved[u["id"]] is None or now - self.ams_saved[u["id"]] >= AMS_INTERVAL]
        if not due:
            return
        with connect_db(self.db_path) as db:
            db.executemany("INSERT INTO ams_readings (printer_id, ams_id, humidity_pct, humidity_level, temp, recorded_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           [(self.printer_id, u["id"], u["humidity_pct"], u["humidity_level"], u["temp"], now.isoformat())
                            for u in due])
        for u in due:
            self.ams_saved[u["id"]] = now

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
            phase_code, phase_text = bambu_status.phase(s)
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
                "error_text": (bambu_errors.describe_print_error(s.get("print_error")) or {}).get("text"),
                "hms": bambu_errors.describe_hms_list(s.get("hms")),
                "ams": ams_units(s),
                "updated_at": self.last_message.isoformat() if self.last_message else None,
                "phase_code": phase_code,
                "phase": phase_text,
                "temps": {k: {"cur": v[0], "target": v[1]} for k, v in bambu_status.temperatures(s).items()},
                "fans": bambu_status.fans(s),
                "speed": bambu_status.speed(s),
                "tray": bambu_status.active_tray(s),
                "plate_idx": as_int(s.get("plate_idx")),
                "plate_cnt": as_int(s.get("plate_cnt")),
            }


def save_snapshot(db_path, upload_dir, print_id, jpeg, status, when):
    """Attaches a camera image to the print as a photo. The cover follows COVER_SQL in app.py: a chosen
    cover or an earlier real photo stays, but the camera image beats a slicer/MakerWorld thumbnail."""
    stored = f"{uuid.uuid4().hex}.jpg"
    name = f"camera-{status}-{when:%Y%m%d-%H%M}.jpg"
    path = os.path.join(upload_dir, stored)
    os.makedirs(upload_dir, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(jpeg)
    try:
        with connect_db(db_path) as db:
            if db.execute("SELECT 1 FROM prints WHERE id = ?", (print_id,)).fetchone() is None:
                raise LookupError(f"print {print_id} no longer exists")
            file_id = db.execute(
                "INSERT INTO files (print_id, kind, stored_name, original_name, size) VALUES (?, 'photo', ?, ?, ?)",
                (print_id, stored, name, len(jpeg))).lastrowid
    except BaseException:
        os.remove(path)
        raise
    return file_id


class PrinterConnection:
    """One MQTT connection (subscribe only) with automatic reconnects by paho."""

    def __init__(self, cfg, tracker):
        import paho.mqtt.client as mqtt
        self.cfg = cfg
        self.tracker = tracker
        self.topic = f"device/{cfg['serial']}/report"
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"printvault-{cfg['id']}-{os.getpid()}")
        c.username_pw_set("bblp", cfg["access_code"])
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # the printer uses a self-signed certificate
        c.tls_set_context(ctx)
        c.reconnect_delay_set(min_delay=2, max_delay=60)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self.client = c

    def start(self):
        log.info("printer %s: connecting to %s:8883", self.cfg["id"], self.cfg["ip"])
        self.client.connect_async(self.cfg["ip"], 8883, keepalive=60)
        self.client.loop_start()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()
        self.tracker.connected = False

    def _on_connect(self, client, _u, _flags, reason, _props=None):
        if reason.is_failure:
            log.warning("printer %s: connection refused (%s)", self.cfg["id"], reason)
            return
        log.info("printer %s: connected", self.cfg["id"])
        self.tracker.connected = True
        client.subscribe(self.topic, qos=0)

    def _on_disconnect(self, _c, _u, _flags, reason, _props=None):
        self.tracker.connected = False
        log.warning("printer %s: connection lost (%s), retrying…", self.cfg["id"], reason)

    def _on_message(self, _c, _u, msg):
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            return
        try:
            self.tracker.handle(payload)
        except Exception:
            log.exception("printer %s: error while processing a message", self.cfg["id"])


class Supervisor:
    """Periodically reads the printer settings and starts/stops connections."""

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.notifier = notify.Notifier(db_path)
        self.connections = {}   # printer_id -> (config key, PrinterConnection)
        self.cfgs = {}          # printer_id -> last read settings
        self.trackers = {}      # printer_id -> PrinterTracker

    def configs(self):
        with connect_db(self.db_path) as db:
            rows = db.execute("SELECT id, name, ip, serial, access_code FROM printers WHERE mqtt_enabled = 1 "
                              "AND COALESCE(ip, '') != '' AND COALESCE(serial, '') != '' "
                              "AND COALESCE(access_code, '') != ''").fetchall()
        return {r["id"]: dict(r) for r in rows}

    def sync(self):
        wanted = self.configs()
        self.cfgs = wanted
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
                tracker = self.trackers[pid] = PrinterTracker(pid, cfg["name"], self.db_path, notifier=self.notifier)
                tracker.snapshotter = lambda print_id, status, pid=pid: self.take_snapshot(pid, print_id, status)
            tracker.printer_name = cfg["name"]
            if pid not in self.connections:
                conn = PrinterConnection(cfg, tracker)
                self.connections[pid] = ((cfg["ip"], cfg["serial"], cfg["access_code"]), conn)
                conn.start()

    def live(self):
        return [t.live() for t in list(self.trackers.values())]

    def take_snapshot(self, printer_id, print_id, status):
        """Camera image in the background, so MQTT processing does not wait."""
        cfg = self.cfgs.get(printer_id)
        if cfg is None:
            return
        threading.Thread(target=self._snapshot_worker, args=(cfg, print_id, status),
                         daemon=True, name=f"snapshot-{print_id}").start()

    def _snapshot_worker(self, cfg, print_id, status):
        try:
            jpeg = camera.grab_snapshot(cfg["ip"], cfg["access_code"])
            file_id = save_snapshot(self.db_path, UPLOAD_DIR, print_id, jpeg, status, now_local())
            log.info("printer %s: camera image for print %s saved (file %s)", cfg["id"], print_id, file_id)
        except Exception as e:
            log.warning("printer %s: camera image for print %s failed: %s",
                        cfg["id"], print_id, camera.redact(e, cfg["access_code"]))

    def run(self):
        while True:
            try:
                self.sync()
            except Exception:
                log.exception("reading the printer settings failed")
            try:
                maintenance.check_and_notify(self.db_path, self.notifier)
            except Exception:
                log.exception("checking maintenance failed")
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
    log.info("live status at http://%s:%d/live", host, port)
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from app import init_db   # makes sure the tables/columns exist
    init_db()
    sup = Supervisor()
    serve_live(sup)
    sup.run()


if __name__ == "__main__":
    main()
