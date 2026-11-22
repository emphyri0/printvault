"""Leesbare waarden uit het MQTT-statusrapport van Bambu Lab-printers (print.*).

Getest met een P2S (firmware 2026). Elk veld is optioneel: ontbreekt het (ander model of
oudere firmware), dan geven de functies None en valt dat stuk in de weergave weg.

BRONNEN
  - Fasecodes (stg_cur): CURRENT_STAGE_IDS uit ha-bambulab (pybambu/const.py, MIT), met hun
    vertalingen; gegenereerd in bambu_errors_data.json ("stages") door scripts/update_bambu_errors.py.
    STAGE_TEXT hieronder gaat voor: eigen Nederlandse teksten voor de gangbare fasen.
  - Snelheidsprofielen (spd_lvl), ventilatorschaal 0–15 en nozzletypecodes (bv. HS01): zoals
    ha-bambulab ze leest (const.SPEED_PROFILE, utils.fan_percentage, models.Info._nozzle_type_name).
  - Temperaturen in device.* zijn verpakt als (doel << 16) | huidig: 0xDC00DC = doel 220, huidig 220.
"""
import bambu_errors

# Eigen Nederlandse teksten; gaan voor op de gegenereerde. Rest: bron-nl, dan bron-en, dan "Fase <code>".
STAGE_TEXT = {
    0: "Printen",
    1: "Bed nivelleren",
    2: "Bed opwarmen",
    3: "Trillingscompensatie",
    4: "Filament wisselen",
    5: "Pauze (M400)",
    6: "Gepauzeerd: filament op",
    7: "Nozzle opwarmen",
    8: "Extrusie kalibreren",
    9: "Bedoppervlak scannen",
    10: "Eerste laag inspecteren",
    11: "Plaattype herkennen",
    12: "Micro-lidar kalibreren",
    13: "Printkop naar thuispositie",
    14: "Nozzle reinigen",
    15: "Nozzletemperatuur controleren",
    16: "Gepauzeerd door gebruiker",
    17: "Gepauzeerd: voorklep",
    18: "Micro-lidar kalibreren",
    19: "Flow kalibreren",
    20: "Gepauzeerd: storing nozzletemperatuur",
    21: "Gepauzeerd: storing bedtemperatuur",
    22: "Filament uitladen",
    23: "Gepauzeerd: stappen overgeslagen",
    24: "Filament laden",
    25: "Motorgeluid kalibreren",
    26: "Gepauzeerd: AMS niet bereikbaar",
    27: "Gepauzeerd: heatbreak-ventilator te traag",
    28: "Gepauzeerd: fout kamertemperatuur",
    29: "Kamer koelen",
    30: "Gepauzeerd door G-code",
    32: "Gepauzeerd: filament rond de nozzle",
    33: "Gepauzeerd: fout filamentsnijder",
    34: "Gepauzeerd: fout eerste laag",
    35: "Gepauzeerd: nozzle verstopt",
    39: "Nozzle-offset kalibreren",
    40: "Bed nivelleren op hoge temperatuur",
    47: "Bed nivelleren (fase 1)",
    48: "Bed nivelleren (fase 2)",
    49: "Kamer opwarmen",
    50: "Bed afkoelen",
    51: "Kalibratielijnen printen",
    52: "Materiaal controleren",
    53: "Camera kalibreren",
    54: "Wachten op bedtemperatuur",
    58: "Thermisch voorbereiden",
    63: "Wachten tot kamertemperatuur stabiel is",
    69: "Nozzle afkoelen",
    76: "Voorextrusie",
    77: "AMS voorbereiden",
    82: "Bed stabiliseren",
}
IDLE_STAGES = {-1, 255}
PAUSE_STAGES = {5, 6, 16, 17, 20, 21, 23, 26, 27, 28, 30, 32, 33, 34, 35}
HEAT_STAGES = {2, 7, 29, 40, 49, 50, 54, 58, 63, 69, 82}
# Fasegroepen voor de kleuren in de grafiek
STAGE_GROUPS = {"printen": {0}, "opwarmen": HEAT_STAGES, "pauze": PAUSE_STAGES,
                "filament": {4, 22, 24, 77}, "nivelleren": {1, 9, 40, 47, 48}}

SPEED_PROFILES = {1: "Stil", 2: "Standaard", 3: "Sport", 4: "Ludicrous"}

NOZZLE_MATERIALS = {"00": "roestvrij staal", "01": "gehard staal", "05": "wolfraamcarbide"}

GCODE_STATES = {"PREPARE": "Voorbereiden", "RUNNING": "Printen", "PAUSE": "Gepauzeerd", "FINISH": "Klaar",
                "FAILED": "Mislukt", "IDLE": "Inactief", "SLICING": "Slicen", "INIT": "Opstarten"}


def _stage_table():
    try:
        import json
        with open(bambu_errors.DATA_FILE, encoding="utf-8") as fh:
            return {int(k): v for k, v in (json.load(fh).get("stages") or {}).items()}
    except (OSError, ValueError):
        return {}


STAGES = _stage_table()


def as_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def as_float(v):
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _get(state, *path):
    for p in path:
        if isinstance(state, dict):
            state = state.get(p)
        elif isinstance(state, list) and isinstance(p, int) and 0 <= p < len(state):
            state = state[p]
        else:
            return None
    return state


def stage_text(code):
    code = as_int(code)
    if code is None:
        return None
    if code in STAGE_TEXT:
        return STAGE_TEXT[code]
    s = STAGES.get(code) or {}
    return s.get("nl") or s.get("en") or f"Fase {code}"


def stage_group(code):
    code = as_int(code)
    for group, codes in STAGE_GROUPS.items():
        if code in codes:
            return group
    return "voorbereiden"


def phase(state):
    """(code, tekst) van wat de printer nu doet. Tijdens een print uit stg_cur, anders de gcode_state."""
    gs = state.get("gcode_state")
    code = as_int(state.get("stg_cur"))
    if gs in ("PREPARE", "RUNNING", "PAUSE") and code is not None and code not in IDLE_STAGES:
        if gs == "PAUSE" and code == 0:
            return code, "Gepauzeerd"
        return code, stage_text(code)
    return None, GCODE_STATES.get(gs, gs)


def unpack_temp(v):
    """(huidig, doel) uit een verpakte temperatuur (doel << 16 | huidig)."""
    v = as_int(v)
    if v is None or v < 0:
        return None, None
    return v & 0xFFFF, v >> 16


def temperatures(state):
    """{'nozzle': (huidig, doel), 'bed': (…), 'chamber': (huidig, doel of None)}; None waar onbekend."""
    ext = _get(state, "device", "extruder", "info", 0, "temp")
    n_cur, n_tar = as_float(state.get("nozzle_temper")), as_float(state.get("nozzle_target_temper"))
    if n_cur is None:
        n_cur, n_tar = unpack_temp(ext)
    b_cur, b_tar = as_float(state.get("bed_temper")), as_float(state.get("bed_target_temper"))
    if b_cur is None:
        b_cur, b_tar = unpack_temp(_get(state, "device", "bed", "info", "temp"))
    c_cur = as_float(state.get("chamber_temper"))   # oudere modellen (X1)
    c_tar = None
    if c_cur is None:
        packed = _get(state, "device", "ctc", "info", "temp")
        if packed is None:
            packed = _get(state, "info", "temp")
        c_cur, c_tar = unpack_temp(packed)
        c_tar = c_tar or None   # 0 = geen doeltemperatuur (de P2S verwarmt de kamer niet)
    return {"nozzle": (n_cur, n_tar), "bed": (b_cur, b_tar), "chamber": (c_cur, c_tar)}


def fan_percent(v):
    """Ventilatorstand 0–15 → procent in stappen van 10."""
    v = as_int(v)
    if v is None:
        return None
    return max(0, min(100, round(v / 15 * 10) * 10))


def fans(state):
    out = {"onderdeel": fan_percent(state.get("cooling_fan_speed")),
           "aux": fan_percent(state.get("big_fan1_speed")),
           "kamer": fan_percent(state.get("big_fan2_speed")),
           "heatbreak": fan_percent(state.get("heatbreak_fan_speed"))}
    return {k: v for k, v in out.items() if v is not None}


def speed(state):
    lvl = as_int(state.get("spd_lvl"))
    if lvl is None:
        return None
    return {"level": lvl, "name": SPEED_PROFILES.get(lvl, f"Profiel {lvl}"), "percent": as_int(state.get("spd_mag"))}


def nozzle_type_name(code):
    """'HS01' → 'gehard staal'; 'HH01' → 'gehard staal, high-flow'; onbekend → None."""
    code = str(code or "").strip().upper()
    if len(code) < 4:
        return None
    if code[1] == "U":
        return "TPU high-flow"
    material = NOZZLE_MATERIALS.get(code[2:4])
    if material is None:
        return None
    return material + (", high-flow" if code[1] in ("H", "E") else "")


def active_tray(state):
    """Het AMS-slot (of de externe spoel) dat nu filament levert, of None."""
    now = as_int(_get(state, "ams", "tray_now"))
    if now is None or now == 255:
        return None
    if now == 254:
        slot = _get(state, "vir_slot", 0) or {}
        unit, tray, label = None, None, "Externe spoel"
    else:
        unit, tray = (now >> 2, now & 3) if now < 128 else (now, 0)   # AMS HT: eigen id, één slot
        slot = {}
        for u in _get(state, "ams", "ams") or []:
            if as_int(u.get("id")) == unit:
                slot = next((t for t in u.get("tray") or [] if as_int(t.get("id")) == tray), {})
        label = (f"AMS {'ABCD'[unit]}{tray + 1}" if unit < 4 else f"AMS HT {unit - 127}") if unit is not None else ""
    color = str(slot.get("tray_color") or "")[:6]
    return {"label": label, "ams_id": unit, "tray_id": tray,
            "type": slot.get("tray_type") or None, "name": slot.get("tray_sub_brands") or None,
            "color": color.upper() if len(color) == 6 else None, "remain": as_int(slot.get("remain"))}


def print_settings(state):
    """Wat bij de start van een print in het printrecord mag (enkel de velden die bekend zijn)."""
    out = {}
    diameter = as_float(state.get("nozzle_diameter") or _get(state, "device", "nozzle", "info", 0, "diameter"))
    if diameter:
        out["nozzle"] = diameter
    code = state.get("nozzle_type") or _get(state, "device", "nozzle", "info", 0, "type")
    if code:
        name = nozzle_type_name(code)
        out["nozzle_type"] = f"{name[0].upper()}{name[1:]} ({code})" if name else str(code)
    plate = _get(state, "device", "plate", "cur_id")
    if plate:
        out["plate_type"] = str(plate)   # de bron heeft geen leesbare naam voor deze codes
    if as_int(state.get("plate_idx")):
        out["plate"] = as_int(state.get("plate_idx"))
    if as_int(state.get("plate_cnt")):
        out["plate_count"] = as_int(state.get("plate_cnt"))
    sp = speed(state)
    if sp:
        out["speed_profile"] = sp["name"]
    xcam = state.get("xcam")
    if isinstance(xcam, dict):
        if isinstance(xcam.get("spaghetti_detector"), bool):
            out["spaghetti_detection"] = int(xcam["spaghetti_detector"])
        if isinstance(xcam.get("first_layer_inspector"), bool):
            out["first_layer_inspection"] = int(xcam["first_layer_inspector"])
    return out


def sample(state):
    """Eén meetpunt voor de tijdreeks van een print."""
    t = temperatures(state)
    code, _ = phase(state)
    return {"nozzle": t["nozzle"][0], "nozzle_target": t["nozzle"][1], "bed": t["bed"][0],
            "bed_target": t["bed"][1], "chamber": t["chamber"][0], "stage": code,
            "percent": as_int(state.get("mc_percent")), "layer": as_int(state.get("layer_num"))}
