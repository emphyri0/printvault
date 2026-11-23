"""Where the data is, and the database schema. Shared by the web app and the listener."""
import os
import sqlite3
from contextlib import closing

import bambu_models
import maintenance

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "printvault.db")

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
                 ("mqtt_enabled", "INTEGER DEFAULT 0"),
                 # bambu_models.MODELS value; camera_type only counts for 'other' (rtsps / tcp6000 / none)
                 ("bambu_model", "TEXT"), ("camera_type", "TEXT")],
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
        # Printers from before the model list: recognise the model in the free-text field
        # (e.g. 'Bambu Lab P2S' → p2s). Only where nothing was chosen yet.
        for pid, text in db.execute("SELECT id, model FROM printers WHERE bambu_model IS NULL").fetchall():
            guess = bambu_models.guess_model(text)
            if guess:
                db.execute("UPDATE printers SET bambu_model = ? WHERE id = ? AND bambu_model IS NULL", (guess, pid))
        # Once: default maintenance tasks for printers that already exist (and have no tasks).
        if db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance_seeded', '1')").rowcount:
            for (pid,) in db.execute("SELECT id FROM printers").fetchall():
                if not db.execute("SELECT 1 FROM maintenance_tasks WHERE printer_id = ?", (pid,)).fetchone():
                    maintenance.seed_defaults(db, pid)
