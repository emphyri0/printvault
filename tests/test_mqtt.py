"""Tests voor mqtt_listener.PrinterTracker met nagebootste Bambu-berichten (geen netwerk).

Draaien:  python3 -m unittest discover -s tests
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_tmp = tempfile.mkdtemp()
os.environ["DATA_DIR"] = _tmp
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402
import mqtt_listener as ml  # noqa: E402

TZ = ZoneInfo("Europe/Brussels")


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 18, 19, 0, tzinfo=TZ)

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += timedelta(minutes=minutes)


def report(state, name="Bed_scraper", task="1001", percent=0, error=0, **extra):
    p = {"command": "push_status", "gcode_state": state, "subtask_name": name, "task_id": task,
         "mc_percent": percent, "mc_remaining_time": 36, "layer_num": 12, "total_layer_num": 37,
         "print_error": error,
         "ams": {"ams": [{"id": "0", "tray": [
             {"id": "0", "tray_type": "PLA", "tray_color": "161616FF", "remain": -1},
             {"id": "1", "tray_type": "PETG", "tray_color": "F95D73FF", "remain": -1},
             {"id": "2"},  # leeg slot
             {"id": "3", "tray_type": "PLA", "tray_color": "url(x)", "remain": -1}]}]}}
    p.update(extra)
    return {"print": p}


class TrackerTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(app.DB_PATH):
            os.remove(app.DB_PATH)
        app.init_db()
        with ml.connect_db(app.DB_PATH) as db:
            self.printer_id = db.execute("INSERT INTO printers (name) VALUES ('P2S')").lastrowid
        self.clock = Clock()

    def tracker(self):
        return ml.PrinterTracker(self.printer_id, "P2S", app.DB_PATH, clock=self.clock)

    def prints(self):
        with ml.connect_db(app.DB_PATH) as db:
            return db.execute("SELECT * FROM prints ORDER BY id").fetchall()

    def ams(self, print_id):
        with ml.connect_db(app.DB_PATH) as db:
            rows = db.execute("SELECT ams_id, tray_id, tray_type, tray_color FROM print_ams WHERE print_id = ? "
                              "ORDER BY tray_id", (print_id,)).fetchall()
        return [tuple(r) for r in rows]

    def test_full_print_success(self):
        t = self.tracker()
        t.handle(report("IDLE"))
        self.assertEqual(self.prints(), [])
        t.handle(report("PREPARE"))
        for _ in range(5):                       # herhaalde volledige status: geen effect
            t.handle(report("RUNNING", percent=40))
        rows = self.prints()
        self.assertEqual(len(rows), 1)
        p = rows[0]
        self.assertEqual((p["status"], p["name"], p["printer_id"], p["source"]), ("bezig", "Bed_scraper", self.printer_id, "mqtt"))
        self.assertEqual(p["started_at"], "2026-11-18T19:00:00+01:00")
        self.assertEqual(p["printed_on"], "2026-11-18")
        self.assertEqual(p["notes"], "")         # start live gezien
        self.assertEqual(self.ams(p["id"]), [("0", "0", "PLA", "161616FF"), ("0", "1", "PETG", "F95D73FF"),
                                            ("0", "3", "PLA", "")])
        self.clock.advance(83)
        t.handle(report("FINISH", percent=100))
        t.handle(report("IDLE"))
        p = self.prints()[0]
        self.assertEqual((p["status"], p["print_time_min"]), ("gelukt", 83))
        self.assertEqual(p["finished_at"], "2026-11-18T20:23:00+01:00")

    def test_failed_keeps_error(self):
        t = self.tracker()
        t.handle(report("RUNNING"))
        self.clock.advance(10)
        t.handle(report("FAILED", error=0x07008011))
        p = self.prints()[0]
        self.assertEqual((p["status"], p["print_error"], p["fail_note"]), ("mislukt", 0x07008011, "Printerfout 0700-8011"))
        self.assertEqual(p["print_time_min"], 10)

    def test_cancelled(self):
        t = self.tracker()
        t.handle(report("RUNNING"))
        t.handle(report("FAILED", error=ml.CANCELLED))
        self.assertEqual(self.prints()[0]["fail_note"], "Geannuleerd op de printer.")

    def test_restart_resumes_without_duplicate(self):
        t = self.tracker()
        t.handle(report("PREPARE"))
        t.handle(report("RUNNING"))
        self.clock.advance(30)
        t2 = self.tracker()                      # listener herstart
        t2.handle(report("RUNNING", percent=60))
        t2.handle(report("RUNNING", percent=61))
        rows = self.prints()
        self.assertEqual(len(rows), 1)
        self.assertEqual(t2.print_id, rows[0]["id"])
        self.clock.advance(30)
        t2.handle(report("FINISH"))
        p = self.prints()[0]
        self.assertEqual((p["status"], p["print_time_min"]), ("gelukt", 60))

    def test_restart_after_finish_closes_open_record(self):
        t = self.tracker()
        t.handle(report("RUNNING"))
        self.clock.advance(45)
        t2 = self.tracker()                      # listener was weg tijdens het einde
        t2.handle(report("FINISH"))
        p = self.prints()[0]
        self.assertEqual((p["status"], p["print_time_min"]), ("gelukt", 45))
        self.assertIn("Einde niet live gezien", p["notes"])

    def test_listener_starts_mid_print(self):
        t = self.tracker()
        t.handle(report("RUNNING", percent=39))
        p = self.prints()[0]
        self.assertEqual(p["status"], "bezig")
        self.assertIn("Start niet live gezien", p["notes"])

    def test_other_task_marks_stale_record(self):
        t = self.tracker()
        t.handle(report("RUNNING", task="1"))
        t2 = self.tracker()
        t2.handle(report("RUNNING", task="2", name="Ander"))
        rows = self.prints()
        self.assertEqual([r["status"] for r in rows], ["onbekend", "bezig"])
        self.assertEqual(rows[1]["name"], "Ander")

    def test_new_task_while_connected(self):
        t = self.tracker()
        t.handle(report("RUNNING", task="1"))
        t.handle(report("RUNNING", task="2", name="Volgende"))   # FINISH gemist
        rows = self.prints()
        self.assertEqual([r["status"] for r in rows], ["onbekend", "bezig"])

    def test_partial_messages_are_merged(self):
        t = self.tracker()
        t.handle(report("RUNNING", percent=10))
        t.handle({"print": {"mc_percent": 55, "layer_num": 20}})   # deelbericht zonder gcode_state
        live = t.live()
        self.assertEqual((live["gcode_state"], live["percent"], live["layer"], live["total_layers"]),
                         ("RUNNING", 55, 20, 37))
        self.assertEqual(len(self.prints()), 1)
        t.handle({"print": {"gcode_state": "FINISH"}})
        self.assertEqual(self.prints()[0]["status"], "gelukt")

    def test_ignores_non_report(self):
        t = self.tracker()
        t.handle({"info": {"command": "get_version"}})
        self.assertIsNone(t.gcode_state)

    def test_two_printers_independent(self):
        with ml.connect_db(app.DB_PATH) as db:
            other = db.execute("INSERT INTO printers (name) VALUES ('X1C')").lastrowid
        a = self.tracker()
        b = ml.PrinterTracker(other, "X1C", app.DB_PATH, clock=self.clock)
        a.handle(report("RUNNING", task="1"))
        b.handle(report("RUNNING", task="9"))
        self.assertEqual([r["status"] for r in self.prints()], ["bezig", "bezig"])

    def test_supervisor_only_complete_configs(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("UPDATE printers SET ip='10.0.0.2', serial='ABC', access_code='x', mqtt_enabled=1")
            db.execute("INSERT INTO printers (name, ip, serial, mqtt_enabled) VALUES ('zonder code', '10.0.0.3', 'D', 1)")
        self.assertEqual(list(ml.Supervisor(app.DB_PATH).configs()), [self.printer_id])


class AppTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(app.DB_PATH):
            os.remove(app.DB_PATH)
        app.init_db()
        self.c = app.app.test_client()

    def test_access_code_not_rendered_and_kept(self):
        self.c.post("/printers/new", data={"name": "P2S", "ip": "192.0.2.10", "serial": "00M00A 000000000",
                                           "access_code": "geheim12", "mqtt_enabled": "1"})
        html = self.c.get("/printers/1/edit").get_data(as_text=True)
        self.assertNotIn("geheim12", html)
        self.assertIn("ingesteld", html)
        self.c.post("/printers/1/edit", data={"name": "P2S", "ip": "192.0.2.10", "serial": "00M00A000000000",
                                              "access_code": "", "mqtt_enabled": "1"})
        with ml.connect_db(app.DB_PATH) as db:
            row = db.execute("SELECT serial, access_code, mqtt_enabled FROM printers").fetchone()
        self.assertEqual(tuple(row), ("00M00A000000000", "geheim12", 1))

    def test_migrates_listener_opnieuw_to_onbekend(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.executemany("INSERT INTO prints (name, status, source, notes) VALUES (?, ?, ?, ?)", [
                ("gemist", "opnieuw", "mqtt", "Einde niet waargenomen (listener was niet verbonden). Controleer de status zelf."),
                ("bewust", "opnieuw", None, "Kleur klopte niet"),
                ("mqtt bewust", "opnieuw", "mqtt", "Handmatig op opnieuw gezet")])
        app.init_db()
        app.init_db()                             # idempotent
        with ml.connect_db(app.DB_PATH) as db:
            rows = db.execute("SELECT name, status FROM prints ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows],
                         [("gemist", "onbekend"), ("bewust", "opnieuw"), ("mqtt bewust", "opnieuw")])

    def test_status_filter_has_onbekend(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO prints (name, status) VALUES ('a', 'onbekend'), ('b', 'gelukt')")
        html = self.c.get("/?status=onbekend").get_data(as_text=True)
        self.assertIn("<option selected>onbekend</option>", html)
        self.assertIn(">a<", html)
        self.assertNotIn(">b<", html)
        self.assertIn("onbekend", app.STATUSES)
        self.assertIn("opnieuw", app.STATUSES)

    def test_live_unavailable(self):
        app.LIVE_URL = "http://127.0.0.1:9/live"
        self.assertEqual(self.c.get("/api/live").get_json(), {"available": False, "printers": []})


if __name__ == "__main__":
    unittest.main()
