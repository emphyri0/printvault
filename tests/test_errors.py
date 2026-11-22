"""Tests voor bambu_errors (vertaling van print_error en HMS)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bambu_errors as be  # noqa: E402


class ErrorCodeTest(unittest.TestCase):
    def test_error_code_format(self):
        self.assertEqual(be.error_code(50348044), "0300-400C")
        self.assertEqual(be.error_code("50348044"), "0300-400C")
        self.assertIsNone(be.error_code(0))
        self.assertIsNone(be.error_code(None))
        self.assertIsNone(be.error_code("x"))

    def test_known_print_error(self):
        d = be.describe_print_error(be.CANCELLED)
        self.assertEqual(d["code"], "0300-400C")
        self.assertEqual(d["text"], "Print geannuleerd op de printer")
        self.assertIsNone(d["reason"])
        self.assertTrue(d["url"].endswith("/0300_400C"))

    def test_unknown_print_error_has_code_and_link(self):
        d = be.describe_print_error(0x07008011)
        self.assertEqual((d["code"], d["text"], d["reason"]), ("0700-8011", None, None))
        self.assertIn("0700_8011", d["url"])
        self.assertIsNone(be.describe_print_error(0))

    def test_hms_code_module_severity(self):
        d = be.describe_hms({"attr": 0x07002000, "code": 0x00020001})
        self.assertEqual(d["code"], "0700_2000_0002_0001")
        self.assertEqual((d["module"], d["severity"], d["text"]), ("AMS", "ernstig", None))
        self.assertEqual(be.label(d), "0700_2000_0002_0001 (AMS)")
        self.assertIsNone(be.describe_hms({"attr": "x"}))
        self.assertIsNone(be.describe_hms("rommel"))

    def test_hms_list_dedup_and_garbage(self):
        e = {"attr": 0x0C001000, "code": 0x00030001}
        self.assertEqual(len(be.describe_hms_list([e, e, None, {}])), 1)
        self.assertEqual(be.describe_hms_list(None), [])

    def test_table_is_extensible_and_suggests_reason(self):
        with mock.patch.dict(be.PRINT_ERRORS, {"0300-9999": ("Testfout", "spaghetti")}), \
             mock.patch.dict(be.HMS_CODES, {"0C00_1000_0003_0001": ("HMS-test", "hechting")}):
            self.assertEqual(be.describe_print_error(0x03009999)["text"], "Testfout")
            self.assertEqual(be.suggest_fail_reason(0x03009999), "spaghetti")
            hms = be.describe_hms_list([{"attr": 0x0C001000, "code": 0x00030001}])
            self.assertEqual(be.label(hms[0]), "HMS-test (0C00_1000_0003_0001)")
            self.assertEqual(be.suggest_fail_reason(0, hms), "hechting")
            self.assertEqual(be.suggest_fail_reason(0x03009999, hms), "spaghetti")   # print_error gaat voor
        self.assertIsNone(be.suggest_fail_reason(be.CANCELLED))

    def test_reasons_are_valid_fail_reasons(self):
        os.environ.setdefault("DATA_DIR", __import__("tempfile").mkdtemp())
        import app
        for text, reason in [*be.PRINT_ERRORS.values(), *be.HMS_CODES.values()]:
            self.assertTrue(text)
            self.assertIn(reason, [None, *app.FAIL_REASONS])


if __name__ == "__main__":
    unittest.main()
