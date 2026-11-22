"""Other Bambu Lab models: model list, camera route per model, the port-6000 JPEG camera and the older or
dual-nozzle report formats.

The report fragments below are built from the formats documented in ha-bambulab (pybambu/models.py
comments), not from real reports of these printers: only the P2S has been tested on hardware.

Run:  python3 -m unittest discover -s tests
"""
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DATA_DIR", _tmp)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402
import bambu_models as bm  # noqa: E402
import bambu_status as bs  # noqa: E402
import camera  # noqa: E402
import mqtt_listener as ml  # noqa: E402

CODE = "12345678"


def jpeg(n, size=20):
    return camera.SOI + bytes([n]) * size + camera.EOI


def packet(data):
    return struct.pack("<IIII", len(data), 0, 1, 0) + data


class ModelsTest(unittest.TestCase):
    def test_guess_model(self):
        cases = {"Bambu Lab P2S": "p2s", "Bambu Lab P2S + AMS 2 Pro": "p2s", "A1 mini": "a1_mini",
                 "Bambu A1 Mini combo": "a1_mini", "A1": "a1", "X1 Carbon": "x1c", "X1C": "x1c", "X1E": "x1e",
                 "X1": "x1", "H2D Pro": "h2d_pro", "H2D": "h2d", "H2S": "h2s", "H2C": "h2c", "p1s": "p1s",
                 "P1P (modded)": "p1p", "Prusa MK4": None, "": None, None: None, "A15": None}
        for text, expected in cases.items():
            self.assertEqual(bm.guess_model(text), expected, text)

    def test_camera_route(self):
        self.assertEqual(bm.camera_route("p2s"), "rtsps")
        self.assertEqual(bm.camera_route("x1c"), "rtsps")
        self.assertEqual(bm.camera_route("h2d"), "rtsps")
        for model in ("a1", "a1_mini", "p1p", "p1s"):
            self.assertEqual(bm.camera_route(model), "tcp6000", model)
        self.assertEqual(bm.camera_route("other", "tcp6000"), "tcp6000")
        self.assertEqual(bm.camera_route("other", "rtsps"), "rtsps")
        self.assertIsNone(bm.camera_route("other", "none"))
        self.assertIsNone(bm.camera_route("other", None))
        self.assertEqual(bm.camera_route(None), "rtsps")            # no model chosen: as before
        self.assertEqual(bm.camera_route("p1s", "rtsps"), "tcp6000")  # manual choice only counts for 'other'
        base = {"ip": "10.0.0.2", "access_code": CODE}
        self.assertEqual(bm.printer_camera({**base, "bambu_model": "a1"}), "tcp6000")
        self.assertIsNone(bm.printer_camera({**base, "access_code": ""}))
        self.assertIsNone(bm.printer_camera({"ip": "", "access_code": CODE, "bambu_model": "p2s"}))

    def test_internal_values_are_snake_case(self):
        for value in bm.MODELS:
            self.assertRegex(value, r"^[a-z0-9_]+$")
        self.assertEqual(app.LABELS["a1_mini"], "A1 mini")


class PrinterFormTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(app.DB_PATH):
            os.remove(app.DB_PATH)
        app.init_db()
        app.cameras = camera.CameraManager()
        app._snapshots.clear()
        self.c = app.app.test_client()

    def printer(self, pid):
        with ml.connect_db(app.DB_PATH) as db:
            return dict(db.execute("SELECT * FROM printers WHERE id = ?", (pid,)).fetchone())

    def test_migration_recognises_the_model_once(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO printers (id, name, model) VALUES (1, 'a', 'Bambu Lab P2S + AMS 2 Pro'), "
                       "(2, 'b', 'my printer'), (3, 'c', 'X1 Carbon')")
            db.execute("UPDATE printers SET bambu_model = 'other' WHERE id = 3")   # chosen by hand: stays
        app.init_db()
        self.assertEqual([self.printer(i)["bambu_model"] for i in (1, 2, 3)], ["p2s", None, "other"])
        self.assertEqual(self.printer(1)["model"], "Bambu Lab P2S + AMS 2 Pro")   # free text kept

    def test_form_saves_model_and_camera_type(self):
        self.c.post("/printers/new", data={"name": "mini", "bambu_model": "a1_mini", "camera_type": "rtsps",
                                           "model": "+ AMS lite"})
        p = self.printer(1)
        self.assertEqual((p["bambu_model"], p["camera_type"], p["model"]), ("a1_mini", None, "+ AMS lite"))
        self.c.post("/printers/1/edit", data={"name": "mini", "bambu_model": "other", "camera_type": "tcp6000"})
        self.assertEqual((self.printer(1)["bambu_model"], self.printer(1)["camera_type"]), ("other", "tcp6000"))
        self.c.post("/printers/1/edit", data={"name": "mini", "bambu_model": "z9", "camera_type": "telnet"})
        self.assertEqual((self.printer(1)["bambu_model"], self.printer(1)["camera_type"]), (None, None))
        html = self.c.get("/printers/1/edit").get_data(as_text=True)
        self.assertIn('<option value="a1_mini" >A1 mini</option>', html)
        self.assertIn('id="camera-type-field" hidden', html)

    def test_camera_routes_follow_the_model(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO printers (id, name, ip, access_code, bambu_model, camera_type) VALUES "
                       "(1, 'p1s', '10.0.0.2', ?, 'p1s', NULL), (2, 'x1c', '10.0.0.3', ?, 'x1c', NULL), "
                       "(3, 'odd', '10.0.0.4', ?, 'other', 'none'), (4, 'odd6000', '10.0.0.5', ?, 'other', 'tcp6000')",
                       (CODE, CODE, CODE, CODE))
            db.execute("INSERT INTO prints (name, status, printer_id) VALUES ('a', 'printing', 1), ('b', 'printing', 3)")
        with mock.patch("camera.grab_snapshot", return_value=jpeg(1)) as grab:
            self.assertEqual(self.c.get("/printers/1/snapshot.jpg").status_code, 200)
            self.assertEqual(grab.call_args.kwargs["route"], "tcp6000")
            self.c.get("/printers/2/snapshot.jpg")
            self.assertEqual(grab.call_args.kwargs["route"], "rtsps")
            self.c.get("/printers/4/snapshot.jpg")
            self.assertEqual(grab.call_args.kwargs["route"], "tcp6000")
        self.assertEqual(self.c.get("/printers/3/snapshot.jpg").status_code, 404)   # other + no camera
        self.assertEqual(self.c.get("/printers/3/camera.mjpg").status_code, 404)
        html = self.c.get("/").get_data(as_text=True)
        self.assertIn('data-printer="1"', html)          # P1S card shows the camera image
        self.assertNotIn('data-printer="3"', html)       # no camera: no live image
        html = self.c.get("/printers").get_data(as_text=True)
        self.assertIn("JPEG stream (port 6000)", html)
        self.assertNotIn("/printers/3/camera.mjpg", html)

    def test_listener_end_photo_uses_the_route(self):
        sup = ml.Supervisor(app.DB_PATH)
        cfg = {"id": 1, "ip": "10.0.0.2", "access_code": CODE, "bambu_model": "a1", "camera_type": None}
        with mock.patch("camera.grab_snapshot", side_effect=camera.CameraError("x")) as grab, \
                self.assertLogs("printvault.mqtt", "WARNING"):
            sup._snapshot_worker(cfg, 1, "succeeded")
        self.assertEqual(grab.call_args.kwargs["route"], "tcp6000")
        with mock.patch("camera.grab_snapshot") as grab, self.assertLogs("printvault.mqtt", "INFO"):
            sup._snapshot_worker({**cfg, "bambu_model": "other"}, 1, "succeeded")
        grab.assert_not_called()


class FakeCamera:
    """A port-6000 camera on one end of a socket pair: checks the login packet, then sends `chunks`."""

    def __init__(self, chunks, close_after=True):
        self.client, self.server = socket.socketpair()
        self.chunks, self.close_after = chunks, close_after
        self.login = None
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        buf = b""
        while len(buf) < 80:
            data = self.server.recv(80 - len(buf))
            if not data:
                return
            buf += data
        self.login = buf
        for chunk in self.chunks:
            self.server.sendall(chunk)
        if self.close_after:
            self.server.close()

    def connect(self, ip, timeout):
        return self.client


class JpegStreamTest(unittest.TestCase):
    def test_login_packet(self):
        p = camera.jpeg_auth_packet(CODE)
        self.assertEqual(len(p), 80)
        self.assertEqual(p[:16], struct.pack("<IIII", 0x40, 0x3000, 0, 0))
        self.assertEqual(p[16:48], b"bblp" + b"\0" * 28)
        self.assertEqual(p[48:], CODE.encode() + b"\0" * 24)
        with self.assertRaises(camera.CameraError):
            camera.jpeg_auth_packet("x" * 33)
        with self.assertRaises(camera.CameraError):
            camera.jpeg_auth_packet("codé")

    def test_frames_in_odd_chunks(self):
        data = packet(jpeg(1)) + packet(jpeg(2, 5000)) + packet(b"not a jpeg") + packet(jpeg(3))
        chunks = [data[i:i + 7] for i in range(0, 49, 7)] + [data[49:]]   # header split over reads too
        fake = FakeCamera(chunks)
        with camera.JpegStream("10.0.0.2", CODE, timeout=5, connect=fake.connect) as stream:
            got = []
            with self.assertLogs("printvault.camera", "WARNING"):   # the non-JPEG image is skipped
                with self.assertRaises(camera.CameraError):          # then the printer closes
                    for frame in stream.frames():
                        got.append(frame)
        self.assertEqual(got, [jpeg(1), jpeg(2, 5000), jpeg(3)])
        self.assertEqual(fake.login, camera.jpeg_auth_packet(CODE))

    def test_closed_connection_and_bad_size(self):
        fake = FakeCamera([])
        with camera.JpegStream("10.0.0.2", CODE, timeout=5, connect=fake.connect) as stream:
            with self.assertRaisesRegex(camera.CameraError, "wrong access code"):
                next(stream.frames())
        fake = FakeCamera([struct.pack("<IIII", 0xFFFFFFF0, 0, 1, 0)])
        with camera.JpegStream("10.0.0.2", CODE, timeout=5, connect=fake.connect) as stream:
            with self.assertRaisesRegex(camera.CameraError, "Unexpected"):
                next(stream.frames())

    def test_timeout(self):
        fake = FakeCamera([], close_after=False)
        with camera.JpegStream("10.0.0.2", CODE, timeout=0.3, connect=fake.connect) as stream:
            with self.assertRaisesRegex(camera.CameraError, "within"):
                next(stream.frames())
        fake.server.close()

    def test_no_connection(self):
        def refuse(ip, timeout):
            raise ConnectionRefusedError("refused")
        with self.assertRaisesRegex(camera.CameraError, "port 6000"):
            camera.JpegStream("10.0.0.2", CODE, connect=refuse).open()

    def test_grab_snapshot_and_live_camera(self):
        fake = FakeCamera([packet(jpeg(4))])
        with mock.patch("camera._tls_connect", fake.connect):
            self.assertEqual(camera.grab_snapshot("10.0.0.2", CODE, route="tcp6000"), jpeg(4))
        fake = FakeCamera([packet(jpeg(5)), packet(jpeg(6))], close_after=False)
        with mock.patch("camera._tls_connect", fake.connect):
            cam = camera.Camera("10.0.0.2", CODE, route="tcp6000", idle=0)
            frames = cam.frames(stall=5)
            self.assertEqual(next(frames), jpeg(5))
            self.assertEqual(next(frames), jpeg(6))
            frames.close()
            cam.close()
        fake.server.close()
        with self.assertRaisesRegex(camera.CameraError, "no camera"):
            camera.grab_snapshot("10.0.0.2", CODE, route=None)

    def test_manager_new_camera_for_new_route(self):
        m = camera.CameraManager()
        a = m.get(1, "10.0.0.2", CODE, "rtsps")
        self.assertIs(m.get(1, "10.0.0.2", CODE, "rtsps"), a)
        b = m.get(1, "10.0.0.2", CODE, "tcp6000")
        self.assertIsNot(a, b)
        self.assertTrue(a.closed)


class ReportFormatsTest(unittest.TestCase):
    """Fields as other models and older firmware send them; missing fields are simply left out."""

    X1C_OLD = {"gcode_state": "RUNNING", "stg_cur": 0, "nozzle_temper": 219.8, "nozzle_target_temper": 220.0,
               "bed_temper": 54.9, "bed_target_temper": 55.0, "chamber_temper": 31.5, "nozzle_diameter": "0.4",
               "nozzle_type": "hardened_steel", "cooling_fan_speed": "15", "big_fan1_speed": "10",
               "big_fan2_speed": "0", "heatbreak_fan_speed": "15", "spd_lvl": 2, "spd_mag": 100,
               "ams": {"tray_now": "254", "ams": []},
               "vt_tray": {"id": "254", "tray_type": "PETG", "tray_color": "FF6A13FF", "tray_sub_brands": "PETG HF"}}
    P1S = {"gcode_state": "RUNNING", "stg_cur": 2, "nozzle_temper": 30, "nozzle_target_temper": 0,
           "bed_temper": 48.5, "bed_target_temper": 65, "nozzle_diameter": "0.4", "nozzle_type": "stainless_steel",
           "cooling_fan_speed": "0", "big_fan1_speed": "0", "big_fan2_speed": "0", "heatbreak_fan_speed": "0"}
    A1 = {"gcode_state": "PREPARE", "nozzle_temper": "24.0", "bed_temper": "23.5", "cooling_fan_speed": "0",
          "heatbreak_fan_speed": "0"}
    H2D = {"gcode_state": "RUNNING", "stg_cur": 0, "nozzle_temper": 250, "nozzle_target_temper": 250,
           "device": {"extruder": {"info": [{"id": 0, "temp": (0 << 16) | 38},
                                            {"id": 1, "temp": (250 << 16) | 249}], "state": 0x12},
                      "bed": {"info": {"temp": (60 << 16) | 59}},
                      "ctc": {"info": {"temp": 43}},
                      "nozzle": {"info": [{"id": 0, "diameter": 0.4, "type": "HS01"},
                                          {"id": 1, "diameter": 0.6, "type": "HH01"}]}}}

    def test_x1c_older_firmware(self):
        s = self.X1C_OLD
        self.assertEqual(bs.temperatures(s), {"nozzle": (219.8, 220.0), "bed": (54.9, 55.0), "chamber": (31.5, None)})
        self.assertEqual(bs.fans(s), {"part": 100, "aux": 70, "chamber": 0, "heatbreak": 100})
        tray = bs.active_tray(s)
        self.assertEqual((tray["label"], tray["type"], tray["color"]), ("External spool", "PETG", "FF6A13"))
        self.assertEqual(bs.print_settings(s)["nozzle_type"], "Hardened steel")
        self.assertEqual(bs.print_settings(s)["nozzle"], 0.4)
        self.assertIsNone(bs.nozzles(s))

    def test_p1s_and_a1_without_chamber(self):
        t = bs.temperatures(self.P1S)
        self.assertEqual(t["chamber"], (None, None))
        self.assertEqual(t["bed"], (48.5, 65.0))
        self.assertEqual(bs.phase(self.P1S)[1], "Heating bed")
        self.assertEqual(bs.print_settings(self.P1S)["nozzle_type"], "Stainless steel")
        t = bs.temperatures(self.A1)   # numbers as strings, no targets, no aux/chamber fan
        self.assertEqual((t["nozzle"][0], t["bed"][0], t["chamber"]), (24.0, 23.5, (None, None)))
        self.assertEqual(bs.fans(self.A1), {"part": 0, "heatbreak": 0})
        self.assertIsNone(bs.active_tray(self.A1))
        self.assertIsNone(bs.speed(self.A1))
        sample = bs.sample(self.A1)
        self.assertEqual((sample["chamber"], sample["percent"]), (None, None))

    def test_h2d_dual_nozzle(self):
        s = self.H2D
        self.assertEqual(bs.active_nozzle(s), 1)
        self.assertEqual(bs.nozzles(s), [
            {"id": 0, "name": "Right", "cur": 38, "target": None, "active": False},
            {"id": 1, "name": "Left", "cur": 249, "target": 250, "active": True}])
        t = bs.temperatures(s)
        self.assertEqual(t["nozzle"], (249, 250))   # the active nozzle
        self.assertEqual(t["bed"], (59, 60))
        self.assertEqual(t["chamber"], (43, None))  # plain number under device.ctc
        settings = bs.print_settings(s)
        self.assertEqual((settings["nozzle"], settings["nozzle_type"]), (0.6, "Hardened steel, high-flow (HH01)"))

    def test_live_json_has_both_nozzles(self):
        t = ml.PrinterTracker(1, "H2D", app.DB_PATH)
        t.connected = True
        t.state = dict(self.H2D)
        live = t.live()
        self.assertEqual([n["active"] for n in live["nozzles"]], [False, True])
        self.assertIsNone(ml.PrinterTracker(2, "X1C", app.DB_PATH).live()["nozzles"])

    def test_temperature_formats(self):
        self.assertEqual(bs.unpack_temp((220 << 16) | 218), (218, 220))
        self.assertEqual(bs.unpack_temp(43), (43, 0))
        self.assertEqual(bs.unpack_temp("31.5"), (31.5, None))
        self.assertEqual(bs.unpack_temp(None), (None, None))
        self.assertEqual(bs.unpack_temp(-1), (None, None))
        self.assertEqual(bs.unpack_temp("x"), (None, None))


if __name__ == "__main__":
    unittest.main()
