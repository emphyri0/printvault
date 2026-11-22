"""Camerabeeld van Bambu Lab-printers (RTSPS op poort 322, o.a. X1, P2S, H2D).

Browsers kunnen RTSP niet afspelen; ffmpeg zet het beeld om naar losse JPEG's.
- Camera: één gedeeld ffmpeg-proces per printer dat start bij de eerste kijker en stopt
  zodra er IDLE_SECONDS niemand meer kijkt (de printer laat maar weinig verbindingen toe).
- grab_snapshot(): één los beeld, ook bruikbaar vanuit de MQTT-listener.

De access code zit in de RTSP-URL en die zet ffmpeg in zijn foutmeldingen;
alles wat gelogd of getoond wordt, gaat daarom eerst door redact().
"""
import collections
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse

log = logging.getLogger("printvault.camera")

RTSP_PORT = 322
FPS = int(os.environ.get("CAMERA_FPS", "5"))
WIDTH = int(os.environ.get("CAMERA_WIDTH", "1280"))
IDLE_SECONDS = 15          # zo lang na de laatste kijker blijft de stream nog open
STALL_SECONDS = 20         # zonder nieuw beeld stopt een kijker-stream
SNAPSHOT_TIMEOUT = 25
SOCKET_TIMEOUT_US = 10_000_000
MAX_FRAME = 8 * 1024 * 1024

SOI, EOI = b"\xff\xd8", b"\xff\xd9"


class CameraError(Exception):
    pass


def rtsp_url(ip, access_code):
    return f"rtsps://bblp:{urllib.parse.quote(access_code, safe='')}@{ip}:{RTSP_PORT}/streaming/live/1"


_URL_CRED = re.compile(r"(rtsps?://[^:/@\s]+:)[^@\s]*@", re.I)


def redact(text, access_code=None):
    """Haalt de access code uit tekst (zowel in een URL als los)."""
    text = _URL_CRED.sub(r"\1***@", str(text))
    if access_code:
        for s in {access_code, urllib.parse.quote(access_code, safe="")}:
            text = text.replace(s, "***")
    return text


def ffmpeg_cmd(url, *, fps=None, width=None, single=False):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
           "-rtsp_transport", "tcp", "-timeout", str(SOCKET_TIMEOUT_US)]
    if not single:
        cmd += ["-fflags", "nobuffer", "-flags", "low_delay"]
    cmd += ["-i", url, "-an"]
    if single:
        cmd += ["-frames:v", "1", "-q:v", "3", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    else:
        cmd += ["-vf", f"fps={fps or FPS},scale='min({width or WIDTH},iw)':-2",
                "-q:v", "6", "-f", "mjpeg", "pipe:1"]
    return cmd


class FrameParser:
    """Knipt een doorlopende MJPEG-stroom (aaneengeplakte JPEG's) in losse beelden."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        frames = []
        while True:
            start = self.buf.find(SOI)
            if start < 0:
                # een 0xFF op het einde kan de eerste helft van de volgende SOI zijn
                del self.buf[:-1 if self.buf.endswith(b"\xff") else len(self.buf)]
                break
            end = self.buf.find(EOI, start + 2)
            if end < 0:
                del self.buf[:start]
                if len(self.buf) > MAX_FRAME:   # nooit een einde gevonden: weggooien
                    self.buf.clear()
                break
            frames.append(bytes(self.buf[start:end + 2]))
            del self.buf[:end + 2]
        return frames


def grab_snapshot(ip, access_code, timeout=SNAPSHOT_TIMEOUT):
    """Eén JPEG van de printercamera (volle resolutie). Geeft CameraError bij een probleem."""
    try:
        res = subprocess.run(ffmpeg_cmd(rtsp_url(ip, access_code), single=True),
                             capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise CameraError("ffmpeg is niet geïnstalleerd.")
    except subprocess.TimeoutExpired:
        raise CameraError(f"Geen camerabeeld binnen {timeout} seconden.")
    if res.returncode != 0 or not res.stdout.startswith(SOI):
        msg = res.stderr.decode(errors="replace").strip().splitlines()
        raise CameraError("Camerabeeld ophalen mislukt: "
                          + redact(msg[-1] if msg else f"ffmpeg stopte met code {res.returncode}", access_code))
    return res.stdout


class Camera:
    """Gedeelde live stream van één printer."""

    def __init__(self, ip, access_code, idle=IDLE_SECONDS):
        self.url = rtsp_url(ip, access_code)
        self.secret = access_code
        self.idle = idle
        self.cond = threading.Condition()
        self.frame = None
        self.frame_no = 0
        self.frame_at = 0.0
        self.viewers = 0
        self.last_used = time.monotonic()
        self.thread = None
        self.proc = None
        self.closed = False
        self.error = None

    # ---------- voor kijkers ----------

    def frames(self, stall=STALL_SECONDS):
        """Generator met nieuwe beelden zolang de kijker blijft; stopt na `stall` s zonder beeld."""
        with self.cond:
            self.viewers += 1
            self._touch_and_start()
            last = self.frame_no - 1 if self.frame is not None else self.frame_no
        try:
            while True:
                with self.cond:
                    if not self.cond.wait_for(lambda: self.closed or self.frame_no != last, timeout=stall):
                        return
                    if self.closed:
                        return
                    frame, last = self.frame, self.frame_no
                    self.last_used = time.monotonic()
                yield frame
        finally:
            with self.cond:
                self.viewers -= 1
                self.last_used = time.monotonic()

    def latest(self, max_age=3.0):
        """Het laatste beeld als de stream loopt en het beeld vers is, anders None."""
        with self.cond:
            if self.frame is not None and time.monotonic() - self.frame_at <= max_age:
                return self.frame
        return None

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()
            proc = self.proc
        _stop(proc)

    # ---------- achtergrond ----------

    def _touch_and_start(self):   # onder self.cond
        self.last_used = time.monotonic()
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True, name="camera")
            self.thread.start()

    def _idle(self):              # onder self.cond
        return self.closed or (self.viewers == 0 and time.monotonic() - self.last_used > self.idle)

    def _run(self):
        backoff = 2
        while True:
            with self.cond:
                if self._idle():
                    self.thread = None
                    self.frame = None
                    self.cond.notify_all()
                    return
            started = time.monotonic()
            self._stream_once()
            if time.monotonic() - started > 30:
                backoff = 2
            else:
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _stream_once(self):
        try:
            proc = subprocess.Popen(ffmpeg_cmd(self.url), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            self.error = "ffmpeg is niet geïnstalleerd."
            log.error("camera: %s", self.error)
            return
        with self.cond:
            self.proc = proc
        errors = collections.deque(maxlen=5)
        def read_errors():
            with proc.stderr:
                for line in proc.stderr:
                    errors.append(line)
        threading.Thread(target=read_errors, daemon=True).start()
        parser = FrameParser()
        try:
            while True:
                data = proc.stdout.read1(65536)
                if not data:
                    break
                frames = parser.feed(data)
                with self.cond:
                    if frames:
                        self.frame = frames[-1]
                        self.frame_no += 1
                        self.frame_at = time.monotonic()
                        self.error = None
                        self.cond.notify_all()
                    if self._idle():
                        break
        finally:
            _stop(proc)
            proc.stdout.close()
            with self.cond:
                self.proc = None
        if proc.returncode not in (0, -15, -9) and not self.closed:
            last = errors[-1].decode(errors="replace").strip() if errors else f"code {proc.returncode}"
            self.error = redact(last, self.secret)
            log.warning("camera: ffmpeg stopte: %s", self.error)


def _stop(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class CameraManager:
    """Eén Camera per printer; een gewijzigd IP of access code geeft een nieuwe."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cameras = {}

    def get(self, printer_id, ip, access_code):
        url = rtsp_url(ip, access_code)
        with self.lock:
            cam = self.cameras.get(printer_id)
            if cam is None or cam.url != url:
                if cam is not None:
                    cam.close()
                cam = self.cameras[printer_id] = Camera(ip, access_code)
            return cam

    def peek(self, printer_id):
        with self.lock:
            return self.cameras.get(printer_id)
