import html
import json
import os
import re
import sqlite3
import urllib.request
import uuid
from contextlib import closing
from datetime import date, datetime

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

import slicer
from mqtt_listener import error_code

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "printvault.db")

PHOTO_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
MODEL_EXT = {"3mf", "stl", "step", "stp", "obj", "gcode"}
STATUSES = ["gepland", "bezig", "gelukt", "mislukt", "opnieuw"]
MATERIALS = ["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "Ander"]
FAIL_REASONS = ["hechting", "warping", "spaghetti", "verstopping", "stroomuitval", "anders"]

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
    status TEXT DEFAULT 'gepland',
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
               ("task_id", "TEXT"), ("source", "TEXT")],
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
                        # webapp en listener starten tegelijk; de ander kan net voor geweest zijn
                        if "duplicate column" not in str(e):
                            raise
        db.execute("CREATE INDEX IF NOT EXISTS idx_print_ams_print ON print_ams(print_id)")


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


def form_to_print(form):
    status = form.get("status", "gepland")
    failed = status == "mislukt"
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
        "status": status if status in STATUSES else "gepland",
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
        "fail_reason": reason if failed and reason in FAIL_REASONS else None,
        "fail_note": (form.get("fail_note", "").strip() or None) if failed else None,
    }


SLICER_FIELDS = {"material": "materiaal", "color": "kleur", "filament_g": "gewicht",
                 "print_time_min": "printtijd", "layer_height": "laagdikte", "nozzle": "nozzle",
                 "infill": "infill", "plate_type": "plaattype", "nozzle_temp": "nozzletemp",
                 "bed_temp": "bedtemp", "plate": "plaatnummer"}


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
    """Leest een 3mf/gcode uit en werkt de print bij. Geeft een korte samenvatting terug."""
    parsed = slicer.parse(path, original_name)
    plates = parsed["plates"]
    if plate_index is None:
        plate_index = plates[0]["index"]
    fields, filaments, thumb = slicer.plate_to_print_fields(parsed, plate_index, MATERIALS)
    db = get_db()
    if overwrite_name:
        stem = re.sub(r"(\.gcode)?\.(3mf|gcode)$", "", original_name, flags=re.I)
        fields["name"] = (parsed.get("name") or stem.replace("_", " ")).strip()[:200] or "Naamloze print"
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
        thumb_name = f"thumbnail-plaat-{plate_index}.{ext}"
        exists = db.execute("SELECT 1 FROM files WHERE print_id = ? AND original_name = ?",
                            (print_id, thumb_name)).fetchone()
        if not exists:
            store_bytes(print_id, "photo", content, ext, thumb_name)
    db.commit()
    found = [label for k, label in SLICER_FIELDS.items() if fields.get(k) not in (None, "")]
    plate_info = (f" Dit bestand heeft {len(plates)} platen; plaat {plate_index} is gebruikt."
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
        flash("Overgeslagen (onbekend bestandstype): " + ", ".join(skipped), "warn")


# Omslagfoto: gekozen foto, anders de eerste echte foto, anders een slicer-/MakerWorld-thumbnail.
COVER_SQL = """COALESCE(
    (SELECT f.stored_name FROM files f WHERE f.id = p.cover_file_id AND f.print_id = p.id AND f.kind = 'photo'),
    (SELECT f.stored_name FROM files f WHERE f.print_id = p.id AND f.kind = 'photo'
       ORDER BY (f.original_name LIKE 'thumbnail-plaat-%' OR f.original_name LIKE 'makerworld-cover.%'), f.id
       LIMIT 1))"""


def get_print(print_id):
    row = get_db().execute("SELECT * FROM prints WHERE id = ?", (print_id,)).fetchone()
    if row is None:
        abort(404)
    return row


@app.template_filter("duration")
def duration(minutes):
    if not minutes:
        return "–"
    h, m = divmod(int(minutes), 60)
    return f"{h}u {m:02d}m" if h else f"{m}m"


@app.template_filter("errcode")
def errcode(err):
    return error_code(err) or "–"


@app.template_filter("clock")
def clock(iso):
    """'2026-11-18T19:02:00+01:00' → '18-11-2026 19:02'."""
    try:
        return datetime.fromisoformat(iso).strftime("%d-%m-%Y %H:%M")
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
            "fail_reasons": FAIL_REASONS}


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    material = request.args.get("material", "")
    status = request.args.get("status", "")
    printer = num(request.args.get("printer"), int)
    fail = request.args.get("fail", "")
    sql = f"""SELECT p.*, pr.name AS printer_name, {COVER_SQL} AS cover
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
        sql += " AND p.status = 'mislukt' AND p.fail_reason = ?"
        args.append(fail)
    sql += " ORDER BY COALESCE(p.printed_on, p.created_at) DESC, p.id DESC"
    db = get_db()
    prints = db.execute(sql, args).fetchall()
    stats = db.execute(
        """SELECT COUNT(*) AS n,
                  COALESCE(SUM(filament_g), 0) AS grams,
                  COALESCE(SUM(print_time_min), 0) AS minutes,
                  SUM(status = 'gelukt') AS ok,
                  SUM(status = 'mislukt') AS failed
           FROM prints"""
    ).fetchone()
    return render_template("index.html", prints=prints, stats=stats, q=q,
                           material=material, status=status, printer=printer, fail=fail)


@app.route("/print/new", methods=["GET", "POST"])
def new_print():
    if request.method == "POST":
        data = form_to_print(request.form)
        if not data["name"]:
            flash("Geef de print een naam.", "warn")
            return render_template("form.html", p=data, materials=MATERIALS, statuses=STATUSES)
        db = get_db()
        cur = db.execute(
            f"INSERT INTO prints ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
            list(data.values()),
        )
        db.commit()
        save_uploads(cur.lastrowid, request.files.getlist("files"))
        flash("Print opgeslagen.", "ok")
        return redirect(url_for("show_print", print_id=cur.lastrowid))
    defaults = {"status": "gelukt", "printed_on": date.today().isoformat()}
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
        content, ext = cover
        stored = f"{uuid.uuid4().hex}.{ext}"
        with open(os.path.join(UPLOAD_DIR, stored), "wb") as fh:
            fh.write(content)
        db.execute(
            "INSERT INTO files (print_id, kind, stored_name, original_name, size) VALUES (?,?,?,?,?)",
            (cur.lastrowid, "photo", stored, f"makerworld-cover.{ext}", len(content)),
        )
    db.commit()
    flash("Geïmporteerd van MakerWorld. Vul aan wat nog ontbreekt en upload eventueel het 3mf-bestand.", "ok")
    return redirect(url_for("edit_print", print_id=cur.lastrowid))


@app.route("/print/import-file", methods=["POST"])
def import_file():
    upload = request.files.get("file")
    if not upload or not upload.filename or ext_of(upload.filename) not in {"3mf", "gcode"}:
        flash("Kies een .3mf- of .gcode-bestand.", "warn")
        return redirect(url_for("new_print"))
    db = get_db()
    cur = db.execute("INSERT INTO prints (name, status) VALUES (?, 'gepland')", ("Import…",))
    print_id = cur.lastrowid
    save_uploads(print_id, [upload])
    f = db.execute("SELECT * FROM files WHERE print_id = ? ORDER BY id DESC LIMIT 1", (print_id,)).fetchone()
    try:
        found, plate_info = apply_slicer_file(print_id, os.path.join(UPLOAD_DIR, f["stored_name"]),
                                              upload.filename, overwrite_name=True)
    except slicer.ParseError as e:
        db.execute("UPDATE prints SET name = ? WHERE id = ?", (upload.filename[:200], print_id))
        db.commit()
        flash(f"{e} Het bestand is wel bewaard; vul de rest zelf in.", "warn")
        return redirect(url_for("edit_print", print_id=print_id))
    if found:
        flash(f"Ingelezen: {', '.join(found)}.{plate_info}", "ok")
    else:
        flash("Bestand bewaard, maar er stonden geen slicergegevens in. Vul de rest zelf in.", "warn")
    return redirect(url_for("edit_print", print_id=print_id))


@app.route("/file/<int:file_id>/apply", methods=["POST"])
def apply_file(file_id):
    f = get_db().execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if f is None or ext_of(f["original_name"]) not in {"3mf", "gcode"}:
        abort(404)
    try:
        found, plate_info = apply_slicer_file(f["print_id"], os.path.join(UPLOAD_DIR, f["stored_name"]),
                                              f["original_name"], num(request.form.get("plate"), int))
        flash((f"Ingelezen: {', '.join(found)}." if found else "Geen slicergegevens gevonden.") + plate_info,
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
    return render_template("detail.html", p=p, printer=printer, cover=cover, ams=ams,
                           photos=[f for f in files if f["kind"] == "photo"],
                           models=[f for f in files if f["kind"] == "model"])


@app.route("/print/<int:print_id>/edit", methods=["GET", "POST"])
def edit_print(print_id):
    p = get_print(print_id)
    if request.method == "POST":
        data = form_to_print(request.form)
        if not data["name"]:
            flash("Geef de print een naam.", "warn")
            return render_template("form.html", p={**data, "id": print_id}, materials=MATERIALS, statuses=STATUSES)
        db = get_db()
        db.execute(
            f"UPDATE prints SET {', '.join(k + ' = ?' for k in data)} WHERE id = ?",
            [*data.values(), print_id],
        )
        db.commit()
        save_uploads(print_id, request.files.getlist("files"))
        flash("Wijzigingen opgeslagen.", "ok")
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
    flash("Print verwijderd.", "ok")
    return redirect(url_for("index"))


@app.route("/print/<int:print_id>/photos", methods=["POST"])
def add_photos(print_id):
    get_print(print_id)
    uploads = [f for f in request.files.getlist("photos") if f and f.filename]
    photos = [f for f in uploads if ext_of(f.filename) in PHOTO_EXT]
    if len(photos) < len(uploads):
        flash("Alleen foto's (jpg, png, webp, gif) kunnen hier; andere bestanden zijn overgeslagen.", "warn")
    if photos:
        save_uploads(print_id, photos)
        flash(f"{len(photos)} " + ("foto" if len(photos) == 1 else "foto's") + " toegevoegd.", "ok")
    return redirect(url_for("show_print", print_id=print_id) + "#fotos")


@app.route("/file/<int:file_id>/cover", methods=["POST"])
def set_cover(file_id):
    db = get_db()
    f = db.execute("SELECT * FROM files WHERE id = ? AND kind = 'photo'", (file_id,)).fetchone()
    if f is None:
        abort(404)
    db.execute("UPDATE prints SET cover_file_id = ? WHERE id = ?", (file_id, f["print_id"]))
    db.commit()
    return redirect(url_for("show_print", print_id=f["print_id"]) + "#fotos")


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
        # leeg laten = huidige access code behouden (die wordt nooit terug in het formulier gezet)
        "access_code": form.get("access_code", "").strip(),
        "mqtt_enabled": 1 if form.get("mqtt_enabled") else 0,
    }


def public_printer(row_or_dict):
    """Printergegevens voor het formulier, zonder access code."""
    d = dict(row_or_dict)
    d["has_access_code"] = bool(d.pop("access_code", None))
    return d


def save_printer_photo(upload):
    """Slaat een geüploade printerfoto op en geeft de stored_name terug (of None)."""
    if not upload or not upload.filename:
        return None
    ext = ext_of(upload.filename)
    if ext not in PHOTO_EXT:
        flash("Printerfoto overgeslagen: alleen jpg, png, webp of gif.", "warn")
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
                  SUM(p.status = 'gelukt') AS ok
           FROM printers pr LEFT JOIN prints p ON p.printer_id = pr.id
           GROUP BY pr.id ORDER BY pr.name"""
    ).fetchall()
    return render_template("printers.html", printers=rows)


@app.route("/printers/new", methods=["GET", "POST"])
def new_printer():
    if request.method == "POST":
        data = printer_from_form(request.form)
        if not data["name"]:
            flash("Geef de printer een naam.", "warn")
            return render_template("printer_form.html", pr=public_printer(data))
        data["photo"] = save_printer_photo(request.files.get("photo"))
        data["access_code"] = data["access_code"] or None
        db = get_db()
        db.execute(f"INSERT INTO printers ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                   list(data.values()))
        db.commit()
        flash("Printer toegevoegd.", "ok")
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
            flash("Geef de printer een naam.", "warn")
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
        flash("Printer opgeslagen.", "ok")
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
    flash("Printer verwijderd. De prints zelf blijven bewaard.", "ok")
    return redirect(url_for("printers"))


# ---------- Live status (van de MQTT-listener) ----------

LIVE_URL = os.environ.get("LIVE_URL", "http://127.0.0.1:8101/live")


@app.route("/api/live")
def api_live():
    try:
        with urllib.request.urlopen(LIVE_URL, timeout=2) as resp:
            data = json.loads(resp.read(1024 * 1024))
        data["available"] = True
    except Exception:
        data = {"available": False, "printers": []}
    return jsonify(data)


# ---------- MakerWorld-import ----------

MW_API = "https://makerworld.com/api/v1/design-service/design/{}"
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
        raise MakerWorldError("Antwoord van MakerWorld is te groot.")
    return body


def _strip_html(text):
    text = re.sub(r"<\s*(br|/p|/li|/h\d)\s*/?>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_makerworld(url):
    """Haalt metadata van een MakerWorld-model op. Geeft (printdata, cover) terug;
    cover is (bytes, extensie) of None."""
    m = re.match(r"^https?://(www\.)?makerworld\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?models/(\d+)", url, re.I)
    if not m:
        raise MakerWorldError("Dat lijkt geen MakerWorld-modellink (https://makerworld.com/…/models/…).")
    design_id = m.group(2)
    try:
        d = json.loads(_http_get(MW_API.format(design_id), 5 * 1024 * 1024))
    except MakerWorldError:
        raise
    except Exception as e:
        raise MakerWorldError(f"MakerWorld kon niet worden opgehaald ({e}). Vul de print handmatig in.")
    if not d.get("title"):
        raise MakerWorldError("Model niet gevonden op MakerWorld.")

    instances = d.get("instances") or []
    inst = next((i for i in instances if i.get("id") == d.get("defaultInstanceId")), None) \
        or next((i for i in instances if i.get("isDefault")), None) \
        or (instances[0] if instances else {})
    filaments = inst.get("instanceFilaments") or []

    material = ""
    types = {f.get("type", "").upper() for f in filaments if f.get("type")}
    if len(types) == 1:
        t = types.pop()
        material = t if t in MATERIALS else "Ander"
    elif types:
        material = "Ander"

    notes = []
    if inst.get("title"):
        notes.append(f"Profiel: {inst['title']}")
    if filaments:
        notes.append("Filament: " + ", ".join(
            f"{f.get('type', '?')} {f.get('color', '')} ({f.get('usedG', '?')} g)".strip() for f in filaments))
    if inst.get("needAms"):
        notes.append("Vereist AMS.")
    summary = _strip_html(d.get("summary"))
    if summary:
        notes.append("")
        notes.append(summary[:3000])

    creator = d.get("designCreator") or {}
    prediction = inst.get("prediction")
    data = {
        "name": d["title"][:200],
        "description": f"MakerWorld-model door {creator.get('name')}" if creator.get("name") else "MakerWorld-model",
        "material": material,
        "color": filaments[0].get("color", "") if len(filaments) == 1 else "",
        "filament_g": float(inst["weight"]) if inst.get("weight") else None,
        "print_time_min": round(prediction / 60) if prediction else None,
        "layer_height": None,
        "nozzle": None,
        "status": "gepland",
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


@app.errorhandler(413)
def too_large(_e):
    flash("Bestand te groot.", "warn")
    return redirect(request.referrer or url_for("index"))


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8100, debug=True)
