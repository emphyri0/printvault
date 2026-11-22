"""Translation of Bambu Lab error codes (print_error and HMS) into readable text.

SOURCE
  The texts and wiki links come from bambu_errors_data.json, generated with
  scripts/update_bambu_errors.py from the error code lists of ha-bambulab
  (https://github.com/greghesp/ha-bambulab, scripts/hms_error_text/, MIT-licentie).
  ha-bambulab gets them from Bambu Lab itself (https://e.bambulab.com/query.php, also used by
  Bambu Studio) and from the wiki (https://wiki.bambulab.com/en/hms/home). Commit, date, model (P2S)
  and language (en) are in the "_source" field of the JSON file.
  Update:  python3 scripts/update_bambu_errors.py

EXTENDING
  - PRINT_ERRORS_EXTRA / HMS_CODES_EXTRA: own texts; they take precedence over the generated ones.
  - FAIL_REASONS_BY_CODE: suggested fail reason per code. Deliberately a small, hand-made list with
    only codes whose official text literally names the cause (see the comment on each line).
    The reason must be one of app.FAIL_REASONS.

NOTATION
  - print_error: an integer, shown as 'XXXX-XXXX' (e.g. 50348044 → '0300-400C').
  - HMS: {"attr": int, "code": int} in print.hms, shown as 'XXXX_XXXX_XXXX_XXXX'.
    The top byte of attr is the module, the top 16 bits of code the severity.

WIKI LINKS (checked on 22 Nov 2026)
  Only HMS codes have their own wiki page, and only the links from the source work reliably
  (a self-built /en/x1/…/hmscode/<code> gave a 404 for 7 of 7 codes without a known link,
  and always a 404 for print_error codes). Without a known link we point to the HMS home page.
"""
import json
import logging
import os

log = logging.getLogger("printvault.errors")

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bambu_errors_data.json")
WIKI_HOME = "https://wiki.bambulab.com/en/hms/home"

# Own additions or corrections: code -> text. Take precedence over the generated table.
PRINT_ERRORS_EXTRA = {}
HMS_CODES_EXTRA = {}

# code -> suggested fail reason, with the official text as a comment.
FAIL_REASONS_BY_CODE = {
    "0300-8003": "spaghetti",            # Spaghetti defects were detected by the AI Print Monitoring.
    "0C00-8042": "spaghetti",            # The AI print monitor has detected a spaghetti defect.
    "0C00-C004": "spaghetti",            # Possible spaghetti failure was detected.
    "0C00_0300_0003_0008": "spaghetti",  # A possible spaghetti failure has been detected.
    "0C00_0300_0003_001B": "spaghetti",  # Possible spaghetti defects were detected.
    "0300-4006": "clog",                 # The nozzle is clogged.
    "0300-8016": "clog",                 # The nozzle is clogged with filament.
    "0300-400D": "power_loss",           # Resume failed after power loss.
}

HMS_MODULES = {
    0x03: "motion control",
    0x05: "mainboard",
    0x07: "AMS",
    0x08: "toolhead",
    0x0C: "camera/AI detection",
    0x12: "AMS lite",
}

HMS_SEVERITY = {1: "fatal", 2: "serious", 3: "common", 4: "info"}

CANCELLED = 0x0300400C


def _load():
    try:
        with open(DATA_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("print_errors", {}), data.get("hms", {}), data.get("wiki", {}), data.get("_source", {})
    except (OSError, ValueError) as e:
        log.warning("error code table not loaded (%s); codes are shown without text", e)
        return {}, {}, {}, {}


PRINT_ERRORS, HMS_CODES, WIKI_LINKS, SOURCE = _load()


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def error_code(err):
    """50348044 → '0300-400C'; 0 or invalid → None."""
    err = _int(err)
    return f"{err >> 16:04X}-{err & 0xFFFF:04X}" if err else None


def wiki_url(code):
    """Wiki page of an HMS code if it is known, else the HMS home page."""
    return WIKI_LINKS.get(code, WIKI_HOME)


def describe_print_error(err):
    """Returns {code, text, reason, url}, or None when there is no error. text/reason are None
    when the code is unknown."""
    code = error_code(err)
    if not code:
        return None
    text = PRINT_ERRORS_EXTRA.get(code) or PRINT_ERRORS.get(code)
    return {"code": code, "text": text, "reason": FAIL_REASONS_BY_CODE.get(code), "url": wiki_url(code)}


def hms_code(attr, code):
    attr, code = _int(attr), _int(code)
    if attr is None or code is None:
        return None
    return f"{attr >> 16:04X}_{attr & 0xFFFF:04X}_{code >> 16:04X}_{code & 0xFFFF:04X}"


def describe_hms(entry):
    """One HMS message ({"attr": …, "code": …}) → {code, text, reason, module, severity, url} or None."""
    if not isinstance(entry, dict):
        return None
    code = hms_code(entry.get("attr"), entry.get("code"))
    if not code:
        return None
    return {
        "code": code,
        "text": HMS_CODES_EXTRA.get(code) or HMS_CODES.get(code),
        "reason": FAIL_REASONS_BY_CODE.get(code),
        "module": HMS_MODULES.get(_int(entry["attr"]) >> 24),
        "severity": HMS_SEVERITY.get(_int(entry["code"]) >> 16),
        "url": wiki_url(code),
    }


def describe_hms_list(entries):
    out = []
    for e in entries if isinstance(entries, list) else []:
        d = describe_hms(e)
        if d and d["code"] not in {x["code"] for x in out}:
            out.append(d)
    return out


def label(d):
    """Short text for a described error: 'text (code)' or 'code (module)'."""
    if d is None:
        return ""
    if d.get("text"):
        return f"{d['text']} ({d['code']})"
    if d.get("module"):
        return f"{d['code']} ({d['module']})"
    return d["code"]


def suggest_fail_reason(print_error=None, hms=()):
    """Suggested fail reason, or None. print_error takes precedence over HMS."""
    d = describe_print_error(print_error)
    if d and d["reason"]:
        return d["reason"]
    for h in hms:
        if h.get("reason"):
            return h["reason"]
    return None
