"""Tests for thumbs.py with the real ffmpeg (run them in the image: see README, Local development).
Without ffmpeg on the machine these tests are skipped; the logic tests at the bottom always run."""
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import testenv  # noqa: E402,F401
import thumbs  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def size_of(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def webp_chunks(path):
    data = read(path)
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    i, chunks = 12, []
    while i + 8 <= len(data):
        cid = data[i:i + 4].decode("latin-1")
        n = struct.unpack("<I", data[i + 4:i + 8])[0]
        chunks.append(cid)
        i += 8 + n + (n & 1)
    return chunks, data


def add_exif(jpeg_path, orientation=6):
    """Adds an EXIF block with an orientation and a GPS latitude to a JPEG."""
    d = read(jpeg_path)
    n0 = 2
    gps_off = 8 + 2 + n0 * 12 + 4
    ifd0 = (struct.pack("<H", n0) + struct.pack("<HHII", 0x0112, 3, 1, orientation)
            + struct.pack("<HHII", 0x8825, 4, 1, gps_off) + struct.pack("<I", 0))
    gps = struct.pack("<H", 1) + struct.pack("<HHI4s", 1, 2, 2, b"N\x00\x00\x00") + struct.pack("<I", 0)
    app1 = b"Exif\x00\x00" + b"II*\x00" + struct.pack("<I", 8) + ifd0 + gps
    with open(jpeg_path, "wb") as fh:
        fh.write(d[:2] + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 + d[2:])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed (run the tests in the printvault image)")
class MakeThumbs(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def path(self, name):
        return os.path.join(self.dir, name)

    def test_large_jpeg_with_exif_orientation_and_gps(self):
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=2400x1200", "-frames:v", "1", self.path("a.jpg"))
        add_exif(self.path("a.jpg"))
        original = read(self.path("a.jpg"))
        self.assertTrue(thumbs.make(self.dir, "a.jpg"))
        # orientation 6 = rotated 90°: 2400x1200 is shown as 1200x2400, then scaled to the width
        self.assertEqual(size_of(thumbs.thumb_path(self.dir, "a.jpg", 600)), (600, 1200))
        self.assertEqual(size_of(thumbs.thumb_path(self.dir, "a.jpg", 1600)), (1200, 2400))
        for size in thumbs.SIZES:
            chunks, data = webp_chunks(thumbs.thumb_path(self.dir, "a.jpg", size))
            self.assertFalse({"EXIF", "XMP ", "ICCP"} & set(chunks), chunks)
            self.assertNotIn(b"Exif", data)
        self.assertEqual(read(self.path("a.jpg")), original)   # original untouched
        self.assertEqual(thumbs.missing(self.dir, "a.jpg"), [])
        self.assertEqual(thumbs.pending(self.dir), [])

    def test_small_png_is_not_enlarged(self):
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x200", "-frames:v", "1", self.path("b.png"))
        self.assertTrue(thumbs.make(self.dir, "b.png"))
        self.assertEqual(size_of(thumbs.thumb_path(self.dir, "b.png", 600)), (320, 200))
        self.assertEqual(size_of(thumbs.thumb_path(self.dir, "b.png", 1600)), (320, 200))

    def test_animated_gif_gives_a_still_first_frame(self):
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=800x600:rate=5", "-t", "1", self.path("c.gif"))
        self.assertTrue(thumbs.make(self.dir, "c.gif"))
        chunks, _ = webp_chunks(thumbs.thumb_path(self.dir, "c.gif", 600))
        self.assertNotIn("ANIM", chunks)
        self.assertNotIn("ANMF", chunks)
        self.assertEqual(size_of(thumbs.thumb_path(self.dir, "c.gif", 600)), (600, 450))

    def test_broken_file_fails_without_leftovers(self):
        with open(self.path("d.jpg"), "wb") as fh:
            fh.write(b"not an image")
        with self.assertLogs("printvault.thumbs", "WARNING"):
            self.assertFalse(thumbs.make(self.dir, "d.jpg"))
        self.assertEqual(os.listdir(os.path.join(self.dir, thumbs.SUBDIR)), [])

    def test_worker_and_remove(self):
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x480", "-frames:v", "1", self.path("e.jpg"))
        ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x480", "-frames:v", "1", self.path("f.webp"))
        w = thumbs.Worker(self.dir)
        w.backfill()
        w.q.join()
        self.assertEqual(thumbs.pending(self.dir), [])
        thumbs.remove(self.dir, "e.jpg")
        self.assertEqual(thumbs.pending(self.dir), ["e.jpg"])


class Logic(unittest.TestCase):
    def test_ffmpeg_present_when_required(self):
        """CI sets REQUIRE_FFMPEG=1: then the ffmpeg tests above must run, not be skipped."""
        if os.environ.get("REQUIRE_FFMPEG") == "1":
            self.assertTrue(HAVE_FFMPEG, "REQUIRE_FFMPEG=1 but ffmpeg/ffprobe is not installed")

    def test_is_photo(self):
        self.assertTrue(thumbs.is_photo("0a1b.JPG"))
        self.assertTrue(thumbs.is_photo("x.gif"))
        for bad in ("x.3mf", "../x.jpg", "a/b.jpg", "x", "", None, "x.jpg-600.webp/.."):
            self.assertFalse(thumbs.is_photo(bad), bad)

    def test_missing_ffmpeg_is_logged(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "a.jpg"), "wb") as fh:
                fh.write(b"\xff\xd8\xff")
            with mock.patch("subprocess.run", side_effect=FileNotFoundError("ffmpeg")), \
                    self.assertLogs("printvault.thumbs", "WARNING"):
                self.assertFalse(thumbs.make(d, "a.jpg"))
            self.assertFalse(thumbs.make(d, "nothere.jpg"))
            self.assertFalse(thumbs.make(d, "a.3mf"))
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
