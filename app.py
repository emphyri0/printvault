import ipaddress
import json
import math
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import IntegerConverter
from werkzeug.utils import secure_filename

import bambu_errors
import bambu_models
import bambu_status
import camera
import icons
import maintenance
import makerworld
import notify
import security
import slicer
import thumbs
import uploads
from bambu_errors import error_code
from db import DB_PATH, UPLOAD_DIR, connect, init_db, insert, update

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
    **bambu_models.LABELS,
}

app = Flask(__name__)


class IdConverter(IntegerConverter):
    """Numbers in URLs; more than SQLite can store is simply not found."""

    def __init__(self, url_map, **options):
        super().__init__(url_map, **{"max": 2 ** 63 - 1, **options})


app.url_map.converters["int"] = IdConverter
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.urandom(32)
security.init_app(app, os.environ.get("ALLOWED_HOSTS"))
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "200")) * 1024 * 1024


def trusted_proxies(value):
    """TRUSTED_PROXIES → number of reverse proxies in front of the app (0 = off, the default)."""
    try:
        n = int(str(value or "0").strip())
    except ValueError:
        app.logger.warning("TRUSTED_PROXIES=%r is not a number; proxy headers are ignored", value)
        return 0
    return max(0, n)


def apply_proxy_fix(wsgi_app, n):
    """Behind n trusted reverse proxies: take the scheme and host from X-Forwarded-Proto and
    X-Forwarded-Host, so absolute links (e.g. for Bambu Studio) use the address the browser used.
    Only switch this on when every request passes through the proxy: otherwise anyone can fake them."""
    return ProxyFix(wsgi_app, x_for=0, x_proto=n, x_host=n, x_port=0, x_prefix=0) if n else wsgi_app


app.wsgi_app = apply_proxy_fix(app.wsgi_app, trusted_proxies(os.environ.get("TRUSTED_PROXIES")))

def get_db():
    if "db" not in g:
        g.db = connect(DB_PATH)
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def num(value, cast=float):
    """Number from a form or URL; None when it is empty, not a number or absurdly large."""
    value = (value or "").strip().replace(",", ".")
    if not value:
        return None
    try:
        f = float(value)
    except ValueError:
        return None
    return cast(f) if math.isfinite(f) and abs(f) <= 1e12 else None


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
        "notes": form.get("notes", "").replace("\r\n", "\n").strip(),
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


def form_base(p):
    """The values a print had when its edit form was opened (hidden field 'base')."""
    return json.dumps({k: p[k] for k in form_to_print({})})


def keep_changes(data, base, current):
    """Fields that were not touched in the form, but changed in the database since it was opened (the
    listener finished the print, for example), keep the value from the database."""
    def norm(v):
        return (v.replace("\r\n", "\n").strip() or None) if isinstance(v, str) else v
    try:
        base = json.loads(base or "{}")
    except ValueError:
        return
    if not isinstance(base, dict):
        return
    for k in data:
        if k in base and norm(data[k]) == norm(base[k]) and norm(current[k]) != norm(base[k]):
            data[k] = current[k]
    if data["status"] != "failed":
        data["fail_reason"] = data["fail_note"] = None


SLICER_FIELDS = {"material": "material", "color": "colour", "filament_g": "weight",
                 "print_time_min": "print time", "layer_height": "layer height", "nozzle": "nozzle",
                 "infill": "infill", "plate_type": "plate type", "nozzle_temp": "nozzle temperature",
                 "bed_temp": "bed temperature", "plate": "plate number"}


def drop_unknown_printer(data):
    """The printer may have been deleted while the form was open."""
    if data["printer_id"] is not None and not get_db().execute(
            "SELECT 1 FROM printers WHERE id = ?", (data["printer_id"],)).fetchone():
        data["printer_id"] = None
        flash("The chosen printer no longer exists; the print was saved without a printer.", "warn")


def print_name(filename):
    """'my_part.gcode.3mf' → 'my part'."""
    stem = re.sub(r"(\.gcode)?\.(3mf|gcode)$", "", filename, flags=re.I)
    return stem.replace("_", " ").strip()[:200] or "Untitled print"


def apply_slicer_file(print_id, path, original_name, plate_index=None, overwrite_name=False):
    """Reads a 3mf/gcode file and updates the print. Returns a short summary."""
    parsed = slicer.parse(path, path, plate_index)
    plates = parsed["plates"]
    plate_index = slicer.find_plate(plates, plate_index)["index"]
    fields, filaments, thumb = slicer.plate_to_print_fields(parsed, plate_index, MATERIALS)
    db = get_db()
    if overwrite_name:
        fields["name"] = (parsed.get("name") or "").strip()[:200] or print_name(original_name)
    if filaments:
        p = db.execute("SELECT notes FROM prints WHERE id = ?", (print_id,)).fetchone()
        line = "Filament (slicer): " + slicer.filament_summary(filaments)
        notes = [l for l in (p["notes"] or "").splitlines() if not l.startswith("Filament (slicer):")]
        fields["notes"] = "\n".join([line] + notes).strip()
    if fields:
        update(db, "prints", print_id, fields)
    if thumb:
        content, ext = thumb
        thumb_name = f"thumbnail-plate-{plate_index}.{ext}"
        exists = db.execute("SELECT 1 FROM files WHERE print_id = ? AND original_name = ?",
                            (print_id, thumb_name)).fetchone()
        if not exists:
            uploads.add_photo(db, print_id, content, thumb_name)
    db.commit()
    found = [label for k, label in SLICER_FIELDS.items() if fields.get(k) not in (None, "")]
    plate_info = (f" This file has {len(plates)} plates; plate {plate_index} was used."
                  if len(plates) > 1 else "")
    return found, plate_info


def save_uploads(print_id, files):
    skipped = uploads.save(get_db(), print_id, files)
    if skipped:
        flash("Skipped (unknown file type or not a real photo): " + ", ".join(skipped), "warn")


# Automatic thumbnails (from the slicer or MakerWorld); everything else is a real photo.
THUMB_SQL = "(f.original_name LIKE 'thumbnail-plate-%' OR f.original_name LIKE 'makerworld-cover.%')"
# Cover photo: the chosen photo, else the first real photo, else a slicer/MakerWorld thumbnail.
COVER_SQL = f"""COALESCE(
    (SELECT f.stored_name FROM files f WHERE f.id = p.cover_file_id AND f.print_id = p.id AND f.kind = 'photo'),
    (SELECT f.stored_name FROM files f WHERE f.print_id = p.id AND f.kind = 'photo'
       ORDER BY {THUMB_SQL}, f.id
       LIMIT 1))"""
# Whether the printer (alias pr) has a camera; same rules as bambu_models.printer_camera
HAS_CAMERA_SQL = """(COALESCE(pr.ip, '') != '' AND COALESCE(pr.access_code, '') != ''
    AND NOT (COALESCE(pr.bambu_model, '') = 'other' AND COALESCE(pr.camera_type, '') NOT IN ('rtsps', 'tcp6000')))"""

# Colours of the AMS slots a print used (comma-separated, may be NULL)
AMS_COLORS_SQL = "(SELECT GROUP_CONCAT(a.tray_color) FROM print_ams a WHERE a.print_id = p.id) AS ams_colors"

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


app.add_template_filter(bambu_status.ams_name, "ams_name")


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
    models = {"bambu_models": bambu_models.MODELS, "camera_types": bambu_models.CAMERA_TYPES}
    try:
        db = get_db()
        printers = db.execute("SELECT id, name FROM printers ORDER BY name").fetchall()
        due = sum(maintenance_due(db, pr["id"]) for pr in printers)
    except sqlite3.Error as e:   # the error page is rendered with this too
        app.logger.error("page rendered without the database: %s", e)
        printers, due = [], 0
    return {"all_printers": printers, "materials": MATERIALS, "statuses": STATUSES,
            "fail_reasons": FAIL_REASONS, "speed_profiles": SPEED_PROFILES, "labels": LABELS,
            "status_icons": icons.STATUS_ICONS, "maintenance_due": due, **models}


def maintenance_due(db, printer_id):
    return sum(t["st"]["due"] for t in maintenance.tasks_for_printer(db, printer_id))


app.add_template_global(icons.icon, "icon")
app.add_template_global(icons.sprite, "icon_sprite")


@app.template_global()
def static_v(filename):
    """Static URL with the file's modification time, so a new version is never served from the browser cache."""
    try:
        v = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
    except OSError:
        v = 0
    return url_for("static", filename=filename, v=v)


HEX_COLOR = re.compile(r"^#?([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?$")
NAMED_COLOR = re.compile(r"^[a-zA-Z]{3,20}$")


def css_color(value):
    """A colour that is safe inside a style attribute ('#rrggbb' or a plain colour word), else None."""
    value = (value or "").strip()
    m = HEX_COLOR.match(value)
    if m:
        return "#" + m.group(1).lower()
    return value.lower() if NAMED_COLOR.match(value) else None


app.add_template_filter(css_color, "css_color")


@app.template_filter("swatches")
def swatches(p):
    """Filament colours of a print: the AMS slots used (if known), else the colour field. Duplicates removed."""
    raw = (p["ams_colors"] or "").split(",") if "ams_colors" in p.keys() and p["ams_colors"] else []
    if not raw and p["color"]:
        raw = [p["color"]]
    out = []
    for c in raw:
        c = css_color(c)
        if c and c not in out:
            out.append(c)
    return out


@app.template_global()
def filter_url(key=None, value=None, **keep):
    """URL of the overview with one filter switched on, or off when it is already on (chips).
    key=None: clear all filters but keep the search term."""
    args = {k: v for k, v in request.args.items() if v}
    if key is None:
        args = {"q": args["q"]} if args.get("q") else {}
    elif args.get(key) == str(value):
        args.pop(key)
    else:
        args[key] = str(value)
    if args.get("status") != "failed":
        args.pop("fail", None)
    args.update({k: v for k, v in keep.items() if v})
    return url_for("index", **args)


@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    material = request.args.get("material", "")
    status = request.args.get("status", "")
    printer = num(request.args.get("printer"), int)
    fail = request.args.get("fail", "")
    nozzle = num(request.args.get("nozzle"))
    sql = f"""SELECT p.*, pr.name AS printer_name, {COVER_SQL} AS cover, {OWN_COVER_SQL} AS own_cover,
                    {HAS_CAMERA_SQL} AS has_camera, {AMS_COLORS_SQL}
             FROM prints p LEFT JOIN printers pr ON pr.id = p.printer_id WHERE 1=1"""
    args = []
    if printer:
        sql += " AND p.printer_id = ?"
        args.append(printer)
    if q:
        fields = ("name", "tags", "description", "notes")
        sql += " AND (" + " OR ".join(rf"p.{f} LIKE ? ESCAPE '\'" for f in fields) + ")"
        args += ["%" + re.sub(r"([\\%_])", r"\\\1", q) + "%"] * len(fields)
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
                  COALESCE(SUM(print_time_min), 0) AS minutes
           FROM prints"""
    ).fetchone()
    nozzles = [r[0] for r in db.execute("SELECT DISTINCT nozzle FROM prints WHERE nozzle > 0 ORDER BY nozzle")]
    counts = {
        "status": dict(db.execute("SELECT status, COUNT(*) FROM prints GROUP BY status").fetchall()),
        "material": dict(db.execute("SELECT material, COUNT(*) FROM prints WHERE material != '' GROUP BY material")
                         .fetchall()),
        "fail": dict(db.execute("SELECT fail_reason, COUNT(*) FROM prints WHERE status = 'failed' "
                                "AND COALESCE(fail_reason, '') != '' GROUP BY fail_reason").fetchall()),
    }
    filtered = bool(q or material or status or printer or fail or nozzle)
    # The two most recently finished prints, next to the live status (only on the unfiltered overview)
    finished = [] if filtered else db.execute(
        f"""SELECT p.*, pr.name AS printer_name, {COVER_SQL} AS cover, {AMS_COLORS_SQL}
            FROM prints p LEFT JOIN printers pr ON pr.id = p.printer_id
            WHERE p.status IN ('succeeded', 'failed')
            ORDER BY COALESCE(p.finished_at, p.printed_on, p.created_at) DESC, p.id DESC LIMIT 2""").fetchall()
    return render_template("index.html", prints=prints, stats=stats, q=q, material=material, status=status,
                           printer=printer, fail=fail, nozzle=nozzle, nozzles=nozzles, counts=counts,
                           filtered=filtered, finished=finished)


@app.route("/print/new", methods=["GET", "POST"])
def new_print():
    if request.method == "POST":
        data = form_to_print(request.form)
        if not data["name"]:
            flash("Give the print a name.", "warn")
            return render_template("form.html", p=data)
        drop_unknown_printer(data)
        db = get_db()
        print_id = insert(db, "prints", data)
        db.commit()
        save_uploads(print_id, request.files.getlist("files"))
        flash("Print saved.", "ok")
        return redirect(url_for("show_print", print_id=print_id))
    defaults = {"status": "succeeded", "printed_on": date.today().isoformat()}
    only = get_db().execute("SELECT id FROM printers").fetchall()
    if len(only) == 1:
        defaults["printer_id"] = only[0]["id"]
    return render_template("form.html", p=defaults)


@app.route("/print/import", methods=["POST"])
def import_print():
    url = request.form.get("url", "").strip()
    try:
        data, cover = makerworld.fetch(url, MATERIALS)
    except makerworld.MakerWorldError as e:
        flash(str(e), "warn")
        return redirect(url_for("new_print"))
    db = get_db()
    print_id = insert(db, "prints", data)
    if cover:
        add_cover(db, print_id, cover)
    db.commit()
    flash("Imported from MakerWorld. Fill in what is missing and upload the 3mf file if you have it.", "ok")
    return redirect(url_for("edit_print", print_id=print_id))


@app.route("/print/import-file", methods=["POST"])
def import_file():
    upload = request.files.get("file")
    if not upload or not upload.filename or uploads.ext_of(upload.filename) not in {"3mf", "gcode"}:
        flash("Choose a .3mf or .gcode file.", "warn")
        return redirect(url_for("new_print"))
    db = get_db()
    cur = db.execute("INSERT INTO prints (name, status) VALUES (?, 'planned')", (print_name(uploads.clean_name(upload.filename, uploads.ext_of(upload.filename))),))
    print_id = cur.lastrowid
    save_uploads(print_id, [upload])
    f = db.execute("SELECT * FROM files WHERE print_id = ? ORDER BY id DESC LIMIT 1", (print_id,)).fetchone()
    try:
        found, plate_info = apply_slicer_file(print_id, os.path.join(UPLOAD_DIR, f["stored_name"]),
                                              f["original_name"], overwrite_name=True)
    except slicer.ParseError as e:
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
    if f is None or uploads.ext_of(f["stored_name"]) not in {"3mf", "gcode"}:
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
    models = [f for f in files if f["kind"] == "model"]
    threemf = [f for f in models if uploads.ext_of(f["stored_name"]) == "3mf"]
    return render_template("detail.html", p=p, printer=printer, cover=cover, ams=ams, samples=samples,
                           photos=[f for f in files if f["kind"] == "photo"], models=models,
                           studio=bambu_studio_links(threemf[-1]) if threemf else None,
                           makerworld_url=p["source_url"] if makerworld.URL.match(p["source_url"] or "") else None)


def bambu_studio_links(f):
    """Links that open a 3MF file in Bambu Studio, which then downloads it from Printvault itself.

    The schemes come from Bambu Studio's own source (src/slic3r/GUI/GUI_App.cpp):
      - Windows/Linux: bambustudio://open?file=<url>   (GUI_App::post_init, extract_model_download_url_from_open)
      - macOS:         bambustudioopen://<url>          (GUI_App::MacOpenURL, extract_model_download_url_from_mac)
    Bambu Studio URL-decodes the whole link and downloads the URL with http(s). The file name comes from a
    '&name=<file>.3mf' suffix (Plater::import_model_id) and must end in .3mf; /download/<id> has no
    extension, so the name is always added. Hosts other than MakerWorld's get a "not from a trusted site,
    open anyway?" question in Bambu Studio. Nothing is sent to the printer.
    """
    name = secure_filename(f["original_name"] or "")
    if not name.lower().endswith(".3mf"):
        name = f"printvault-{f['id']}.3mf"
    target = urllib.parse.quote(f"{url_for('download', file_id=f['id'], _external=True)}&name={name}", safe="")
    return {"file": f, "download": url_for("download", file_id=f["id"]),
            "open": f"bambustudio://open?file={target}", "mac": f"bambustudioopen://{target}"}


@app.route("/print/<int:print_id>/edit", methods=["GET", "POST"])
def edit_print(print_id):
    p = get_print(print_id)
    if request.method == "POST":
        data = form_to_print(request.form)
        if not data["name"]:
            flash("Give the print a name.", "warn")
            return render_template("form.html", p={**data, "id": print_id}, base=request.form.get("base"))
        keep_changes(data, request.form.get("base"), p)
        drop_unknown_printer(data)
        db = get_db()
        mw_msg = enrich_from_makerworld(db, print_id, p, data)
        update(db, "prints", print_id, data)
        db.commit()
        save_uploads(print_id, request.files.getlist("files"))
        flash("Changes saved.", "ok")
        if mw_msg:
            flash(*mw_msg)
        return redirect(url_for("show_print", print_id=print_id))
    return render_template("form.html", p=p, base=form_base(p))


@app.route("/print/<int:print_id>/delete", methods=["POST"])
def delete_print(print_id):
    get_print(print_id)
    db = get_db()
    files = db.execute("SELECT stored_name FROM files WHERE print_id = ?", (print_id,)).fetchall()
    db.execute("DELETE FROM prints WHERE id = ?", (print_id,))
    db.commit()
    for f in files:   # only once the database change is certain
        uploads.remove(f["stored_name"])
    flash("Print deleted.", "ok")
    return redirect(url_for("index"))


@app.route("/print/<int:print_id>/photos", methods=["POST"])
def add_photos(print_id):
    get_print(print_id)
    chosen = [f for f in request.files.getlist("photos") if f and f.filename]
    photos = [f for f in chosen if uploads.ext_of(f.filename) in uploads.PHOTO_EXT]
    if len(photos) < len(chosen):
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
    db.execute("DELETE FROM files WHERE id = ?", (file_id,))
    db.commit()
    uploads.remove(f["stored_name"])
    return redirect(url_for("show_print", print_id=f["print_id"]))


@app.route("/uploads/<path:stored_name>")
def uploaded(stored_name):
    return send_from_directory(UPLOAD_DIR, stored_name)


@app.route("/thumb/<int:size>/<stored_name>")
def thumb(size, stored_name):
    """WebP thumbnail of a photo; the original while the thumbnail does not exist (yet)."""
    if size not in thumbs.SIZES or not thumbs.is_photo(stored_name):
        abort(404)
    if os.path.exists(thumbs.thumb_path(UPLOAD_DIR, stored_name, size)):
        return send_from_directory(os.path.join(UPLOAD_DIR, thumbs.SUBDIR), thumbs.thumb_name(stored_name, size),
                                   mimetype="image/webp", max_age=30 * 86400)
    if not os.path.isfile(os.path.join(UPLOAD_DIR, stored_name)):
        abort(404)
    uploads.THUMBS.add(stored_name)
    resp = send_from_directory(UPLOAD_DIR, stored_name)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.template_global()
def thumb_url(stored_name, size=600):
    """URL of a photo at card (600) or detail (1600) size. A GIF at detail size stays the original
    (the animation is only shown there)."""
    if not stored_name:
        return None
    if size > 600 and stored_name.lower().endswith(".gif"):
        return url_for("uploaded", stored_name=stored_name)
    return url_for("thumb", size=size, stored_name=stored_name)


@app.route("/download/<int:file_id>")
def download(file_id):
    f = get_db().execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if f is None:
        abort(404)
    return send_from_directory(UPLOAD_DIR, f["stored_name"], as_attachment=True,
                               download_name=uploads.download_name(f))


# ---------- Printers ----------

HOST_LABEL = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def valid_address(text):
    """An IPv4 address or a host name."""
    labels = text.split(".")
    if len(text) > 253 or not all(HOST_LABEL.fullmatch(p) for p in labels):
        return False
    if not labels[-1].isdigit():
        return True
    try:   # a name cannot end in a number, so this has to be an address
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


def printer_errors(data):
    if not data["name"]:
        return "Give the printer a name."
    if data["ip"] and not valid_address(data["ip"]):
        return f"'{data['ip'][:60]}' is not a valid IP address or host name. Nothing was saved."
    return None


def printer_from_form(form):
    return {
        "name": form.get("name", "").strip(),
        "model": form.get("model", "").strip(),
        "bambu_model": form.get("bambu_model") if form.get("bambu_model") in bambu_models.MODELS else None,
        # only used for model 'other'; the other models have a fixed camera route
        "camera_type": (form.get("camera_type") if form.get("bambu_model") == "other"
                        and form.get("camera_type") in bambu_models.CAMERA_TYPES else None),
        "notes": form.get("notes", "").strip(),
        "ip": form.get("ip", "").strip(),
        "serial": re.sub(r"[^A-Za-z0-9]", "", form.get("serial", "")),
        # left empty = keep the current access code (it is never put back into the form)
        "access_code": form.get("access_code", "").strip(),
        "mqtt_enabled": 1 if form.get("mqtt_enabled") else 0,
    }


def printer_photo(upload):
    """stored_name of an uploaded printer photo, or None."""
    if not upload or not upload.filename:
        return None
    stored = uploads.save_printer_photo(upload)
    if stored is None:
        flash("Printer photo skipped: only jpg, png, webp or gif.", "warn")
    return stored


def public_printer(row_or_dict):
    """Printer data for the form, without the access code."""
    d = dict(row_or_dict)
    d["has_access_code"] = bool(d.pop("access_code", None))
    return d


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
    due = {pr["id"]: maintenance_due(db, pr["id"]) for pr in rows}
    cams = {pr["id"]: bambu_models.printer_camera(pr) for pr in rows}
    return render_template("printers.html", printers=rows, due=due, cams=cams)


@app.route("/printers/new", methods=["GET", "POST"])
def new_printer():
    if request.method == "POST":
        data = printer_from_form(request.form)
        error = printer_errors(data)
        if error:
            flash(error, "warn")
            return render_template("printer_form.html", pr=public_printer(data))
        data["photo"] = printer_photo(request.files.get("photo"))
        data["access_code"] = data["access_code"] or None
        db = get_db()
        insert(db, "printers", data)
        db.commit()
        flash("Printer added.", "ok")
        return redirect(url_for("printers"))
    return render_template("printer_form.html", pr={})


@app.route("/printers/<int:printer_id>/edit", methods=["GET", "POST"])
def edit_printer(printer_id):
    db = get_db()
    pr = get_printer(printer_id)
    if request.method == "POST":
        data = printer_from_form(request.form)
        error = printer_errors(data)
        if error:
            flash(error, "warn")
            return render_template("printer_form.html", pr=public_printer({**data, "id": printer_id, "photo": pr["photo"]}))
        if pr["access_code"] and data["ip"] and data["ip"] != pr["ip"] and not data["access_code"]:
            # otherwise the stored code would be sent to whatever answers at the new address
            flash("The IP address changed: enter the access code again. Nothing was saved.", "warn")
            return render_template("printer_form.html", pr=public_printer(
                {**data, "id": printer_id, "photo": pr["photo"], "access_code": None}))
        photo = pr["photo"]
        new_photo = printer_photo(request.files.get("photo"))
        if new_photo or request.form.get("remove_photo"):
            photo = new_photo
        if not data["access_code"]:
            data["access_code"] = pr["access_code"]
        data["photo"] = photo
        update(db, "printers", printer_id, data)
        db.commit()
        if photo != pr["photo"]:
            uploads.remove(pr["photo"])
        flash("Printer saved.", "ok")
        return redirect(url_for("printers"))
    return render_template("printer_form.html", pr=public_printer(pr))


@app.route("/printers/<int:printer_id>/delete", methods=["POST"])
def delete_printer(printer_id):
    db = get_db()
    pr = get_printer(printer_id)
    db.execute("UPDATE prints SET printer_id = NULL WHERE printer_id = ?", (printer_id,))
    db.execute("DELETE FROM printers WHERE id = ?", (printer_id,))
    db.commit()
    uploads.remove(pr["photo"])
    flash("Printer deleted. Its prints are kept.", "ok")
    return redirect(url_for("printers"))


# ---------- Camera ----------

cameras = camera.CameraManager()

# Snapshots are kept ~20 s per printer (a failed attempt too), so that several open tabs
# together make at most one connection to the printer every 20 s.
SNAPSHOT_CACHE_SECONDS = 20
# Each live stream keeps one of gunicorn's 8 threads (see Dockerfile) busy; leave some for the pages.
MAX_STREAMS = 4
_streams = 0
_streams_lock = threading.Lock()
_snapshots = {}                   # printer_id -> (rtsp url, time, jpeg or CameraError)
_snapshot_locks = {}
_snapshot_locks_guard = threading.Lock()


def cached_snapshot(printer_id, ip, access_code, route=bambu_models.RTSPS):
    """JPEG from the printer: a fresh frame from a running live stream, else from the cache or fetched anew.
    Raises CameraError (also a cached one) when there is no image."""
    url = (route, camera.rtsp_url(ip, access_code))
    cam = cameras.peek(printer_id)
    if cam is not None and cam.key == url:
        jpeg = cam.latest()
        if jpeg is not None:
            return jpeg
    with _snapshot_locks_guard:
        lock = _snapshot_locks.setdefault(printer_id, threading.Lock())
    with lock:   # concurrent requests wait for a single fetch
        hit = _snapshots.get(printer_id)
        if hit is None or hit[0] != url or time.monotonic() - hit[1] >= SNAPSHOT_CACHE_SECONDS:
            try:
                result = camera.grab_snapshot(ip, access_code, route=route)
            except camera.CameraError as e:
                result = e
            hit = _snapshots[printer_id] = (url, time.monotonic(), result)
    if isinstance(hit[2], camera.CameraError):
        raise hit[2]
    return hit[2]


def camera_printer(printer_id):
    """(printer, camera route); 404 when the printer has no camera."""
    pr = get_printer(printer_id)
    route = bambu_models.printer_camera(pr)
    if route is None:
        abort(404)
    return pr, route


@app.route("/printers/<int:printer_id>/camera.mjpg")
def camera_stream(printer_id):
    global _streams
    pr, route = camera_printer(printer_id)
    with _streams_lock:
        if _streams >= MAX_STREAMS:
            return Response("Too many live videos are open; close one and try again.", status=503, mimetype="text/plain",
                            headers={"Cache-Control": "no-store"})
        _streams += 1
    cam = cameras.get(printer_id, pr["ip"], pr["access_code"], route)
    released = False

    def release():   # may be called more than once
        global _streams
        nonlocal released
        with _streams_lock:
            if not released:
                released = True
                _streams -= 1

    def generate():
        for frame in cam.frames():
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(frame)
                   + frame + b"\r\n")

    resp = Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    resp.call_on_close(release)   # also when the stream was never read (HEAD request, client gone)
    return resp


@app.route("/printers/<int:printer_id>/snapshot.jpg")
def camera_snapshot(printer_id):
    pr, route = camera_printer(printer_id)
    try:
        jpeg = cached_snapshot(printer_id, pr["ip"], pr["access_code"], route)
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
            insert(db, "maintenance_tasks", data)
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
        update(db, "maintenance_tasks", task_id, data)
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
        elif cfg.get("ntfy_token") and notify.server_of(values.get("ntfy_url", cfg.get("ntfy_url"))) \
                != notify.server_of(cfg.get("ntfy_url")):
            values["ntfy_token"] = ""   # never send the stored token to another server
            problems.append("The ntfy server changed, so the access token was cleared. Enter it again if the new "
                            "server needs one.")
        for key in notify.KINDS.values():
            values[key] = "1" if f.get(key) else "0"
        notify.save_settings(db, values)
        db.commit()
        for p in problems:
            flash(p, "warn")
        flash("Settings saved.",
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
                p["cover"], p["own_cover"] = thumb_url(row["cover"], 1600), bool(row["own"])
        pr = db.execute("SELECT * FROM printers WHERE id = ?", (p.get("printer_id"),)).fetchone()
        p["printer_photo"] = thumb_url(pr["photo"], 1600) if pr and pr["photo"] else None
        p["camera"] = bool(pr and bambu_models.printer_camera(pr))
        p["model"] = pr["bambu_model"] if pr else None
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

# Fields filled in from MakerWorld when editing, if they are still empty.
# Status, date, print time and printer come from the printer itself and are left alone.
MW_FILL_FIELDS = ("description", "material", "color", "filament_g", "layer_height", "tags", "designer", "license")


def add_cover(db, print_id, cover):
    content, ext = cover
    uploads.add_photo(db, print_id, content, f"makerworld-cover.{ext}")


def enrich_from_makerworld(db, print_id, old, data):
    """Completes `data` with MakerWorld data when a new MakerWorld link was given, or when an earlier
    attempt failed (neither a cover nor a designer yet). Returns a flash message or None."""
    url = data.get("source_url") or ""
    new, known = makerworld.URL.match(url), makerworld.URL.match(old["source_url"] or "")
    if not new:
        return None
    has_cover = db.execute("SELECT 1 FROM files WHERE print_id = ? AND original_name LIKE 'makerworld-cover.%'",
                           (print_id,)).fetchone()
    if known and known.group(2) == new.group(2) and (has_cover or old["designer"]):
        return None
    try:
        mw, cover = makerworld.fetch(url, MATERIALS)
    except makerworld.MakerWorldError as e:
        return (f"Link saved, but fetching the MakerWorld data failed: {e}", "warn")
    filled = [k for k in MW_FILL_FIELDS if not data.get(k) and mw.get(k)]
    for k in filled:
        data[k] = mw[k]
    if mw.get("notes") and mw["notes"] not in (data.get("notes") or ""):
        data["notes"] = f"{data['notes']}\n\n{mw['notes']}".strip() if data.get("notes") else mw["notes"]
    data["source_url"] = mw["source_url"]
    if cover and not has_cover:
        add_cover(db, print_id, cover)
    return ("MakerWorld data filled in" + (" (with image)." if cover and not has_cover else "."), "ok")


@app.route("/maintenance")
def maintenance_overview():
    """Sidebar link: the maintenance page of the only printer, else the printer list."""
    rows = get_db().execute("SELECT id FROM printers ORDER BY name").fetchall()
    if len(rows) == 1:
        return redirect(url_for("printer_maintenance", printer_id=rows[0]["id"]))
    return redirect(url_for("printers"))


ERRORS = {400: ("Bad request", "The request was not understood."),
          404: ("Not found", "This page or file does not exist (any more)."),
          405: ("Not allowed", "This address does not accept this kind of request."),
          500: ("Something went wrong", "An unexpected error occurred. It has been logged; try again in a moment.")}


def error_page(e):
    code = getattr(e, "code", 500) or 500
    title, text = ERRORS.get(code, ERRORS[500])
    return render_template("error.html", code=code, title=title, text=text), code


for _code in (400, 404, 405, 500):
    app.register_error_handler(_code, error_page)


@app.errorhandler(413)
def too_large(_e):
    flash("File too large.", "warn")
    return redirect(request.referrer or url_for("index"))


init_db()
uploads.THUMBS.backfill()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8100)
