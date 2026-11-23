"""The tests must never touch real data, whatever DATA_DIR is set to (e.g. /data inside the live container)."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

TESTS = Path(__file__).resolve().parent


class DataIsolationTest(unittest.TestCase):
    def test_existing_data_survives_a_test_run(self):
        for names in (["discover", "-s", "tests", "-k", "AppTest"], ["tests/test_pages.py", "-k", "test_empty_states"]):
            with tempfile.TemporaryDirectory() as real:
                db, photo = Path(real, "printvault.db"), Path(real, "uploads", "photo.jpg")
                photo.parent.mkdir()
                with closing(sqlite3.connect(db)) as con, con:   # a valid database: the app would open it
                    con.execute("CREATE TABLE precious (x)")
                before = db.read_bytes()
                photo.write_bytes(b"precious")
                r = subprocess.run([sys.executable, "-m", "unittest", *names], cwd=TESTS.parent, capture_output=True,
                                   text=True, env={**os.environ, "DATA_DIR": real})
                self.assertTrue(db.exists() and db.read_bytes() == before, "database touched")
                self.assertTrue(photo.exists() and photo.read_bytes() == b"precious", "uploads touched")
                self.assertEqual(sorted(p.name for p in Path(real).rglob("*")), ["photo.jpg", "printvault.db", "uploads"])
                self.assertEqual(r.returncode, 0, r.stderr[-500:])

    def test_hard_stop_outside_the_temporary_folder(self):
        code = ("import sys; sys.path.insert(0, 'tests'); import testenv, app; app.DB_PATH = sys.argv[1]; testenv.reset()")
        with tempfile.TemporaryDirectory() as real:
            db = Path(real, "printvault.db")
            db.write_bytes(b"precious")
            r = subprocess.run([sys.executable, "-c", code, str(db)], cwd=TESTS.parent, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("refusing", r.stderr)
            self.assertEqual(db.read_bytes(), b"precious")

    def test_app_imported_too_early_is_refused(self):
        code = "import sys; sys.path.insert(0, 'tests'); import app; import testenv"
        with tempfile.TemporaryDirectory() as other:
            r = subprocess.run([sys.executable, "-c", code], cwd=TESTS.parent, capture_output=True, text=True,
                               env={**os.environ, "DATA_DIR": other})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing", r.stderr)

    def test_no_test_bypasses_testenv(self):
        for path in TESTS.glob("test_*.py"):
            if path.name == "test_testenv.py":
                continue
            text = path.read_text()
            for bad in ("DATA_DIR", "remove(app.DB_PATH", "rmtree(app.", "unlink("):
                self.assertNotIn(bad, text, path.name)
            if "import app" in text:
                self.assertLess(text.index("import testenv"), text.index("import app"), path.name)


if __name__ == "__main__":
    unittest.main()
