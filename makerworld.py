"""Metadata and cover of a MakerWorld model, from the website's own (undocumented) API."""
import html
import http.client
import json
import logging
import re
import urllib.parse
import urllib.request

import slicer
import thumbs

log = logging.getLogger("printvault.makerworld")

API = "https://makerworld.com/api/v1/design-service/design/{}"
URL = re.compile(r"^https?://(www\.)?makerworld\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?models/(\d{1,20})(?!\d)", re.I)
IMAGE_HOST = re.compile(r"^https://[a-z0-9.-]+\.bblmw\.com/")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MAX_COVER_BYTES = 15 * 1024 * 1024


class MakerWorldError(Exception):
    pass


def redirect_allowed(url):
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and parts.netloc.lower() == host and (
        host in ("makerworld.com", "www.makerworld.com") or host.endswith(".bblmw.com"))


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects only to MakerWorld itself, never to another (e.g. internal) host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not redirect_allowed(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_Redirects)


def _http_get(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    with _opener.open(req, timeout=20) as resp:
        body = resp.read(limit + 1)
    if len(body) > limit:
        raise MakerWorldError("Response from MakerWorld is too large.")
    return body


def _strip_html(text):
    text = re.sub(r"<\s*(br|/p|/li|/h\d)\s*/?>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# MakerWorld's API is not documented: every field may be missing or of another type.
def _dict(value):
    return value if isinstance(value, dict) else {}


def _dicts(value):
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _text(value):
    return value.replace("\r\n", "\n").strip() if isinstance(value, str) else ""


def _number(value):
    """Positive number, or None."""
    f = slicer.to_float(value)
    return f if f and f > 0 else None


def fetch(url, materials):
    """Fetches the metadata of a MakerWorld model. `materials`: the known materials. Returns
    (print data, cover); cover is (bytes, extension) or None."""
    m = URL.match(url)
    if not m:
        raise MakerWorldError("That does not look like a MakerWorld model link (https://makerworld.com/…/models/…).")
    design_id = m.group(2)
    try:
        d = json.loads(_http_get(API.format(design_id), 5 * 1024 * 1024))
    except (OSError, http.client.HTTPException, ValueError, RecursionError) as e:
        raise MakerWorldError(f"Could not fetch MakerWorld ({e}). Fill in the print manually.") from e
    d = _dict(d)
    title = _text(d.get("title"))
    if not title:
        raise MakerWorldError("Model not found on MakerWorld.")

    instances = _dicts(d.get("instances"))
    inst = next((i for i in instances if i.get("id") == d.get("defaultInstanceId")), None) \
        or next((i for i in instances if i.get("isDefault")), None) \
        or (instances[0] if instances else {})
    profile = _text(inst.get("title"))
    filaments = [{"type": _text(f.get("type")), "color": _text(f.get("color")), "used_g": _number(f.get("usedG"))}
                 for f in _dicts(inst.get("instanceFilaments"))]
    found = {slicer.material_of(f["type"], materials) for f in filaments if f["type"]}

    notes = []
    if profile:
        notes.append(f"Profile: {profile}")
    if filaments:
        notes.append("Filament: " + slicer.filament_summary(filaments))
    if inst.get("needAms"):
        notes.append("Requires AMS.")
    summary = _strip_html(_text(d.get("summary"))[:20000])
    if summary:
        notes += ["", summary[:3000]]

    designer = _text(_dict(d.get("designCreator")).get("name"))
    prediction = _number(inst.get("prediction"))
    layer = re.search(r"(0\.\d{1,4})\s*mm\s*layer", profile, re.I)
    tags = d.get("tags") if isinstance(d.get("tags"), list) else []
    data = {
        "name": title[:200],
        "description": f"MakerWorld model by {designer}" if designer else "MakerWorld model",
        "material": (found.pop() if len(found) == 1 else "Other") if found else "",
        "color": filaments[0]["color"] if len(filaments) == 1 else "",
        "filament_g": _number(inst.get("weight")),
        "print_time_min": round(prediction / 60) if prediction else None,
        "layer_height": float(layer.group(1)) if layer else None,
        "nozzle": None,
        "status": "planned",
        "tags": ", ".join([t for t in map(_text, tags) if t][:8]),
        "notes": "\n".join(notes).strip(),
        "printed_on": None,
        "printer_id": None,
        "source_url": f"https://makerworld.com/en/models/{design_id}",
        "designer": designer or None,
        "license": _text(d.get("license")) or None,
    }
    return data, fetch_cover(_text(d.get("coverUrl")))


def fetch_cover(url):
    """(bytes, extension), or None when there is no usable cover."""
    if not IMAGE_HOST.match(url):
        return None
    try:
        content = _http_get(url, MAX_COVER_BYTES)
    except (OSError, http.client.HTTPException, MakerWorldError) as e:
        log.warning("MakerWorld cover not fetched: %s", e)
        return None
    ext = thumbs.sniff(content)
    return (content, ext) if ext else None
