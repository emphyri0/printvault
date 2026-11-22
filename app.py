import html
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
import uuid
from contextlib import closing
from datetime import date, datetime

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

import bambu_errors
import bambu_status
import camera
import maintenance
import notify
import slicer
from bambu_errors import error_code

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "printvault.db")

PHOTO_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
MODEL_EXT = {"3mf", "stl", "step", "stp", "obj", "gcode"}
# Internal values are lowercase snake_case; what the UI shows comes from LABELS.
# "unknown": the listener did not see the print end. "reprint": deliberately to be printed again.
STATUSES = ["planned", "printing", "succeeded", "failed", "unknown", "reprint"]
FAIL_REASONS = ["adhesion", "warping", "spaghetti", "clog", "power_loss", "other"]
SPEED_PROFILES = list(bambu_status.SPEED_PROFILES)
MATERIALS = ["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "Other"]
LABELS = {
    "planned": "Planned", "printing": "Printing", "succeeded": "Succeeded", "failed": "Failed",
    "unknown": "Unknown", "reprint": "Reprint",
    "adhesion": "Adhesion", "warping": "Warping", "spaghetti": "Spaghetti", "clog": "Clog",
    "power_loss": "Power loss", "other": "Other",
    **bambu_status.SPEED_PROFILES,
}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.urandom(32)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "200")) * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS prints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    material TEXT DEFAULT '',
    color TEXT DEFAULT '',
    filament_g REAL,
    print_time_min INTEGER,
    layer_height REAL,
    nozzle REAL,
    status TEXT DEFAULT 'planned',
    tags TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    printed_on TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS printers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    model TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    photo TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS print_ams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    print_id INTEGER NOT NULL REFERENCES prints(id) ON DELETE CASCADE,
    ams_id TEXT,
    tray_id TEXT,
    tray_type TEXT,
    tray_color TEXT
);
CREATE TABLE IF NOT EXISTS ams_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
    ams_id TEXT NOT NULL,
    humidity_pct REAL,
    humidity_level INTEGER,
    temp REAL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ams_readings ON ams_readings(printer_id, ams_id, recorded_at);
CREATE TABLE IF NOT EXISTS print_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    print_id INTEGER NOT NULL REFERENCES prints(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    nozzle REAL,
    nozzle_target REAL,
    bed REAL,
    bed_target REAL,
    chamber REAL,
    stage INTEGER,
    percent INTEGER,
    layer INTEGER
);
CREATE INDEX IF NOT EXISTS idx_print_samples ON print_samples(print_id, recorded_at);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS maintenance_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    interval_hours REAL,
    interval_days INTEGER,
    last_done_at TEXT,
    notified_at TEXT,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    print_id INTEGER NOT NULL REFERENCES prints(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


MIGRATIONS = {
    "prints": [("printer_id", "INTEGER REFERENCES printers(id) ON DELETE SET NULL"),
               ("source_url", "TEXT"), ("designer", "TEXT"), ("license", "TEXT"),
               ("infill", "TEXT"), ("plate_type", "TEXT"), ("nozzle_temp", "INTEGER"),
               ("bed_temp", "INTEGER"), ("plate", "INTEGER"), ("cover_file_id", "INTEGER"),
               ("fail_reason", "TEXT"), ("fail_note", "TEXT"),
               ("started_at", "TEXT"), ("finished_at", "TEXT"), ("print_error", "INTEGER"),
               ("task_id", "TEXT"), ("source", "TEXT"),
               ("plate_count", "INTEGER"), ("nozzle_type", "TEXT"), ("speed_profile", "TEXT"),
               ("spaghetti_detection", "INTEGER"), ("first_layer_inspection", "INTEGER")],
    "printers": [("ip", "TEXT"), ("serial", "TEXT"), ("access_code", "TEXT"),
                 ("mqtt_enabled", "INTEGER DEFAULT 0")],
}


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as db, db:
        db.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            for col, typ in columns:
                if col not in cols:
                    try:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                    except sqlite3.OperationalError as e:
                        # web app and listener start at the same time; the other one may have just done it
                        if "duplicate column" not in str(e):
                            raise
        db.execute("CREATE INDEX IF NOT EXISTS idx_print_ams_print ON print_ams(print_id)")
        migrate_legacy(db)
        # Once: default maintenance tasks for printers that already exist (and have no tasks).
        if db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance_seeded', '1')").rowcount:
            for (pid,) in db.execute("SELECT id FROM printers").fetchall():
                if not db.execute("SELECT 1 FROM maintenance_tasks WHERE printer_id = ?", (pid,)).fetchone():
                    maintenance.seed_defaults(db, pid)


# ---------- Migration from the Dutch version (idempotent: only touches old Dutch values) ----------

LEGACY_STATUSES = {"gepland": "planned", "bezig": "printing", "gelukt": "succeeded", "mislukt": "failed",
                   "onbekend": "unknown", "opnieuw": "reprint"}
LEGACY_FAIL_REASONS = {"hechting": "adhesion", "warping": "warping", "spaghetti": "spaghetti",
                       "verstopping": "clog", "stroomuitval": "power_loss", "anders": "other"}
LEGACY_SPEED_PROFILES = {"Stil": "silent", "Standaard": "standard", "Sport": "sport", "Ludicrous": "ludicrous"}
LEGACY_NOZZLE_TYPES = [("Roestvrij staal", "Stainless steel"), ("Gehard staal", "Hardened steel"),
                       ("Wolfraamcarbide", "Tungsten carbide")]
# Sentences the listener used to write into the notes.
LEGACY_NOTES = {
    "Start niet live gezien: listener startte tijdens de print, starttijd en duur zijn bij benadering.":
        "Start not seen live: the listener started during the print, start time and duration are approximate.",
    "Einde niet live gezien: eindtijd is het moment waarop de listener startte.":
        "End not seen live: the end time is when the listener started.",
    "Einde niet waargenomen (listener was niet verbonden). Controleer de status zelf.":
        "End not observed (listener was not connected). Check the status yourself.",
}


def migrate_legacy(db):
    # Older: prints the listener set to 'opnieuw' because it missed the end become 'unknown'.
    db.execute("UPDATE prints SET status = 'onbekend' WHERE status = 'opnieuw' AND source = 'mqtt' "
               "AND notes LIKE '%Einde niet waargenomen%'")
    for old, new in LEGACY_STATUSES.items():
        db.execute("UPDATE prints SET status = ? WHERE status = ?", (new, old))
    for old, new in LEGACY_FAIL_REASONS.items():
        db.execute("UPDATE prints SET fail_reason = ? WHERE fail_reason = ?", (new, old))
    for old, new in LEGACY_SPEED_PROFILES.items():
        db.execute("UPDATE prints SET speed_profile = ? WHERE speed_profile = ?", (new, old))
    db.execute("UPDATE prints SET material = 'Other' WHERE material = 'Ander'")
    for old, new in LEGACY_NOZZLE_TYPES:   # e.g. 'Gehard staal, high-flow (HH01)' → 'Hardened steel, high-flow (HH01)'
        db.execute("UPDATE prints SET nozzle_type = ? || substr(nozzle_type, ?) WHERE nozzle_type LIKE ?",
                   (new, len(old) + 1, old + "%"))
    for old, new in LEGACY_NOTES.items():
        db.execute("UPDATE prints SET notes = replace(notes, ?, ?) WHERE instr(notes, ?) > 0", (old, new, old))
    for old, new in maintenance.LEGACY_DEFAULT_NAMES.items():   # only names that are still the Dutch default
        db.execute("UPDATE maintenance_tasks SET name = ? WHERE name = ?", (new, old))


def ext_of(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def num(value, cast=float):
    value = (value or "").strip().replace(",", ".")
    if not value:
        return None
    try:
        return cast(float(value))
    except ValueError:
        return None


def onoff(value):
    """'1' → 1, '0' → 0, empty → None (not filled in)."""
    return {"1": 1, "0": 0}.get(value)


def form_to_print(form):
    status = form.get("status", "planned")
    failed = status == "failed"
    reason = form.get("fail_reason", "")
    hours = num(form.get("hours"), int) or 0
    minutes = num(form.get("minutes"), int) or 0
    total = hours * 60 + minutes
    return {
        "name": form.get("name", "").strip(),
        "description": form.get("description", "").strip(),
        "material": form.get("material", "").strip(),
        "color": form.get("color", "").strip(),
        "filament_g": num(form.get("filament_g")),
        "print_time_min": total or None,
        "layer_height": num(form.get("layer_height")),
        "nozzle": num(form.get("nozzle")),
        "status": status if status in STATUSES else "planned",
        "tags": ", ".join(t.strip() for t in form.get("tags", "").split(",") if t.strip()),
        "notes": form.get("notes", "").strip(),
        "printed_on": form.get("printed_on") or None,
        "printer_id": num(form.get("printer_id"), int),
        "source_url": (lambda u: u if re.match(r"^https?://", u, re.I) else None)(form.get("source_url", "").strip()),
        "designer": form.get("designer", "").strip() or None,
        "license": form.get("license", "").strip() or None,
        "infill": form.get("infill", "").strip() or None,
        "plate_type": form.get("plate_type", "").strip() or None,
        "nozzle_temp": num(form.get("nozzle_temp"), int),
        "bed_temp": num(form.get("bed_temp"), int),
        "plate": num(form.get("plate"), int),
        "plate_count": num(form.get("plate_count"), int),
        "nozzle_type": form.get("nozzle_type", "").strip() or None,
        "speed_profile": form.get("speed_profile") if form.get("speed_profile") in SPEED_PROFILES else None,
        "spaghetti_detection": onoff(form.get("spaghetti_detection")),
        "first_layer_inspection": onoff(form.get("first_layer_inspection")),
        "fail_reason": reason if failed and reason in FAIL_REASONS else None,
        "fail_note": (form.get("fail_note", "").strip() or None) if failed else None,
    }


SLICER_FIELDS = {"material": "material", "color": "colour", "filament_g": "weight",
                 "print_time_min": "print time", "layer_height": "layer height", "nozzle": "nozzle",
                 "infill": "infill", "plate_type": "plate type", "nozzle_temp": "nozzle temperature",
                 "bed_temp": "bed temperature", "plate": "plate number"}


def store_bytes(print_id, kind, content, ext, original_name):
    stored = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(UPLOAD_DIR, stored), "wb") as fh:
        fh.write(content)
    cur = get_db().execute(
        "INSERT INTO files (print_id, kind, stored_name, original_name, size) VALUES (?,?,?,?,?)",
        (print_id, kind, stored, original_name, len(content)),
    )
    return cur.lastrowid


def apply_slicer_file(print_id, path, original_name, plate_index=None, overwrite_name=False):
    """Reads a 3mf/gcode file and updates the print. Returns a short summary."""
    parsed = slicer.parse(path, original_name)
    plates = parsed["plates"]
    if plate_index is None:
        plate_index = plates[0]["index"]
    fields, filaments, thumb = slicer.plate_to_print_fields(parsed, plate_index, MATERIALS)
    db = get_db()
    if overwrite_name:
        stem = re.sub(r"(\.gcode)?\.(3mf|gcode)$", "", original_name, flags=re.I)
        fields["name"] = (parsed.get("name") or stem.replace("_", " ")).strip()[:200] or "Untitled print"
    if filaments:
        p = db.execute("SELECT notes FROM prints WHERE id = ?", (print_id,)).fetchone()
        line = "Filament (slicer): " + slicer.filament_summary(filaments)
        notes = [l for l in (p["notes"] or "").splitlines() if not l.startswith("Filament (slicer):")]
        fields["notes"] = "\n".join([line] + notes).strip()
    if fields:
        db.execute(f"UPDATE prints SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
                   [*fields.values(), print_id])
    if thumb:
        content, ext = thumb
        thumb_name = f"thumbnail-plate-{plate_index}.{ext}"
        exists = db.execute("SELECT 1 FROM files WHERE print_id = ? AND original_name = ?",
                            (print_id, thumb_name)).fetchone()
        if not exists:
            store_bytes(print_id, "photo", content, ext, thumb_name)
    db.commit()
    found = [label for k, label in SLICER_FIELDS.items() if fields.get(k) not in (None, "")]
    plate_info = (f" This file has {len(plates)} plates; plate {plate_index} was used."
                  if len(plates) > 1 else "")
    return found, plate_info


def save_uploads(print_id, uploads):
    db = get_db()
    skipped = []
    for f in uploads:
        if not f or not f.filename:
            continue
        ext = ext_of(f.filename)
        if ext in PHOTO_EXT:
            kind = "photo"
        elif ext in MODEL_EXT:
            kind = "model"
        else:
            skipped.append(f.filename)
            continue
        stored = f"{uuid.uuid4().hex}.{ext}"
        path = os.path.join(UPLOAD_DIR, stored)
        f.save(path)
        db.execute(
            "INSERT INTO files (print_id, kind, stored_name, original_name, size) VALUES (?,?,?,?,?)",
            (print_id, kind, stored, secure_filename(f.filename) or stored, os.path.getsize(path)),
        )
    db.commit()
    if skipped:
        flash("Skipped (unknown file type): " + ", ".join(skipped), "warn")


# Automatic thumbnails (from the slicer or MakerWorld); everything else is a real photo.
# 'thumbnail-plaat-' is the name older versions used.
THUMB_SQL = ("(f.original_name LIKE 'thumbnail-plate-%' OR f.original_name LIKE 'thumbnail-plaat-%' "
             "OR f.original_name LIKE 'makerworld-cover.%')")
# Cover photo: the chosen photo, else the first real photo, else a slicer/MakerWorld thumbnail.
COVER_SQL = f"""COALESCE(
    (SELECT f.stored_name FROM files f WHERE f.id = p.cover_file_id AND f.print_id = p.id AND f.kind = 'photo'),
    (SELECT f.stored_name FROM files f WHERE f.print_id = p.id AND f.kind = 'photo'
       ORDER BY {THUMB_SQL}, f.id
       LIMIT 1))"""
# Own cover: a chosen photo or a real photo. A thumbnail may be replaced by the camera image.
OWN_COVER_SQL = f"""EXISTS (SELECT 1 FROM files f WHERE f.print_id = p.id AND f.kind = 'photo'
    AND (f.id = p.cover_file_id OR NOT {THUMB_SQL}))"""


def get_print(print_id):
    row = get_db().execute("SELECT * FROM prints WHERE id = ?", (print_id,)).fetchone()
    if row is None:
        abort(404)
    return row


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_day(d):
    """date/datetime → '22 Nov 2026' (fixed English month names, independent of the system locale)."""
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


@app.template_filter("label")
def label(value):
    """Internal value → display text, e.g. 'power_loss' → 'Power loss'."""
    if value in LABELS:
        return LABELS[value]
    return str(value).replace("_", " ").capitalize() if value else "–"


@app.template_filter("duration")
def duration(minutes):
    if not minutes:
        return "–"
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


@app.template_filter("errcode")
def errcode(err):
    return error_code(err) or "–"


@app.template_filter("errinfo")
def errinfo(err):
    """print_error → {code, text, reason, url} of None."""
    return bambu_errors.describe_print_error(err)


@app.template_filter("clock")
def clock(iso):
    """'2026-11-18T19:02:00+01:00' → '18 Nov 2026, 19:02' (24-hour clock)."""
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or "–"
    return f"{format_day(dt)}, {dt:%H:%M}"


@app.template_filter("day")
def day(iso):
    """'2026-11-18' → '18 Nov 2026'."""
    try:
        return format_day(date.fromisoformat(str(iso)[:10]))
    except (TypeError, ValueError):
        return iso or "–"


@app.template_filter("filesize")
def filesize(size):
    size = size or 0
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@app.context_processor
def inject_lists():
    printers = get_db().execute("SELECT id, name FROM printers ORDER BY name").fetchall()
    return {"all_printers": printers, "materials": MATERIALS, "statuses": STATUSES,
            "fail_reasons": FAIL_REASONS, "speed_profiles": SPEED_PROFILES, "labels": LABELS}


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    material = request.args.get("material", "")
    status = request.args.get("status", "")
    printer = num(request.args.get("printer"), int)
    fail = request.args.get("fail", "")
    nozzle = num(request.args.get("nozzle"))
    sql = f"""SELECT p.*, pr.name AS printer_name, {COVER_SQL} AS cover, {OWN_COVER_SQL} AS own_cover,
                    (COALESCE(pr.ip, '') != '' AND COALESCE(pr.access_code, '') != '') AS has_camera
             FROM prints p LEFT JOIN printers pr ON pr.id = p.printer_id WHERE 1=1"""
    args = []
    if printer:
        sql += " AND p.printer_id = ?"
        args.append(printer)
    if q:
        sql += " AND (p.name LIKE ? OR p.tags LIKE ? OR p.description LIKE ? OR p.notes LIKE ?)"
        args += [f"%{q}%"] * 4
    if material:
        sql += " AND p.material = ?"
        args.append(material)
    if status:
        sql += " AND p.status = ?"
        args.append(status)
    if fail in FAIL_REASONS:
        sql += " AND p.status = 'failed' AND p.fail_reason = ?"
        args.append(fail)
    if nozzle:
        sql += " AND ABS(p.nozzle - ?) < 0.001"
        args.append(nozzle)
    sql += " ORDER BY COALESCE(p.printed_on, p.created_at) DESC, p.id DESC"
    db = get_db()
    prints = db.execute(sql, args).fetchall()
    stats = db.execute(
        """SELECT COUNT(*) AS n,
                  COALESCE(SUM(filament_g), 0) AS grams,
                  COALESCE(SUM(print_time_min), 0) AS minutes,
                  SUM(status = 'succeeded') AS ok,
                  SUM(status = 'failed') AS failed
           FROM prints"""
    ).fetchone()
    nozzles = [r[0] for r in db.execute("SELECT DISTINCT nozzle FROM prints WHERE nozzle > 0 ORDER BY nozzle")]
    return render_template("index.html", prints=prints, stats=stats, q=q, material=material, status=status,
                           printer=printer, fail=fail, nozzle=nozzle, nozzles=nozzles)


@app.route("/print/new", methods=["GET", "POST"])
def new_print():
    if request.method == "POST":
        data = form_to_print(request.form)
        if not data["name"]:
            flash("Give the print a name.", "warn")
            return render_template("form.html", p=data, materials=MATERIALS, statuses=STATUSES)
        db = get_db()
        cur = db.execute(
            f"INSERT INTO prints ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
            list(data.values()),
        )
        db.commit()
        save_uploads(cur.lastrowid, request.files.getlist("files"))
        flash("Print saved.", "ok")
        return redirect(url_for("show_print", print_id=cur.lastrowid))
    defaults = {"status": "succeeded", "printed_on": date.today().isoformat()}
    only = get_db().execute("SELECT id FROM printers").fetchall()
    if len(only) == 1:
        defaults["printer_id"] = only[0]["id"]
    return render_template("form.html", p=defaults)


@app.route("/print/import", methods=["POST"])
def import_print():
    url = request.form.get("url", "").strip()
    try:
        data, cover = fetch_makerworld(url)
    except MakerWorldError as e:
        flash(str(e), "warn")
        return redirect(url_for("new_print"))
    db = get_db()
    cur = db.execute(
        f"INSERT INTO prints ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
        list(data.values()),
    )
    if cover:
        save_makerworld_cover(db, cur.lastrowid, cover)
    db.commit()
    flash("Imported from MakerWorld. Fill in what is missing and upload the 3mf file if you have it.", "ok")
    return redirect(url_for("edit_print", print_id=cur.lastrowid))


@app.route("/print/import-file", methods=["POST"])
def import_file():
    upload = request.files.get("file")
    if not upload or not upload.filename or ext_of(upload.filename) not in {"3mf", "gcode"}:
        flash("Choose a .3mf or .gcode file.", "warn")
        return redirect(url_for("new_print"))
    db = get_db()
    cur = db.execute("INSERT INTO prints (name, status) VALUES (?, 'planned')", ("Import…",))
    print_id = cur.lastrowid
    save_uploads(print_id, [upload])
    f = db.execute("SELECT * FROM files WHERE print_id = ? ORDER BY id DESC LIMIT 1", (print_id,)).fetchone()
    try:
        found, plate_info = apply_slicer_file(print_id, os.path.join(UPLOAD_DIR, f["stored_name"]),
                                              upload.filename, overwrite_name=True)
    except slicer.ParseError as e:
        db.execute("UPDATE prints SET name = ? WHERE id = ?", (upload.filename[:200], print_id))
        db.commit()
        flash(f"{e} The file was kept; fill in the rest yourself.", "warn")
        return redirect(url_for("edit_print", print_id=print_id))
    if found:
        flash(f"Read: {', '.join(found)}.{plate_info}", "ok")
    else:
        flash("File kept, but it contained no slicer data. Fill in the rest yourself.", "warn")
    return redirect(url_for("edit_print", print_id=print_id))


@app.route("/file/<int:file_id>/apply", methods=["POST"])
def apply_file(file_id):
    f = get_db().execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if f is None or ext_of(f["original_name"]) not in {"3mf", "gcode"}:
        abort(404)
    try:
        found, plate_info = apply_slicer_file(f["print_id"], os.path.join(UPLOAD_DIR, f["stored_name"]),
                                              f["original_name"], num(request.form.get("plate"), int))
        flash((f"Read: {', '.join(found)}." if found else "No slicer data found.") + plate_info,
              "ok" if found else "warn")
    except slicer.ParseError as e:
        flash(str(e), "warn")
    return redirect(url_for("show_print", print_id=f["print_id"]))


@app.route("/print/<int:print_id>")
def show_print(print_id):
    p = get_print(print_id)
    db = get_db()
    files = db.execute("SELECT * FROM files WHERE print_id = ? ORDER BY id", (print_id,)).fetchall()
    printer = db.execute("SELECT * FROM printers WHERE id = ?", (p["printer_id"],)).fetchone() if p["printer_id"] else None
    cover = db.execute(f"SELECT {COVER_SQL} FROM prints p WHERE p.id = ?", (print_id,)).fetchone()[0]
    ams = db.execute("SELECT * FROM print_ams WHERE print_id = ? ORDER BY ams_id, tray_id", (print_id,)).fetchall()
    samples = [[r["recorded_at"], r["nozzle"], r["nozzle_target"], r["bed"], r["bed_target"], r["chamber"],
                r["stage"], bambu_status.stage_text(r["stage"]) if r["stage"] is not None else None,
                bambu_status.stage_group(r["stage"]) if r["stage"] is not None else None, r["percent"], r["layer"]]
               for r in db.execute("SELECT * FROM print_samples WHERE print_id = ? ORDER BY recorded_at", (print_id,))]
    return render_template("detail.html", p=p, printer=printer, cover=cover, ams=ams, samples=samples,
                           photos=[f for f in files if f["kind"] == "photo"],
                           models=[f for f in files if f["kind"] == "model"])


@app.route("/print/<int:print_id>/edit", methods=["GET", "POST"])
def edit_print(print_id):
    p = get_print(print_id)
    if request.method == "POST":
        data = form_to_print(request.form)
        if not data["name"]:
            flash("Give the print a name.", "warn")
            return render_template("form.html", p={**data, "id": print_id}, materials=MATERIALS, statuses=STATUSES)
        db = get_db()
        mw_msg = enrich_from_makerworld(db, print_id, p, data)
        db.execute(
            f"UPDATE prints SET {', '.join(k + ' = ?' for k in data)} WHERE id = ?",
            [*data.values(), print_id],
        )
        db.commit()
        save_uploads(print_id, request.files.getlist("files"))
        flash("Changes saved.", "ok")
        if mw_msg:
            flash(*mw_msg)
        return redirect(url_for("show_print", print_id=print_id))
    return render_template("form.html", p=p, materials=MATERIALS, statuses=STATUSES)


@app.route("/print/<int:print_id>/delete", methods=["POST"])
def delete_print(print_id):
    get_print(print_id)
    db = get_db()
    for f in db.execute("SELECT stored_name FROM files WHERE print_id = ?", (print_id,)):
        try:
            os.remove(os.path.join(UPLOAD_DIR, f["stored_name"]))
        except FileNotFoundError:
            pass
    db.execute("DELETE FROM prints WHERE id = ?", (print_id,))
    db.commit()
    flash("Print deleted.", "ok")
    return redirect(url_for("index"))


@app.route("/print/<int:print_id>/photos", methods=["POST"])
def add_photos(print_id):
    get_print(print_id)
    uploads = [f for f in request.files.getlist("photos") if f and f.filename]
    photos = [f for f in uploads if ext_of(f.filename) in PHOTO_EXT]
    if len(photos) < len(uploads):
        flash("Only photos (jpg, png, webp, gif) can go here; other files were skipped.", "warn")
    if photos:
        save_uploads(print_id, photos)
        flash(f"{len(photos)} " + ("photo" if len(photos) == 1 else "photos") + " added.", "ok")
    return redirect(url_for("show_print", print_id=print_id) + "#photos")


@app.route("/file/<int:file_id>/cover", methods=["POST"])
def set_cover(file_id):
    db = get_db()
    f = db.execute("SELECT * FROM files WHERE id = ? AND kind = 'photo'", (file_id,)).fetchone()
    if f is None:
        abort(404)
    db.execute("UPDATE prints SET cover_file_id = ? WHERE id = ?", (file_id, f["print_id"]))
    db.commit()
    return redirect(url_for("show_print", print_id=f["print_id"]) + "#photos")


@app.route("/file/<int:file_id>/delete", methods=["POST"])
def delete_file(file_id):
    db = get_db()
    f = db.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if f is None:
        abort(404)
    try:
        os.remove(os.path.join(UPLOAD_DIR, f["stored_name"]))
    except FileNotFoundError:
        pass
    db.execute("DELETE FROM files WHERE id = ?", (file_id,))
    db.commit()
    return redirect(url_for("show_print", print_id=f["print_id"]))


@app.route("/uploads/<path:stored_name>")
def uploaded(stored_name):
    return send_from_directory(UPLOAD_DIR, stored_name)


@app.route("/download/<int:file_id>")
def download(file_id):
    f = get_db().execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if f is None:
        abort(404)
    return send_from_directory(UPLOAD_DIR, f["stored_name"], as_attachment=True,
                               download_name=f["original_name"])


# ---------- Printers ----------

IP_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$|^[A-Za-z0-9.-]+$")


def printer_from_form(form):
    ip = form.get("ip", "").strip()
    return {
        "name": form.get("name", "").strip(),
        "model": form.get("model", "").strip(),
        "notes": form.get("notes", "").strip(),
        "ip": ip if IP_RE.match(ip) else "",
        "serial": re.sub(r"[^A-Za-z0-9]", "", form.get("serial", "")),
        # left empty = keep the current access code (it is never put back into the form)
        "access_code": form.get("access_code", "").strip(),
        "mqtt_enabled": 1 if form.get("mqtt_enabled") else 0,
    }


def public_printer(row_or_dict):
    """Printer data for the form, without the access code."""
    d = dict(row_or_dict)
    d["has_access_code"] = bool(d.pop("access_code", None))
    return d


def save_printer_photo(upload):
    """Stores an uploaded printer photo and returns its stored_name (or None)."""
    if not upload or not upload.filename:
        return None
    ext = ext_of(upload.filename)
    if ext not in PHOTO_EXT:
        flash("Printer photo skipped: only jpg, png, webp or gif.", "warn")
        return None
    stored = f"{uuid.uuid4().hex}.{ext}"
    upload.save(os.path.join(UPLOAD_DIR, stored))
    return stored


def remove_upload(stored_name):
    if not stored_name:
        return
    try:
        os.remove(os.path.join(UPLOAD_DIR, stored_name))
    except FileNotFoundError:
        pass


@app.route("/printers")
def printers():
    rows = get_db().execute(
        """SELECT pr.*, COUNT(p.id) AS n,
                  COALESCE(SUM(p.filament_g), 0) AS grams,
                  COALESCE(SUM(p.print_time_min), 0) AS minutes,
                  SUM(p.status = 'succeeded') AS ok
           FROM printers pr LEFT JOIN prints p ON p.printer_id = pr.id
           GROUP BY pr.id ORDER BY pr.name"""
    ).fetchall()
    db = get_db()
    due = {pr["id"]: sum(t["st"]["due"] for t in maintenance.tasks_for_printer(db, pr["id"])) for pr in rows}
    return render_template("printers.html", printers=rows, due=due)


@app.route("/printers/new", methods=["GET", "POST"])
def new_printer():
    if request.method == "POST":
        data = printer_from_form(request.form)
        if not data["name"]:
            flash("Give the printer a name.", "warn")
            return render_template("printer_form.html", pr=public_printer(data))
        data["photo"] = save_printer_photo(request.files.get("photo"))
        data["access_code"] = data["access_code"] or None
        db = get_db()
        db.execute(f"INSERT INTO printers ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                   list(data.values()))
        db.commit()
        flash("Printer added.", "ok")
        return redirect(url_for("printers"))
    return render_template("printer_form.html", pr={})


@app.route("/printers/<int:printer_id>/edit", methods=["GET", "POST"])
def edit_printer(printer_id):
    db = get_db()
    pr = db.execute("SELECT * FROM printers WHERE id = ?", (printer_id,)).fetchone()
    if pr is None:
        abort(404)
    if request.method == "POST":
        data = printer_from_form(request.form)
        if not data["name"]:
            flash("Give the printer a name.", "warn")
            return render_template("printer_form.html", pr=public_printer({**data, "id": printer_id, "photo": pr["photo"]}))
        photo = pr["photo"]
        new_photo = save_printer_photo(request.files.get("photo"))
        if new_photo or request.form.get("remove_photo"):
            remove_upload(photo)
            photo = new_photo
        if not data["access_code"]:
            data["access_code"] = pr["access_code"]
        data["photo"] = photo
        db.execute(f"UPDATE printers SET {', '.join(k + ' = ?' for k in data)} WHERE id = ?",
                   [*data.values(), printer_id])
        db.commit()
        flash("Printer saved.", "ok")
        return redirect(url_for("printers"))
    return render_template("printer_form.html", pr=public_printer(pr))


@app.route("/printers/<int:printer_id>/delete", methods=["POST"])
def delete_printer(printer_id):
    db = get_db()
    pr = db.execute("SELECT * FROM printers WHERE id = ?", (printer_id,)).fetchone()
    if pr is None:
        abort(404)
    remove_upload(pr["photo"])
    db.execute("UPDATE prints SET printer_id = NULL WHERE printer_id = ?", (printer_id,))
    db.execute("DELETE FROM printers WHERE id = ?", (printer_id,))
    db.commit()
    flash("Printer deleted. Its prints are kept.", "ok")
    return redirect(url_for("printers"))


# ---------- Camera ----------

cameras = camera.CameraManager()

# Snapshots are kept ~20 s per printer (a failed attempt too), so that several open tabs
# together make at most one connection to the printer every 20 s.
SNAPSHOT_CACHE_SECONDS = 20
_snapshots = {}                   # printer_id -> (rtsp url, time, jpeg or CameraError)
_snapshot_locks = {}
_snapshot_locks_guard = threading.Lock()


def cached_snapshot(printer_id, ip, access_code):
    """JPEG from the printer: a fresh frame from a running live stream, else from the cache or fetched anew.
    Raises CameraError (also a cached one) when there is no image."""
    url = camera.rtsp_url(ip, access_code)
    cam = cameras.peek(printer_id)
    if cam is not None and cam.url == url:
        jpeg = cam.latest()
        if jpeg is not None:
            return jpeg
    with _snapshot_locks_guard:
        lock = _snapshot_locks.setdefault(printer_id, threading.Lock())
    with lock:   # concurrent requests wait for a single fetch
        hit = _snapshots.get(printer_id)
        if hit is None or hit[0] != url or time.monotonic() - hit[1] >= SNAPSHOT_CACHE_SECONDS:
            try:
                result = camera.grab_snapshot(ip, access_code)
            except camera.CameraError as e:
                result = e
            hit = _snapshots[printer_id] = (url, time.monotonic(), result)
    if isinstance(hit[2], camera.CameraError):
        raise hit[2]
    return hit[2]


def camera_printer(printer_id):
    pr = get_printer(printer_id)
    if not pr["ip"] or not pr["access_code"]:
        abort(404)
    return pr


@app.route("/printers/<int:printer_id>/camera.mjpg")
def camera_stream(printer_id):
    pr = camera_printer(printer_id)
    cam = cameras.get(printer_id, pr["ip"], pr["access_code"])

    def generate():
        for frame in cam.frames():
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame)
                   + frame + b"\r\n")

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.route("/printers/<int:printer_id>/snapshot.jpg")
def camera_snapshot(printer_id):
    pr = camera_printer(printer_id)
    try:
        jpeg = cached_snapshot(printer_id, pr["ip"], pr["access_code"])
    except camera.CameraError as e:
        return Response(str(e), status=502, mimetype="text/plain", headers={"Cache-Control": "no-store"})
    return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


# ---------- Maintenance ----------

def get_printer(printer_id):
    pr = get_db().execute("SELECT * FROM printers WHERE id = ?", (printer_id,)).fetchone()
    if pr is None:
        abort(404)
    return pr


def get_task(task_id):
    t = get_db().execute("SELECT * FROM maintenance_tasks WHERE id = ?", (task_id,)).fetchone()
    if t is None:
        abort(404)
    return t


def task_from_form(form):
    hours = num(form.get("interval_hours"))
    days = num(form.get("interval_days"), int)
    data = {"name": form.get("name", "").strip()[:200],
            "interval_hours": hours if hours and hours > 0 else None,
            "interval_days": days if days and days > 0 else None,
            "notes": form.get("notes", "").strip()}
    done = form.get("last_done_at", "").strip()
    if done:
        try:
            data["last_done_at"] = datetime.fromisoformat(done).replace(tzinfo=maintenance.TZ, second=0).isoformat()
        except ValueError:
            pass
    return data


def task_errors(data):
    if not data["name"]:
        return "Give the task a name."
    if not data["interval_hours"] and not data["interval_days"]:
        return "Give an interval in print hours and/or days."
    return None


@app.template_filter("localinput")
def localinput(iso):
    """ISO time → value for <input type=datetime-local> in the local time zone."""
    try:
        return maintenance.parse_time(iso).astimezone(maintenance.TZ).strftime("%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return ""


@app.route("/printers/<int:printer_id>/maintenance", methods=["GET", "POST"])
def printer_maintenance(printer_id):
    pr = get_printer(printer_id)
    db = get_db()
    if request.method == "POST":
        data = task_from_form(request.form)
        error = task_errors(data)
        if error:
            flash(error, "warn")
        else:
            data.setdefault("last_done_at", maintenance.now_local().isoformat())
            data["printer_id"] = printer_id
            db.execute(f"INSERT INTO maintenance_tasks ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                       list(data.values()))
            db.commit()
            flash("Maintenance task added.", "ok")
        return redirect(url_for("printer_maintenance", printer_id=printer_id))
    return render_template("maintenance.html", pr=pr, tasks=maintenance.tasks_for_printer(db, printer_id),
                           now=localinput(maintenance.now_local().isoformat()))


@app.route("/printers/<int:printer_id>/maintenance/defaults", methods=["POST"])
def seed_maintenance(printer_id):
    get_printer(printer_id)
    db = get_db()
    maintenance.seed_defaults(db, printer_id)
    db.commit()
    flash("Default tasks added. Feel free to adjust the intervals.", "ok")
    return redirect(url_for("printer_maintenance", printer_id=printer_id))


@app.route("/maintenance/<int:task_id>/edit", methods=["GET", "POST"])
def edit_maintenance(task_id):
    t = get_task(task_id)
    pr = get_printer(t["printer_id"])
    if request.method == "POST":
        data = task_from_form(request.form)
        error = task_errors(data)
        if error:
            flash(error, "warn")
            return render_template("maintenance_form.html", t={**dict(t), **data}, pr=pr)
        db = get_db()
        db.execute(f"UPDATE maintenance_tasks SET {', '.join(k + ' = ?' for k in data)} WHERE id = ?",
                   [*data.values(), task_id])
        db.commit()
        flash("Maintenance task saved.", "ok")
        return redirect(url_for("printer_maintenance", printer_id=pr["id"]))
    return render_template("maintenance_form.html", t=t, pr=pr)


@app.route("/maintenance/<int:task_id>/done", methods=["POST"])
def done_maintenance(task_id):
    t = get_task(task_id)
    db = get_db()
    db.execute("UPDATE maintenance_tasks SET last_done_at = ?, notified_at = NULL WHERE id = ?",
               (maintenance.now_local().isoformat(), task_id))
    db.commit()
    flash(f"'{t['name']}' marked as done.", "ok")
    return redirect(url_for("printer_maintenance", printer_id=t["printer_id"]))


@app.route("/maintenance/<int:task_id>/delete", methods=["POST"])
def delete_maintenance(task_id):
    t = get_task(task_id)
    db = get_db()
    db.execute("DELETE FROM maintenance_tasks WHERE id = ?", (task_id,))
    db.commit()
    flash("Maintenance task deleted.", "ok")
    return redirect(url_for("printer_maintenance", printer_id=t["printer_id"]))


# ---------- Settings (ntfy) ----------

@app.route("/settings", methods=["GET", "POST"])
def settings():
    db = get_db()
    cfg = notify.load_settings(db)
    if request.method == "POST":
        f = request.form
        url = f.get("ntfy_url", "").strip()
        topic = f.get("ntfy_topic", "").strip()
        values = {"ntfy_url": url, "ntfy_topic": topic}
        problems = []
        if url and not notify.URL_RE.match(url):
            problems.append("The ntfy URL must start with http:// or https:// (e.g. https://ntfy.sh).")
            values.pop("ntfy_url")
        if topic and not notify.TOPIC_RE.match(topic):
            problems.append("A topic may only contain letters, digits, - and _ (max. 64 characters).")
            values.pop("ntfy_topic")
        token = f.get("ntfy_token", "").strip()
        if f.get("clear_token"):
            values["ntfy_token"] = ""
        elif token:
            values["ntfy_token"] = token
        for key in notify.KINDS.values():
            values[key] = "1" if f.get(key) else "0"
        notify.save_settings(db, values)
        db.commit()
        for p in problems:
            flash(p, "warn")
        flash("Settings saved." + (" Other fields were not changed." if problems else ""),
              "warn" if problems else "ok")
        return redirect(url_for("settings"))
    public = {k: v for k, v in cfg.items() if k != "ntfy_token"}
    public["has_token"] = bool(cfg.get("ntfy_token"))
    return render_template("settings.html", cfg=public)


@app.route("/settings/test", methods=["POST"])
def settings_test():
    cfg = notify.load_settings(get_db())
    try:
        notify.post(cfg, "Printvault: test notification", "Notifications are arriving.", tags=["tada"])
        flash("Test notification sent.", "ok")
    except notify.NotifyError as e:
        flash(str(e), "warn")
    return redirect(url_for("settings"))


# Old Dutch URLs (bookmarks) keep working.
@app.route("/instellingen")
def legacy_settings():
    return redirect(url_for("settings"), 301)


@app.route("/printers/<int:printer_id>/onderhoud")
def legacy_maintenance(printer_id):
    return redirect(url_for("printer_maintenance", printer_id=printer_id), 301)


# ---------- Live status (from the MQTT listener) ----------

LIVE_URL = os.environ.get("LIVE_URL", "http://127.0.0.1:8101/live")


@app.route("/api/live")
def api_live():
    try:
        with urllib.request.urlopen(LIVE_URL, timeout=2) as resp:
            data = json.loads(resp.read(1024 * 1024))
        data["available"] = True
    except Exception:
        data = {"available": False, "printers": []}
    db = get_db()
    for p in data.get("printers") or []:
        if not isinstance(p, dict):
            continue
        if p.get("ams"):
            p["ams_history"] = ams_history(p.get("printer_id"))
        # image next to the status card: the print's cover (and whether it is an own photo), else the printer photo
        p["cover"] = p["own_cover"] = None
        if p.get("print_id"):
            row = db.execute(f"SELECT {COVER_SQL} AS cover, {OWN_COVER_SQL} AS own FROM prints p WHERE p.id = ?",
                             (p["print_id"],)).fetchone()
            if row and row["cover"]:
                p["cover"], p["own_cover"] = url_for("uploaded", stored_name=row["cover"]), bool(row["own"])
        pr = db.execute("SELECT photo, COALESCE(ip, '') != '' AND COALESCE(access_code, '') != '' AS cam "
                        "FROM printers WHERE id = ?", (p.get("printer_id"),)).fetchone()
        p["printer_photo"] = url_for("uploaded", stored_name=pr["photo"]) if pr and pr["photo"] else None
        p["camera"] = bool(pr and pr["cam"])
    return jsonify(data)


def ams_history(printer_id, hours=24):
    """AMS readings of the last `hours` hours per unit: {ams_id: [[time, %, level, °C], …]}."""
    rows = get_db().execute(
        "SELECT ams_id, recorded_at, humidity_pct, humidity_level, temp FROM ams_readings "
        "WHERE printer_id = ? AND datetime(recorded_at) >= datetime('now', ?) ORDER BY recorded_at",
        (printer_id, f"-{int(hours)} hours")).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["ams_id"], []).append([r["recorded_at"], r["humidity_pct"], r["humidity_level"], r["temp"]])
    return out


# ---------- MakerWorld import ----------

MW_API = "https://makerworld.com/api/v1/design-service/design/{}"
MW_URL = re.compile(r"^https?://(www\.)?makerworld\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?models/(\d+)", re.I)
MW_IMAGE_HOST = re.compile(r"^https://[a-z0-9.-]+\.bblmw\.com/")
MW_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MAX_COVER_BYTES = 15 * 1024 * 1024


class MakerWorldError(Exception):
    pass


def _http_get(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": MW_UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read(limit + 1)
    if len(body) > limit:
        raise MakerWorldError("Response from MakerWorld is too large.")
    return body


def _strip_html(text):
    text = re.sub(r"<\s*(br|/p|/li|/h\d)\s*/?>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_makerworld(url):
    """Fetches the metadata of a MakerWorld model. Returns (print data, cover);
    cover is (bytes, extension) or None."""
    m = MW_URL.match(url)
    if not m:
        raise MakerWorldError("That does not look like a MakerWorld model link (https://makerworld.com/…/models/…).")
    design_id = m.group(2)
    try:
        d = json.loads(_http_get(MW_API.format(design_id), 5 * 1024 * 1024))
    except MakerWorldError:
        raise
    except Exception as e:
        raise MakerWorldError(f"Could not fetch MakerWorld ({e}). Fill in the print manually.")
    if not d.get("title"):
        raise MakerWorldError("Model not found on MakerWorld.")

    instances = d.get("instances") or []
    inst = next((i for i in instances if i.get("id") == d.get("defaultInstanceId")), None) \
        or next((i for i in instances if i.get("isDefault")), None) \
        or (instances[0] if instances else {})
    filaments = inst.get("instanceFilaments") or []

    material = ""
    types = {f.get("type", "").upper() for f in filaments if f.get("type")}
    if len(types) == 1:
        t = types.pop()
        material = t if t in MATERIALS else "Other"
    elif types:
        material = "Other"

    notes = []
    if inst.get("title"):
        notes.append(f"Profile: {inst['title']}")
    if filaments:
        notes.append("Filament: " + ", ".join(
            f"{f.get('type', '?')} {f.get('color', '')} ({f.get('usedG', '?')} g)".strip() for f in filaments))
    if inst.get("needAms"):
        notes.append("Requires AMS.")
    summary = _strip_html(d.get("summary"))
    if summary:
        notes.append("")
        notes.append(summary[:3000])

    creator = d.get("designCreator") or {}
    prediction = inst.get("prediction")
    data = {
        "name": d["title"][:200],
        "description": f"MakerWorld model by {creator.get('name')}" if creator.get("name") else "MakerWorld model",
        "material": material,
        "color": filaments[0].get("color", "") if len(filaments) == 1 else "",
        "filament_g": float(inst["weight"]) if inst.get("weight") else None,
        "print_time_min": round(prediction / 60) if prediction else None,
        "layer_height": None,
        "nozzle": None,
        "status": "planned",
        "tags": ", ".join((d.get("tags") or [])[:8]),
        "notes": "\n".join(notes),
        "printed_on": None,
        "printer_id": None,
        "source_url": f"https://makerworld.com/en/models/{design_id}",
        "designer": creator.get("name"),
        "license": d.get("license"),
    }
    lh = re.search(r"(0\.\d+)\s*mm\s*layer", inst.get("title") or "", re.I)
    if lh:
        data["layer_height"] = float(lh.group(1))

    cover = None
    cover_url = d.get("coverUrl") or ""
    if MW_IMAGE_HOST.match(cover_url):
        ext = ext_of(cover_url.split("?")[0])
        if ext in PHOTO_EXT:
            try:
                cover = (_http_get(cover_url, MAX_COVER_BYTES), ext)
            except Exception:
                cover = None
    return data, cover

def save_makerworld_cover(db, print_id, cover):
    content, ext = cover
    stored = f"{uuid.uuid4().hex}.{ext}"
    with open(os.path.join(UPLOAD_DIR, stored), "wb") as fh:
        fh.write(content)
    db.execute(
        "INSERT INTO files (print_id, kind, stored_name, original_name, size) VALUES (?,?,?,?,?)",
        (print_id, "photo", stored, f"makerworld-cover.{ext}", len(content)),
    )


# Fields filled in from MakerWorld when editing, if they are still empty.
# Status, date, print time and printer come from the printer itself and are left alone.
MW_FILL_FIELDS = ("description", "material", "color", "filament_g", "layer_height", "tags", "designer", "license")


def enrich_from_makerworld(db, print_id, old, data):
    """Completes `data` with MakerWorld data when a (new) MakerWorld link was given,
    or when the print has no MakerWorld cover yet. Returns a flash message or None."""
    url = data.get("source_url") or ""
    if not MW_URL.match(url):
        return None
    has_cover = db.execute("SELECT 1 FROM files WHERE print_id = ? AND original_name LIKE 'makerworld-cover.%'",
                           (print_id,)).fetchone()
    if url == (old["source_url"] or "") and has_cover:
        return None
    try:
        mw, cover = fetch_makerworld(url)
    except MakerWorldError as e:
        return (f"Link saved, but fetching the MakerWorld data failed: {e}", "warn")
    filled = [k for k in MW_FILL_FIELDS if not data.get(k) and mw.get(k)]
    for k in filled:
        data[k] = mw[k]
    if mw.get("notes") and mw["notes"] not in (data.get("notes") or ""):
        data["notes"] = f"{data['notes']}\n\n{mw['notes']}".strip() if data.get("notes") else mw["notes"]
    data["source_url"] = mw["source_url"]
    if cover and not has_cover:
        save_makerworld_cover(db, print_id, cover)
    return ("MakerWorld data filled in" + (" (with image)." if cover and not has_cover else "."), "ok")


@app.errorhandler(413)
def too_large(_e):
    flash("File too large.", "warn")
    return redirect(request.referrer or url_for("index"))


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8100, debug=True)
