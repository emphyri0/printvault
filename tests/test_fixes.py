"""Smaller fixes: live video, thumbnails in one process, printer address, AMS names, maintenance hours,
error pages, search, camera thread."""
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402
import app  # noqa: E402
import bambu_status as bs  # noqa: E402
import camera  # noqa: E402
import maintenance  # noqa: E402
import mqtt_listener as ml  # noqa: E402

ROOT = Path(testenv.ROOT)
TZ = ZoneInfo("Europe/Brussels")


class Base(unittest.TestCase):
    def setUp(self):
        testenv.reset()
        self.c = app.app.test_client()

    def run_sql(self, sql, *args):
        with ml.connect_db(app.DB_PATH) as db:
            return db.execute(sql, args).fetchall()


class LiveVideoTest(unittest.TestCase):
    def test_removed_box_does_not_stream(self):
        node = shutil.which("node")
        if node is None:
            if os.environ.get("REQUIRE_NODE") == "1":
                self.fail("REQUIRE_NODE=1 but node is not installed")
            self.skipTest("node not installed")
        r = subprocess.run([node, str(ROOT / "tests/js/live_video.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


class ListenerImportTest(unittest.TestCase):
    def test_listener_does_not_load_the_web_app(self):
        code = ("import sys; sys.path.insert(0, 'tests'); import testenv, unittest.mock as m, mqtt_listener as ml\n"
                "with m.patch.object(ml, 'serve_live'), m.patch.object(ml.Supervisor, 'run'): ml.main()\n"
                "import os, sqlite3; print('app' in sys.modules, 'flask' in sys.modules)\n"
                "print(sqlite3.connect(ml.DB_PATH).execute('SELECT COUNT(*) FROM prints').fetchone())")
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.stdout, "False False\n(0,)\n", r.stderr)


class PrinterAddressTest(Base):
    def test_invalid_address_changes_nothing(self):
        self.c.post("/printers/new", data={"name": "p", "ip": "192.0.2.1", "serial": "S"})
        for bad in ("192.0.2.1:322", "192.0.2", "999.1.1.1", "a..b", "-x.lan", "x y", "http://x", "a/b", "x" * 300, ".", "é.lan"):
            r = self.c.post("/printers/1/edit", data={"name": "new", "ip": bad, "serial": "S"})
            self.assertEqual(r.status_code, 200, bad)
            text = r.get_data(as_text=True)
            self.assertIn("not a valid IP address or host name", text, bad)
            self.assertIn('value="new"', text)
            self.assertEqual(tuple(self.run_sql("SELECT name, ip FROM printers")[0]), ("p", "192.0.2.1"))
            self.assertEqual(self.c.post("/printers/new", data={"name": "n", "ip": bad}).status_code, 200, bad)
        self.assertEqual(len(self.run_sql("SELECT 1 FROM printers")), 1)

    def test_valid_addresses(self):
        for good in ("192.0.2.9", "printer", "p2s.lan", "my-printer.home.example", "3dp.lan", ""):
            self.assertEqual(self.c.post("/printers/new", data={"name": good or "none", "ip": good}).status_code, 302, good)
        self.assertEqual([r[0] for r in self.run_sql("SELECT ip FROM printers ORDER BY id")][-2:], ["3dp.lan", ""])


class AmsNameTest(Base):
    def test_names(self):
        for ams_id, name in (("0", "AMS A"), (3, "AMS D"), ("128", "AMS HT 1"), (129, "AMS HT 2"), (5, "AMS 5"),
                             ("x", "AMS x"), (None, "AMS")):
            self.assertEqual(bs.ams_name(ams_id), name)

    def test_active_tray(self):
        def label(now):
            return bs.active_tray({"ams": {"tray_now": str(now), "ams": []}})["label"]
        self.assertEqual([label(n) for n in (1, 15, 20, 128)], ["AMS A2", "AMS D4", "AMS 5 slot 1", "AMS HT 1"])

    def test_same_name_everywhere(self):
        t = ml.PrinterTracker(1, "p", app.DB_PATH)
        t.state = {"ams": {"ams": [{"id": "0", "temp": "20"}, {"id": "128", "temp": "21"}]}}
        self.assertEqual([u["name"] for u in t.live()["ams"]], ["AMS A", "AMS HT 1"])
        self.run_sql("INSERT INTO prints (id, name) VALUES (1, 'x')")
        self.run_sql("INSERT INTO print_ams (print_id, ams_id, tray_id, tray_type) VALUES (1, '0', '1', 'PLA'), (1, '128', '0', 'PA'), "
                     "(1, 'x', 'y', 'PC')")
        text = self.c.get("/print/1").get_data(as_text=True)
        self.assertIn("AMS A · slot 2", text)
        self.assertIn("AMS HT 1 · slot 1", text)
        self.assertNotIn("AMS 129", text)
        self.assertIn("u.name", (ROOT / "static/live.js").read_text())

    def test_strange_units(self):
        for units in (["x", None], [{"id": "0", "tray": ["x", None]}], "x", [{"tray": 5}]):
            state = {"ams": {"tray_now": "0", "ams": units}}
            bs.active_tray(state)
            ml.ams_slots(state)
            ml.ams_units(state)


class MaintenanceHoursTest(Base):
    def minutes(self, done, printed_on, created_at, finished_at=None):
        self.run_sql("DELETE FROM prints")
        self.run_sql("INSERT OR IGNORE INTO printers (id, name) VALUES (1, 'p')")
        self.run_sql("INSERT INTO prints (name, status, printer_id, print_time_min, printed_on, created_at, finished_at) "
                     "VALUES ('x', 'succeeded', 1, 60, ?, ?, ?)", printed_on, created_at, finished_at)
        with ml.connect_db(app.DB_PATH) as db:
            return maintenance.print_minutes_since(db, 1, done)

    def test_print_on_the_day_of_the_maintenance(self):
        noon = "2026-11-20T12:00:00+01:00"
        # entered that evening (created_at is UTC): counts; entered that morning: does not
        self.assertEqual(self.minutes(noon, "2026-11-20", "2026-11-20 18:00:00"), 60)
        self.assertEqual(self.minutes(noon, "2026-11-20", "2026-11-20 08:00:00"), 0)
        # entered on another day: the time is unknown; counted, so maintenance is never too late
        self.assertEqual(self.minutes(noon, "2026-11-20", "2026-11-25 08:00:00"), 60)
        self.assertEqual(self.minutes(noon, "2026-11-20", "2026-11-01 08:00:00"), 60)

    def test_other_days_and_exact_times(self):
        noon = "2026-11-20T12:00:00+01:00"
        self.assertEqual(self.minutes(noon, "2026-11-19", "2026-11-25 08:00:00"), 0)
        self.assertEqual(self.minutes(noon, "2026-11-21", "2026-11-21 08:00:00"), 60)
        self.assertEqual(self.minutes(noon, "2026-11-20", "2026-11-25 08:00:00", "2026-11-20T11:00:00+01:00"), 0)
        self.assertEqual(self.minutes(noon, "2026-11-20", "2026-11-20 08:00:00", "2026-11-20T13:00:00+01:00"), 60)
        self.assertEqual(self.minutes(noon, None, "2026-11-20 10:59:00"), 0)
        self.assertEqual(self.minutes(noon, None, "2026-11-20 11:01:00"), 60)
        self.assertEqual(self.minutes(noon, "", "2026-11-20 11:01:00"), 60)
        self.assertEqual(self.minutes(noon, "garbage", "2026-11-20 11:01:00"), 60)
        self.assertEqual(self.minutes("2026-11-20", "2026-11-21", "2026-11-21 08:00:00"), 60)


class ErrorPageTest(Base):
    def test_error_page_without_a_database(self):
        with mock.patch.object(app.sqlite3, "connect", side_effect=sqlite3.OperationalError("unable to open")), \
                mock.patch.dict(app.app.config, {"PROPAGATE_EXCEPTIONS": False}), self.assertLogs(app.app.logger, "ERROR"):
            r = self.c.get("/")
            self.assertEqual(r.status_code, 500)
            self.assertIn("Something went wrong", r.get_data(as_text=True))
            self.assertIn("Printvault", r.get_data(as_text=True))
            r = self.c.get("/nothing")
            self.assertEqual(r.status_code, 404)
            self.assertIn("Not found", r.get_data(as_text=True))


class SearchTest(Base):
    def test_wildcards_are_plain_characters(self):
        for name in ("100% infill", "100 infill", "a_b", "axb", r"back\slash"):
            self.run_sql("INSERT INTO prints (name) VALUES (?)", name)

        def found(q):
            text = self.c.get("/", query_string={"q": q}).get_data(as_text=True)
            return [n for n in ("100% infill", "100 infill", "a_b", "axb", r"back\slash") if f">{n}<" in text]
        self.assertEqual(found("100%"), ["100% infill"])
        self.assertEqual(found("a_b"), ["a_b"])
        self.assertEqual(found("\\"), [r"back\slash"])
        self.assertEqual(found("INFILL"), ["100% infill", "100 infill"])


class CameraThreadTest(unittest.TestCase):
    def test_unexpected_error_does_not_kill_the_camera(self):
        cam = camera.Camera("192.0.2.1", "sec/ret", idle=30)
        self.addCleanup(cam.close)
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise PermissionError("rtsps://bblp:sec%2Fret@192.0.2.1 sec/ret")
            with cam.cond:
                cam._publish(b"frame")
            time.sleep(0.05)
        with mock.patch.object(cam, "_stream_once", flaky), mock.patch.object(camera, "RETRY_SECONDS", 0.01), \
                self.assertLogs("printvault.camera", "ERROR") as logs:
            self.assertEqual(next(cam.frames(stall=5)), b"frame")
        self.assertNotIn("sec/ret", "".join(logs.output))
        self.assertNotIn("sec%2Fret", "".join(logs.output))


if __name__ == "__main__":
    unittest.main()
