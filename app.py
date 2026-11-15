import os
import sqlite3
import uuid
from datetime import date

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_from_directory, url_for)
from werkzeug.utils import secure_filename

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "printvault.db")

PHOTO_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
MODEL_EXT = {"3mf", "stl", "step", "stp", "obj", "gcode"}
STATUSES = ["gepland", "gelukt", "mislukt", "opnieuw"]
MATERIALS = ["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "Ander"]

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
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)


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
    }


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


@app.template_filter("filesize")
def filesize(size):
    size = size or 0
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    material = request.args.get("material", "")
    status = request.args.get("status", "")
    sql = """SELECT p.*,
               (SELECT stored_name FROM files f WHERE f.print_id = p.id AND f.kind = 'photo'
                ORDER BY f.id LIMIT 1) AS cover
             FROM prints p WHERE 1=1"""
    args = []
    if q:
        sql += " AND (p.name LIKE ? OR p.tags LIKE ? OR p.description LIKE ? OR p.notes LIKE ?)"
        args += [f"%{q}%"] * 4
    if material:
        sql += " AND p.material = ?"
        args.append(material)
    if status:
        sql += " AND p.status = ?"
        args.append(status)
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
                           material=material, status=status,
                           materials=MATERIALS, statuses=STATUSES)


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
    return render_template("form.html", p={"status": "gelukt", "printed_on": date.today().isoformat()},
                           materials=MATERIALS, statuses=STATUSES)


@app.route("/print/<int:print_id>")
def show_print(print_id):
    p = get_print(print_id)
    files = get_db().execute("SELECT * FROM files WHERE print_id = ? ORDER BY id", (print_id,)).fetchall()
    return render_template("detail.html", p=p,
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


@app.errorhandler(413)
def too_large(_e):
    flash("Bestand te groot.", "warn")
    return redirect(request.referrer or url_for("index"))


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8100, debug=True)
