"""Tests for mqtt_listener.PrinterTracker with faked Bambu messages (no network).

Run:  python3 -m unittest discover -s tests
"""
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402
import app  # noqa: E402
import mqtt_listener as ml  # noqa: E402
import bambu_errors as be  # noqa: E402

TZ = ZoneInfo("Europe/Brussels")


def active_chip(text):
    """Regex for a filter chip that is switched on and contains `text`."""
    return re.compile(r'<a class="chip"[^>]*aria-current="true"[^>]*>(?:(?!</a>).)*' + re.escape(text), re.S)


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
             {"id": "2"},  # empty slot
             {"id": "3", "tray_type": "PLA", "tray_color": "url(x)", "remain": -1}]}]}}
    p.update(extra)
    return {"print": p}


class TrackerTest(unittest.TestCase):
    def setUp(self):
        testenv.reset()
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
        for _ in range(5):                       # repeated full status: no effect
            t.handle(report("RUNNING", percent=40))
        rows = self.prints()
        self.assertEqual(len(rows), 1)
        p = rows[0]
        self.assertEqual((p["status"], p["name"], p["printer_id"], p["source"]), ("printing", "Bed_scraper", self.printer_id, "mqtt"))
        self.assertEqual(p["started_at"], "2026-11-18T19:00:00+01:00")
        self.assertEqual(p["printed_on"], "2026-11-18")
        self.assertEqual(p["notes"], "")         # start seen live
        self.assertEqual(self.ams(p["id"]), [("0", "0", "PLA", "161616FF"), ("0", "1", "PETG", "F95D73FF"),
                                            ("0", "3", "PLA", "")])
        self.clock.advance(83)
        t.handle(report("FINISH", percent=100))
        t.handle(report("IDLE"))
        p = self.prints()[0]
        self.assertEqual((p["status"], p["print_time_min"]), ("succeeded", 83))
        self.assertEqual(p["finished_at"], "2026-11-18T20:23:00+01:00")

    def test_failed_keeps_error(self):
        t = self.tracker()
        t.handle(report("RUNNING"))
        self.clock.advance(10)
        t.handle(report("FAILED", error=0x07008011))
        p = self.prints()[0]
        self.assertEqual((p["status"], p["print_error"]), ("failed", 0x07008011))
        self.assertEqual(p["fail_note"], "Printer error 0700-8011: AMS filament ran out. Please insert a new filament into the same AMS slot.")
        self.assertIsNone(p["fail_reason"])
        self.assertEqual(p["print_time_min"], 10)

    def test_failed_with_known_code_fills_reason_and_hms(self):
        t = self.tracker()
        t.handle(report("RUNNING"))
        hms = [{"attr": 0x0C00FFFF, "code": 0x0003FFFF}]       # unknown HMS code
        t.handle(report("FAILED", error=0x03008003, hms=hms))
        p = self.prints()[0]
        self.assertEqual(p["fail_reason"], "spaghetti")
        self.assertEqual(p["fail_note"], "Printer error 0300-8003: " + be.PRINT_ERRORS["0300-8003"] + "\n"
                                         "HMS 0C00_FFFF_0003_FFFF (camera/AI detection)")

    def test_failed_keeps_manual_reason(self):
        t = self.tracker()
        t.handle(report("RUNNING"))
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("UPDATE prints SET fail_reason = 'warping', fail_note = 'saw it myself'")
        t.handle(report("FAILED", error=0x03008003))
        p = self.prints()[0]
        self.assertEqual((p["fail_reason"], p["fail_note"]), ("warping", "saw it myself"))

    def test_live_has_error_text_and_hms(self):
        t = self.tracker()
        t.handle(report("PAUSE", error=ml.CANCELLED, hms=[{"attr": 0x07002000, "code": 0x00020001}]))
        live = t.live()
        self.assertEqual((live["error"], live["error_text"]), ("0300-400C", "The task was canceled."))
        self.assertEqual(live["hms"][0]["code"], "0700_2000_0002_0001")

    def test_cancelled(self):
        t = self.tracker()
        t.handle(report("RUNNING"))
        t.handle(report("FAILED", error=ml.CANCELLED))
        self.assertEqual(self.prints()[0]["fail_note"], "Cancelled on the printer.")

    def test_restart_resumes_without_duplicate(self):
        t = self.tracker()
        t.handle(report("PREPARE"))
        t.handle(report("RUNNING"))
        self.clock.advance(30)
        t2 = self.tracker()                      # listener restarts
        t2.handle(report("RUNNING", percent=60))
        t2.handle(report("RUNNING", percent=61))
        rows = self.prints()
        self.assertEqual(len(rows), 1)
        self.assertEqual(t2.print_id, rows[0]["id"])
        self.clock.advance(30)
        t2.handle(report("FINISH"))
        p = self.prints()[0]
        self.assertEqual((p["status"], p["print_time_min"]), ("succeeded", 60))

    def test_restart_after_finish_closes_open_record(self):
        t = self.tracker()
        t.handle(report("RUNNING"))
        self.clock.advance(45)
        t2 = self.tracker()                      # listener was away during the end
        t2.handle(report("FINISH"))
        p = self.prints()[0]
        self.assertEqual((p["status"], p["print_time_min"]), ("succeeded", 45))
        self.assertIn("End not seen live", p["notes"])

    def test_listener_starts_mid_print(self):
        t = self.tracker()
        t.handle(report("RUNNING", percent=39))
        p = self.prints()[0]
        self.assertEqual(p["status"], "printing")
        self.assertIn("Start not seen live", p["notes"])

    def test_other_task_marks_stale_record(self):
        t = self.tracker()
        t.handle(report("RUNNING", task="1"))
        t2 = self.tracker()
        t2.handle(report("RUNNING", task="2", name="Other"))
        rows = self.prints()
        self.assertEqual([r["status"] for r in rows], ["unknown", "printing"])
        self.assertEqual(rows[1]["name"], "Other")

    def test_new_task_while_connected(self):
        t = self.tracker()
        t.handle(report("RUNNING", task="1"))
        t.handle(report("RUNNING", task="2", name="Next"))   # FINISH missed
        rows = self.prints()
        self.assertEqual([r["status"] for r in rows], ["unknown", "printing"])

    def test_partial_messages_are_merged(self):
        t = self.tracker()
        t.handle(report("RUNNING", percent=10))
        t.handle({"print": {"mc_percent": 55, "layer_num": 20}})   # partial message without gcode_state
        live = t.live()
        self.assertEqual((live["gcode_state"], live["percent"], live["layer"], live["total_layers"]),
                         ("RUNNING", 55, 20, 37))
        self.assertEqual(len(self.prints()), 1)
        t.handle({"print": {"gcode_state": "FINISH"}})
        self.assertEqual(self.prints()[0]["status"], "succeeded")

    def ams_report(self, hum="3", raw="23", temp="24.6", **extra):
        return {"print": {"ams": {"ams": [{"id": "0", "humidity": hum, "humidity_raw": raw, "temp": temp, "tray": []},
                                          {"id": "1", "humidity": "5", "temp": "0.0", "tray": []}]}, **extra}}

    def readings(self):
        with ml.connect_db(app.DB_PATH) as db:
            return [tuple(r) for r in db.execute(
                "SELECT ams_id, humidity_pct, humidity_level, temp, recorded_at FROM ams_readings ORDER BY id")]

    def test_ams_units_parsing(self):
        units = ml.ams_units(self.ams_report()["print"])
        self.assertEqual(units, [{"id": "0", "name": "AMS A", "humidity_pct": 23.0, "humidity_level": 3, "temp": 24.6},
                                 {"id": "1", "name": "AMS B", "humidity_pct": None, "humidity_level": 5, "temp": None}])
        self.assertEqual(ml.ams_units({"ams": {"ams": [{"id": "0", "temp": "x"}, "rommel"]}}), [])
        self.assertEqual(ml.ams_units({}), [])

    def test_ams_readings_max_one_per_10_min(self):
        t = self.tracker()
        t.handle(self.ams_report())
        self.assertEqual(self.readings(), [("0", 23.0, 3, 24.6, "2026-11-18T19:00:00+01:00"),
                                           ("1", None, 5, None, "2026-11-18T19:00:00+01:00")])
        for _ in range(9):                       # a message every minute: nothing extra
            self.clock.advance(1)
            t.handle(self.ams_report(raw="25"))
        self.assertEqual(len(self.readings()), 2)
        self.clock.advance(1)                    # 10 minutes after the previous one
        t.handle(self.ams_report(raw="26", temp="25.1"))
        rows = self.readings()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[2], ("0", 26.0, 3, 25.1, "2026-11-18T19:10:00+01:00"))

    def test_ams_throttle_survives_restart(self):
        self.tracker().handle(self.ams_report())
        self.clock.advance(5)
        t2 = self.tracker()
        t2.handle(self.ams_report())
        self.assertEqual(len(self.readings()), 2)
        self.clock.advance(5)
        t2.handle(self.ams_report())
        self.assertEqual(len(self.readings()), 4)

    def test_live_has_ams(self):
        t = self.tracker()
        t.handle(self.ams_report(gcode_state="IDLE"))
        self.assertEqual(t.live()["ams"][0], {"id": "0", "name": "AMS A", "humidity_pct": 23.0, "humidity_level": 3, "temp": 24.6})

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
        self.assertEqual([r["status"] for r in self.prints()], ["printing", "printing"])

    def test_supervisor_only_complete_configs(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("UPDATE printers SET ip='10.0.0.2', serial='ABC', access_code='x', mqtt_enabled=1")
            db.execute("INSERT INTO printers (name, ip, serial, mqtt_enabled) VALUES ('no code', '10.0.0.3', 'D', 1)")
        self.assertEqual(list(ml.Supervisor(app.DB_PATH).configs()), [self.printer_id])


class AppTest(unittest.TestCase):
    def setUp(self):
        testenv.reset()
        self.c = app.app.test_client()

    def test_access_code_not_rendered_and_kept(self):
        self.c.post("/printers/new", data={"name": "P2S", "ip": "192.0.2.10", "serial": "00M00A 000000000",
                                           "access_code": "12345678", "mqtt_enabled": "1"})
        html = self.c.get("/printers/1/edit").get_data(as_text=True)
        self.assertNotIn("12345678", html)
        self.assertIn("set, leave empty to keep", html)
        self.c.post("/printers/1/edit", data={"name": "P2S", "ip": "192.0.2.10", "serial": "00M00A000000000",
                                              "access_code": "", "mqtt_enabled": "1"})
        with ml.connect_db(app.DB_PATH) as db:
            row = db.execute("SELECT serial, access_code, mqtt_enabled FROM printers").fetchone()
        self.assertEqual(tuple(row), ("00M00A000000000", "12345678", 1))

    def test_status_filter_has_unknown(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO prints (name, status) VALUES ('a', 'unknown'), ('b', 'succeeded')")
        html = self.c.get("/?status=unknown").get_data(as_text=True)
        self.assertRegex(html, active_chip("Unknown"))   # the status chip is shown as active
        self.assertIn(">a<", html)
        self.assertNotIn(">b<", html)
        self.assertIn("unknown", app.STATUSES)
        self.assertIn("reprint", app.STATUSES)

    def test_detail_shows_error_text_and_link(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO prints (name, status, print_error) VALUES ('x', 'failed', ?), ('y', 'failed', ?)",
                       (ml.CANCELLED, 0x07008011))
        html = self.c.get("/print/1").get_data(as_text=True)
        self.assertIn("The task was canceled.", html)
        self.assertIn('href="https://wiki.bambulab.com/en/hms/home"', html)
        html = self.c.get("/print/2").get_data(as_text=True)
        self.assertIn("AMS filament ran out.", html)
        self.assertIn(">0700-8011</a>", html)

    def test_api_live_adds_ams_history(self):
        with ml.connect_db(app.DB_PATH) as db:
            pid = db.execute("INSERT INTO printers (name) VALUES ('P2S')").lastrowid
            now = datetime.now(TZ).replace(microsecond=0)
            db.executemany("INSERT INTO ams_readings (printer_id, ams_id, humidity_pct, humidity_level, temp, recorded_at) "
                           "VALUES (?, '0', ?, 3, 24.0, ?)",
                           [(pid, 30.0, (now - timedelta(hours=30)).isoformat()),   # too old
                            (pid, 22.0, (now - timedelta(hours=2)).isoformat()),
                            (pid, 21.0, (now - timedelta(minutes=5)).isoformat())])
        live = {"printers": [{"printer_id": pid, "ams": [{"id": "0"}]}, {"printer_id": 99, "ams": []}]}
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(live).encode()
        with mock.patch.object(app.urllib.request, "urlopen", return_value=resp):
            data = self.c.get("/api/live").get_json()
        self.assertTrue(data["available"])
        self.assertEqual([r[1] for r in data["printers"][0]["ams_history"]["0"]], [22.0, 21.0])
        self.assertNotIn("ams_history", data["printers"][1])

    def test_ams_readings_removed_with_printer(self):
        with ml.connect_db(app.DB_PATH) as db:
            pid = db.execute("INSERT INTO printers (name) VALUES ('P2S')").lastrowid
            db.execute("INSERT INTO ams_readings (printer_id, ams_id, recorded_at) VALUES (?, '0', 'x')", (pid,))
        self.c.post(f"/printers/{pid}/delete")
        with ml.connect_db(app.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ams_readings").fetchone()[0], 0)

    def test_live_unavailable(self):
        app.LIVE_URL = "http://127.0.0.1:9/live"
        self.assertEqual(self.c.get("/api/live").get_json(), {"available": False, "printers": []})


if __name__ == "__main__":
    unittest.main()
