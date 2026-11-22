"""Tests voor camera.py en het camerabeeld bij het einde van een print (geen netwerk; ffmpeg wordt nagebootst).

Draaien:  python3 -m unittest discover -s tests
"""
import logging
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DATA_DIR", _tmp)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402
import camera  # noqa: E402
import mqtt_listener as ml  # noqa: E402

TZ = ZoneInfo("Europe/Brussels")
CODE = "a/b@c:12"   # met tekens die in een URL gecodeerd worden


def jpeg(n):
    return camera.SOI + bytes([n]) * 10 + camera.EOI


class HelpersTest(unittest.TestCase):
    def test_rtsp_url_quotes_code(self):
        self.assertEqual(camera.rtsp_url("192.0.2.10", CODE),
                         "rtsps://bblp:a%2Fb%40c%3A12@192.0.2.10:322/streaming/live/1")

    def test_redact(self):
        url = camera.rtsp_url("192.0.2.10", CODE)
        for text in (f"{url}: Connection refused", f"fout met {CODE}", "rtsps://bblp:wat-dan-ook@host:322/x"):
            out = camera.redact(text, CODE)
            self.assertNotIn(CODE, out)
            self.assertNotIn("a%2Fb%40c%3A12", out)
            self.assertNotIn("wat-dan-ook", out)
        self.assertIn("rtsps://bblp:***@192.0.2.10", camera.redact(url))

    def test_cmd_has_tcp_and_single_frame(self):
        cmd = camera.ffmpeg_cmd("rtsps://x", single=True)
        self.assertIn("tcp", cmd)
        self.assertEqual(cmd[cmd.index("-frames:v") + 1], "1")
        self.assertIn("-vf", camera.ffmpeg_cmd("rtsps://x"))

    def test_frame_parser_across_chunks(self):
        p = camera.FrameParser()
        data = b"rommel" + jpeg(1) + jpeg(2) + jpeg(3)
        out = []
        for i in range(0, len(data), 7):
            out += p.feed(data[i:i + 7])
        self.assertEqual(out, [jpeg(1), jpeg(2), jpeg(3)])
        self.assertEqual(p.feed(b"\xff"), [])


class SnapshotTest(unittest.TestCase):
    def run_result(self, code, out=b"", err=b""):
        return subprocess.CompletedProcess([], code, stdout=out, stderr=err)

    def test_ok(self):
        with mock.patch("subprocess.run", return_value=self.run_result(0, jpeg(5))) as run:
            self.assertEqual(camera.grab_snapshot("10.0.0.2", CODE), jpeg(5))
        self.assertIn(camera.rtsp_url("10.0.0.2", CODE), run.call_args[0][0])

    def test_error_is_redacted(self):
        err = f"{camera.rtsp_url('10.0.0.2', CODE)}: Connection refused\n".encode()
        with mock.patch("subprocess.run", return_value=self.run_result(1, err=err)):
            with self.assertRaises(camera.CameraError) as cm:
                camera.grab_snapshot("10.0.0.2", CODE)
        self.assertIn("Connection refused", str(cm.exception))
        self.assertNotIn("a%2Fb%40c%3A12", str(cm.exception))

    def test_timeout_and_missing_ffmpeg(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
            self.assertRaises(camera.CameraError, camera.grab_snapshot, "10.0.0.2", CODE)
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertRaises(camera.CameraError, camera.grab_snapshot, "10.0.0.2", CODE)


FAKE_FFMPEG = textwrap.dedent("""\
    #!{python}
    import sys, time
    n = 0
    while True:
        sys.stdout.buffer.write(b"\\xff\\xd8" + bytes([n % 256]) * 50 + b"\\xff\\xd9")
        sys.stdout.buffer.flush()
        n += 1
        time.sleep(0.05)
""")


class LiveStreamTest(unittest.TestCase):
    """Camera met een nagebootste ffmpeg die doorlopend JPEG's schrijft."""

    def setUp(self):
        self.bindir = tempfile.mkdtemp()
        path = os.path.join(self.bindir, "ffmpeg")
        with open(path, "w") as fh:
            fh.write(FAKE_FFMPEG.format(python=sys.executable))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        patcher = mock.patch.dict(os.environ, {"PATH": self.bindir + os.pathsep + os.environ["PATH"]})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_shared_stream_stops_when_idle(self):
        cam = camera.Camera("10.0.0.2", CODE, idle=0.3)
        self.addCleanup(cam.close)
        a, b = cam.frames(stall=5), cam.frames(stall=5)
        fa, fb = next(a), next(b)
        self.assertTrue(fa.startswith(camera.SOI) and fb.startswith(camera.SOI))
        proc = cam.proc
        self.assertIsNotNone(proc)
        self.assertIsNotNone(cam.latest())
        next(a)
        a.close()
        b.close()
        self.assertEqual(cam.viewers, 0)
        deadline = time.monotonic() + 5
        while cam.thread is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(cam.thread)
        self.assertIsNotNone(proc.poll())        # ffmpeg is gestopt
        self.assertIsNone(cam.latest())
        self.assertTrue(next(cam.frames(stall=5)).startswith(camera.SOI))   # en start opnieuw

    def test_manager_replaces_on_new_code(self):
        m = camera.CameraManager()
        c1 = m.get(1, "10.0.0.2", "x")
        self.assertIs(m.get(1, "10.0.0.2", "x"), c1)
        c2 = m.get(1, "10.0.0.2", "y")
        self.assertIsNot(c2, c1)
        self.assertTrue(c1.closed)
        self.assertIs(m.peek(1), c2)


def report(state, task="1001"):
    return {"print": {"gcode_state": state, "subtask_name": "Bed_scraper", "task_id": task, "print_error": 0}}


class EndSnapshotTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(app.DB_PATH):
            os.remove(app.DB_PATH)
        app.init_db()
        with ml.connect_db(app.DB_PATH) as db:
            self.printer_id = db.execute("INSERT INTO printers (name, ip, serial, access_code, mqtt_enabled) "
                                         "VALUES ('P2S', '10.0.0.2', 'SER', ?, 1)", (CODE,)).lastrowid
        self.calls = []

    def tracker(self):
        t = ml.PrinterTracker(self.printer_id, "P2S", app.DB_PATH)
        t.snapshotter = lambda print_id, status: self.calls.append((print_id, status))
        return t

    def test_called_on_finish_and_failed(self):
        t = self.tracker()
        t.handle(report("RUNNING", "1"))
        t.handle(report("FINISH", "1"))
        t.handle(report("RUNNING", "2"))
        t.handle(report("FAILED", "2"))
        self.assertEqual(self.calls, [(1, "gelukt"), (2, "mislukt")])

    def test_not_called_when_end_not_seen_live(self):
        self.tracker().handle(report("RUNNING"))
        self.calls.clear()
        self.tracker().handle(report("FINISH"))    # listener herstart na het einde
        self.assertEqual(self.calls, [])

    def test_snapshotter_error_does_not_break_tracking(self):
        t = self.tracker()
        t.snapshotter = mock.Mock(side_effect=RuntimeError("stuk"))
        t.handle(report("RUNNING"))
        with self.assertLogs("printvault.mqtt", logging.ERROR):
            t.handle(report("FINISH"))
        with ml.connect_db(app.DB_PATH) as db:
            self.assertEqual(db.execute("SELECT status FROM prints").fetchone()[0], "gelukt")

    # ---------- save_snapshot ----------

    def new_print(self, photos=(), cover_index=None):
        with ml.connect_db(app.DB_PATH) as db:
            pid = db.execute("INSERT INTO prints (name) VALUES ('x')").lastrowid
            ids = [db.execute("INSERT INTO files (print_id, kind, stored_name, original_name) VALUES (?, 'photo', ?, ?)",
                              (pid, f"s{i}.jpg", name)).lastrowid for i, name in enumerate(photos)]
            if cover_index is not None:
                db.execute("UPDATE prints SET cover_file_id = ? WHERE id = ?", (ids[cover_index], pid))
        return pid, ids

    def save(self, pid):
        return ml.save_snapshot(app.DB_PATH, app.UPLOAD_DIR, pid, jpeg(9), "gelukt", datetime(2026, 11, 22, 15, 30, tzinfo=TZ))

    def cover(self, pid):
        with app.app.test_request_context():
            return app.get_db().execute(f"SELECT {app.COVER_SQL} FROM prints p WHERE p.id = ?", (pid,)).fetchone()[0]

    def test_becomes_cover_without_photos(self):
        pid, _ = self.new_print()
        fid = self.save(pid)
        with ml.connect_db(app.DB_PATH) as db:
            f = db.execute("SELECT * FROM files WHERE id = ?", (fid,)).fetchone()
            self.assertEqual(db.execute("SELECT cover_file_id FROM prints WHERE id = ?", (pid,)).fetchone()[0], fid)
        self.assertEqual((f["kind"], f["original_name"], f["size"]), ("photo", "camera-gelukt-20261122-1530.jpg", 14))
        with open(os.path.join(app.UPLOAD_DIR, f["stored_name"]), "rb") as fh:
            self.assertEqual(fh.read(), jpeg(9))
        self.assertEqual(self.cover(pid), f["stored_name"])

    def test_keeps_existing_thumbnail_as_cover(self):
        pid, ids = self.new_print(["thumbnail-plaat-1.png"])
        self.save(pid)
        self.assertEqual(self.cover(pid), "s0.jpg")

    def test_keeps_chosen_cover(self):
        pid, ids = self.new_print(["eigen.jpg", "tweede.jpg"], cover_index=1)
        self.save(pid)
        self.assertEqual(self.cover(pid), "s1.jpg")

    def test_deleted_print_removes_file(self):
        before = set(os.listdir(app.UPLOAD_DIR))
        self.assertRaises(LookupError, self.save, 999)
        self.assertEqual(set(os.listdir(app.UPLOAD_DIR)), before)

    def test_worker_logs_redacted_and_continues(self):
        sup = ml.Supervisor(app.DB_PATH)
        cfg = sup.configs()[self.printer_id]
        err = camera.CameraError(f"mislukt: {camera.rtsp_url('10.0.0.2', CODE)}")
        with mock.patch("camera.grab_snapshot", side_effect=err), \
                self.assertLogs("printvault.mqtt", logging.WARNING) as logs:
            sup._snapshot_worker(cfg, 1, "gelukt")
        out = "\n".join(logs.output)
        self.assertIn("camerabeeld bij print 1 mislukt", out)
        self.assertNotIn("a%2Fb%40c%3A12", out)
        self.assertNotIn(CODE, out)


class CameraRoutesTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(app.DB_PATH):
            os.remove(app.DB_PATH)
        app.init_db()
        app.cameras = camera.CameraManager()
        app._snapshots.clear()
        self.c = app.app.test_client()
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO printers (name, ip, access_code) VALUES ('met', '10.0.0.2', ?)", (CODE,))
            db.execute("INSERT INTO printers (name) VALUES ('zonder')")

    def test_404_without_camera_settings(self):
        self.assertEqual(self.c.get("/printers/2/camera.mjpg").status_code, 404)
        self.assertEqual(self.c.get("/printers/2/snapshot.jpg").status_code, 404)
        self.assertEqual(self.c.get("/printers/9/snapshot.jpg").status_code, 404)

    def test_snapshot(self):
        with mock.patch("camera.grab_snapshot", return_value=jpeg(4)):
            r = self.c.get("/printers/1/snapshot.jpg")
        self.assertEqual((r.status_code, r.mimetype, r.data), (200, "image/jpeg", jpeg(4)))

    def test_snapshot_cached(self):
        with mock.patch("camera.grab_snapshot", return_value=jpeg(4)) as grab:
            for _ in range(3):
                self.assertEqual(self.c.get("/printers/1/snapshot.jpg").data, jpeg(4))
            self.assertEqual(grab.call_count, 1)
            with mock.patch.object(app, "SNAPSHOT_CACHE_SECONDS", 0):
                self.c.get("/printers/1/snapshot.jpg")
            self.assertEqual(grab.call_count, 2)

    def test_snapshot_failure_cached_too(self):
        with mock.patch("camera.grab_snapshot", side_effect=camera.CameraError("weg")) as grab:
            codes = [self.c.get("/printers/1/snapshot.jpg").status_code for _ in range(3)]
        self.assertEqual((codes, grab.call_count), ([502] * 3, 1))

    def test_snapshot_cache_follows_new_code(self):
        with mock.patch("camera.grab_snapshot", return_value=jpeg(4)) as grab:
            self.c.get("/printers/1/snapshot.jpg")
            with ml.connect_db(app.DB_PATH) as db:
                db.execute("UPDATE printers SET access_code = 'nieuw' WHERE id = 1")
            self.c.get("/printers/1/snapshot.jpg")
        self.assertEqual(grab.call_count, 2)

    def test_snapshot_prefers_running_live_stream(self):
        cam = app.cameras.get(1, "10.0.0.2", CODE)
        with mock.patch.object(cam, "latest", return_value=jpeg(7)), mock.patch("camera.grab_snapshot") as grab:
            self.assertEqual(self.c.get("/printers/1/snapshot.jpg").data, jpeg(7))
        grab.assert_not_called()

    def test_running_print_card_shows_snapshot(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO prints (id, name, status, printer_id) VALUES "
                       "(1, 'loopt', 'bezig', 1), (2, 'klaar', 'gelukt', 1), (3, 'zonder camera', 'bezig', 2), "
                       "(4, 'met foto', 'bezig', 1)")
            db.execute("INSERT INTO files (print_id, kind, stored_name, original_name) VALUES (4, 'photo', 'f.jpg', 'f.jpg')")
        html = self.c.get("/").get_data(as_text=True)
        self.assertEqual(html.count('class="livesnap"'), 1)
        self.assertIn('data-printer="1" data-print="1"', html)
        self.assertIn("/printers/1/snapshot.jpg", html)
        self.assertEqual(html.count('<div class="nothumb">geen foto</div>'), 2)   # klaar + zonder camera
        self.assertNotIn(CODE, html)

    def test_snapshot_error_redacted(self):
        err = camera.CameraError(camera.redact(f"{camera.rtsp_url('10.0.0.2', CODE)} weg", CODE))
        with mock.patch("camera.grab_snapshot", side_effect=err):
            r = self.c.get("/printers/1/snapshot.jpg")
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("a%2Fb%40c%3A12", r.get_data(as_text=True))

    def test_stream_sends_multipart_frames(self):
        fake = mock.Mock(frames=lambda: iter([jpeg(1), jpeg(2)]))
        with mock.patch.object(app.cameras, "get", return_value=fake):
            r = self.c.get("/printers/1/camera.mjpg")
            body = r.get_data()
        self.assertTrue(r.content_type.startswith("multipart/x-mixed-replace; boundary=frame"))
        self.assertEqual(body.count(b"--frame\r\nContent-Type: image/jpeg"), 2)
        self.assertIn(jpeg(2), body)

    def test_printers_page_links_camera_only_when_configured(self):
        html = self.c.get("/printers").get_data(as_text=True)
        self.assertIn("/printers/1/camera.mjpg", html)
        self.assertNotIn("/printers/2/camera.mjpg", html)
        self.assertNotIn(CODE, html)


if __name__ == "__main__":
    unittest.main()
