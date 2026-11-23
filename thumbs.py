"""Smaller WebP versions of uploaded photos, made with ffmpeg.

For every photo `<stored_name>` there are two files in `uploads/thumbs/`:
`<stored_name>-600.webp` (cards) and `<stored_name>-1600.webp` (detail page). The originals are
never changed. Images are only scaled down, never up. Of an animated GIF only the first frame is used.

ffmpeg applies the EXIF orientation of JPEG photos, and `-map_metadata -1` plus the WebP encoder
write no metadata at all: no EXIF, no GPS, no XMP (checked in tests/test_thumbs.py).
"""
import logging
import os
import queue
import re
import subprocess
import threading

SIZES = (600, 1600)
PHOTO_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
SUBDIR = "thumbs"
TIMEOUT = 120
_SAFE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9]+$")

log = logging.getLogger("printvault.thumbs")


SNIFF_BYTES = 12
# content type → ffmpeg demuxer, so ffmpeg never guesses (a playlist named x.jpg stays a broken photo)
DEMUXERS = {"jpg": "jpeg_pipe", "png": "png_pipe", "gif": "gif", "webp": "webp_pipe"}


def sniff(head):
    """Extension that belongs to the content (its first bytes), or None when it is not a photo."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def is_photo(stored_name):
    return bool(stored_name) and bool(_SAFE.match(stored_name)) and \
        stored_name.rsplit(".", 1)[1].lower() in PHOTO_EXT


def thumb_name(stored_name, size):
    return f"{stored_name}-{size}.webp"


def thumb_path(upload_dir, stored_name, size):
    return os.path.join(upload_dir, SUBDIR, thumb_name(stored_name, size))


def missing(upload_dir, stored_name):
    return [s for s in SIZES if not os.path.exists(thumb_path(upload_dir, stored_name, s))]


def ffmpeg_cmd(src, dst, size, kind):
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-protocol_whitelist", "file", "-f", DEMUXERS[kind], "-i", src, "-map", "0:v:0", "-frames:v", "1", "-map_metadata", "-1",
            "-vf", f"scale='min({size},iw)':-1:flags=lanczos",
            "-c:v", "libwebp", "-quality", "82", "-f", "webp", dst]


def make(upload_dir, stored_name):
    """Makes the missing thumbnails of one photo. Returns True when all exist afterwards.
    A failure is logged; callers then simply keep using the original."""
    if not is_photo(stored_name):
        return False
    src = os.path.join(upload_dir, stored_name)
    if not os.path.isfile(src):
        return False
    os.makedirs(os.path.join(upload_dir, SUBDIR), exist_ok=True)
    ok = True
    for size in missing(upload_dir, stored_name):
        dst = thumb_path(upload_dir, stored_name, size)
        tmp = f"{dst}.tmp-{os.getpid()}-{threading.get_ident()}"
        try:
            with open(src, "rb") as fh:
                kind = sniff(fh.read(SNIFF_BYTES))
            if kind is None:
                raise ValueError("not a JPEG, PNG, GIF or WebP image")
            r = subprocess.run(ffmpeg_cmd(src, tmp, size, kind), capture_output=True, timeout=TIMEOUT)
            if r.returncode != 0 or not os.path.getsize(tmp):
                raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[-300:] or f"exit {r.returncode}")
            os.replace(tmp, dst)
        except Exception as e:   # noqa: BLE001 - a thumbnail must never break an upload
            ok = False
            log.warning("thumbnail %s (%d px) failed: %s", stored_name, size, e)
            try:
                os.remove(tmp)
            except OSError:
                pass
    return ok


def remove(upload_dir, stored_name):
    if not stored_name:
        return
    for size in SIZES:
        try:
            os.remove(thumb_path(upload_dir, stored_name, size))
        except OSError:
            pass


def pending(upload_dir):
    """Photos in upload_dir that still miss a thumbnail."""
    try:
        names = sorted(os.listdir(upload_dir))
    except OSError:
        return []
    return [n for n in names if is_photo(n) and os.path.isfile(os.path.join(upload_dir, n))
            and missing(upload_dir, n)]


class Worker:
    """One background thread that makes thumbnails in order, so an upload request does not wait."""

    def __init__(self, upload_dir):
        self.upload_dir = upload_dir
        self.q = queue.Queue()
        self._queued = set()
        self._lock = threading.Lock()
        self._thread = None

    def add(self, stored_name):
        if not is_photo(stored_name):
            return
        with self._lock:
            if stored_name in self._queued:
                return
            self._queued.add(stored_name)
            self.q.put(stored_name)   # inside the lock: the thread only stops when the queue is empty
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="thumbs", daemon=True)
                self._thread.start()

    def backfill(self):
        """Queues every existing photo without thumbnails (once, at start-up)."""
        names = pending(self.upload_dir)
        if names:
            log.info("making thumbnails for %d existing photos", len(names))
        for n in names:
            self.add(n)

    def _run(self):
        while True:
            try:
                name = self.q.get(timeout=30)
            except queue.Empty:
                with self._lock:
                    if self.q.empty():
                        self._thread = None
                        return
                continue
            try:
                make(self.upload_dir, name)
            finally:
                with self._lock:
                    self._queued.discard(name)
                self.q.task_done()
