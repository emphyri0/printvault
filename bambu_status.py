"""Readable values from the MQTT status report of Bambu Lab printers (print.*).

Tested with a P2S (2026 firmware). Every field is optional: when it is missing (other model or
older firmware) the functions return None and that part simply drops out of the display.

SOURCES
  - Stage codes (stg_cur): CURRENT_STAGE_IDS from ha-bambulab (pybambu/const.py, MIT) with their
    translations; generated into bambu_errors_data.json ("stages") by scripts/update_bambu_errors.py.
    STAGE_TEXT below takes precedence: own wording where the source text is awkward.
  - Speed profiles (spd_lvl), the 0–15 fan scale and nozzle type codes (e.g. HS01): read the way
    ha-bambulab reads them (const.SPEED_PROFILE, utils.fan_percentage, models.Info._nozzle_type_name).
  - Temperatures under device.* are packed as (target << 16) | current: 0xDC00DC = target 220, current 220.
    Older firmware (X1, P1, A1) sends plain numbers instead (nozzle_temper, bed_temper, chamber_temper);
    both are accepted everywhere. Dual-nozzle printers (H2D, H2C) list both nozzles under
    device.extruder.info; bits 4–7 of device.extruder.state give the active one (id 0 = right, 1 = left).
    Field formats per model as documented in ha-bambulab (MIT), pybambu/models.py.
"""
import bambu_errors

# Own texts; take precedence over the generated ones. Otherwise: source (en), then "Stage <code>".
STAGE_TEXT = {
    0: "Printing",
    1: "Bed leveling",
    2: "Heating bed",
    3: "Vibration compensation",
    5: "Paused (M400)",
    7: "Heating nozzle",
    11: "Identifying plate type",
    13: "Homing toolhead",
    14: "Cleaning nozzle",
    20: "Paused: nozzle temperature error",
    21: "Paused: bed temperature error",
    27: "Paused: heatbreak fan too slow",
    28: "Paused: chamber temperature error",
    31: "Motor noise demo",
    40: "Bed leveling at high temperature",
    47: "Bed leveling (phase 1)",
    48: "Bed leveling (phase 2)",
    50: "Cooling bed",
    63: "Waiting for chamber temperature",
    65: "Calibrating nozzle clump detection",
    82: "Stabilizing bed",
}
IDLE_STAGES = {-1, 255}
PAUSE_STAGES = {5, 6, 16, 17, 20, 21, 23, 26, 27, 28, 30, 32, 33, 34, 35}
HEAT_STAGES = {2, 7, 29, 40, 49, 50, 54, 58, 63, 69, 82}
# Stage groups (internal value → stages)
STAGE_GROUPS = {"printing": {0}, "heating": HEAT_STAGES, "paused": PAUSE_STAGES,
                "filament": {4, 22, 24, 77}, "leveling": {1, 9, 40, 47, 48}}

# Internal value → label; SPEED_LEVELS maps the printer's spd_lvl to the internal value.
SPEED_PROFILES = {"silent": "Silent", "standard": "Standard", "sport": "Sport", "ludicrous": "Ludicrous"}
SPEED_LEVELS = {1: "silent", 2: "standard", 3: "sport", 4: "ludicrous"}

NOZZLE_MATERIALS = {"00": "stainless steel", "01": "hardened steel", "05": "tungsten carbide"}
# Older firmware sends the nozzle type as a word instead of a code like HS01
NOZZLE_WORDS = {"stainless_steel": "stainless steel", "hardened_steel": "hardened steel",
                "tungsten_carbide": "tungsten carbide", "brass": "brass"}
NOZZLE_SIDES = {0: "Right", 1: "Left"}

GCODE_STATES = {"PREPARE": "Preparing", "RUNNING": "Printing", "PAUSE": "Paused", "FINISH": "Finished",
                "FAILED": "Failed", "IDLE": "Idle", "SLICING": "Slicing", "INIT": "Starting up"}


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
    return s.get("text") or s.get("en") or f"Stage {code}"


def stage_group(code):
    code = as_int(code)
    for group, codes in STAGE_GROUPS.items():
        if code in codes:
            return group
    return "preparing"


def phase(state):
    """(code, text) of what the printer is doing now. During a print from stg_cur, else from gcode_state."""
    gs = state.get("gcode_state")
    code = as_int(state.get("stg_cur"))
    if gs in ("PREPARE", "RUNNING", "PAUSE") and code is not None and code not in IDLE_STAGES:
        if gs == "PAUSE" and code == 0:
            return code, "Paused"
        return code, stage_text(code)
    return None, GCODE_STATES.get(gs, gs)


def unpack_temp(v):
    """(current, target) from a packed temperature (target << 16 | current). A plain number (older
    firmware) gives (value, 0); a fractional one keeps its decimal and has no target."""
    f = as_float(v)
    if f is None or f < 0:
        return None, None
    if f != int(f):
        return f, None
    v = int(f)
    return v & 0xFFFF, v >> 16


def _extruders(state):
    info = _get(state, "device", "extruder", "info")
    return [e for e in info if isinstance(e, dict)] if isinstance(info, list) else []


def active_nozzle(state):
    """Id of the active nozzle on a dual-nozzle printer (device.extruder.state bits 4–7), else None."""
    st = as_int(_get(state, "device", "extruder", "state"))
    return None if st is None else (st >> 4) & 0xF


def nozzles(state):
    """Both nozzles of a dual-nozzle printer: [{id, name, cur, target, active}], else None."""
    ext = _extruders(state)
    if len(ext) < 2:
        return None
    active = active_nozzle(state)
    out = []
    for e in sorted(ext, key=lambda e: as_int(e.get("id")) or 0):
        i = as_int(e.get("id"))
        cur, tar = unpack_temp(e.get("temp"))
        out.append({"id": i, "name": NOZZLE_SIDES.get(i, f"Nozzle {(i or 0) + 1}"), "cur": cur,
                    "target": tar or None, "active": i == active})
    return out


def temperatures(state):
    """{'nozzle': (current, target), 'bed': (…), 'chamber': (current, target or None)}; None where unknown."""
    ext = _extruders(state)
    if len(ext) >= 2:   # dual nozzle: the active one (the top-level fields follow it too, but not always)
        active = active_nozzle(state)
        entry = next((e for e in ext if as_int(e.get("id")) == active), ext[0])
        n_cur, n_tar = unpack_temp(entry.get("temp"))
    else:
        n_cur, n_tar = as_float(state.get("nozzle_temper")), as_float(state.get("nozzle_target_temper"))
        if n_cur is None and ext:
            n_cur, n_tar = unpack_temp(ext[0].get("temp"))
    b_cur, b_tar = as_float(state.get("bed_temper")), as_float(state.get("bed_target_temper"))
    if b_cur is None:
        b_cur, b_tar = unpack_temp(_get(state, "device", "bed", "info", "temp"))
    c_cur = as_float(state.get("chamber_temper"))   # older models (X1)
    c_tar = None
    if c_cur is None:
        packed = _get(state, "device", "ctc", "info", "temp")
        if packed is None:
            packed = _get(state, "info", "temp")
        c_cur, c_tar = unpack_temp(packed)
        c_tar = c_tar or None   # 0 = no target temperature (the P2S does not heat its chamber)
    return {"nozzle": (n_cur, n_tar), "bed": (b_cur, b_tar), "chamber": (c_cur, c_tar)}


def fan_percent(v):
    """Fan level 0–15 → percent in steps of 10."""
    v = as_int(v)
    if v is None:
        return None
    return max(0, min(100, round(v / 15 * 10) * 10))


def fans(state):
    out = {"part": fan_percent(state.get("cooling_fan_speed")),
           "aux": fan_percent(state.get("big_fan1_speed")),
           "chamber": fan_percent(state.get("big_fan2_speed")),
           "heatbreak": fan_percent(state.get("heatbreak_fan_speed"))}
    return {k: v for k, v in out.items() if v is not None}


def speed(state):
    lvl = as_int(state.get("spd_lvl"))
    if lvl is None:
        return None
    value = SPEED_LEVELS.get(lvl)
    return {"level": lvl, "value": value, "name": SPEED_PROFILES.get(value, f"Profile {lvl}"),
            "percent": as_int(state.get("spd_mag"))}


def nozzle_type_name(code):
    """'HS01' → 'hardened steel'; 'HH01' → 'hardened steel, high-flow'; 'hardened_steel' (older
    firmware) → 'hardened steel'; unknown → None."""
    word = str(code or "").strip().lower()
    if word in NOZZLE_WORDS:
        return NOZZLE_WORDS[word]
    code = word.upper()
    if len(code) < 4:
        return None
    if code[1] == "U":
        return "TPU high-flow"
    material = NOZZLE_MATERIALS.get(code[2:4])
    if material is None:
        return None
    return material + (", high-flow" if code[1] in ("H", "E") else "")


def active_tray(state):
    """The AMS slot (or the external spool) that is feeding filament now, or None."""
    now = as_int(_get(state, "ams", "tray_now"))
    if now is None or now == 255:
        return None
    if now == 254:
        slot = _get(state, "vir_slot", 0) or state.get("vt_tray") or {}   # vt_tray: older firmware
        if not isinstance(slot, dict):
            slot = {}
        unit, tray, label = None, None, "External spool"
    else:
        unit, tray = (now >> 2, now & 3) if now < 128 else (now, 0)   # AMS HT: own id, one slot
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
    """What may go into the print record at the start of a print (only the fields that are known)."""
    out = {}
    info = _get(state, "device", "nozzle", "info")
    info = [n for n in info if isinstance(n, dict)] if isinstance(info, list) else []
    active = active_nozzle(state)
    nozzle = next((n for n in info if active is not None and as_int(n.get("id")) == active), info[0] if info else {})
    diameter = as_float(state.get("nozzle_diameter") or nozzle.get("diameter"))
    if diameter:
        out["nozzle"] = diameter
    code = state.get("nozzle_type") or nozzle.get("type")
    if code:
        name = nozzle_type_name(code)
        if name and str(code).strip().lower() in NOZZLE_WORDS:
            out["nozzle_type"] = name[0].upper() + name[1:]          # older firmware: a word, no code
        else:
            out["nozzle_type"] = f"{name[0].upper()}{name[1:]} ({code})" if name else str(code)
    plate = _get(state, "device", "plate", "cur_id")
    if plate:
        out["plate_type"] = str(plate)   # the source has no readable name for these codes
    if as_int(state.get("plate_idx")):
        out["plate"] = as_int(state.get("plate_idx"))
    if as_int(state.get("plate_cnt")):
        out["plate_count"] = as_int(state.get("plate_cnt"))
    sp = speed(state)
    if sp and sp["value"]:
        out["speed_profile"] = sp["value"]
    xcam = state.get("xcam")
    if isinstance(xcam, dict):
        if isinstance(xcam.get("spaghetti_detector"), bool):
            out["spaghetti_detection"] = int(xcam["spaghetti_detector"])
        if isinstance(xcam.get("first_layer_inspector"), bool):
            out["first_layer_inspection"] = int(xcam["first_layer_inspector"])
    return out


def sample(state):
    """One data point for a print's time series."""
    t = temperatures(state)
    code, _ = phase(state)
    return {"nozzle": t["nozzle"][0], "nozzle_target": t["nozzle"][1], "bed": t["bed"][0],
            "bed_target": t["bed"][1], "chamber": t["chamber"][0], "stage": code,
            "percent": as_int(state.get("mc_percent")), "layer": as_int(state.get("layer_num"))}
