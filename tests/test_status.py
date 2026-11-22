"""Tests for bambu_status and the listener extensions (fields at the start, time series),
using a real P2S report (anonymised) in tests/fixtures/p2s_report.json.
"""
import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DATA_DIR", _tmp)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402
import bambu_status as bs  # noqa: E402
import mqtt_listener as ml  # noqa: E402

TZ = ZoneInfo("Europe/Brussels")
with open(os.path.join(os.path.dirname(__file__), "fixtures", "p2s_report.json"), encoding="utf-8") as fh:
    REPORT = json.load(fh)


def state(**changes):
    s = copy.deepcopy(REPORT["print"])
    s.update(changes)
    return s


class StatusTest(unittest.TestCase):
    def test_temperatures_from_p2s(self):
        self.assertEqual(bs.temperatures(state()), {"nozzle": (220.0, 220.0), "bed": (55.0, 55.0), "chamber": (35, None)})

    def test_packed_temperatures_as_fallback(self):
        s = state()
        for k in ("nozzle_temper", "nozzle_target_temper", "bed_temper", "bed_target_temper"):
            del s[k]
        s["device"]["extruder"]["info"][0]["temp"] = (250 << 16) | 180
        self.assertEqual(bs.temperatures(s)["nozzle"], (180, 250))
        self.assertEqual(bs.temperatures(s)["bed"], (55, 55))
        self.assertEqual(bs.temperatures({}), {"nozzle": (None, None), "bed": (None, None), "chamber": (None, None)})
        self.assertEqual(bs.temperatures({"chamber_temper": 31.5})["chamber"], (31.5, None))   # X1

    def test_phase(self):
        self.assertEqual(bs.phase(state()), (0, "Printing"))
        self.assertEqual(bs.phase(state(stg_cur=54, gcode_state="PREPARE")), (54, "Waiting for bed temperature"))
        self.assertEqual(bs.phase(state(stg_cur=16, gcode_state="PAUSE")), (16, "Paused by user"))
        self.assertEqual(bs.phase(state(stg_cur=0, gcode_state="PAUSE")), (0, "Paused"))
        self.assertEqual(bs.phase(state(stg_cur=255, gcode_state="FINISH")), (None, "Finished"))
        self.assertEqual(bs.phase(state(stg_cur=-1, gcode_state="IDLE")), (None, "Idle"))
        self.assertEqual(bs.stage_text(60), bs.STAGES[60]["text"])   # from the source
        self.assertEqual(bs.stage_text(3), "Vibration compensation")  # own wording beats the source
        self.assertEqual(bs.stage_text(999), "Stage 999")
        self.assertEqual(bs.stage_group(2), "heating")
        self.assertEqual(bs.stage_group(0), "printing")
        self.assertEqual(bs.stage_group(13), "preparing")
        for code in REPORT["print"]["stg"]:   # every stage the P2S planned for this print has a text
            self.assertNotIn("Stage", bs.stage_text(code))

    def test_fans_and_speed(self):
        self.assertEqual(bs.fans(state()), {"part": 100, "aux": 90, "chamber": 90, "heatbreak": 100})
        self.assertEqual(bs.fan_percent("0"), 0)
        self.assertEqual(bs.fan_percent("7"), 50)
        self.assertEqual(bs.speed(state()), {"level": 2, "value": "standard", "name": "Standard", "percent": 100})
        self.assertEqual(bs.speed(state(spd_lvl=4, spd_mag=166))["value"], "ludicrous")
        self.assertEqual(bs.speed(state(spd_lvl=9))["name"], "Profile 9")
        self.assertIsNone(bs.speed({}))

    def test_active_tray(self):
        self.assertEqual(bs.active_tray(state()), {"label": "AMS A2", "ams_id": 0, "tray_id": 1, "type": "PLA",
                                                   "name": "PLA Pure", "color": "F7CED7", "remain": 100})
        s = state()
        s["ams"]["tray_now"] = "255"
        self.assertIsNone(bs.active_tray(s))
        s["ams"]["tray_now"] = "254"
        self.assertEqual(bs.active_tray(s)["label"], "External spool")
        self.assertIsNone(bs.active_tray({}))

    def test_nozzle_type_names(self):
        self.assertEqual(bs.nozzle_type_name("HS01"), "hardened steel")
        self.assertEqual(bs.nozzle_type_name("HH01"), "hardened steel, high-flow")
        self.assertEqual(bs.nozzle_type_name("HS00"), "stainless steel")
        self.assertEqual(bs.nozzle_type_name("HU01"), "TPU high-flow")
        self.assertIsNone(bs.nozzle_type_name("HSxx"))
        self.assertIsNone(bs.nozzle_type_name(""))

    def test_print_settings(self):
        self.assertEqual(bs.print_settings(state()), {
            "nozzle": 0.4, "nozzle_type": "Hardened steel (HS01)", "plate_type": "P0101", "plate": 1, "plate_count": 2,
            "speed_profile": "standard", "spaghetti_detection": 1, "first_layer_inspection": 1})
        self.assertEqual(bs.print_settings({"gcode_state": "RUNNING"}), {})


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 22, 14, 0, tzinfo=TZ)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def msg(**changes):
    return {"print": state(**changes)}


class ListenerTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(app.DB_PATH):
            os.remove(app.DB_PATH)
        app.init_db()
        with ml.connect_db(app.DB_PATH) as db:
            self.printer_id = db.execute("INSERT INTO printers (name) VALUES ('P2S')").lastrowid
        self.clock = Clock()

    def tracker(self):
        return ml.PrinterTracker(self.printer_id, "P2S", app.DB_PATH, clock=self.clock)

    def row(self, print_id=1):
        with ml.connect_db(app.DB_PATH) as db:
            return db.execute("SELECT * FROM prints WHERE id = ?", (print_id,)).fetchone()

    def samples(self):
        with ml.connect_db(app.DB_PATH) as db:
            return [dict(r) for r in db.execute("SELECT * FROM print_samples ORDER BY id")]

    def test_settings_filled_at_start(self):
        self.tracker().handle(msg(gcode_state="PREPARE", stg_cur=2))
        p = self.row()
        self.assertEqual((p["nozzle"], p["nozzle_type"], p["plate_type"], p["plate"], p["plate_count"],
                          p["speed_profile"], p["spaghetti_detection"], p["first_layer_inspection"]),
                         (0.4, "Hardened steel (HS01)", "P0101", 1, 2, "standard", 1, 1))

    def test_settings_do_not_overwrite_own_values(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO prints (id, name, status, printer_id, task_id, nozzle, plate_type, speed_profile, "
                       "spaghetti_detection) VALUES (1, 'own', 'printing', ?, '1001', 0.6, 'Textured PEI Plate', 'sport', 0)",
                       (self.printer_id,))
        self.tracker().handle(msg())     # listener (re)starts during the print: resumes
        p = self.row()
        self.assertEqual((p["nozzle"], p["plate_type"], p["speed_profile"], p["spaghetti_detection"]),
                         (0.6, "Textured PEI Plate", "sport", 0))
        self.assertEqual((p["nozzle_type"], p["plate_count"], p["first_layer_inspection"]), ("Hardened steel (HS01)", 2, 1))

    def test_settings_arriving_later_are_filled(self):
        t = self.tracker()
        t.handle({"print": {"gcode_state": "RUNNING", "task_id": "1001", "subtask_name": "x", "stg_cur": 0}})
        self.assertIsNone(self.row()["nozzle_type"])
        t.handle(msg())                  # the full report only arrives later
        self.assertEqual(self.row()["nozzle_type"], "Hardened steel (HS01)")
        with ml.connect_db(app.DB_PATH) as db:   # changed by the user during the print: stays
            db.execute("UPDATE prints SET nozzle_type = 'own'")
        t.handle(msg(nozzle_type="HH01"))
        self.assertEqual(self.row()["nozzle_type"], "own")

    def test_samples_max_one_per_minute_plus_phase_changes(self):
        t = self.tracker()
        t.handle(msg(gcode_state="PREPARE", stg_cur=2, bed_temper=30.0))
        for _ in range(5):                      # within the same minute, same stage: nothing new
            self.clock.advance(10)
            t.handle(msg(gcode_state="PREPARE", stg_cur=2, bed_temper=40.0))
        self.clock.advance(5)
        t.handle(msg(gcode_state="PREPARE", stg_cur=1))      # stage change: a point right away
        self.clock.advance(30)
        t.handle(msg(gcode_state="RUNNING", stg_cur=0))      # another stage
        self.clock.advance(30)
        t.handle(msg(gcode_state="RUNNING", stg_cur=0))      # < 1 min since the previous: nothing
        self.clock.advance(31)
        t.handle(msg(gcode_state="RUNNING", stg_cur=0))      # ≥ 1 min: a point
        self.clock.advance(5)
        t.handle(msg(gcode_state="FINISH", stg_cur=255))     # end: always a last point
        t.handle(msg(gcode_state="FINISH", stg_cur=255))
        rows = self.samples()
        self.assertEqual([r["stage"] for r in rows], [2, 1, 0, 0, None])
        self.assertEqual(rows[0]["bed"], 30.0)
        self.assertEqual((rows[2]["nozzle"], rows[2]["nozzle_target"], rows[2]["chamber"], rows[2]["percent"],
                          rows[2]["layer"]), (220.0, 220.0, 35, 12, 5))
        self.assertEqual(rows[0]["recorded_at"], "2026-11-22T14:00:00+01:00")

    def test_samples_interval_survives_restart(self):
        self.tracker().handle(msg())
        self.clock.advance(20)
        self.tracker().handle(msg())      # new listener, within the minute, same stage
        self.assertEqual(len(self.samples()), 1)

    def test_samples_removed_with_print(self):
        self.tracker().handle(msg())
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("DELETE FROM prints")
        self.assertEqual(self.samples(), [])

    def test_live_has_screen_fields(self):
        t = self.tracker()
        t.handle(msg())
        live = t.live()
        self.assertEqual(live["phase"], "Printing")
        self.assertEqual(live["temps"], {"nozzle": {"cur": 220.0, "target": 220.0}, "bed": {"cur": 55.0, "target": 55.0},
                                         "chamber": {"cur": 35, "target": None}})
        self.assertEqual(live["speed"]["name"], "Standard")
        self.assertEqual(live["tray"]["color"], "F7CED7")
        self.assertEqual((live["plate_idx"], live["plate_cnt"], live["percent"], live["remaining_min"]), (1, 2, 12, 160))
        self.assertEqual(live["fans"]["part"], 100)
        json.dumps(live)


class AppTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(app.DB_PATH):
            os.remove(app.DB_PATH)
        app.init_db()
        self.c = app.app.test_client()

    def test_nozzle_filter(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO prints (name, nozzle) VALUES ('four', 0.4), ('six', 0.6), ('none', NULL)")
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn('<option value="0.4"', html)
        self.assertIn('<option value="0.6"', html)
        html = self.c.get("/?nozzle=0.6").get_data(as_text=True)
        self.assertIn(">six<", html)
        self.assertNotIn(">four<", html)
        self.assertIn('<option value="0.6" selected', html)

    def test_form_saves_new_fields(self):
        self.c.post("/print/new", data={"name": "x", "nozzle": "0.4", "nozzle_type": "Hardened steel (HS01)",
                                        "speed_profile": "sport", "plate": "2", "plate_count": "3",
                                        "spaghetti_detection": "1", "first_layer_inspection": "0"})
        with ml.connect_db(app.DB_PATH) as db:
            p = db.execute("SELECT * FROM prints").fetchone()
        self.assertEqual((p["nozzle_type"], p["speed_profile"], p["plate"], p["plate_count"],
                          p["spaghetti_detection"], p["first_layer_inspection"]),
                         ("Hardened steel (HS01)", "sport", 2, 3, 1, 0))
        html = self.c.get("/print/1").get_data(as_text=True)
        self.assertIn("plate 2 of 3", html)
        self.assertIn("Hardened steel (HS01)", html)
        self.assertIn("<dd>Sport</dd>", html)
        self.assertIn("Spaghetti on", html)
        self.assertIn("first layer off", html)
        self.c.post("/print/1/edit", data={"name": "x", "speed_profile": "Turbo"})   # unknown value: not stored
        with ml.connect_db(app.DB_PATH) as db:
            self.assertIsNone(db.execute("SELECT speed_profile FROM prints").fetchone()[0])
        self.c.post("/print/1/edit", data={"name": "x", "spaghetti_detection": ""})
        with ml.connect_db(app.DB_PATH) as db:
            self.assertIsNone(db.execute("SELECT spaghetti_detection FROM prints").fetchone()[0])

    def test_detail_has_chart_data(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO prints (id, name) VALUES (1, 'x')")
            db.executemany("INSERT INTO print_samples (print_id, recorded_at, nozzle, nozzle_target, bed, bed_target, "
                           "chamber, stage, percent, layer) VALUES (1, ?, ?, 220, ?, 55, 30, ?, ?, ?)",
                           [("2026-11-22T14:00:00+01:00", 25, 25, 2, 0, 0), ("2026-11-22T14:01:00+01:00", 220, 55, 0, 1, 1)])
        html = self.c.get("/print/1").get_data(as_text=True)
        self.assertIn('id="tempchart"', html)
        self.assertIn("Heating bed", html)
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO prints (id, name) VALUES (2, 'without')")
        self.assertNotIn('id="tempchart"', self.c.get("/print/2").get_data(as_text=True))

    def test_api_live_adds_cover_and_camera(self):
        from unittest import mock
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO printers (id, name, ip, access_code, photo) VALUES (1, 'P2S', '10.0.0.2', 'x', 'pr.jpg')")
            db.execute("INSERT INTO prints (id, name, printer_id) VALUES (5, 'running', 1)")
            db.execute("INSERT INTO files (print_id, kind, stored_name, original_name) VALUES (5, 'photo', 't.png', 'thumbnail-plate-1.png')")
        live = json.dumps({"printers": [{"printer_id": 1, "print_id": 5}]}).encode()
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = live
        with mock.patch("urllib.request.urlopen", return_value=resp):
            p = self.c.get("/api/live").get_json()["printers"][0]
        self.assertEqual((p["cover"], p["own_cover"], p["printer_photo"], p["camera"]),
                         ("/uploads/t.png", False, "/uploads/pr.jpg", True))


class FormatTest(unittest.TestCase):
    def test_dates_are_english_24h(self):
        self.assertEqual(app.clock("2026-11-22T16:42:00+01:00"), "22 Nov 2026, 16:42")
        self.assertEqual(app.clock("2026-01-05T09:07:00+01:00"), "5 Jan 2026, 09:07")
        self.assertEqual(app.day("2026-11-22"), "22 Nov 2026")
        self.assertEqual(app.clock(None), "–")
        self.assertEqual(app.duration(125), "2h 05m")

    def test_labels(self):
        self.assertEqual([app.label(v) for v in ("power_loss", "succeeded", "reprint", "standard", "ludicrous")],
                         ["Power loss", "Succeeded", "Reprint", "Standard", "Ludicrous"])
        self.assertEqual(app.label("some_new_value"), "Some new value")
        self.assertEqual(app.label(None), "–")
        for values in (app.STATUSES, app.FAIL_REASONS, app.SPEED_PROFILES):   # snake_case, each with a label
            for v in values:
                self.assertRegex(v, r"^[a-z]+(_[a-z]+)*$")
                self.assertIn(v, app.LABELS)


if __name__ == "__main__":
    unittest.main()
