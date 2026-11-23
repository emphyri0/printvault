"""Security: cross-site requests, host check, secrets, uploads, redirects and headers."""
import http.server
import io
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import zipfile
from contextlib import closing
from pathlib import Path
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DATA_DIR", _tmp)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import app  # noqa: E402
import notify  # noqa: E402
import security  # noqa: E402
import slicer  # noqa: E402
import thumbs  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0image\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\nimage"


class Base(unittest.TestCase):
    def setUp(self):
        if os.path.exists(app.DB_PATH):
            os.remove(app.DB_PATH)
        app.init_db()
        add = mock.patch.object(app.THUMBS, "add")
        add.start()
        self.addCleanup(add.stop)
        self.c = app.app.test_client()

    def one(self, sql, *args):
        with closing(sqlite3.connect(app.DB_PATH)) as db:
            return db.execute(sql, args).fetchone()


class CrossSiteTest(Base):
    def post(self, headers, base_url="http://localhost"):
        return self.c.post("/print/new", data={"name": "x"}, headers=headers, base_url=base_url).status_code

    def test_cross_site_posts_are_refused(self):
        with self.assertLogs(app.app.logger, "WARNING") as logs:
            for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                            {"Origin": "http://evil.example"}, {"Origin": "null"},
                            {"Origin": "http://localhost:9999"}, {"Referer": "http://evil.example/x"},
                            {"Sec-Fetch-Site": "cross-site", "Origin": "http://localhost"}):
                self.assertEqual(self.post(headers), 403, headers)
        self.assertIn("cross-site", logs.output[0])
        self.assertEqual(self.one("SELECT COUNT(*) FROM prints"), (0,))

    def test_own_posts_work(self):
        for headers in ({"Sec-Fetch-Site": "same-origin"}, {"Sec-Fetch-Site": "none"}, {"Origin": "http://localhost"},
                        {"Referer": "http://localhost/print/new"}, {}):
            self.assertEqual(self.post(headers), 302, headers)
        self.assertEqual(self.post({"Origin": "http://192.0.2.10:8100"}, "http://192.0.2.10:8100"), 302)
        # HTTPS ends at the proxy, which passes the Host on: the scheme differs, the host is the same
        self.assertEqual(self.post({"Origin": "https://192.0.2.10"}, "http://192.0.2.10"), 302)

    def test_reading_is_not_affected(self):
        self.assertEqual(self.c.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 200)

    def test_every_post_route_is_covered(self):
        for rule in app.app.url_map.iter_rules():
            if "POST" in rule.methods:
                url = re.sub(r"<[^>]+>", "1", rule.rule)
                with self.assertLogs(app.app.logger, "WARNING"):
                    self.assertEqual(self.c.post(url, headers={"Origin": "http://evil.example"}).status_code, 403, url)


class HostTest(Base):
    def test_parse(self):
        self.assertEqual(security.parse_hosts(" Printvault.Example , nas.lan:8100,, "), {"printvault.example", "nas.lan"})
        self.assertEqual(security.parse_hosts(None), set())

    def test_allowed(self):
        ok = ("localhost", "localhost:8100", "127.0.0.1:8100", "192.0.2.10", "[::1]:8100", "[2001:db8::1]",
              "printvault.example", "PRINTVAULT.example:443", "printvault.example.")
        for host in ok:
            self.assertTrue(security.host_allowed(host, {"printvault.example"}), host)
        for host in ("evil.example", "printvault.example.evil.com", "sub.printvault.example", "", None,
                     "localhost.evil.example", "192.0.2.10.evil.example", "[::1", "999.1.1.1"):
            self.assertFalse(security.host_allowed(host, {"printvault.example"}), host)

    def test_refused_host_says_what_to_do(self):
        with self.assertLogs(app.app.logger, "WARNING") as logs:
            r = self.c.get("/", base_url="http://printvault.example")
        self.assertEqual(r.status_code, 421)
        self.assertEqual(r.mimetype, "text/plain")
        for text in (logs.output[0], r.get_data(as_text=True)):
            self.assertIn("printvault.example", text)
            self.assertIn("ALLOWED_HOSTS", text)
        self.assertNotIn("TRUSTED_PROXIES", logs.output[0])

    def test_log_hint_for_forgotten_trusted_proxies(self):
        with self.assertLogs(app.app.logger, "WARNING") as logs:
            self.c.get("/", base_url="http://backend", headers={"X-Forwarded-Host": "printvault.example"})
        self.assertIn("TRUSTED_PROXIES", logs.output[0])

    def test_log_injection(self):
        r = None
        with self.assertLogs(app.app.logger, "WARNING") as logs:
            r = self.c.get("/", headers={"Host": "evil.example", "X-Forwarded-Host": "a\x1b[31m"})
        self.assertEqual(r.status_code, 421)
        self.assertNotIn("\x1b", logs.output[0])

    def test_static_and_files_are_covered_too(self):
        for url in ("/static/app.js", "/uploads/x.jpg", "/api/live", "/nothing"):
            with self.assertLogs(app.app.logger, "WARNING"):
                self.assertEqual(self.c.get(url, base_url="http://evil.example").status_code, 421, url)

    def test_host_behind_proxy(self):
        original = app.app.wsgi_app
        app.app.wsgi_app = app.apply_proxy_fix(original, 1)
        try:
            with mock.patch.dict(app.app.config, {"ALLOWED_HOSTS": {"printvault.example"}}):
                h = {"X-Forwarded-Host": "printvault.example", "X-Forwarded-Proto": "https"}
                self.assertEqual(self.c.get("/", base_url="http://backend:5000", headers=h).status_code, 200)
                h["Origin"] = "https://printvault.example"
                self.assertEqual(self.c.post("/print/new", data={"name": "x"}, base_url="http://backend:5000",
                                             headers=h).status_code, 302)
                with self.assertLogs(app.app.logger, "WARNING"):
                    self.assertEqual(self.c.get("/", base_url="http://localhost",
                                                headers={"X-Forwarded-Host": "evil.example"}).status_code, 421)
        finally:
            app.app.wsgi_app = original

    def test_forwarded_host_ignored_without_proxy(self):
        with mock.patch.dict(app.app.config, {"ALLOWED_HOSTS": {"printvault.example"}}), \
                self.assertLogs(app.app.logger, "WARNING"):
            r = self.c.get("/", base_url="http://evil.example", headers={"X-Forwarded-Host": "printvault.example"})
        self.assertEqual(r.status_code, 421)


class SecretsTest(Base):
    def test_token_is_not_sent_to_a_new_server(self):
        self.c.post("/settings", data={"ntfy_url": "https://good.example", "ntfy_topic": "t", "ntfy_token": "tk_secret"})
        html = self.c.post("/settings", data={"ntfy_url": "https://other.example", "ntfy_topic": "t"},
                           follow_redirects=True).get_data(as_text=True)
        self.assertEqual(self.one("SELECT value FROM settings WHERE key='ntfy_token'"), ("",))
        self.assertIn("token", html)
        # a new token with the new server is fine; another path on the same server keeps it
        self.c.post("/settings", data={"ntfy_url": "https://other.example", "ntfy_topic": "t", "ntfy_token": "tk_2"})
        self.c.post("/settings", data={"ntfy_url": "https://other.example/ntfy", "ntfy_topic": "t"})
        self.assertEqual(self.one("SELECT value FROM settings WHERE key='ntfy_token'"), ("tk_2",))
        # nor by way of http or an empty URL
        self.c.post("/settings", data={"ntfy_url": "http://other.example/ntfy", "ntfy_topic": "t"})
        self.assertEqual(self.one("SELECT value FROM settings WHERE key='ntfy_token'"), ("",))

    def test_access_code_is_not_sent_to_a_new_address(self):
        self.c.post("/printers/new", data={"name": "p", "ip": "192.0.2.1", "serial": "S", "access_code": "12345678"})
        r = self.c.post("/printers/1/edit", data={"name": "p", "ip": "192.0.2.99", "serial": "S"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("access code", r.get_data(as_text=True))
        self.assertEqual(self.one("SELECT ip, access_code FROM printers"), ("192.0.2.1", "12345678"))
        # by way of an empty address
        self.c.post("/printers/1/edit", data={"name": "p", "ip": "", "serial": "S"})
        self.c.post("/printers/1/edit", data={"name": "p", "ip": "192.0.2.99", "serial": "S"})
        self.assertNotEqual(self.one("SELECT ip FROM printers"), ("192.0.2.99",))
        self.c.post("/printers/1/edit", data={"name": "p", "ip": "192.0.2.99", "serial": "S", "access_code": "87654321"})
        self.assertEqual(self.one("SELECT ip, access_code FROM printers"), ("192.0.2.99", "87654321"))
        self.c.post("/printers/1/edit", data={"name": "renamed", "ip": "192.0.2.99", "serial": "S"})
        self.assertEqual(self.one("SELECT name, access_code FROM printers"), ("renamed", "87654321"))


class _Server:
    """Local HTTP server that redirects or answers; records the requests."""

    def __init__(self, status=200, location=None, body=b"{}"):
        seen = self.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def _do(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            do_GET = do_POST = _do

            def log_message(self, *a):
                pass
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class RedirectTest(unittest.TestCase):
    def test_ntfy_redirect_not_followed(self):
        target = _Server()
        source = _Server(302, target.url + "/steal")
        self.addCleanup(target.close)
        self.addCleanup(source.close)
        with self.assertRaises(notify.NotifyError) as cm:
            notify.post({"ntfy_url": source.url, "ntfy_topic": "t", "ntfy_token": "tk_secret"}, "T", "M")
        self.assertIn("redirect", str(cm.exception))
        self.assertEqual(target.seen, [])

    def test_makerworld_redirect_only_to_allowed_hosts(self):
        target = _Server()
        source = _Server(302, target.url + "/internal")
        self.addCleanup(target.close)
        self.addCleanup(source.close)
        with self.assertRaises(urllib.error.HTTPError):
            app._http_get(source.url, 1000)
        self.assertEqual(target.seen, [])

    def test_allowed_redirect_targets(self):
        for url in ("https://makerworld.com/x", "https://www.makerworld.com/x", "https://makerworld.bblmw.com/a.jpg"):
            self.assertTrue(app.mw_redirect_allowed(url), url)
        for url in ("http://makerworld.com/x", "https://makerworld.com.evil.example/", "https://127.0.0.1/",
                    "https://evilmakerworld.com/", "file:///etc/passwd", "https://user@evil.example/makerworld.com",
                    "https://makerworld.com:8080/", "https://evilbblmw.com/"):
            self.assertFalse(app.mw_redirect_allowed(url), url)

    def test_error_text_of_other_servers_is_not_shown(self):
        source = _Server(500, body=b"internal secret page")
        self.addCleanup(source.close)
        with self.assertRaises(notify.NotifyError) as cm:
            notify.post({"ntfy_url": source.url, "ntfy_topic": "t"}, "T", "M")
        self.assertNotIn("internal secret", str(cm.exception))
        self.assertIn("HTTP 500", str(cm.exception))


class UploadTest(Base):
    def upload(self, content, name):
        with closing(sqlite3.connect(app.DB_PATH)) as db, db:
            pid = db.execute("INSERT INTO prints (name) VALUES ('x')").lastrowid
        self.c.post(f"/print/{pid}/photos", data={"photos": (io.BytesIO(content), name)},
                    content_type="multipart/form-data")
        return self.one("SELECT stored_name FROM files WHERE print_id = ?", pid)

    def test_content_decides(self):
        before = set(os.listdir(app.UPLOAD_DIR))
        for bad in (b"ffconcat version 1.0\nfile other.png\n", b"#EXTM3U\n", b"<svg onload=alert(1)>",
                    b"\x00\x00\x00\x18ftypmp42", b"", b"RIFF....AVI "):
            self.assertIsNone(self.upload(bad, "x.jpg"), bad)
        self.assertEqual(set(os.listdir(app.UPLOAD_DIR)), before)   # nothing left behind
        self.assertTrue(self.upload(JPEG, "x.jpg")[0].endswith(".jpg"))
        self.assertTrue(self.upload(b"RIFF\x10\x00\x00\x00WEBPVP8 ", "x.webp")[0].endswith(".webp"))
        self.assertTrue(self.upload(b"GIF89a...", "x.gif")[0].endswith(".gif"))

    def test_extension_follows_the_content(self):
        self.assertTrue(self.upload(PNG, "photo.jpg")[0].endswith(".png"))

    def test_printer_photo(self):
        self.c.post("/printers/new", data={"name": "p", "photo": (io.BytesIO(b"#EXTM3U"), "x.jpg")},
                    content_type="multipart/form-data")
        self.assertEqual(self.one("SELECT photo FROM printers"), (None,))

    def test_ffmpeg_input_is_fixed(self):
        with tempfile.TemporaryDirectory() as d:
            for name, content, demuxer in (("a.jpg", JPEG, "jpeg_pipe"), ("b.jpg", PNG, "png_pipe"),
                                           ("c.gif", b"GIF89a", "gif"), ("d.webp", b"RIFF1234WEBP", "webp_pipe")):
                Path(d, name).write_bytes(content)
                with mock.patch("subprocess.run", side_effect=OSError("x")) as run, \
                        self.assertLogs("printvault.thumbs", "WARNING"):
                    thumbs.make(d, name)
                cmd = run.call_args[0][0]
                self.assertEqual(cmd[cmd.index("-f") + 1], demuxer)
                self.assertLess(cmd.index("-f"), cmd.index("-i"))
                self.assertEqual(cmd[cmd.index("-protocol_whitelist") + 1], "file")
            Path(d, "e.jpg").write_bytes(b"ffconcat version 1.0\nfile a.jpg\n")
            with mock.patch("subprocess.run") as run, self.assertLogs("printvault.thumbs", "WARNING"):
                self.assertFalse(thumbs.make(d, "e.jpg"))
            run.assert_not_called()


class SlicerLimitsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def gcode(self, text):
        path = os.path.join(self.dir, "x.gcode")
        Path(path).write_text(text)
        t = time.monotonic()
        result = slicer.parse(path, "x.gcode")
        return result, time.monotonic() - t

    def test_parsing_time_is_linear(self):
        for text in ("; thumbnail begin 1x1 1\n" * 40000, "; a" + " " * 300000 + "x!\n", ";\n" * 300000,
                     "; a =" + " " * 300000 + "x\n", ";" * 300000):
            _, seconds = self.gcode(text)
            self.assertLess(seconds, 3, text[:20])

    def test_empty_value_does_not_take_the_next_line(self):
        result, _ = self.gcode("; notes = \n; layer_height = 0.2\n; nozzle_diameter = 0.4\n")
        self.assertEqual(result["settings"], {"layer_height": 0.2, "nozzle": 0.4})

    def test_only_the_chosen_thumbnail_is_read(self):
        path = os.path.join(self.dir, "x.3mf")
        with zipfile.ZipFile(path, "w") as z:
            for i in range(1, 30):
                z.writestr(f"Metadata/plate_{i}.png", PNG + bytes([i]))
        with mock.patch.object(slicer, "_read", wraps=slicer._read) as read:
            parsed = slicer.parse(path, "x.3mf", 7)
        self.assertEqual([c.args[1] for c in read.call_args_list if c.args[1].endswith(".png")], ["Metadata/plate_7.png"])
        self.assertEqual(slicer.plate_to_print_fields(parsed, 7, [])[2], (PNG + b"\x07", "png"))


class StreamLimitTest(Base):
    def test_limit_and_release(self):
        with closing(sqlite3.connect(app.DB_PATH)) as db, db:
            db.execute("INSERT INTO printers (name, ip, access_code) VALUES ('p', '192.0.2.1', 'x')")
        fake = mock.Mock(frames=lambda: iter([JPEG]))
        with mock.patch.object(app.cameras, "get", return_value=fake), mock.patch.object(app, "MAX_STREAMS", 2), \
                mock.patch.object(app, "_streams", 0):
            url = "/printers/1/camera.mjpg"
            a, b = self.c.get(url), self.c.get(url)
            self.assertEqual((a.status_code, b.status_code, self.c.get(url).status_code), (200, 200, 503))
            a.close()
            c = self.c.get(url)
            self.assertEqual(c.status_code, 200)
            b.close()
            c.close()
            for _ in range(5):
                r = self.c.get(url)
                self.assertEqual(r.status_code, 200)
                r.close()
                r.close()   # closing twice must not free two places
            self.assertEqual(app._streams, 0)


class HeadersTest(Base):
    def test_headers(self):
        for url in ("/", "/static/app.js", "/nothing", "/settings"):
            with self.c.get(url) as r:
                h = r.headers
            self.assertEqual(h["X-Content-Type-Options"], "nosniff", url)
            self.assertEqual(h["X-Frame-Options"], "DENY")
            self.assertEqual(h["Cross-Origin-Resource-Policy"], "same-origin")
            self.assertEqual(h["Referrer-Policy"], "same-origin")
            csp = h["Content-Security-Policy"]
            self.assertIn("default-src 'self'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertIn("frame-src bambustudio: bambustudioopen:", csp)   # 'Open in Bambu Studio' in Firefox
            self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)

    def test_templates_have_no_inline_scripts(self):
        for path in (ROOT / "templates").glob("*.html"):
            text = path.read_text()
            self.assertIsNone(re.search(r"<script(?![^>]*\b(src|type=\"application/json\"))", text), path.name)
            self.assertIsNone(re.search(r"\son[a-z]+=\"", text), path.name)

    def test_printer_form_title(self):
        html = self.c.get("/printers/new").get_data(as_text=True)
        self.assertNotIn("<script", re.search(r"<title>(.*?)</title>", html, re.S).group(1))


class DeploymentTest(unittest.TestCase):
    def test_no_debug_server(self):
        self.assertNotIn("debug=True", (ROOT / "app.py").read_text())

    def test_compose_binds_to_localhost_by_default(self):
        text = (ROOT / "docker-compose.yml").read_text()
        self.assertIn('"${PRINTVAULT_BIND:-127.0.0.1}:8100:5000"', text)
        self.assertNotIn('"0.0.0.0:', text)

    def test_documented(self):
        for name in ("PRINTVAULT_BIND", "ALLOWED_HOSTS"):
            self.assertIn(name, (ROOT / ".env.example").read_text())
            self.assertIn(name, (ROOT / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()
