"""Protection against other websites, for an app without a login.

- Host check: only localhost, IP addresses and the names in ALLOWED_HOSTS. A DNS rebinding attack needs
  a host name of its own, so it stops here.
- Cross-site check: a request that changes something must come from Printvault's own pages.
- Response headers.
"""
import ipaddress
import logging
from urllib.parse import unquote, urlsplit

from flask import Response, current_app, request, url_for
from werkzeug.exceptions import BadRequest, HTTPException

log = logging.getLogger("printvault.security")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DEFAULT_PORTS = {"http": 80, "https": 443}

HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    # Inline styles are used for colours and progress bars. The frame-src schemes are for
    # 'Open in Bambu Studio' in Firefox (a hidden frame, see static/app.js).
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data: blob:; object-src 'none'; base-uri 'none'; form-action 'self'; "
                               "frame-ancestors 'none'; frame-src bambustudio: bambustudioopen:",
}


def _split(host):
    """'name:8100' → ('name', 8100); '[::1]' → ('::1', None). Raises ValueError when it is not a host."""
    parts = urlsplit(f"//{host}")
    if not parts.hostname or parts.username is not None or parts.path:
        raise ValueError(f"not a host: {host!r}")
    return parts.hostname.rstrip("."), parts.port


def parse_hosts(value):
    """ALLOWED_HOSTS ('a.example, b.lan:8100') → {'a.example', 'b.lan'}."""
    hosts = set()
    for item in (value or "").split(","):
        if item.strip():
            try:
                hosts.add(_split(item.strip())[0])
            except ValueError:
                log.warning("ALLOWED_HOSTS: %r is not a host name; skipped", item.strip())
    return hosts


def host_allowed(host, extra):
    try:
        name, _port = _split(host or "")
    except ValueError:
        return False
    if name == "localhost" or name in extra:
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def _same_host(url, host):
    """Whether an Origin/Referer has the host and port of this request. The scheme may differ: HTTPS can
    end at the proxy, so a default port counts as no port."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in DEFAULT_PORTS:
            return False
        name, port = _split(host)
        own = None if port in DEFAULT_PORTS.values() else port
        theirs = None if parts.port == DEFAULT_PORTS[parts.scheme] else parts.port
        return bool(parts.hostname) and (parts.hostname.rstrip("."), theirs) == (name, own)
    except ValueError:
        return False


def cross_site(headers, host):
    """Why this request does not come from Printvault's own pages, or None when it does. Browsers send
    Sec-Fetch-Site (over HTTPS and on localhost) and Origin with every POST; without any of these
    headers it is not a browser (e.g. curl)."""
    site = headers.get("Sec-Fetch-Site")
    if site is not None:
        return None if site in ("same-origin", "none") else f"Sec-Fetch-Site: {site}"
    for name in ("Origin", "Referer"):
        value = headers.get(name)
        if value is not None:
            return None if _same_host(value, host) else f"{name}: {value}"
    return None


def own_page(referrer):
    """Relative URL of the Printvault page a Referer points to, or None when it is not one. The URL is built
    anew from the matching route; nothing of the header itself ends up in it but the route's arguments."""
    if not referrer or not _same_host(referrer, request.host):
        return None
    try:
        path = unquote(urlsplit(referrer).path)
        endpoint, args = current_app.create_url_adapter(request).match(path, method="GET")
    except HTTPException:   # no such page, POST only, or a redirect of werkzeug itself
        return None
    return url_for(endpoint, **args)


def _refuse(status, text):
    return Response(text + "\n", status=status, mimetype="text/plain", headers={"Cache-Control": "no-store"})


def check_request():
    try:
        host = request.host
    except BadRequest:   # werkzeug refuses impossible host names itself
        host = request.headers.get("Host", "")
    if not host_allowed(host, current_app.config["ALLOWED_HOSTS"]):
        hint = ""
        forwarded = request.headers.get("X-Forwarded-Host")
        if forwarded and forwarded != host:
            hint = (f" A proxy sent X-Forwarded-Host {forwarded!r}, which is ignored: set TRUSTED_PROXIES=1 "
                    "if that is your own reverse proxy.")
        current_app.logger.warning("Refused a request for host %r: not in ALLOWED_HOSTS. If this is your own address, add the "
                    "host name to ALLOWED_HOSTS in .env and recreate the container (docker compose up -d).%s", host, hint)
        return _refuse(421, f"Host {host!r} is not allowed. Localhost and IP addresses always work; add your own host "
                            "name to ALLOWED_HOSTS in .env and recreate the container (docker compose up -d).")
    if request.method not in SAFE_METHODS:
        reason = cross_site(request.headers, host)
        if reason:
            current_app.logger.warning("Refused a cross-site %s %r for host %r (%r)", request.method, request.path, host, reason)
            return _refuse(403, "Refused: this request came from another website. Open Printvault and try again.")
    return None


def add_headers(response):
    for name, value in HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def init_app(app, allowed_hosts):
    app.config["ALLOWED_HOSTS"] = parse_hosts(allowed_hosts)
    app.before_request(check_request)
    app.after_request(add_headers)
