"""Maintenance tasks per printer, with an interval in print hours and/or days.

Print hours since the last maintenance = the sum of the print time of that printer's prints that
finished after `last_done_at` (finished_at, else printed_on, else created_at). Planned and running
prints do not count.
"""
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("PRINTVAULT_TZ", "Europe/Brussels"))

# Starting values for a Bambu Lab P2S; adjust them on the printer's maintenance page.
# (name, interval in print hours or None, interval in days or None)
DEFAULT_TASKS = [
    ("Clean build plate (water and dish soap)", 25, None),
    ("Inspect and clean nozzle", 100, None),
    ("Clean carbon rods / rails", 100, 30),
    ("Lubricate Z lead screws and guides", 200, 90),
    ("Replace exhaust filter (HEPA/activated carbon)", 500, 180),
]


def now_local():
    return datetime.now(TZ).replace(microsecond=0)


def parse_time(text):
    """ISO time from the database; without a time zone (e.g. created_at) it is UTC."""
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def seed_defaults(db, printer_id, now=None):
    now = (now or now_local()).isoformat()
    db.executemany("INSERT INTO maintenance_tasks (printer_id, name, interval_hours, interval_days, last_done_at) "
                   "VALUES (?, ?, ?, ?, ?)", [(printer_id, n, h, d, now) for n, h, d in DEFAULT_TASKS])


def print_minutes_since(db, printer_id, since):
    row = db.execute(
        "SELECT COALESCE(SUM(print_time_min), 0) FROM prints WHERE printer_id = ? "
        "AND status NOT IN ('planned', 'printing') "
        "AND datetime(COALESCE(finished_at, printed_on, created_at)) > datetime(?)",
        (printer_id, since)).fetchone()
    return row[0]


def task_status(db, task, now=None):
    """Status of a task: hours/days used, what is left, progress (0–1) and whether it is due."""
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
    """'312 of 200 print hours · 45 of 90 days'."""
    parts = []
    if task["interval_hours"]:
        parts.append(f"{st['hours']:.0f} of {task['interval_hours']:g} print hours")
    if task["interval_days"]:
        parts.append(f"{st['days']} of {task['interval_days']} days")
    return " · ".join(parts) or "no interval"


def tasks_for_printer(db, printer_id, now=None):
    rows = db.execute("SELECT * FROM maintenance_tasks WHERE printer_id = ? ORDER BY name", (printer_id,)).fetchall()
    out = [{**dict(r), "st": task_status(db, r, now)} for r in rows]
    for t in out:
        t["summary"] = describe(t, t["st"])
    return sorted(out, key=lambda t: (not t["st"]["due"], -t["st"]["progress"], t["name"]))


def check_and_notify(db_path, notifier, now=None):
    """Sends one notification each time a task becomes due. A task that is not (or no longer) due
    is re-armed so that it notifies again the next time it becomes due."""
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
                if notifier("maintenance", f"Maintenance: {t['name']}",
                            f"{t['printer_name']}: {describe(t, st)}", tags=["wrench"]):
                    db.execute("UPDATE maintenance_tasks SET notified_at = ? WHERE id = ?", (now.isoformat(), t["id"]))
                    sent += 1
            elif not st["due"] and t["notified_at"]:
                db.execute("UPDATE maintenance_tasks SET notified_at = NULL WHERE id = ?", (t["id"],))
    return sent
