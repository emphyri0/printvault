"""Import: slicer files (also broken ones), MakerWorld data, file names, plates and numbers in forms and URLs."""
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402
import app  # noqa: E402
import mqtt_listener as ml  # noqa: E402
import slicer  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n"
MW = "https://makerworld.com/en/models/123"
SETTINGS = "Metadata/project_settings.config"
SLICE_INFO = "Metadata/slice_info.config"


def make_3mf(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def settings(**cfg):
    return make_3mf({SETTINGS: json.dumps(cfg)})


class Base(unittest.TestCase):
    def setUp(self):
        testenv.reset(uploads=True)
        add = mock.patch.object(app.THUMBS, "add")
        add.start()
        self.addCleanup(add.stop)
        self.c = app.app.test_client()

    def rows(self, sql, *args):
        with ml.connect_db(app.DB_PATH) as db:
            return [dict(r) for r in db.execute(sql, args)]

    def one(self, sql, *args):
        return self.rows(sql, *args)[0]

    def upload(self, name, content):
        r = self.c.post("/print/import-file", data={"file": (io.BytesIO(content), name)},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302, name)
        return self.one("SELECT * FROM prints ORDER BY id DESC LIMIT 1")

    def flashes(self):
        with self.c.session_transaction() as s:
            return [text for _kind, text in s.pop("_flashes", [])]


class ImportFileTest(Base):
    def test_good_file(self):
        p = self.upload("my_part.gcode.3mf", make_3mf({
            SETTINGS: json.dumps({"layer_height": "0.2", "filament_type": ["PLA"]}), "Metadata/plate_1.png": PNG}))
        self.assertEqual((p["name"], p["layer_height"], p["material"], p["status"]), ("my part", 0.2, "PLA", "planned"))
        files = self.rows("SELECT kind, original_name FROM files ORDER BY id")
        self.assertEqual([tuple(f.values()) for f in files],
                         [("model", "my_part.gcode.3mf"), ("photo", "thumbnail-plate-1.png")])

    def broken_files(self):
        damaged = bytearray(settings(layer_height="0.2"))
        damaged[40 + len(SETTINGS)] ^= 0xff
        encrypted = bytearray(make_3mf({SETTINGS: "{}"}))
        encrypted[6] |= 1
        encrypted[encrypted.find(b"PK\x01\x02") + 8] |= 1
        method = bytearray(make_3mf({SETTINGS: "{}"}))
        method[8] = 99
        method[method.find(b"PK\x01\x02") + 10] = 99
        return {"damaged.3mf": bytes(damaged), "encrypted.3mf": bytes(encrypted), "method.3mf": bytes(method),
                "nozip.3mf": b"hello", "empty.3mf": b"", "empty.gcode": b"",
                "nested.3mf": make_3mf({SETTINGS: "[" * 100000})}

    def odd_values(self):
        plate = '<config><plate><metadata key="index" value="%s"/><metadata key="prediction" value="%s"/>' \
                '<metadata key="weight" value="%s"/></plate></config>'
        return {"list.3mf": make_3mf({SETTINGS: "[1]"}), "number.3mf": make_3mf({SETTINGS: "5"}),
                "infill.3mf": settings(sparse_infill_density="abc"), "infinfill.3mf": settings(sparse_infill_density="inf"),
                "inf.3mf": settings(nozzle_temperature="inf", layer_height="nan", nozzle_diameter="1e400"),
                "big.3mf": settings(nozzle_temperature="1e30", nozzle_diameter=["9e99"]),
                "types.3mf": settings(filament_type={"a": 1}, filament_colour=5, layer_height=[], curr_bed_type=[{}]),
                "plates.3mf": make_3mf({SLICE_INFO: plate % ("1e30", "inf", "nan")}),
                "xml.3mf": make_3mf({SLICE_INFO: "<config><plate>"}),
                "longplate.3mf": make_3mf({f"Metadata/plate_{'9' * 5000}.png": PNG}),
                "inf.gcode": b"; temperature = inf\n; layer_height = nan\n",
                "time.gcode": b"; total estimated time: " + b"9" * 400 + b"h\n; total filament weight [g] : " + b"9" * 400}

    def test_broken_files_are_kept_with_a_message(self):
        for name, content in self.broken_files().items():
            with self.subTest(name):
                p = self.upload(name, content)
                self.assertEqual(p["name"], Path(name).stem)
                f = self.one("SELECT * FROM files WHERE print_id = ?", p["id"])
                self.assertEqual(Path(app.UPLOAD_DIR, f["stored_name"]).read_bytes(), content)
                self.assertTrue(any("kept" in t for t in self.flashes()))
                self.assertEqual(self.c.get(f"/print/{p['id']}").status_code, 200)
                self.assertEqual(self.c.post(f"/file/{f['id']}/apply").status_code, 302)

    def test_odd_values_are_left_out(self):
        for name, content in self.odd_values().items():
            with self.subTest(name):
                p = self.upload(name, content)
                for field in ("nozzle_temp", "layer_height", "nozzle", "infill", "print_time_min", "filament_g"):
                    self.assertIsNone(p[field], field)
                self.assertEqual(self.c.get(f"/print/{p['id']}").status_code, 200)

    def test_unexpected_error_leaves_no_strange_name(self):
        with mock.patch.object(slicer, "parse", side_effect=ZeroDivisionError), \
                mock.patch.dict(app.app.config, {"PROPAGATE_EXCEPTIONS": False}), self.assertLogs(app.app.logger, "ERROR"):
            self.c.post("/print/import-file", data={"file": (io.BytesIO(b"x"), "part.gcode")},
                        content_type="multipart/form-data")
        self.assertEqual([r["name"] for r in self.rows("SELECT name FROM prints")], ["part"])


class SlotTest(unittest.TestCase):
    CFG = {"nozzle_temperature": ["200", "250"], "curr_bed_type": "Textured PEI Plate",
           "textured_plate_temp": ["55", "70"], "filament_type": ["PLA", "PETG"], "filament_colour": ["#111111", "#222222"]}

    def fields(self, cfg, *slots):
        plate = "".join(f'<filament id="{s}" type="PETG" color="#222222" used_g="5"/>' for s in slots)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "slot.3mf")
        Path(path).write_bytes(make_3mf({SETTINGS: json.dumps(cfg), SLICE_INFO:
                                         f'<config><plate><metadata key="index" value="1"/>{plate}</plate></config>'}))
        return slicer.plate_to_print_fields(slicer.parse(path, path), 1, app.MATERIALS)[0]

    def test_temperatures_of_the_used_slot(self):
        for slots, expected in (((2,), (250, 70)), ((1,), (200, 55)), ((2, 1), (250, 70)), ((), (200, 55)),
                                ((9,), (200, 55)), (("x",), (200, 55))):
            f = self.fields(self.CFG, *slots)
            self.assertEqual((f["nozzle_temp"], f["bed_temp"]), expected, slots)

    def test_other_list_lengths_are_not_per_slot(self):
        f = self.fields({**self.CFG, "nozzle_temperature": ["200", "210", "250", "260"]}, 2)
        self.assertEqual((f["nozzle_temp"], f["bed_temp"]), (200, 70))

    def test_nozzle_is_not_per_slot(self):
        self.assertEqual(self.fields({**self.CFG, "nozzle_diameter": ["0.4", "0.6"]}, 2)["nozzle"], 0.4)


class PlateTest(Base):
    def setUp(self):
        super().setUp()
        self.p = self.upload("two.3mf", make_3mf({"Metadata/plate_1.png": PNG + b"1", "Metadata/plate_2.png": PNG + b"2"}))
        self.fid = self.one("SELECT id FROM files WHERE kind = 'model'")["id"]
        self.flashes()

    def apply(self, plate):
        self.c.post(f"/file/{self.fid}/apply", data={"plate": plate})
        return " ".join(self.flashes())

    def test_unknown_plate(self):
        before = self.rows("SELECT * FROM prints"), self.rows("SELECT * FROM files")
        for plate in ("9", "0", "-1"):
            text = self.apply(plate)
            self.assertIn(f"no plate {plate}", text)
            self.assertIn("1, 2", text)
        self.assertEqual((self.rows("SELECT * FROM prints"), self.rows("SELECT * FROM files")), before)

    def test_chosen_plate(self):
        self.assertIn("plate 2 was used", self.apply("2"))
        self.assertEqual(self.one("SELECT plate FROM prints")["plate"], 2)
        f = self.one("SELECT * FROM files WHERE original_name = 'thumbnail-plate-2.png'")
        self.assertEqual(Path(app.UPLOAD_DIR, f["stored_name"]).read_bytes(), PNG + b"2")

    def test_only_plate_is_always_used(self):
        p = self.upload("one.3mf", make_3mf({"Metadata/plate_3.png": PNG + b"3"}))
        fid = self.one("SELECT id FROM files WHERE kind = 'model' AND print_id = ?", p["id"])["id"]
        self.flashes()
        self.c.post(f"/file/{fid}/apply", data={"plate": "1"})
        self.assertNotIn("no plate", " ".join(self.flashes()))
        self.assertEqual(len(self.rows("SELECT 1 FROM files WHERE original_name = 'thumbnail-plate-3.png'")), 1)
        self.assertEqual(len(self.rows("SELECT 1 FROM files WHERE original_name = 'thumbnail-plate-1.png' AND print_id = ?",
                                       p["id"])), 0)


class FileNameTest(Base):
    def test_names(self):
        for given, stored in (("日本.3mf", "日本.3mf"), ("Schräg & Söhne.3mf", "Schräg & Söhne.3mf"),
                              ("../../etc/x.3mf", "x.3mf"), ("C:\\dir\\y.3mf", "y.3mf"), ("a\x00b\n\u202ec.3mf", "abc.3mf"),
                              (".3mf", "file.3mf"), ("...3mf", "file.3mf"), ("X.3MF", "X.3mf"),
                              ('q"<>|?*.3mf', "q______.3mf")):
            self.assertEqual(app.clean_name(given, "3mf"), stored, given)
        self.assertEqual(len(app.clean_name("é" * 500 + ".3mf", "3mf")), 150)

    def test_unicode_name_works_everywhere(self):
        p = self.upload("日本.3mf", settings(layer_height="0.2"))
        f = self.one("SELECT * FROM files")
        self.assertEqual((p["name"], f["original_name"]), ("日本", "日本.3mf"))
        self.assertEqual(self.c.post(f"/file/{f['id']}/apply").status_code, 302)
        with self.c.get(f"/download/{f['id']}") as r:
            self.assertIn("filename*=UTF-8''%E6%97%A5%E6%9C%AC.3mf", r.headers["Content-Disposition"])
        self.assertIn("bambustudio://", self.c.get(f"/print/{p['id']}").get_data(as_text=True))

    def test_names_stored_by_older_versions(self):
        p = self.upload("x.3mf", settings(layer_height="0.2"))
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("UPDATE files SET original_name = '3mf'")
        self.assertEqual(self.c.post("/file/1/apply").status_code, 302)
        with self.c.get("/download/1") as r:
            self.assertIn("filename=3mf.3mf", r.headers["Content-Disposition"])
        text = self.c.get(f"/print/{p['id']}").get_data(as_text=True)
        self.assertIn("bambustudio://", text)
        self.assertIn("/file/1/apply", text)

    def test_other_models_cannot_be_applied(self):
        with ml.connect_db(app.DB_PATH) as db:
            db.execute("INSERT INTO prints (id, name) VALUES (1, 'x')")
            db.execute("INSERT INTO files (id, print_id, kind, stored_name, original_name) VALUES (1, 1, 'model', 'a.stl', 'a.stl')")
        self.assertEqual(self.c.post("/file/1/apply").status_code, 404)


DESIGN = {"title": "Boat", "summary": "<p>Line one</p>\r\n<p>two</p>", "designCreator": {"name": "Ann"}, "license": "BY",
          "tags": ["sea", "toy"], "coverUrl": "https://makerworld.bblmw.com/c.png?x=1", "defaultInstanceId": 2,
          "instances": [{"id": 1, "title": "other"},
                        {"id": 2, "title": "0.16mm layer, 2 walls", "weight": 12.5, "prediction": 3600, "needAms": True,
                         "instanceFilaments": [{"type": "PLA Basic", "color": "#FFF", "usedG": "12"}]}]}


class MakerWorldTest(Base):
    def get(self, design, cover=PNG):
        def fake(url, _limit):
            return cover if "bblmw" in url else (design if isinstance(design, bytes) else json.dumps(design).encode())
        return mock.patch.object(app, "_http_get", side_effect=fake)

    def test_import(self):
        with self.get(DESIGN):
            self.c.post("/print/import", data={"url": MW})
        p = self.one("SELECT * FROM prints")
        self.assertEqual((p["name"], p["designer"], p["license"], p["material"], p["filament_g"], p["print_time_min"],
                          p["layer_height"], p["tags"], p["color"], p["source_url"]),
                         ("Boat", "Ann", "BY", "PLA", 12.5, 60, 0.16, "sea, toy", "#FFF", MW))
        self.assertIn("Requires AMS.", p["notes"])
        self.assertIn("Line one", p["notes"])
        self.assertNotIn("\r", p["notes"])
        self.assertEqual(self.one("SELECT original_name FROM files")["original_name"], "makerworld-cover.png")

    def test_strange_answers_never_crash(self):
        for design in (b"[]", b"null", b"5", b"not json", b'"x"', b"\xff\xfe", b"[" * 100000, {}, {"title": 5}, {"title": ""},
                       {"title": None}, {"title": " "}):
            with self.subTest(str(design)[:30]), self.get(design):
                self.assertEqual(self.c.post("/print/import", data={"url": MW}).status_code, 302)
                self.assertEqual(self.rows("SELECT 1 FROM prints"), [])

    def test_strange_fields_are_left_out(self):
        for change in ({"instances": 5}, {"instances": ["x", None]}, {"instances": {"a": 1}}, {"tags": "abc"},
                       {"tags": [1, None, "ok"]}, {"designCreator": "Ann"}, {"designCreator": {"name": 5}}, {"license": {}},
                       {"summary": 5}, {"coverUrl": 5}, {"coverUrl": ["x"]},
                       {"instances": [{"weight": "n/a", "prediction": "x", "title": 5, "instanceFilaments": "x"}]},
                       {"instances": [{"weight": {}, "prediction": [], "instanceFilaments": [None, 5, {"type": 5, "color": 6}]}]},
                       {"instances": [{"weight": "1e999", "prediction": 1e300}]}):
            with self.subTest(str(change)), self.get({**DESIGN, **change}):
                self.assertEqual(self.c.post("/print/import", data={"url": MW}).status_code, 302)
                p = self.one("SELECT * FROM prints ORDER BY id DESC LIMIT 1")
                self.assertEqual(p["name"], "Boat")
                self.assertEqual(self.c.get(f"/print/{p['id']}").status_code, 200)

    def test_cover_that_fails_is_left_out(self):
        with self.get(DESIGN, cover=b"<html>"):
            self.c.post("/print/import", data={"url": MW})
        self.assertEqual(self.rows("SELECT 1 FROM files"), [])

    def edit(self, pid=1, **data):
        p = self.one("SELECT * FROM prints WHERE id = ?", pid)
        form = {k: p[k] or "" for k in ("name", "source_url", "notes", "designer", "status")}
        form.update(data)
        form["notes"] = form["notes"].replace("\n", "\r\n")     # as a browser sends it
        self.c.post(f"/print/{pid}/edit", data=form)

    def test_notes_are_added_once(self):
        self.c.post("/print/new", data={"name": "x", "notes": "mine"})
        with self.get({**DESIGN, "coverUrl": ""}) as get:
            self.edit(source_url=MW)
            for _ in range(3):
                self.edit()
            self.assertEqual(get.call_count, 1)          # not fetched again without a new link
        p = self.one("SELECT * FROM prints")
        self.assertEqual((p["notes"].count("Profile:"), p["notes"].count("mine"), p["designer"]), (1, 1, "Ann"))

    def test_removed_notes_stay_removed(self):
        with self.get({**DESIGN, "coverUrl": ""}):
            self.c.post("/print/import", data={"url": MW})
            self.edit(notes="only mine")
        self.assertEqual(self.one("SELECT notes FROM prints")["notes"], "only mine")

    def test_same_model_with_another_address_is_not_fetched_again(self):
        with self.get(DESIGN) as get:
            self.c.post("/print/import", data={"url": MW})
            n = get.call_count
            self.edit(source_url="https://makerworld.com/nl/models/123-boat?from=search")
            self.assertEqual(get.call_count, n)

    def test_failed_fetch_is_tried_again(self):
        self.c.post("/print/new", data={"name": "x"})
        with mock.patch.object(app, "_http_get", side_effect=OSError("down")):
            self.edit(source_url=MW)
        self.assertEqual(self.one("SELECT source_url FROM prints")["source_url"], MW)
        with self.get(DESIGN):
            self.edit()
        self.assertEqual(self.one("SELECT designer FROM prints")["designer"], "Ann")

    def test_new_link_is_fetched(self):
        with self.get(DESIGN):
            self.c.post("/print/import", data={"url": MW})
        with self.get({**DESIGN, "summary": "Second"}) as get:
            self.edit(source_url="https://makerworld.com/en/models/456")
            self.assertTrue(get.called)
        self.assertIn("Second", self.one("SELECT notes FROM prints")["notes"])


class NumbersTest(Base):
    def test_num(self):
        for text in ("inf", "-inf", "nan", "1e400", "1e30", "abc", "", None):
            self.assertIsNone(app.num(text), text)
            self.assertIsNone(app.num(text, int), text)
        self.assertEqual((app.num("0,4"), app.num("12.7", int), app.num("-5", int)), (0.4, 12, -5))

    def test_urls(self):
        for url in ("/?printer=inf", "/?printer=1e30", "/?nozzle=nan", "/?printer=999999999999"):
            self.assertEqual(self.c.get(url).status_code, 200, url)
        big = "9" * 30
        for url in (f"/print/{big}", f"/print/{big}/edit", f"/download/{big}", f"/printers/{big}/edit",
                    f"/printers/{big}/snapshot.jpg", f"/maintenance/{big}/edit", f"/thumb/{big}/x.jpg"):
            self.assertEqual(self.c.get(url).status_code, 404, url)
        self.assertEqual(self.c.get(f"/print/{2 ** 63 - 1}").status_code, 404)

    def test_form_values(self):
        for data in ({"nozzle_temp": "inf"}, {"hours": "1e30"}, {"filament_g": "nan"}, {"plate": "1e20"}, {"printer_id": "999"},
                     {"printer_id": "1e30"}, {"printer_id": "-1"}):
            with self.subTest(str(data)):
                r = self.c.post("/print/new", data={"name": "x", **data})
                self.assertEqual(r.status_code, 302)
                self.assertEqual(self.c.get(r.headers["Location"]).status_code, 200)
                self.assertEqual(self.c.post(r.headers["Location"] + "/edit", data={"name": "y", **data}).status_code, 302)
        self.assertEqual({r["printer_id"] for r in self.rows("SELECT printer_id FROM prints")}, {None})

    def test_unknown_printer_gives_a_message(self):
        self.c.post("/print/new", data={"name": "x", "printer_id": "999"})
        self.assertTrue(any("printer" in t.lower() and "no longer" in t for t in self.flashes()))


if __name__ == "__main__":
    unittest.main()
