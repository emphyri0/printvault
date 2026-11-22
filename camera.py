"""Camera image of Bambu Lab printers. Two routes (which one per model: bambu_models.py):

- RTSPS on port 322 (X1, H2, P2 series): browsers cannot play RTSP; ffmpeg converts the stream
  into separate JPEGs.
- JPEG over TLS on port 6000 (A1, P1 series): the printer sends complete JPEGs (about one every
  1–2 seconds) after a login packet; read in Python (JpegStream), no ffmpeg needed.
  Protocol as implemented in ha-bambulab (MIT, https://github.com/greghesp/ha-bambulab),
  pybambu/bambu_client.py, ChamberImageThread.

- Camera: one shared stream per printer that starts with the first viewer and stops once nobody
  has watched for IDLE_SECONDS (the printer only allows a few connections).
- grab_snapshot(): a single image, also usable from the MQTT listener.

The access code is part of the RTSP URL, and ffmpeg prints that URL in its error messages;
everything that is logged or shown therefore goes through redact() first.
"""
import collections
import logging
import os
import re
import socket
import ssl
import struct
import subprocess
import threading
import time
import urllib.parse

from bambu_models import RTSPS, TCP6000

log = logging.getLogger("printvault.camera")

RTSP_PORT = 322
JPEG_PORT = 6000
JPEG_READ_TIMEOUT = 15     # the printer sends a new image every 1–2 s
FPS = int(os.environ.get("CAMERA_FPS", "5"))
WIDTH = int(os.environ.get("CAMERA_WIDTH", "1280"))
IDLE_SECONDS = 15          # the stream stays open this long after the last viewer
STALL_SECONDS = 20         # a viewer stream stops without a new frame for this long
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
    """Removes the access code from text (both inside a URL and on its own)."""
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
    """Cuts a continuous MJPEG stream (concatenated JPEGs) into separate frames."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        frames = []
        while True:
            start = self.buf.find(SOI)
            if start < 0:
                # a 0xFF at the end may be the first half of the next SOI
                del self.buf[:-1 if self.buf.endswith(b"\xff") else len(self.buf)]
                break
            end = self.buf.find(EOI, start + 2)
            if end < 0:
                del self.buf[:start]
                if len(self.buf) > MAX_FRAME:   # never found an end: discard
                    self.buf.clear()
                break
            frames.append(bytes(self.buf[start:end + 2]))
            del self.buf[:end + 2]
        return frames


# ---------- JPEG over TLS (port 6000) ----------

def jpeg_auth_packet(access_code):
    """Login packet: 4 little-endian words (0x40, 0x3000, 0, 0), then user 'bblp' and the access code,
    each zero-padded to 32 bytes (ha-bambulab, ChamberImageThread)."""
    try:
        code = access_code.encode("ascii")
    except UnicodeEncodeError:
        raise CameraError("The access code contains characters the camera protocol does not accept.")
    if len(code) > 32:
        raise CameraError("The access code is too long for the camera protocol.")
    return struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + code.ljust(32, b"\0")


def _tls_connect(ip, timeout):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # the printer has a self-signed certificate (as with MQTT)
    sock = socket.create_connection((ip, JPEG_PORT), timeout=timeout)
    try:
        return ctx.wrap_socket(sock, server_hostname=ip)
    except BaseException:
        sock.close()
        raise


class JpegStream:
    """Frames from the port-6000 camera. Each image: a 16-byte header (bytes 0–3: payload size,
    little-endian) followed by the JPEG itself. `connect(ip, timeout)` returns a connected socket;
    tests pass a fake one."""

    def __init__(self, ip, access_code, timeout=JPEG_READ_TIMEOUT, connect=None):
        self.ip, self.code, self.timeout = ip, access_code, timeout
        self.connect = connect or _tls_connect
        self.sock = None

    def open(self):
        packet = jpeg_auth_packet(self.code)
        try:
            self.sock = self.connect(self.ip, self.timeout)
            self.sock.settimeout(self.timeout)
            self.sock.sendall(packet)
        except (OSError, ssl.SSLError) as e:
            self.close()
            raise CameraError(f"No connection to the camera on port {JPEG_PORT}: {e}")
        return self

    def close(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _read(self, n):
        buf = bytearray()
        while len(buf) < n:
            sock = self.sock
            if sock is None:
                raise CameraError("Camera connection closed.")
            try:
                chunk = sock.recv(min(65536, n - len(buf)))
            except socket.timeout:
                raise CameraError(f"No camera image within {self.timeout} seconds.")
            except (OSError, ssl.SSLError) as e:
                raise CameraError(f"Camera connection lost: {e}")
            if not chunk:
                raise CameraError("The printer closed the camera connection (wrong access code, "
                                  "or LAN live view off?).")
            buf += chunk
        return bytes(buf)

    def frames(self):
        while True:
            header = self._read(16)
            size = struct.unpack_from("<I", header)[0]
            if not 4 <= size <= MAX_FRAME:
                raise CameraError(f"Unexpected camera data (image size {size}).")
            jpeg = self._read(size)
            if jpeg.startswith(SOI) and jpeg.endswith(EOI):
                yield jpeg
            else:
                log.warning("camera: skipped an image without JPEG start/end markers")

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()


# ---------- both routes ----------

def grab_snapshot(ip, access_code, timeout=SNAPSHOT_TIMEOUT, route=RTSPS):
    """One JPEG from the printer camera (full resolution). Raises CameraError on a problem."""
    if route == TCP6000:
        with JpegStream(ip, access_code, timeout=min(timeout, JPEG_READ_TIMEOUT)) as stream:
            for jpeg in stream.frames():
                return jpeg
    if route != RTSPS:
        raise CameraError("This printer has no camera.")
    try:
        res = subprocess.run(ffmpeg_cmd(rtsp_url(ip, access_code), single=True),
                             capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise CameraError("ffmpeg is not installed.")
    except subprocess.TimeoutExpired:
        raise CameraError(f"No camera image within {timeout} seconds.")
    if res.returncode != 0 or not res.stdout.startswith(SOI):
        msg = res.stderr.decode(errors="replace").strip().splitlines()
        raise CameraError("Fetching the camera image failed: "
                          + redact(msg[-1] if msg else f"ffmpeg exited with code {res.returncode}", access_code))
    return res.stdout


class Camera:
    """Shared live stream of one printer."""

    def __init__(self, ip, access_code, idle=IDLE_SECONDS, route=RTSPS):
        self.route = route
        self.ip = ip
        self.url = rtsp_url(ip, access_code)
        self.key = (route, self.url)
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
        self.stream = None
        self.closed = False
        self.error = None

    # ---------- for viewers ----------

    def frames(self, stall=STALL_SECONDS):
        """Generator of new frames for as long as the viewer stays; stops after `stall` s without a frame."""
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
        """The latest frame if the stream is running and the frame is fresh, else None."""
        with self.cond:
            if self.frame is not None and time.monotonic() - self.frame_at <= max_age:
                return self.frame
        return None

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()
            proc, stream = self.proc, self.stream
        _stop(proc)
        if stream is not None:
            stream.close()

    # ---------- background ----------

    def _touch_and_start(self):   # under self.cond
        self.last_used = time.monotonic()
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True, name="camera")
            self.thread.start()

    def _idle(self):              # under self.cond
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

    def _publish(self, frame):   # under self.cond
        self.frame = frame
        self.frame_no += 1
        self.frame_at = time.monotonic()
        self.error = None
        self.cond.notify_all()

    def _stream_once(self):
        if self.route == TCP6000:
            return self._stream_jpeg()
        try:
            proc = subprocess.Popen(ffmpeg_cmd(self.url), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            self.error = "ffmpeg is not installed."
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
                        self._publish(frames[-1])
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
            log.warning("camera: ffmpeg exited: %s", self.error)


    def _stream_jpeg(self):
        stream = JpegStream(self.ip, self.secret)
        with self.cond:
            if self.closed:
                return
            self.stream = stream
        try:
            stream.open()
            for frame in stream.frames():
                with self.cond:
                    self._publish(frame)
                    if self._idle():
                        break
        except CameraError as e:
            if not self.closed:
                self.error = redact(e, self.secret)
                log.warning("camera: %s", self.error)
        finally:
            stream.close()
            with self.cond:
                self.stream = None


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
    """One Camera per printer; a changed IP or access code gives a new one."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cameras = {}

    def get(self, printer_id, ip, access_code, route=RTSPS):
        key = (route, rtsp_url(ip, access_code))
        with self.lock:
            cam = self.cameras.get(printer_id)
            if cam is None or cam.key != key:
                if cam is not None:
                    cam.close()
                cam = self.cameras[printer_id] = Camera(ip, access_code, route=route)
            return cam

    def peek(self, printer_id):
        with self.lock:
            return self.cameras.get(printer_id)
