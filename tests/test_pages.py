"""Every page renders in the new design, plus the helpers behind it (chips, colours, thumbnails, errors).

Run:  python3 -m unittest discover -s tests
"""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402
import app  # noqa: E402
import icons  # noqa: E402
import mqtt_listener as ml  # noqa: E402
import thumbs  # noqa: E402


def chip_on(text):
    return re.compile(r'<a class="chip"[^>]*aria-current="true"[^>]*>(?:(?!</a>).)*' + re.escape(text), re.S)


class PagesTest(unittest.TestCase):
    def setUp(self):
        testenv.reset(uploads=True)
        self.c = app.app.test_client()
        add = mock.patch.object(app.THUMBS, "add")   # no ffmpeg in the page tests
        self.queued = add.start()
        self.addCleanup(add.stop)
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO printers (id, name, model) VALUES (1, 'Shop', 'P2S')")
            db.executemany(
                "INSERT INTO prints (id, name, status, material, nozzle, printer_id, printed_on, finished_at, "
                "fail_reason, color, filament_g, print_time_min, tags, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(1, "Benchy", "succeeded", "PLA", 0.4, 1, "2026-11-20", "2026-11-20T10:00:00+01:00", None, "red", 12, 65,
                  "test, boat", "first"),
                 (2, "Clip", "failed", "PETG", 0.4, 1, "2026-11-21", "2026-11-21T10:00:00+01:00", "warping", None, 3, 9,
                  "", ""),
                 (3, "Vase", "planned", "PLA", 0.6, None, None, None, None, "#00FF00", None, None, "", ""),
                 (4, "Older", "succeeded", "PLA", 0.4, 1, "2026-11-01", "2026-11-01T10:00:00+01:00", None, None, 5, 30,
                  "", "")])
            db.executemany("INSERT INTO print_ams (print_id, ams_id, tray_id, tray_type, tray_color) VALUES (?,?,?,?,?)",
                           [(1, "0", "0", "PLA", "F7CED7FF"), (1, "0", "1", "PLA", "000000FF"),
                            (1, "0", "2", "PLA", "F7CED7FF")])
            db.execute("INSERT INTO files (id, print_id, kind, stored_name, original_name, size) VALUES "
                       "(10, 1, 'photo', 'p1.jpg', 'boat.jpg', 100), (11, 1, 'model', 'm1.3mf', 'boat.3mf', 2000), "
                       "(12, 1, 'photo', 'p2.gif', 'anim.gif', 100)")
            db.execute("INSERT INTO print_samples (print_id, recorded_at, nozzle, nozzle_target, bed, bed_target, chamber, "
                       "stage, percent, layer) VALUES (1, '2026-11-20T09:00:00+01:00', 200, 220, 50, 55, 30, 2, 0, 0), "
                       "(1, '2026-11-20T09:01:00+01:00', 220, 220, 55, 55, 31, 0, 5, 3)")

    def get(self, url, code=200):
        r = self.c.get(url)
        self.assertEqual(r.status_code, code, url)
        return r.get_data(as_text=True)

    def test_every_page_renders(self):
        pages = {"/": "Prints", "/print/1": "Prints", "/print/2": "Prints", "/print/3": "Prints",
                 "/print/new": "Prints", "/print/1/edit": "Prints", "/printers": "Printers",
                 "/printers/new": "Printers", "/printers/1/edit": "Printers", "/printers/1/maintenance": "Maintenance",
                 "/settings": "Settings"}
        for url, nav in pages.items():
            html = self.get(url)
            self.assertIn('<svg class="sprite"', html, url)
            self.assertRegex(html, rf'<a class="nav on" href="[^"]*" aria-current="page">.*?<span>{nav}</span>', url)
            self.assertNotIn("fonts.googleapis", html)
            self.assertIn("/static/style.css?v=", html)

    def test_empty_states(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("DELETE FROM prints")
            db.execute("DELETE FROM printers")
        html = self.get("/")
        self.assertIn("No prints yet", html)
        self.assertIn("Add a printer", html)
        self.assertIn("No printers yet", self.get("/printers"))
        self.assertIn("Nothing found", self.get("/?q=zzz"))

    def test_error_pages(self):
        html = self.get("/does-not-exist", 404)
        self.assertIn("Not found", html)
        self.assertIn('<aside class="side"', html)   # inside the normal layout
        self.assertIn("Not found", self.get("/print/999", 404))
        self.assertIn("Not allowed", self.get("/print/1/delete", 405))

    def test_maintenance_link(self):
        r = self.c.get("/maintenance")
        self.assertTrue(r.headers["Location"].endswith("/printers/1/maintenance"))
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO printers (id, name) VALUES (2, 'Second')")
        self.assertTrue(self.c.get("/maintenance").headers["Location"].endswith("/printers"))

    def test_hero_shows_the_two_last_finished(self):
        html = self.get("/")
        hero = html[html.index('class="hero"'):html.index('class="sectionhead"')]
        self.assertIn("Last finished", hero)
        self.assertIn(">Clip<", hero)
        self.assertIn(">Benchy<", hero)
        self.assertNotIn(">Older<", hero)
        self.assertNotIn('class="hero"', self.get("/?material=PLA"))   # filtered: no hero

    def test_chips(self):
        html = self.get("/")
        self.assertRegex(html, chip_on("All"))
        self.assertIn('href="/?status=failed"', html)
        self.assertIn('href="/?material=PETG"', html)
        self.assertIn('href="/?nozzle=0.6"', html)
        self.assertNotIn('href="/?printer=1"', html)   # one printer: no printer chips
        html = self.get("/?status=failed")
        self.assertRegex(html, chip_on("Failed"))
        self.assertIn(">Clip<", html)
        self.assertNotIn(">Benchy<", html)
        self.assertIn('href="/?status=failed&amp;fail=warping"', html)   # fail reasons only for failed prints
        # switching the status off also drops the fail reason
        html = self.get("/?status=failed&fail=warping")
        self.assertIn('href="/"', html)
        self.assertRegex(html, chip_on("Warping"))
        # the search keeps the filters, the filters keep the search
        html = self.get("/?q=Ben&material=PLA")
        self.assertIn('<input type="hidden" name="material" value="PLA">', html)
        self.assertIn('href="/?q=Ben"', html)   # the PLA chip switches PLA off but keeps the search

    def test_cards(self):
        html = self.get("/")
        # status always with an icon and a word
        self.assertIn('<span class="badge st-failed"><svg class="bico" aria-hidden="true" focusable="false">'
                      '<use href="#i-x"></use></svg>Failed · Warping</span>', html)
        # AMS colours first, duplicates removed; otherwise the colour field (sanitised)
        self.assertIn('aria-label="Filament colours: #f7ced7, #000000"', html)
        self.assertIn('aria-label="Filament colour: #00ff00"', html)
        self.assertIn('src="/thumb/600/p1.jpg"', html)

    def test_detail(self):
        html = self.get("/print/1")
        self.assertIn('src="/thumb/1600/p1.jpg"', html)          # cover large
        self.assertIn('src="/thumb/600/p2.gif"', html)           # other photo small (a still frame)
        self.assertIn('href="/uploads/p1.jpg"', html)            # original opens in full
        self.assertIn('id="samples-data"', html)
        self.assertIn("/static/chart.js?v=", html)
        self.assertIn("boat.3mf", html)
        self.assertIn('href="/?q=boat"', html)
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("UPDATE prints SET cover_file_id = 12 WHERE id = 1")
        html = self.get("/print/1")
        self.assertIn('src="/uploads/p2.gif"', html)             # a GIF cover keeps its animation on the detail page
        self.assertNotIn("chart.js", self.get("/print/2"))

    def test_print_again_buttons(self):
        html = self.get("/print/1")
        # Bambu Studio scheme (Windows/Linux) and the macOS variant; the download URL is absolute and gets
        # a '&name=…3mf' suffix, everything URL-encoded
        target = "http%3A%2F%2Flocalhost%2Fdownload%2F11%26name%3Dboat.3mf"
        self.assertIn(f'href="bambustudio://open?file={target}"', html)
        self.assertIn(f'data-studio-mac="bambustudioopen://{target}"', html)
        self.assertIn('data-download="/download/11"', html)
        self.assertNotIn("Open on MakerWorld", html)
        # only a MakerWorld link: no Studio button, a MakerWorld button instead of "View source"
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("UPDATE prints SET source_url = 'https://makerworld.com/en/models/123-boat' WHERE id = 2")
            db.execute("UPDATE prints SET source_url = 'https://www.printables.com/model/1' WHERE id = 3")
            db.execute("INSERT INTO files (print_id, kind, stored_name, original_name) VALUES (3, 'model', 'v.stl', 'v.stl')")
        html = self.get("/print/2")
        self.assertIn('href="https://makerworld.com/en/models/123-boat" target="_blank" rel="noopener noreferrer"', html)
        self.assertIn("Open on MakerWorld", html)
        self.assertNotIn("bambustudio", html)
        self.assertNotIn("View source", html)
        html = self.get("/print/3")                                   # stl + other source: neither button
        self.assertNotIn("Print again", html)
        self.assertIn("View source", html)

    def test_studio_link_name(self):
        with app.app.test_request_context("/", base_url="https://printvault.example"):
            links = app.bambu_studio_links({"id": 7, "original_name": "my model (v2).gcode.3mf"})
            self.assertEqual(links["open"], "bambustudio://open?file=https%3A%2F%2Fprintvault.example%2Fdownload%2F7"
                                            "%26name%3Dmy_model_v2.gcode.3mf")
            self.assertTrue(app.bambu_studio_links({"id": 8, "original_name": "日本.3mf"})["open"]
                            .endswith("%26name%3Dprintvault-8.3mf"))

    def test_proxy_headers(self):
        headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "printvault.example"}
        # default: headers ignored
        html = self.c.get("/print/1", headers=headers).get_data(as_text=True)
        self.assertIn("bambustudio://open?file=http%3A%2F%2Flocalhost%2Fdownload%2F11", html)
        original = app.app.wsgi_app
        app.app.wsgi_app = app.apply_proxy_fix(original, 1)
        try:
            with mock.patch.dict(app.app.config, {"ALLOWED_HOSTS": {"printvault.example"}}):
                html = self.c.get("/print/1", headers=headers).get_data(as_text=True)
            self.assertIn("bambustudio://open?file=https%3A%2F%2Fprintvault.example%2Fdownload%2F11", html)
        finally:
            app.app.wsgi_app = original
        self.assertIs(app.apply_proxy_fix(original, 0), original)

    def test_thumb_route(self):
        os.makedirs(os.path.join(app.UPLOAD_DIR, thumbs.SUBDIR), exist_ok=True)
        with open(os.path.join(app.UPLOAD_DIR, "p1.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8original")
        r = self.c.get("/thumb/600/p1.jpg")          # no thumbnail yet: the original, queued, not cached
        self.assertEqual(r.get_data(), b"\xff\xd8original")
        self.assertEqual(r.headers["Cache-Control"], "no-cache")
        self.queued.assert_called_with("p1.jpg")
        r.close()
        with open(thumbs.thumb_path(app.UPLOAD_DIR, "p1.jpg", 600), "wb") as fh:
            fh.write(b"RIFFxxxxWEBP")
        r = self.c.get("/thumb/600/p1.jpg")
        self.assertEqual((r.mimetype, r.get_data()), ("image/webp", b"RIFFxxxxWEBP"))
        self.assertIn("max-age", r.headers["Cache-Control"])
        r.close()
        for bad in ("/thumb/700/p1.jpg", "/thumb/600/m1.3mf", "/thumb/600/nothere.jpg", "/thumb/600/..%2Fx.jpg"):
            self.assertEqual(self.c.get(bad).status_code, 404, bad)

    def test_deleting_a_photo_removes_its_thumbnails(self):
        os.makedirs(os.path.join(app.UPLOAD_DIR, thumbs.SUBDIR), exist_ok=True)
        for path in [os.path.join(app.UPLOAD_DIR, "p1.jpg")] + [thumbs.thumb_path(app.UPLOAD_DIR, "p1.jpg", s)
                                                                 for s in thumbs.SIZES]:
            open(path, "wb").close()
        self.c.post("/file/10/delete")
        self.assertEqual(os.listdir(os.path.join(app.UPLOAD_DIR, thumbs.SUBDIR)), [])

    def test_upload_queues_thumbnails(self):
        import io
        self.c.post("/print/2/photos", data={"photos": (io.BytesIO(b"\xff\xd8\xffx"), "new.jpg")},
                    content_type="multipart/form-data")
        self.assertTrue(self.queued.called)
        self.assertTrue(self.queued.call_args[0][0].endswith(".jpg"))


class HelpersTest(unittest.TestCase):
    def test_css_color(self):
        self.assertEqual(app.css_color("#FF00AA"), "#ff00aa")
        self.assertEqual(app.css_color("F7CED7FF"), "#f7ced7")
        self.assertEqual(app.css_color(" Black "), "black")
        for bad in ("red;background:url(http://x)", "url(x)", "#12", "", None, "light blue"):
            self.assertIsNone(app.css_color(bad), bad)

    def test_trusted_proxies_setting(self):
        self.assertEqual([app.trusted_proxies(v) for v in (None, "", "0", "1", " 2 ", "-1")], [0, 0, 0, 1, 2, 0])
        with self.assertLogs(app.app.logger, "WARNING"):
            self.assertEqual(app.trusted_proxies("yes"), 0)

    def test_icons(self):
        self.assertIn('<use href="#i-clock">', str(icons.icon("clock")))
        with self.assertRaises(KeyError):
            icons.icon("nope")
        for name in icons.STATUS_ICONS.values():
            self.assertIn(name, icons.PATHS)
        for status in app.STATUSES:
            self.assertIn(status, icons.STATUS_ICONS)
        # every icon the templates and scripts ask for exists
        root = os.path.join(os.path.dirname(__file__), "..")
        used = set()
        for folder, pattern in (("templates", r"icon\('([a-z-]+)'"), ("static", r'icon\("([a-z-]+)"')):
            for name in os.listdir(os.path.join(root, folder)):
                if name.endswith((".html", ".js")):
                    with open(os.path.join(root, folder, name), encoding="utf-8") as fh:
                        used |= set(re.findall(pattern, fh.read()))
        self.assertTrue(used)
        self.assertEqual(used - set(icons.PATHS), set())

    def test_static_files_are_local(self):
        root = os.path.join(os.path.dirname(__file__), "..", "static")
        with open(os.path.join(root, "style.css"), encoding="utf-8") as fh:
            css = fh.read()
        self.assertNotRegex(css, r"url\((?!fonts/)")
        for font in re.findall(r"url\((fonts/[^)]+)\)", css):
            self.assertTrue(os.path.isfile(os.path.join(root, font)), font)
        for lic in ("LICENSE-inter.txt", "LICENSE-jetbrains-mono.txt"):
            self.assertTrue(os.path.isfile(os.path.join(root, "fonts", lic)))


if __name__ == "__main__":
    unittest.main()
