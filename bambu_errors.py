"""Vertaling van Bambu Lab-foutcodes (print_error en HMS) naar leesbare tekst.

Bewust een kleine tabel: alleen codes waarvan de betekenis vaststaat. Een onbekende code wordt
getoond als code met een link naar de Bambu-wiki. Uitbreiden: voeg een regel toe aan
PRINT_ERRORS of HMS_CODES met (tekst, voorgestelde faalreden of None). De faalreden moet een
van de waarden in app.FAIL_REASONS zijn.

Notatie
- print_error: geheel getal, getoond als 'XXXX-XXXX' (bv. 50348044 → '0300-400C'),
  zoals Bambu Studio en de printer het tonen.
- HMS: lijst van {"attr": int, "code": int} in print.hms, getoond als 'XXXX_XXXX_XXXX_XXXX'.
  De bovenste byte van attr is de module, de bovenste 16 bits van code de ernst.
"""

# URL-sjabloon voor de wiki; {code} is de code met underscores (bv. 0300_400C).
WIKI_URL = "https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/{code}"

# code ('XXXX-XXXX') -> (leesbare tekst, voorgestelde faalreden of None)
PRINT_ERRORS = {
    "0300-400C": ("Print geannuleerd op de printer", None),
}

# code ('XXXX_XXXX_XXXX_XXXX') -> (leesbare tekst, voorgestelde faalreden of None)
HMS_CODES = {
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
    return WIKI_URL.format(code=code.replace("-", "_"))


def describe_print_error(err):
    """Geeft {code, text, reason, url} terug, of None als er geen fout is. text/reason zijn None
    als de code niet in de tabel staat."""
    code = error_code(err)
    if not code:
        return None
    text, reason = PRINT_ERRORS.get(code, (None, None))
    return {"code": code, "text": text, "reason": reason, "url": wiki_url(code)}


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
    text, reason = HMS_CODES.get(code, (None, None))
    return {
        "code": code,
        "text": text,
        "reason": reason,
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
    """Voorgestelde faalreden uit de tabellen, of None. print_error gaat voor op HMS."""
    d = describe_print_error(print_error)
    if d and d["reason"]:
        return d["reason"]
    for h in hms:
        if h.get("reason"):
            return h["reason"]
    return None
