"""Tests voor ntfy-meldingen, onderhoudstaken en de bijbehorende pagina's (geen netwerk)."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402
import maintenance  # noqa: E402
import mqtt_listener as ml  # noqa: E402
import notify  # noqa: E402

TZ = ZoneInfo("Europe/Brussels")
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=TZ)


class FakeUrlopen:
    """Vangt verstuurde requests op in plaats van ze te versturen."""

    def __init__(self, fail=None):
        self.requests = []
        self.fail = fail

    def __call__(self, req, timeout=None):
        if self.fail:
            raise self.fail
        self.requests.append(req)
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"{}"
        return resp

    def bodies(self):
        return [json.loads(r.data) for r in self.requests]


def fresh_db():
    if os.path.exists(app.DB_PATH):
        os.remove(app.DB_PATH)
    app.init_db()


def db_conn():
    db = sqlite3.connect(app.DB_PATH)
    db.row_factory = sqlite3.Row
    return closing(db)


def set_cfg(**values):
    base = {"ntfy_url": "https://ntfy.example", "ntfy_topic": "pv-test",
            "notify_finish": "1", "notify_failed": "1", "notify_maintenance": "1"}
    base.update(values)
    with db_conn() as db, db:
        notify.save_settings(db, base)


class PostTest(unittest.TestCase):
    def test_post_json_with_token(self):
        f = FakeUrlopen()
        notify.post({"ntfy_url": "https://ntfy.example/", "ntfy_topic": "abc", "ntfy_token": "tk_geheim"},
                    "Titel", "Tekst", tags=["x"], priority=4, urlopen=f)
        req = f.requests[0]
        self.assertEqual((req.full_url, req.get_method()), ("https://ntfy.example", "POST"))
        self.assertEqual(f.bodies()[0], {"topic": "abc", "title": "Titel", "message": "Tekst", "tags": ["x"], "priority": 4})
        self.assertEqual(req.get_header("Authorization"), "Bearer tk_geheim")

    def test_post_without_token_or_config(self):
        f = FakeUrlopen()
        notify.post({"ntfy_url": "https://ntfy.example", "ntfy_topic": "abc"}, "T", "M", urlopen=f)
        self.assertIsNone(f.requests[0].get_header("Authorization"))
        with self.assertRaises(notify.NotifyError):
            notify.post({"ntfy_url": "", "ntfy_topic": "abc"}, "T", "M", urlopen=f)

    def test_post_error_does_not_leak_token(self):
        f = FakeUrlopen(fail=OSError("verbinding geweigerd"))
        with self.assertRaises(notify.NotifyError) as cm:
            notify.post({"ntfy_url": "https://x", "ntfy_topic": "a", "ntfy_token": "tk_geheim"}, "T", "M", urlopen=f)
        self.assertNotIn("tk_geheim", str(cm.exception))


class NotifierTest(unittest.TestCase):
    def setUp(self):
        fresh_db()
        self.f = FakeUrlopen()
        self.n = notify.Notifier(app.DB_PATH, urlopen=self.f, background=False)

    def test_toggles_per_kind(self):
        set_cfg(notify_finish="0")
        self.assertFalse(self.n("finish", "a", "b"))
        self.assertTrue(self.n("failed", "a", "b"))
        self.assertTrue(self.n("maintenance", "a", "b"))
        self.assertEqual(len(self.f.requests), 2)

    def test_not_configured_sends_nothing(self):
        set_cfg(ntfy_topic="")
        self.assertFalse(self.n("failed", "a", "b"))
        self.assertEqual(self.f.requests, [])

    def test_send_failure_is_logged_not_raised(self):
        set_cfg()
        n = notify.Notifier(app.DB_PATH, urlopen=FakeUrlopen(fail=OSError("weg")), background=False)
        with self.assertLogs("printvault.notify", "WARNING"):
            self.assertTrue(n("failed", "a", "b"))


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def report(state, error=0, task="1"):
    return {"print": {"gcode_state": state, "subtask_name": "Bed_scraper", "task_id": task, "print_error": error}}


class TrackerNotifyTest(unittest.TestCase):
    def setUp(self):
        fresh_db()
        with db_conn() as db, db:
            self.pid = db.execute("INSERT INTO printers (name) VALUES ('P2S')").lastrowid
        self.sent = []
        self.clock = Clock()

    def notifier(self, kind, title, message, tags=(), priority=None):
        self.sent.append((kind, title, message))
        return True

    def tracker(self):
        return ml.PrinterTracker(self.pid, "P2S", app.DB_PATH, clock=self.clock, notifier=self.notifier)

    def test_finish_notification(self):
        t = self.tracker()
        t.handle(report("RUNNING"))
        self.clock.t += timedelta(minutes=83)
        t.handle(report("FINISH"))
        self.assertEqual(self.sent, [("finish", "Print klaar: Bed_scraper", "P2S · 1u 23m")])

    def test_failed_notification_has_error_text(self):
        t = self.tracker()
        t.handle(report("RUNNING"))
        t.handle(report("FAILED", error=ml.CANCELLED))
        self.assertEqual(self.sent, [("failed", "Print mislukt: Bed_scraper", "P2S\nGeannuleerd op de printer.")])
        self.sent.clear()
        t.handle(report("RUNNING", task="2"))
        t.handle(report("FAILED", error=0x07008011, task="2"))
        self.assertEqual(self.sent[0][2], "P2S\nPrinterfout 0700-8011: AMS filament is op. "
                                          "Plaats een nieuw filament in dezelfde AMS-sleuf.")
        self.sent.clear()
        t.handle(report("RUNNING", task="3"))
        t.handle(report("FAILED", error=0x0300FFFF, task="3"))      # onbekende code
        self.assertEqual(self.sent[0][2], "P2S\nPrinterfout 0300-FFFF")

    def test_no_notification_for_end_seen_after_restart(self):
        self.tracker().handle(report("RUNNING"))
        self.tracker().handle(report("FINISH"))   # listener was weg tijdens het einde
        self.assertEqual(self.sent, [])

    def test_notifier_exception_does_not_break_tracking(self):
        def broken(*a, **k):
            raise RuntimeError("stuk")
        t = ml.PrinterTracker(self.pid, "P2S", app.DB_PATH, clock=self.clock, notifier=broken)
        t.handle(report("RUNNING"))
        with self.assertLogs("printvault.mqtt", "ERROR"):
            t.handle(report("FINISH"))
        with db_conn() as db:
            self.assertEqual(db.execute("SELECT status FROM prints").fetchone()[0], "gelukt")


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        fresh_db()
        with db_conn() as db, db:
            self.pid = db.execute("INSERT INTO printers (name) VALUES ('P2S')").lastrowid
        self.sent = []

    def notifier(self, kind, title, message, tags=(), priority=None):
        self.sent.append((kind, title, message))
        return True

    def add_task(self, hours=None, days=None, done=T0):
        with db_conn() as db, db:
            return db.execute("INSERT INTO maintenance_tasks (printer_id, name, interval_hours, interval_days, last_done_at) "
                              "VALUES (?, 'Smeren', ?, ?, ?)", (self.pid, hours, days, done.isoformat())).lastrowid

    def add_print(self, minutes, finished, status="gelukt", printer=None):
        with db_conn() as db, db:
            db.execute("INSERT INTO prints (name, status, printer_id, print_time_min, finished_at) VALUES ('p', ?, ?, ?, ?)",
                       (status, printer or self.pid, minutes, finished.isoformat()))

    def task(self, tid):
        with db_conn() as db:
            return db.execute("SELECT * FROM maintenance_tasks WHERE id = ?", (tid,)).fetchone()

    def status(self, tid, now):
        with db_conn() as db:
            return maintenance.task_status(db, self.task(tid), now)

    def test_hours_count_only_prints_after_last_done(self):
        tid = self.add_task(hours=2)
        self.add_print(600, T0 - timedelta(hours=1))                # vóór het onderhoud
        self.add_print(60, T0 + timedelta(hours=1))
        self.add_print(30, T0 + timedelta(hours=2), status="mislukt")
        self.add_print(500, T0 + timedelta(hours=3), status="bezig")  # telt niet
        with db_conn() as db, db:
            other = db.execute("INSERT INTO printers (name) VALUES ('X1C')").lastrowid
        self.add_print(500, T0 + timedelta(hours=3), printer=other)  # andere printer
        st = self.status(tid, T0 + timedelta(days=1))
        self.assertEqual((st["hours"], st["due"]), (1.5, False))
        self.assertAlmostEqual(st["progress"], 0.75)
        self.add_print(30, T0 + timedelta(hours=4))
        self.assertTrue(self.status(tid, T0 + timedelta(days=1))["due"])

    def test_days_interval_and_earliest_wins(self):
        tid = self.add_task(hours=100, days=30)
        self.assertFalse(self.status(tid, T0 + timedelta(days=29))["due"])
        st = self.status(tid, T0 + timedelta(days=30))
        self.assertEqual((st["days"], st["due"], st["progress"]), (30, True, 1.0))

    def test_manual_print_dates_count(self):
        tid = self.add_task(hours=1)
        with db_conn() as db, db:
            db.execute("INSERT INTO prints (name, status, printer_id, print_time_min, printed_on) "
                       "VALUES ('handmatig', 'gelukt', ?, 90, '2026-11-05')", (self.pid,))
        self.assertTrue(self.status(tid, T0 + timedelta(days=5))["due"])

    def test_notify_once_then_rearm_after_done(self):
        tid = self.add_task(days=7)
        now = T0 + timedelta(days=8)
        self.assertEqual(maintenance.check_and_notify(app.DB_PATH, self.notifier, now), 1)
        self.assertEqual(self.sent, [("maintenance", "Onderhoud: Smeren", "P2S: 8 van 7 dagen")])
        self.assertEqual(maintenance.check_and_notify(app.DB_PATH, self.notifier, now + timedelta(hours=1)), 0)
        self.assertEqual(len(self.sent), 1)
        app.app.test_client().post(f"/onderhoud/{tid}/done")       # uitgevoerd
        self.assertIsNone(self.task(tid)["notified_at"])
        later = maintenance.now_local() + timedelta(days=8)
        self.assertEqual(maintenance.check_and_notify(app.DB_PATH, self.notifier, later), 1)

    def test_rearm_when_interval_raised(self):
        tid = self.add_task(days=7)
        maintenance.check_and_notify(app.DB_PATH, self.notifier, T0 + timedelta(days=8))
        with db_conn() as db, db:
            db.execute("UPDATE maintenance_tasks SET interval_days = 30 WHERE id = ?", (tid,))
        maintenance.check_and_notify(app.DB_PATH, self.notifier, T0 + timedelta(days=9))
        self.assertIsNone(self.task(tid)["notified_at"])

    def test_disabled_kind_does_not_mark_notified(self):
        set_cfg(notify_maintenance="0")
        tid = self.add_task(days=7)
        n = notify.Notifier(app.DB_PATH, urlopen=FakeUrlopen(), background=False)
        self.assertEqual(maintenance.check_and_notify(app.DB_PATH, n, T0 + timedelta(days=8)), 0)
        self.assertIsNone(self.task(tid)["notified_at"])

    def test_seed_once_for_existing_printers(self):
        os.remove(app.DB_PATH)
        app.init_db()                                   # lege database: vlag gezet, geen printers
        with db_conn() as db, db:
            db.execute("DELETE FROM settings WHERE key = 'maintenance_seeded'")   # bestaande installatie van vóór deze versie
            db.execute("INSERT INTO printers (name, model) VALUES ('3DP', 'Bambu Lab P2S')")
        app.init_db()
        app.init_db()
        with db_conn() as db:
            names = [r[0] for r in db.execute("SELECT name FROM maintenance_tasks ORDER BY id")]
        self.assertEqual(names, [n for n, _h, _d in maintenance.DEFAULT_TASKS])


class PagesTest(unittest.TestCase):
    def setUp(self):
        fresh_db()
        with db_conn() as db, db:
            self.pid = db.execute("INSERT INTO printers (name) VALUES ('P2S')").lastrowid
        self.c = app.app.test_client()

    def cfg(self):
        with db_conn() as db:
            return notify.load_settings(db)

    def test_settings_saved_and_token_hidden(self):
        r = self.c.post("/instellingen", data={"ntfy_url": "https://ntfy.sh", "ntfy_topic": "pv-abc",
                                               "ntfy_token": "tk_geheim", "notify_failed": "1"})
        self.assertEqual(r.status_code, 302)
        cfg = self.cfg()
        self.assertEqual((cfg["ntfy_url"], cfg["ntfy_topic"], cfg["ntfy_token"]), ("https://ntfy.sh", "pv-abc", "tk_geheim"))
        self.assertEqual((cfg["notify_finish"], cfg["notify_failed"], cfg["notify_maintenance"]), ("0", "1", "0"))
        html = self.c.get("/instellingen").get_data(as_text=True)
        self.assertNotIn("tk_geheim", html)
        self.assertIn("ingesteld, leeg laten om te behouden", html)
        self.c.post("/instellingen", data={"ntfy_url": "https://ntfy.sh", "ntfy_topic": "pv-abc", "ntfy_token": ""})
        self.assertEqual(self.cfg()["ntfy_token"], "tk_geheim")
        self.c.post("/instellingen", data={"ntfy_url": "https://ntfy.sh", "ntfy_topic": "pv-abc", "clear_token": "1"})
        self.assertEqual(self.cfg()["ntfy_token"], "")

    def test_settings_rejects_bad_url_and_topic(self):
        self.c.post("/instellingen", data={"ntfy_url": "javascript:alert(1)", "ntfy_topic": "met spatie"})
        cfg = self.cfg()
        self.assertNotIn("ntfy_url", cfg)
        self.assertNotIn("ntfy_topic", cfg)

    def test_test_button(self):
        set_cfg()
        f = FakeUrlopen()
        with mock.patch.object(notify.urllib.request, "urlopen", f):
            html = self.c.post("/instellingen/test", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Testmelding verstuurd", html)
        self.assertEqual(f.bodies()[0]["topic"], "pv-test")
        with mock.patch.object(notify.urllib.request, "urlopen", FakeUrlopen(fail=OSError("weg"))):
            html = self.c.post("/instellingen/test", follow_redirects=True).get_data(as_text=True)
        self.assertIn("mislukt", html)

    def test_maintenance_pages(self):
        r = self.c.post(f"/printers/{self.pid}/onderhoud/standaard", follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("Exhaust-filter (HEPA/actieve kool) vervangen", html)
        r = self.c.post(f"/printers/{self.pid}/onderhoud", data={"name": "Riemen", "interval_days": "1",
                                                                "last_done_at": "2026-01-01T10:00"})
        with db_conn() as db:
            t = db.execute("SELECT * FROM maintenance_tasks WHERE name = 'Riemen'").fetchone()
        self.assertEqual((t["interval_days"], t["interval_hours"]), (1, None))
        self.assertTrue(t["last_done_at"].startswith("2026-01-01T10:00:00+01:00"))
        html = self.c.get("/printers").get_data(as_text=True)
        self.assertIn('Onderhoud <span class="badge mislukt">1</span>', html)
        self.assertIn("vervallen", self.c.get(f"/printers/{self.pid}/onderhoud").get_data(as_text=True))
        # zonder interval: geweigerd
        self.c.post(f"/printers/{self.pid}/onderhoud", data={"name": "Leeg"})
        with db_conn() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM maintenance_tasks WHERE name = 'Leeg'").fetchone())
        self.c.post(f"/onderhoud/{t['id']}/edit", data={"name": "Riemen spannen", "interval_hours": "50",
                                                        "interval_days": "", "last_done_at": "2026-01-01T10:00"})
        with db_conn() as db:
            t2 = db.execute("SELECT * FROM maintenance_tasks WHERE id = ?", (t["id"],)).fetchone()
        self.assertEqual((t2["name"], t2["interval_hours"], t2["interval_days"]), ("Riemen spannen", 50, None))
        self.assertEqual(self.c.get(f"/onderhoud/{t['id']}/edit").status_code, 200)
        self.c.post(f"/onderhoud/{t['id']}/delete")
        with db_conn() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM maintenance_tasks WHERE id = ?", (t["id"],)).fetchone())
        self.assertEqual(self.c.get("/onderhoud/999/edit").status_code, 404)

    def test_tasks_removed_with_printer(self):
        self.c.post(f"/printers/{self.pid}/onderhoud/standaard")
        self.c.post(f"/printers/{self.pid}/delete")
        with db_conn() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM maintenance_tasks").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
