"""Vertaling van Bambu Lab-foutcodes (print_error en HMS) naar leesbare tekst.

BRON
  De teksten en wiki-links komen uit bambu_errors_data.json, gegenereerd met
  scripts/update_bambu_errors.py uit de foutcodelijsten van ha-bambulab
  (https://github.com/greghesp/ha-bambulab, scripts/hms_error_text/, MIT-licentie).
  ha-bambulab haalt die op bij Bambu Lab zelf (https://e.bambulab.com/query.php, ook gebruikt door
  Bambu Studio) en bij de wiki (https://wiki.bambulab.com/en/hms/home). Commit, datum, model (P2S)
  en taal (nl, terugval en) staan in het veld "_source" van het JSON-bestand.
  Bijwerken:  python3 scripts/update_bambu_errors.py

UITBREIDEN
  - PRINT_ERRORS_EXTRA / HMS_CODES_EXTRA: eigen teksten; die gaan voor op de gegenereerde.
  - FAIL_REASONS_BY_CODE: voorgestelde faalreden per code. Bewust een kleine, handmatige lijst met
    alleen codes waarvan de officiële tekst de oorzaak letterlijk noemt (zie commentaar per regel).
    De reden moet een van app.FAIL_REASONS zijn.

NOTATIE
  - print_error: geheel getal, getoond als 'XXXX-XXXX' (bv. 50348044 → '0300-400C').
  - HMS: {"attr": int, "code": int} in print.hms, getoond als 'XXXX_XXXX_XXXX_XXXX'.
    De bovenste byte van attr is de module, de bovenste 16 bits van code de ernst.

WIKI-LINKS (gecontroleerd op 22-11-2026)
  Alleen HMS-codes hebben een eigen wikipagina, en enkel de links uit de bron werken betrouwbaar
  (een zelf samengestelde /en/x1/…/hmscode/<code> gaf voor 7 van 7 codes zonder bekende link een 404,
  en voor print_error-codes altijd een 404). Zonder bekende link verwijzen we naar de HMS-startpagina.
"""
import json
import logging
import os

log = logging.getLogger("printvault.errors")

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bambu_errors_data.json")
WIKI_HOME = "https://wiki.bambulab.com/en/hms/home"

# Eigen aanvullingen of correcties: code -> tekst. Gaan voor op de gegenereerde tabel.
PRINT_ERRORS_EXTRA = {}
HMS_CODES_EXTRA = {}

# code -> voorgestelde faalreden. Tussen haakjes de officiële Engelse tekst.
FAIL_REASONS_BY_CODE = {
    "0300-8003": "spaghetti",            # Spaghetti defects were detected by the AI Print Monitoring.
    "0C00-8042": "spaghetti",            # The AI print monitor has detected a spaghetti defect.
    "0C00-C004": "spaghetti",            # Possible spaghetti failure was detected.
    "0C00_0300_0003_0008": "spaghetti",  # A possible spaghetti failure has been detected.
    "0C00_0300_0003_001B": "spaghetti",  # Possible spaghetti defects were detected.
    "0300-4006": "verstopping",          # The nozzle is clogged.
    "0300-8016": "verstopping",          # The nozzle is clogged with filament.
    "0300-400D": "stroomuitval",         # Resume failed after power loss.
}

HMS_MODULES = {
    0x03: "bewegingssturing",
    0x05: "moederbord",
    0x07: "AMS",
    0x08: "printkop",
    0x0C: "camera/AI-detectie",
    0x12: "AMS lite",
}

HMS_SEVERITY = {1: "kritiek", 2: "ernstig", 3: "normaal", 4: "info"}

CANCELLED = 0x0300400C


def _load():
    try:
        with open(DATA_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("print_errors", {}), data.get("hms", {}), data.get("wiki", {}), data.get("_source", {})
    except (OSError, ValueError) as e:
        log.warning("foutcodetabel niet geladen (%s); codes worden zonder tekst getoond", e)
        return {}, {}, {}, {}


PRINT_ERRORS, HMS_CODES, WIKI_LINKS, SOURCE = _load()


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def error_code(err):
    """50348044 → '0300-400C'; 0 of ongeldig → None."""
    err = _int(err)
    return f"{err >> 16:04X}-{err & 0xFFFF:04X}" if err else None


def wiki_url(code):
    """Wikipagina van een HMS-code als die bekend is, anders de HMS-startpagina."""
    return WIKI_LINKS.get(code, WIKI_HOME)


def describe_print_error(err):
    """Geeft {code, text, reason, url} terug, of None als er geen fout is. text/reason zijn None
    als de code onbekend is."""
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
    """Eén HMS-melding ({"attr": …, "code": …}) → {code, text, reason, module, severity, url} of None."""
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
    """Korte tekst voor een beschreven fout: 'tekst (code)' of 'code (module)'."""
    if d is None:
        return ""
    if d.get("text"):
        return f"{d['text']} ({d['code']})"
    if d.get("module"):
        return f"{d['code']} ({d['module']})"
    return d["code"]


def suggest_fail_reason(print_error=None, hms=()):
    """Voorgestelde faalreden, of None. print_error gaat voor op HMS."""
    d = describe_print_error(print_error)
    if d and d["reason"]:
        return d["reason"]
    for h in hms:
        if h.get("reason"):
            return h["reason"]
    return None
