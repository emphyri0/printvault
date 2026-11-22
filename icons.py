"""Own inline SVG icons (24×24, stroke 1.8, currentColor). No third-party icon set or brand marks.

base.html includes SPRITE once; templates use {{ icon("clock") }} and JavaScript uses
<svg><use href="#i-clock"></use></svg> (see static/app.js: PV.icon).
"""
from markupsafe import Markup

PATHS = {
    "logo": '<path d="M4 8.5 12 4l8 4.5v7L12 20l-8-4.5z"/><path d="M4 8.5 12 13l8-4.5M12 13v7"/>',
    "grid": '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/>'
            '<rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
    "printer": '<rect x="4" y="3.5" width="16" height="17" rx="2"/><path d="M4 8h16M9 13h6M12 13v3"/>',
    "wrench": '<path d="M14.5 5.5a4 4 0 0 0 4.9 4.9l-8.8 8.8a2 2 0 0 1-2.8-2.8l8.8-8.8a4 4 0 0 0-2.1-2.1z"/>',
    "gear": '<circle cx="12" cy="12" r="3"/><path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8'
            'M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "search": '<circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/>',
    "clock": '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l2.5 2"/>',
    "weight": '<path d="M7 9h10l2 11H5z"/><circle cx="12" cy="6" r="2"/>',
    "camera": '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8 7l1.5-3h5L16 7"/><circle cx="12" cy="13.5" r="3.5"/>',
    "video": '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="m16 10 5-3v10l-5-3"/>',
    "nozzle": '<path d="M8 3h8v6l-2.5 4h-3L8 9z"/><path d="M12 13v4M10 21h4"/>',
    "bed": '<rect x="3" y="14" width="18" height="4" rx="1"/><path d="M7 10c0-1.5 1-1.5 1-3M12 10c0-1.5 1-1.5 1-3'
           'M17 10c0-1.5 1-1.5 1-3"/>',
    "box": '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 15h2M14 15h2"/>',
    "fan": '<circle cx="12" cy="12" r="1.5"/><path d="M12 10.5c0-4 1-6 3-6s2 3-3 6zM13.5 12c4 0 6 1 6 3s-3 2-6-3z'
           'M12 13.5c0 4-1 6-3 6s-2-3 3-6zM10.5 12c-4 0-6-1-6-3s3-2 6 3z"/>',
    "layers": '<path d="m12 4 8 4-8 4-8-4z"/><path d="m4 12 8 4 8-4M4 16l8 4 8-4"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7"/>',
    "x": '<path d="M6 6l12 12M18 6 6 18"/>',
    "play": '<path d="M8 5v14l11-7z"/>',
    "pause": '<path d="M8 5v14M16 5v14"/>',
    "help": '<circle cx="12" cy="12" r="8"/><path d="M9.8 9.5a2.3 2.3 0 1 1 3.3 2.1c-.7.3-1.1.9-1.1 1.6v.3M12 16.5v.5"/>',
    "calendar": '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M8 3v4M16 3v4"/>',
    "speed": '<path d="M4 16a8 8 0 1 1 16 0"/><path d="m12 16 4-5"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
    "trash": '<path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13"/>',
    "repeat": '<path d="M17 3l3 3-3 3M4 11V9a3 3 0 0 1 3-3h13M7 21l-3-3 3-3M20 13v2a3 3 0 0 1-3 3H4"/>',
    "back": '<path d="M19 12H5M11 6l-6 6 6 6"/>',
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "spool": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.5"/><path d="M12 4v5.5M12 14.5V20"/>',
    "drop": '<path d="M12 3.5s6 6.4 6 10.5a6 6 0 0 1-12 0c0-4.1 6-10.5 6-10.5z"/>',
    "thermo": '<path d="M10 14.5V5a2 2 0 1 1 4 0v9.5a4 4 0 1 1-4 0z"/><path d="M12 9v7"/>',
    "external": '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    "upload": '<path d="M12 16V4M7 9l5-5 5 5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
    "download": '<path d="M12 4v12M7 11l5 5 5-5M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
    "file": '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>',
    "star": '<path d="m12 4 2.4 5 5.4.6-4 3.7 1.1 5.3L12 16l-4.9 2.6 1.1-5.3-4-3.7 5.4-.6z"/>',
    "alert": '<path d="M12 4 2.8 19.5h18.4z"/><path d="M12 10v4M12 16.8v.2"/>',
    "info": '<circle cx="12" cy="12" r="8"/><path d="M12 11v5M12 8v.2"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "tag": '<path d="M3.5 12.5V4h8.5l8.5 8.5-8 8z"/><circle cx="8" cy="8.5" r="1.3"/>',
    "image": '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.8"/>'
             '<path d="m20.5 16-5-5L6 19.5"/>',
    "bell": '<path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    "user": '<circle cx="12" cy="8.5" r="3.5"/><path d="M5 20c.8-3.5 3.5-5.5 7-5.5s6.2 2 7 5.5"/>',
    "send": '<path d="m4 12 16-8-6 16-2.5-6.5z"/><path d="M11.5 13.5 20 4"/>',
    "wifi-off": '<path d="M4 4l16 16M8.5 16.5a5 5 0 0 1 7 0M5 13a10 10 0 0 1 4.5-2.6M19 13a10 10 0 0 0-3-2.1'
                'M2 9.5A15 15 0 0 1 7 6.6M22 9.5a15 15 0 0 0-10.5-4.2"/>',
}

# Status → icon. A status is always shown as icon + word, never by colour alone.
STATUS_ICONS = {"printing": "play", "succeeded": "check", "failed": "x", "planned": "calendar",
                "unknown": "help", "reprint": "repeat"}


def sprite():
    # stroke settings on each symbol: a <use> copy inherits from the <use>, not from this sprite
    attrs = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" ' \
            'stroke-linejoin="round"'
    symbols = "".join(f'<symbol id="i-{name}" {attrs}>{body}</symbol>' for name, body in PATHS.items())
    return Markup(f'<svg class="sprite" aria-hidden="true" focusable="false">{symbols}</svg>')


def icon(name, cls="ico"):
    if name not in PATHS:
        raise KeyError(f"unknown icon {name!r}")
    return Markup(f'<svg class="{cls}" aria-hidden="true" focusable="false"><use href="#i-{name}"></use></svg>')
