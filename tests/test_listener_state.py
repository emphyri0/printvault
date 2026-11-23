"""Listener: partial reports, errors halfway, deleted prints; and edits that cross a running print."""
import html
import json
import os
import re
import sqlite3
import sys
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402
import app  # noqa: E402
import uploads  # noqa: E402
import maintenance  # noqa: E402
import mqtt_listener as ml  # noqa: E402
import notify  # noqa: E402

TZ = ZoneInfo("Europe/Brussels")


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 23, 10, 0, tzinfo=TZ)

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        testenv.reset(uploads=True)
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO printers (id, name) VALUES (1, 'P1S')")
        self.clock = Clock()
        self.t = ml.PrinterTracker(1, "P1S", app.DB_PATH, clock=self.clock)
        self.c = app.app.test_client()

    def send(self, minutes=0, **report):
        self.clock.t += timedelta(minutes=minutes)
        self.t.handle({"print": report})

    def job(self, state, task="1", name="Part", **extra):
        self.send(gcode_state=state, task_id=task, subtask_name=name, nozzle_temper=200, **extra)

    def rows(self, sql="SELECT * FROM prints ORDER BY id"):
        with ml.connect_db(app.DB_PATH) as db:
            return [dict(r) for r in db.execute(sql)]


class PartialReportsTest(Base):
    def full(self):
        self.send(msg=0, gcode_state="IDLE", task_id="1", subtask_name="Old", nozzle_temper=25.0, bed_temper=22.0,
                  nozzle_target_temper=0, bed_target_temper=0, mc_percent=100, layer_num=50, total_layer_num=50,
                  mc_remaining_time=0, stg_cur=255, print_error=0, hms=[], spd_lvl=2, spd_mag=100,
                  ams={"tray_now": "255", "ams_exist_bits": "3", "ams": [
                      {"id": "0", "humidity": "4", "temp": "25.0", "tray": [
                          {"id": "0", "tray_type": "PLA", "tray_color": "FF0000FF"},
                          {"id": "1", "tray_type": "PETG", "tray_color": "00FF00FF"}]},
                      {"id": "1", "humidity": "5", "temp": "24.0", "tray": [
                          {"id": "0", "tray_type": "TPU", "tray_color": "0000FFFF"}]}]})

    def test_whole_print_in_partial_reports(self):
        self.full()
        self.send(gcode_state="PREPARE", msg=1)                        # state first ...
        self.send(task_id="2", msg=1)                                  # ... then the job, piece by piece
        self.send(subtask_name="New part", mc_percent=0, layer_num=0, total_layer_num=120, msg=1)
        self.send(nozzle_target_temper=220, bed_target_temper=55, msg=1)
        self.send(stg_cur=2, msg=1)
        self.send(1, bed_temper=40.0, msg=1)
        self.send(1, nozzle_temper=219.5, gcode_state="RUNNING", stg_cur=0, msg=1)
        self.send(30, mc_percent=50, msg=1)
        self.send(layer_num=60, mc_remaining_time=31, msg=1)
        live = self.t.live()
        self.assertEqual((live["gcode_state"], live["name"], live["percent"], live["layer"], live["total_layers"],
                          live["remaining_min"]), ("RUNNING", "New part", 50, 60, 120, 31))
        self.assertEqual(live["temps"]["nozzle"], {"cur": 219.5, "target": 220.0})
        self.assertEqual(live["temps"]["bed"], {"cur": 40.0, "target": 55.0})
        self.assertEqual(live["speed"]["value"], "standard")
        self.send(31, gcode_state="FINISH", msg=1)
        rows = self.rows()
        self.assertEqual([(r["name"], r["status"], r["task_id"], r["print_time_min"]) for r in rows],
                         [("New part", "succeeded", "2", 63)])
        samples = self.rows("SELECT * FROM print_samples ORDER BY id")
        self.assertEqual((samples[-1]["nozzle"], samples[-1]["bed_target"], samples[-1]["percent"]), (219.5, 55, 50))
        self.assertEqual(len(self.rows("SELECT * FROM print_ams")), 3)

    def test_two_prints_in_a_row(self):
        self.full()
        for task, name in (("2", "First"), ("3", "Second")):
            self.send(gcode_state="PREPARE", msg=1)
            self.send(task_id=task, msg=1)
            self.send(subtask_name=name, msg=1)
            self.send(gcode_state="RUNNING", msg=1)
            self.send(10, gcode_state="FINISH", msg=1)
        self.assertEqual([(r["name"], r["status"]) for r in self.rows()], [("First", "succeeded"), ("Second", "succeeded")])

    def test_same_file_again_keeps_its_name(self):
        self.full()
        self.send(gcode_state="RUNNING", msg=1)     # nothing about the job is sent: it did not change
        self.send(5, gcode_state="FINISH", msg=1)
        self.assertEqual([(r["name"], r["status"]) for r in self.rows()], [("Old", "succeeded")])

    def test_name_changed_by_the_user_stays(self):
        self.full()
        self.send(gcode_state="PREPARE", msg=1)
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("UPDATE prints SET name = 'mine'")
        self.send(subtask_name="New part", task_id="2", msg=1)
        self.assertEqual([(r["name"], r["task_id"]) for r in self.rows()], [("mine", "2")])

    def test_ams_lists_are_merged_by_id(self):
        self.full()
        self.send(msg=1, ams={"ams": [{"id": "1", "humidity": "3"}]})
        self.assertEqual([(u["id"], u["humidity_level"], u["temp"]) for u in ml.ams_units(self.t.state)],
                         [("0", 4, 25.0), ("1", 3, 24.0)])
        self.send(msg=1, ams={"ams": [{"id": "0", "tray": [{"id": "1", "tray_color": "FFFFFFFF"}]}]})
        self.assertEqual(ml.ams_slots(self.t.state), [("0", "0", "PLA", "FF0000FF"), ("0", "1", "PETG", "FFFFFFFF"),
                                                      ("1", "0", "TPU", "0000FFFF")])
        self.send(msg=1, ams={"ams": [{"id": "0", "tray": [{"id": "0"}]}]})          # spool taken out
        self.assertEqual([s[:2] for s in ml.ams_slots(self.t.state)], [("0", "1"), ("1", "0")])
        self.send(msg=1, ams={"ams_exist_bits": "1"})                                 # second AMS unplugged
        self.assertEqual([u["id"] for u in ml.ams_units(self.t.state)], ["0"])

    def test_full_report_replaces_lists(self):
        self.full()
        self.send(msg=0, ams={"ams": [{"id": "0", "humidity": "2", "tray": []}]})
        self.assertEqual([u["id"] for u in ml.ams_units(self.t.state)], ["0"])

    def test_lists_without_ids_are_replaced(self):
        self.send(hms=[{"attr": 1, "code": 2}, {"attr": 3, "code": 4}], stg=[1, 2, 3])
        self.send(hms=[], stg=[4])
        self.assertEqual((self.t.state["hms"], self.t.state["stg"]), ([], [4]))

    def test_reports_are_not_shared(self):
        a = {"print": {"ams": {"ams": [{"id": "0", "temp": "20"}]}}}
        self.t.handle(a)
        self.send(ams={"ams": [{"id": "0", "temp": "30"}]})
        self.assertEqual(a["print"]["ams"]["ams"][0]["temp"], "20")

    def test_listener_never_publishes(self):
        self.assertNotRegex(Path(ml.__file__).read_text(), r"\.publish\(")


class DeletedPrintTest(Base):
    def test_print_deleted_while_printing(self):
        self.job("RUNNING", "0", "A")
        self.c.post(f"/print/{self.t.print_id}/delete")
        self.clock.t += timedelta(minutes=2)
        self.job("RUNNING", "0", "A")
        self.job("PAUSE", "0", "A")
        self.job("RUNNING", "0", "A")
        self.assertEqual(self.rows(), [])            # deleted stays deleted
        self.assertIsNone(self.t.live()["print_id"])
        self.job("FINISH", "0", "A")
        self.job("PREPARE", "0", "B")
        self.job("RUNNING", "0", "B")
        self.job("FINISH", "0", "B")
        self.assertEqual([(r["name"], r["status"]) for r in self.rows()], [("B", "succeeded")])

    def test_deleted_just_before_the_end(self):
        self.job("RUNNING", "0", "A")
        self.c.post(f"/print/{self.t.print_id}/delete")
        self.job("FINISH", "0", "A")
        self.assertIsNone(self.t.print_id)

    def test_next_job_after_deletion_without_seeing_the_end(self):
        self.job("RUNNING", "1", "A")
        self.c.post(f"/print/{self.t.print_id}/delete")
        self.clock.t += timedelta(minutes=2)
        self.job("RUNNING", "1", "A")
        self.job("RUNNING", "2", "B")
        self.assertEqual([(r["name"], r["status"]) for r in self.rows()], [("B", "printing")])

    def test_end_missed_with_the_same_task_id(self):   # LAN prints all have task_id 0
        self.job("RUNNING", "0", "A")
        self.job("IDLE", "0", "A")
        self.job("PREPARE", "0", "B")
        self.assertEqual([(r["name"], r["status"]) for r in self.rows()], [("A", "unknown"), ("B", "printing")])


class ErrorHalfwayTest(Base):
    def fail_once(self, method):
        real, calls = getattr(self.t, method), []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(*a, **k)
        return mock.patch.object(self.t, method, flaky)

    def test_start_is_tried_again(self):
        for method in ("_save_ams", "_find_running"):
            with self.subTest(method), self.fail_once(method):
                with self.assertRaises(sqlite3.OperationalError):
                    self.job("RUNNING", method, method)
                self.assertIsNone(self.t.print_id)
                self.job("RUNNING", method, method)
                self.job("FINISH", method, method)
                self.assertEqual([r["status"] for r in self.rows() if r["name"] == method], ["succeeded"])

    def test_end_is_tried_again_and_reported_once(self):
        sent = []
        self.t.notifier = lambda kind, title, message, **k: sent.append(kind)
        for method in ("_save_ams", "_record_sample", "_finish"):
            with self.subTest(method):
                sent.clear()
                self.job("RUNNING", method, method)
                with self.fail_once(method), self.assertRaises(sqlite3.OperationalError):
                    self.job("FINISH", method, method)
                self.job("FINISH", method, method)
                self.assertEqual([r["status"] for r in self.rows() if r["name"] == method], ["succeeded"])
                self.assertEqual(sent, ["finish"])

    def test_ams_error_does_not_stop_the_tracking(self):
        with self.fail_once("_record_ams"), self.assertRaises(sqlite3.OperationalError):
            self.job("RUNNING")
        self.assertEqual([r["status"] for r in self.rows()], ["printing"])


class SupervisorTest(Base):
    def sync(self, sup, **cfg):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("UPDATE printers SET ip=:ip, serial=:serial, access_code='x', mqtt_enabled=1", cfg)
        with mock.patch.object(ml, "PrinterConnection"):
            sup.sync()
        return sup.trackers[1]

    def test_other_printer_starts_clean(self):
        sup = ml.Supervisor(app.DB_PATH)
        a = self.sync(sup, ip="192.0.2.1", serial="A")
        a.handle({"print": {"gcode_state": "RUNNING", "task_id": "1"}})
        self.assertIs(self.sync(sup, ip="192.0.2.2", serial="A"), a)       # same printer, new address
        b = self.sync(sup, ip="192.0.2.2", serial="B")
        self.assertIsNot(b, a)
        self.assertEqual((b.state, b.print_id, b.gcode_state), ({}, None, None))
        self.assertIsNotNone(b.snapshotter)

    def test_maintenance_notification_is_repeated_until_sent(self):
        with closing(sqlite3.connect(app.DB_PATH)) as db, db:
            notify.save_settings(db, {"ntfy_url": "https://ntfy.example", "ntfy_topic": "t", "notify_maintenance": "1"})
            db.execute("INSERT INTO maintenance_tasks (printer_id, name, interval_days, last_done_at) "
                       "VALUES (1, 'Oil', 1, '2026-01-01T00:00:00+01:00')")
        sup = ml.Supervisor(app.DB_PATH)
        ok = mock.MagicMock()
        with mock.patch.object(notify._opener, "open", side_effect=[OSError("down"), ok, ok]) as post:
            with self.assertLogs("printvault.notify", "WARNING"):
                sup.tick()
            self.assertIsNone(self.rows("SELECT notified_at FROM maintenance_tasks")[0]["notified_at"])
            sup.tick()
            self.assertIsNotNone(self.rows("SELECT notified_at FROM maintenance_tasks")[0]["notified_at"])
            sup.tick()
        self.assertEqual(post.call_count, 2)

    def test_database_is_not_locked_while_sending(self):
        with closing(sqlite3.connect(app.DB_PATH)) as db, db:
            for name in ("a", "b"):
                db.execute("INSERT INTO maintenance_tasks (printer_id, name, interval_days, last_done_at) "
                           "VALUES (1, ?, 1, '2026-01-01T00:00:00+01:00')", (name,))

        def slow(*a, **k):
            with closing(sqlite3.connect(app.DB_PATH, timeout=0)) as other, other:
                other.execute("UPDATE printers SET notes = 'x'")
            return True
        self.assertEqual(maintenance.check_and_notify(app.DB_PATH, slow), 2)


class CrossingEditTest(Base):
    def form(self, pid):
        text = self.c.get(f"/print/{pid}/edit").get_data(as_text=True)
        return html.unescape(re.search(r'name="base" value="([^"]*)"', text).group(1))

    def test_result_of_the_print_is_kept(self):
        self.job("RUNNING")
        base = self.form(1)
        self.clock.t += timedelta(minutes=30)
        self.job("FAILED", print_error=0x03008003)
        self.c.post("/print/1/edit", data={"base": base, "name": "Renamed", "status": "printing", "tags": "new",
                                           "printer_id": "1", "printed_on": "2026-11-23"})
        p = self.rows()[0]
        self.assertEqual((p["name"], p["tags"], p["status"], p["print_time_min"], p["fail_reason"]),
                         ("Renamed", "new", "failed", 30, "spaghetti"))
        self.assertTrue(p["fail_note"])

    def test_own_choice_wins(self):
        self.job("RUNNING")
        base = self.form(1)
        self.job("FAILED", print_error=0x03008003)
        self.c.post("/print/1/edit", data={"base": base, "name": "Part", "status": "succeeded", "printer_id": "1"})
        p = self.rows()[0]
        self.assertEqual((p["status"], p["fail_reason"], p["fail_note"]), ("succeeded", None, None))

    def test_fields_can_still_be_cleared(self):
        self.c.post("/print/new", data={"name": "x", "tags": "a", "notes": "one\r\ntwo", "hours": "2"})
        base = self.form(1)
        self.c.post("/print/1/edit", data={"base": base, "name": "x", "tags": "", "notes": "", "hours": ""})
        p = self.rows()[0]
        self.assertEqual((p["tags"], p["notes"], p["print_time_min"]), ("", "", None))

    def test_notes_added_by_the_listener_are_kept(self):
        self.job("RUNNING", notes="")
        base = self.form(1)
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("UPDATE prints SET notes = 'End not seen live'")
        self.c.post("/print/1/edit", data={"base": base, "name": "Part", "status": "printing",
                                           "notes": json.loads(base)["notes"].replace("\n", "\r\n")})
        self.assertEqual(self.rows()[0]["notes"], "End not seen live")

    def test_broken_base_is_ignored(self):
        self.job("RUNNING")
        for base in ("", "x", "[]", "null", '{"status": {}}'):
            self.assertEqual(self.c.post("/print/1/edit", data={"base": base, "name": "n"}).status_code, 302, base)

    def test_base_survives_a_form_error(self):
        self.job("RUNNING")
        base = self.form(1)
        text = self.c.post("/print/1/edit", data={"base": base, "name": ""}).get_data(as_text=True)
        self.assertEqual(html.unescape(re.search(r'name="base" value="([^"]*)"', text).group(1)), base)


class FilesAfterCommitTest(Base):
    """Files may only go once the database change is certain."""

    def setUp(self):
        super().setUp()
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO prints (id, name) VALUES (1, 'x')")
            db.execute("INSERT INTO files (id, print_id, kind, stored_name, original_name) VALUES (1, 1, 'photo', 'a.jpg', 'a.jpg')")
            db.execute("UPDATE printers SET photo = 'p.jpg'")
        for name in ("a.jpg", "p.jpg"):
            Path(app.UPLOAD_DIR, name).write_bytes(b"\xff\xd8\xff")
        add = mock.patch.object(uploads.THUMBS, "add")
        add.start()
        self.addCleanup(add.stop)

    URLS = (("/print/1/delete", {}), ("/file/1/delete", {}), ("/printers/1/edit", {"name": "p", "remove_photo": "1"}),
            ("/printers/1/delete", {}))

    def test_kept_when_the_database_is_locked(self):
        real = sqlite3.connect
        with closing(real(app.DB_PATH)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            with mock.patch.object(app.sqlite3, "connect", lambda path, timeout=0: real(path, timeout=0)), \
                    mock.patch.dict(app.app.config, {"PROPAGATE_EXCEPTIONS": False}), self.assertLogs(app.app.logger, "ERROR"):
                for url, data in self.URLS:
                    self.assertEqual(self.c.post(url, data=data).status_code, 500, url)
        for name in ("a.jpg", "p.jpg"):
            self.assertTrue(Path(app.UPLOAD_DIR, name).exists(), name)

    def test_removed_after_success(self):
        for url, data in (self.URLS[1], self.URLS[2]):
            self.c.post(url, data=data)
        self.assertEqual([p for p in os.listdir(app.UPLOAD_DIR) if p.endswith(".jpg")], [])


if __name__ == "__main__":
    unittest.main()
