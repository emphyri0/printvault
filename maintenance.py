"""Onderhoudstaken per printer, met een interval in printuren en/of dagen.

Printuren sinds het laatste onderhoud = som van de printtijd van de prints van die printer die
na `last_done_at` klaar waren (finished_at, anders printed_on, anders created_at). Geplande en
lopende prints tellen niet mee.
"""
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("PRINTVAULT_TZ", "Europe/Brussels"))

# Startwaarden voor een Bambu Lab P2S; pas ze aan op de onderhoudspagina van de printer.
# (naam, interval in printuren of None, interval in dagen of None)
DEFAULT_TASKS = [
    ("Plaat reinigen (water en afwasmiddel)", 25, None),
    ("Nozzle controleren en reinigen", 100, None),
    ("Carbon rods / rails reinigen", 100, 30),
    ("Z-spindels en geleiders smeren", 200, 90),
    ("Exhaust-filter (HEPA/actieve kool) vervangen", 500, 180),
]


def now_local():
    return datetime.now(TZ).replace(microsecond=0)


def parse_time(text):
    """ISO-tijd uit de database; zonder tijdzone (bv. created_at) is het UTC."""
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def seed_defaults(db, printer_id, now=None):
    now = (now or now_local()).isoformat()
    db.executemany("INSERT INTO maintenance_tasks (printer_id, name, interval_hours, interval_days, last_done_at) "
                   "VALUES (?, ?, ?, ?, ?)", [(printer_id, n, h, d, now) for n, h, d in DEFAULT_TASKS])


def print_minutes_since(db, printer_id, since):
    row = db.execute(
        "SELECT COALESCE(SUM(print_time_min), 0) FROM prints WHERE printer_id = ? "
        "AND status NOT IN ('gepland', 'bezig') "
        "AND datetime(COALESCE(finished_at, printed_on, created_at)) > datetime(?)",
        (printer_id, since)).fetchone()
    return row[0]


def task_status(db, task, now=None):
    """Status van een taak: verbruikte uren/dagen, wat nog rest, voortgang (0–1) en of ze vervallen is."""
    now = now or now_local()
    since = task["last_done_at"] or task["created_at"]
    hours = print_minutes_since(db, task["printer_id"], since) / 60
    days = max(0, (now - parse_time(since)).days)
    st = {"hours": hours, "days": days, "due": False, "progress": 0.0, "hours_left": None, "days_left": None}
    if task["interval_hours"]:
        st["hours_left"] = task["interval_hours"] - hours
        st["progress"] = max(st["progress"], hours / task["interval_hours"])
    if task["interval_days"]:
        st["days_left"] = task["interval_days"] - days
        st["progress"] = max(st["progress"], days / task["interval_days"])
    st["due"] = (st["hours_left"] is not None and st["hours_left"] <= 0) or \
                (st["days_left"] is not None and st["days_left"] <= 0)
    st["progress"] = min(1.0, st["progress"])
    return st


def describe(task, st):
    """'312 van 200 printuren · 45 van 90 dagen'."""
    parts = []
    if task["interval_hours"]:
        parts.append(f"{st['hours']:.0f} van {task['interval_hours']:g} printuren")
    if task["interval_days"]:
        parts.append(f"{st['days']} van {task['interval_days']} dagen")
    return " · ".join(parts) or "geen interval"


def tasks_for_printer(db, printer_id, now=None):
    rows = db.execute("SELECT * FROM maintenance_tasks WHERE printer_id = ? ORDER BY name", (printer_id,)).fetchall()
    out = [{**dict(r), "st": task_status(db, r, now)} for r in rows]
    for t in out:
        t["summary"] = describe(t, t["st"])
    return sorted(out, key=lambda t: (not t["st"]["due"], -t["st"]["progress"], t["name"]))


def check_and_notify(db_path, notifier, now=None):
    """Stuurt één melding per keer dat een taak vervalt. Een taak die niet (meer) vervallen is,
    wordt opnieuw 'gewapend' zodat ze na de volgende keer weer een melding geeft."""
    if notifier is None:
        return 0
    now = now or now_local()
    sent = 0
    with closing(sqlite3.connect(db_path, timeout=30)) as db, db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT t.*, p.name AS printer_name FROM maintenance_tasks t "
                          "JOIN printers p ON p.id = t.printer_id").fetchall()
        for t in rows:
            st = task_status(db, t, now)
            if st["due"] and not t["notified_at"]:
                if notifier("maintenance", f"Onderhoud: {t['name']}",
                            f"{t['printer_name']}: {describe(t, st)}", tags=["wrench"]):
                    db.execute("UPDATE maintenance_tasks SET notified_at = ? WHERE id = ?", (now.isoformat(), t["id"]))
                    sent += 1
            elif not st["due"] and t["notified_at"]:
                db.execute("UPDATE maintenance_tasks SET notified_at = NULL WHERE id = ?", (t["id"],))
    return sent
