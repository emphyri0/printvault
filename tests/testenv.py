"""Import this before `app` in every test: it points DATA_DIR at a fresh temporary folder, whatever it was
set to, and refuses to delete anything anywhere else. The tests delete databases, so this must never be
the folder of a real installation."""
import atexit
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.realpath(tempfile.mkdtemp(prefix="printvault-test-"))


def check(path):
    """Hard stop when `path` is not inside the temporary folder."""
    real = os.path.realpath(path)
    if os.path.commonpath([real, DATA_DIR]) != DATA_DIR:
        raise RuntimeError(f"tests: refusing to touch {real}: it is not inside {DATA_DIR}")


if "app" in sys.modules:
    os.rmdir(DATA_DIR)
    raise RuntimeError("tests: refusing to run: app was imported before testenv")

os.environ["DATA_DIR"] = DATA_DIR
for _path in (os.path.join(ROOT, "scripts"), ROOT):
    sys.path.insert(0, _path)
atexit.register(shutil.rmtree, DATA_DIR, ignore_errors=True)


def reset(uploads=False):
    """A new, empty database (and an empty upload folder)."""
    import app
    check(app.DB_PATH)
    check(app.UPLOAD_DIR)
    for suffix in ("", "-journal", "-wal", "-shm"):
        if os.path.exists(app.DB_PATH + suffix):
            os.remove(app.DB_PATH + suffix)
    if uploads:
        shutil.rmtree(app.UPLOAD_DIR, ignore_errors=True)
    app.init_db()
