"""Bambu Lab printer models and which camera route each one uses.

Source for the camera split: ha-bambulab (MIT), pybambu/models.py, Device.supports_feature:
CAMERA_RTSP for the H2, P2, X1/X1E (and X2/H3) series, CAMERA_IMAGE (the JPEG stream on TCP
port 6000) for the A1, A2L and P1 series. All models use the same MQTT protocol (port 8883).

Internal values are lowercase snake_case; LABELS gives the display name.
"""
import re

RTSPS = "rtsps"        # RTSPS on port 322, converted by ffmpeg
TCP6000 = "tcp6000"    # JPEG over TLS on port 6000
NONE = "none"
CAMERA_TYPES = {RTSPS: "RTSPS (port 322)", TCP6000: "JPEG stream (port 6000)", NONE: "No camera"}

# internal value → (label, camera route). Order = order in the select list.
MODELS = {
    "a1_mini": ("A1 mini", TCP6000),
    "a1": ("A1", TCP6000),
    "p1p": ("P1P", TCP6000),
    "p1s": ("P1S", TCP6000),
    "p2s": ("P2S", RTSPS),
    "x1": ("X1", RTSPS),
    "x1c": ("X1C", RTSPS),
    "x1e": ("X1E", RTSPS),
    "h2c": ("H2C", RTSPS),
    "h2d": ("H2D", RTSPS),
    "h2d_pro": ("H2D Pro", RTSPS),
    "h2s": ("H2S", RTSPS),
    "other": ("Other", None),       # camera route chosen by hand (camera_type)
}
LABELS = {k: v[0] for k, v in MODELS.items()}
DUAL_NOZZLE = {"h2c", "h2d", "h2d_pro"}

# Recognising the model in free text (the old "Model" field), most specific first.
_GUESS = [
    ("a1_mini", r"A1\s*MINI"), ("h2d_pro", r"H2D\s*PRO"),
    ("x1c", r"X1\s*C(ARBON)?\b"), ("x1e", r"X1\s*E\b"), ("x1", r"X1\b"),
    ("h2c", r"H2C\b"), ("h2d", r"H2D\b"), ("h2s", r"H2S\b"),
    ("p1p", r"P1P\b"), ("p1s", r"P1S\b"), ("p2s", r"P2S\b"), ("a1", r"A1\b"),
]


def guess_model(text):
    """'Bambu Lab P2S + AMS 2 Pro' → 'p2s'; None when no known model is named."""
    text = (text or "").upper()
    for value, pattern in _GUESS:
        if re.search(r"(?<![A-Z0-9])" + pattern, text):
            return value
    return None


def camera_route(model, camera_type=None):
    """Camera route for a printer: from the model, or from the manual choice for 'other'.
    A printer without a model keeps the old behaviour (RTSPS), so nothing changes for existing setups."""
    if model in MODELS and model != "other":
        return MODELS[model][1]
    if model == "other":
        return camera_type if camera_type in (RTSPS, TCP6000) else None
    return RTSPS


def printer_camera(pr):
    """Camera route of a printer row/dict, or None when it has no camera (or no IP/access code)."""
    get = pr.get if isinstance(pr, dict) else (lambda k: pr[k] if k in pr.keys() else None)
    if not get("ip") or not get("access_code"):
        return None
    return camera_route(get("bambu_model"), get("camera_type"))
