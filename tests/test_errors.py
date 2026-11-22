"""Tests voor bambu_errors (vertaling van print_error en HMS) en de gegenereerde tabel."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import bambu_errors as be  # noqa: E402
import update_bambu_errors as gen  # noqa: E402

UNKNOWN = 0x0300FFFF   # komt niet voor in de tabel (gecontroleerd in setUpModule)


def setUpModule():
    assert be.error_code(UNKNOWN) not in be.PRINT_ERRORS


class ErrorCodeTest(unittest.TestCase):
    def test_error_code_format(self):
        self.assertEqual(be.error_code(50348044), "0300-400C")
        self.assertEqual(be.error_code("50348044"), "0300-400C")
        self.assertIsNone(be.error_code(0))
        self.assertIsNone(be.error_code(None))
        self.assertIsNone(be.error_code("x"))

    def test_generated_table_loaded_with_source(self):
        self.assertGreater(len(be.PRINT_ERRORS), 1000)
        self.assertGreater(len(be.HMS_CODES), 5000)
        self.assertEqual(be.SOURCE["repo"], "https://github.com/greghesp/ha-bambulab")
        self.assertEqual(len(be.SOURCE["commit"]), 40)
        self.assertEqual((be.SOURCE["model"], be.SOURCE["lang"]), ("P2S", "nl"))

    def test_known_print_error_official_text(self):
        d = be.describe_print_error(be.CANCELLED)
        self.assertEqual((d["code"], d["text"], d["reason"]), ("0300-400C", "De taak is geannuleerd.", None))
        self.assertEqual(d["url"], be.WIKI_HOME)           # print_error-codes hebben geen eigen wikipagina
        self.assertIn("AMS", be.describe_print_error(0x07008011)["text"])

    def test_unknown_print_error_has_code_and_link(self):
        d = be.describe_print_error(UNKNOWN)
        self.assertEqual((d["code"], d["text"], d["reason"], d["url"]), ("0300-FFFF", None, None, be.WIKI_HOME))
        self.assertIsNone(be.describe_print_error(0))

    def test_hms_text_module_severity_and_wiki(self):
        d = be.describe_hms({"attr": 0x07002000, "code": 0x00020001})
        self.assertEqual(d["code"], "0700_2000_0002_0001")
        self.assertEqual((d["module"], d["severity"]), ("AMS", "ernstig"))
        self.assertIn("filament", d["text"])
        # P2S-specifieke wikipagina gaat voor
        self.assertEqual(be.describe_hms({"attr": 0x03000100, "code": 0x00010007})["url"],
                         "https://wiki.bambulab.com/en/p2s/troubleshooting/hmscode/0300_0100_0001_0007")
        unknown = be.describe_hms({"attr": 0x0300FFFF, "code": 0x0001FFFF})
        self.assertEqual((unknown["text"], unknown["url"]), (None, be.WIKI_HOME))
        self.assertEqual(be.label(unknown), "0300_FFFF_0001_FFFF (bewegingssturing)")
        self.assertIsNone(be.describe_hms({"attr": "x"}))
        self.assertIsNone(be.describe_hms("rommel"))

    def test_hms_list_dedup_and_garbage(self):
        e = {"attr": 0x0C001000, "code": 0x00030001}
        self.assertEqual(len(be.describe_hms_list([e, e, None, {}])), 1)
        self.assertEqual(be.describe_hms_list(None), [])

    def test_extra_overrides_generated(self):
        with mock.patch.dict(be.PRINT_ERRORS_EXTRA, {"0300-400C": "Eigen tekst"}), \
             mock.patch.dict(be.HMS_CODES_EXTRA, {"0300_FFFF_0001_FFFF": "Eigen HMS"}):
            self.assertEqual(be.describe_print_error(be.CANCELLED)["text"], "Eigen tekst")
            self.assertEqual(be.describe_hms({"attr": 0x0300FFFF, "code": 0x0001FFFF})["text"], "Eigen HMS")

    def test_fail_reason_suggestions(self):
        self.assertEqual(be.suggest_fail_reason(0x03008003), "spaghetti")
        self.assertEqual(be.suggest_fail_reason(0x03004006), "verstopping")
        self.assertEqual(be.suggest_fail_reason(0x0300400D), "stroomuitval")
        hms = be.describe_hms_list([{"attr": 0x0C000300, "code": 0x00030008}])
        self.assertEqual(be.suggest_fail_reason(0, hms), "spaghetti")
        self.assertEqual(be.suggest_fail_reason(0x03004006, hms), "verstopping")   # print_error gaat voor
        self.assertIsNone(be.suggest_fail_reason(be.CANCELLED))
        self.assertIsNone(be.suggest_fail_reason(UNKNOWN))

    def test_fail_reason_codes_exist_and_are_valid(self):
        os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
        import app
        for code, reason in be.FAIL_REASONS_BY_CODE.items():
            self.assertIn(reason, app.FAIL_REASONS)
            self.assertIn(code, be.HMS_CODES if "_" in code else be.PRINT_ERRORS, code)

    def test_missing_data_file_degrades(self):
        with mock.patch.object(be, "DATA_FILE", "/bestaat/niet.json"), self.assertLogs("printvault.errors", "WARNING"):
            self.assertEqual(be._load(), ({}, {}, {}, {}))


class GeneratorTest(unittest.TestCase):
    """Het generatiescript, met een kleine nagebootste bron (geen netwerk)."""

    def test_build_prefers_model_and_language(self):
        nl = {"device_error": {"0300400C": {"De taak is geannuleerd.": []},
                               "03001111": {"Algemeen": [], "Voor P2S": ["P2S", "H2D"]},
                               "XYZ": {"ongeldig": []}},
              "device_hms": {"0300010000010007": {"": []}}}
        en = {"device_error": {"03002222": {"Only English": []}},
              "device_hms": {"0300010000010007": {"Heatbed sensor open": []}}}
        wiki = {"0300010000010007": {"/en/x1/troubleshooting/hmscode/0300_0100_0001_0007": [],
                                     "/en/p2s/troubleshooting/hmscode/0300_0100_0001_0007": ["P2S"]},
                "0300010000010003": {"https://elders.example/x": []}}
        out = gen.build(nl, en, wiki, "P2S")
        self.assertEqual(out["print_errors"], {"0300-400C": "De taak is geannuleerd.", "0300-1111": "Voor P2S",
                                               "0300-2222": "Only English"})
        self.assertEqual(out["hms"], {"0300_0100_0001_0007": "Heatbed sensor open"})   # lege nl-tekst → en
        self.assertEqual(out["wiki"], {"0300_0100_0001_0007":
                                       "https://wiki.bambulab.com/en/p2s/troubleshooting/hmscode/0300_0100_0001_0007"})
        self.assertEqual(gen.build(nl, en, wiki, "X1C")["print_errors"]["0300-1111"], "Algemeen")

    def test_stage_table_read_without_executing(self):
        src = (
            "import os\nos.system('echo NIET UITVOEREN')\n"
            "CURRENT_STAGE_IDS = {\n    'default': 'unknown',\n    0: 'printing',\n    54: 'waiting',\n    -1: 'idle',\n}\n"
            "OTHER = {1: 'x'}\n")
        ids = gen.literal_dict(src, "CURRENT_STAGE_IDS")
        self.assertEqual(ids, {"default": "unknown", 0: "printing", 54: "waiting", -1: "idle"})
        tr = {"entity": {"sensor": {"stage": {"state": {"printing": "Aan het printen"}}}}}
        tr_en = {"entity": {"sensor": {"stage": {"state": {"printing": "Printing", "waiting": "Waiting"}}}}}
        self.assertEqual(gen.build_stages(ids, tr, tr_en), {
            "0": {"key": "printing", "en": "Printing", "nl": "Aan het printen"},
            "54": {"key": "waiting", "en": "Waiting", "nl": None},
            "-1": {"key": "idle", "en": None, "nl": None}})
        call_with_func = "CURRENT_STAGE_IDS = dict(a=1)\n"
        with self.assertRaises(ValueError):
            gen.literal_dict(call_with_func, "CURRENT_STAGE_IDS")

    def test_data_file_matches_generator_format(self):
        with open(be.DATA_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(set(data), {"print_errors", "hms", "wiki", "stages", "_source"})
        self.assertEqual(data["stages"]["0"]["key"], "printing")
        self.assertTrue(all(k.lstrip("-").isdigit() and v["key"] for k, v in data["stages"].items()))
        self.assertTrue(all(len(k) == 9 for k in data["print_errors"]))
        self.assertTrue(all(len(k) == 19 for k in data["hms"]))
        self.assertTrue(all(u.startswith("https://wiki.bambulab.com/en/") for u in data["wiki"].values()))


if __name__ == "__main__":
    unittest.main()
