"""Reading sliced files (Bambu Studio / Orca / PrusaSlicer).

parse(path, filename) returns a dict:
    {
      "name": str | None,
      "plates": [ {"index": 1, "fields": {...}, "filaments": [...], "thumbnail": (bytes, "png") | None}, ... ],
      "settings": {...},          # plate-independent settings
    }
All fields are optional; whatever is not in the file is left out.
"""
import base64
import json
import re
import xml.etree.ElementTree as ET
import zipfile

HEAD_BYTES = 6 * 1024 * 1024
TAIL_BYTES = 1024 * 1024
MAX_THUMB = 8 * 1024 * 1024
MAX_MEMBER = 64 * 1024 * 1024

# Bambu: the bed temperature depends on the plate type
BED_TEMP_KEYS = {
    "cool plate": "cool_plate_temp",
    "supertack plate": "supertack_plate_temp",
    "engineering plate": "eng_plate_temp",
    "high temp plate": "hot_plate_temp",
    "smooth pei plate": "hot_plate_temp",
    "textured pei plate": "textured_plate_temp",
}


class ParseError(Exception):
    pass


def first(value):
    """First value of a list or of a ';'/','-separated string."""
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    value = str(value).strip().strip('"')
    for sep in (";", ","):
        if sep in value:
            value = value.split(sep)[0].strip()
    return value or None


def as_list(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value]
    if value is None:
        return []
    value = str(value).strip().strip('"')
    sep = ";" if ";" in value else ","
    return [v.strip() for v in value.split(sep)] if value else []


def to_float(value):
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def to_int(value):
    f = to_float(value)
    return int(round(f)) if f is not None else None


def parse_duration(text):
    """'1d 2h 3m 4s' -> minutes."""
    if not text:
        return None
    units = {"d": 1440, "h": 60, "m": 1, "s": 1 / 60}
    total, found = 0.0, False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", text.lower()):
        total += float(amount) * units[unit]
        found = True
    return round(total) if found else None


def settings_to_fields(cfg):
    """Common slicer settings -> print fields."""
    out = {}
    lh = to_float(first(cfg.get("layer_height")))
    if lh:
        out["layer_height"] = lh
    infill = first(cfg.get("sparse_infill_density") or cfg.get("fill_density"))
    if infill:
        infill = infill if infill.endswith("%") else (
            f"{round(to_float(infill) * 100)}%" if (to_float(infill) or 0) <= 1 else f"{infill}%")
        out["infill"] = infill
    nozzle = to_float(first(cfg.get("nozzle_diameter")))
    if nozzle:
        out["nozzle"] = nozzle
    temp = to_int(first(cfg.get("nozzle_temperature") or cfg.get("temperature")))
    if temp:
        out["nozzle_temp"] = temp
    bed_type = first(cfg.get("curr_bed_type"))
    if bed_type:
        out["plate_type"] = bed_type
        key = BED_TEMP_KEYS.get(bed_type.lower())
        bed = to_int(first(cfg.get(key))) if key else None
        if bed:
            out["bed_temp"] = bed
    if "bed_temp" not in out:
        bed = to_int(first(cfg.get("bed_temperature") or cfg.get("hot_plate_temp")))
        if bed:
            out["bed_temp"] = bed
    return out


def filaments_from_cfg(cfg):
    types = as_list(cfg.get("filament_type"))
    colors = as_list(cfg.get("filament_colour") or cfg.get("extruder_colour"))
    return [{"type": t, "color": colors[i] if i < len(colors) else ""} for i, t in enumerate(types) if t]


# ---------- 3MF ----------

def _read(zf, name, limit=MAX_MEMBER):
    info = zf.getinfo(name)
    if info.file_size > limit:
        return None
    return zf.read(name)


def parse_3mf(path):
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ParseError("This 3mf file is not a valid zip archive.")
    with zf:
        names = set(zf.namelist())
        result = {"name": None, "plates": [], "settings": {}, "filaments": []}

        cfg = {}
        if "Metadata/project_settings.config" in names:
            try:
                cfg = json.loads(_read(zf, "Metadata/project_settings.config") or b"{}")
            except ValueError:
                cfg = {}
        result["settings"] = settings_to_fields(cfg)
        result["filaments"] = filaments_from_cfg(cfg)

        if "3D/3dmodel.model" in names:
            head = (_read(zf, "3D/3dmodel.model") or b"")[:200000].decode("utf-8", "ignore")
            m = re.search(r'<metadata name="Title">([^<]+)</metadata>', head)
            if m and m.group(1).strip():
                result["name"] = m.group(1).strip()

        if "Metadata/slice_info.config" in names:
            try:
                root = ET.fromstring(_read(zf, "Metadata/slice_info.config") or b"<config/>")
            except ET.ParseError:
                root = ET.Element("config")
            for plate in root.findall("plate"):
                meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
                idx = to_int(meta.get("index")) or len(result["plates"]) + 1
                fields = {}
                pred = to_float(meta.get("prediction"))
                if pred:
                    fields["print_time_min"] = round(pred / 60)
                weight = to_float(meta.get("weight"))
                if weight:
                    fields["filament_g"] = weight
                nozzle = to_float(first(meta.get("nozzle_diameters")))
                if nozzle:
                    fields["nozzle"] = nozzle
                filaments = [{
                    "type": f.get("type", ""), "color": f.get("color", ""),
                    "used_g": to_float(f.get("used_g")), "slot": f.get("id"),
                } for f in plate.findall("filament")]
                result["plates"].append({"index": idx, "fields": fields, "filaments": filaments})

        # unsliced project file: one 'plate' without time/weight
        if not result["plates"]:
            plate_ids = sorted({int(m.group(1)) for n in names
                                if (m := re.match(r"Metadata/plate_(\d+)\.png$", n))}) or [1]
            result["plates"] = [{"index": i, "fields": {}, "filaments": []} for i in plate_ids]

        for p in result["plates"]:
            thumb = None
            for cand in (f"Metadata/plate_{p['index']}.png", "Metadata/thumbnail.png",
                         "Auxiliaries/.thumbnails/thumbnail_middle.png"):
                if cand in names:
                    data = _read(zf, cand, MAX_THUMB)
                    if data:
                        thumb = (data, "png")
                        break
            p["thumbnail"] = thumb
    return result


# ---------- G-code ----------

def parse_gcode(path):
    with open(path, "rb") as fh:
        head = fh.read(HEAD_BYTES)
        fh.seek(0, 2)
        size = fh.tell()
        tail = b""
        if size > HEAD_BYTES:
            fh.seek(max(HEAD_BYTES, size - TAIL_BYTES))
            tail = fh.read()
    text = head.decode("utf-8", "ignore") + "\n" + tail.decode("utf-8", "ignore")

    cfg = {}
    for m in re.finditer(r"^;\s*([a-zA-Z0-9_ \[\]^/()-]+?)\s*[=:]\s*(.*?)\s*$", text, re.M):
        key = m.group(1).strip()
        cfg.setdefault(key, m.group(2))

    fields = {}
    t = (re.search(r";\s*total estimated time:\s*([^;\n]+)", text)
         or re.search(r";\s*estimated printing time \(normal mode\)\s*=\s*([^\n]+)", text))
    if t:
        fields["print_time_min"] = parse_duration(t.group(1))
    w = (re.search(r";\s*total filament weight \[g\]\s*:\s*([\d.]+)", text)
         or re.search(r";\s*(?:total )?filament used \[g\]\s*=\s*([\d.,\s]+)", text))
    if w:
        parts = [to_float(x) for x in re.split(r"[,;]", w.group(1)) if to_float(x)]
        if parts:
            fields["filament_g"] = round(sum(parts), 2)

    thumb = None
    best = 0
    for m in re.finditer(r";\s*thumbnail(?:_PNG)? begin (\d+)x(\d+) \d+\s*\n(.*?);\s*thumbnail(?:_PNG)? end",
                         text, re.S):
        area = int(m.group(1)) * int(m.group(2))
        if area > best:
            b64 = "".join(line.lstrip("; ").strip() for line in m.group(3).splitlines())
            try:
                data = base64.b64decode(b64)
            except ValueError:
                continue
            if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) <= MAX_THUMB:
                thumb, best = (data, "png"), area

    return {
        "name": None,
        "settings": settings_to_fields(cfg),
        "filaments": filaments_from_cfg(cfg),
        "plates": [{"index": 1, "fields": fields, "filaments": [], "thumbnail": thumb}],
    }


def parse(path, filename):
    lower = filename.lower()
    if lower.endswith(".3mf"):
        return parse_3mf(path)
    if lower.endswith(".gcode"):
        return parse_gcode(path)
    raise ParseError("Only .3mf and .gcode files can be read.")


def plate_to_print_fields(parsed, plate_index, materials):
    """Combines the settings + the chosen plate into fields for a print.
    Returns (fields, filaments, thumbnail)."""
    plates = parsed["plates"]
    plate = next((p for p in plates if p["index"] == plate_index), plates[0])
    fields = dict(parsed["settings"])
    fields.update({k: v for k, v in plate["fields"].items() if v is not None})
    # without slice info (project file) we only know the configured slots
    filaments = plate["filaments"] or parsed["filaments"]

    types = {material_of(f["type"], materials) for f in filaments if f.get("type")}
    if len(types) == 1:
        fields["material"] = types.pop()
    elif types:
        fields["material"] = "Other"
    if len(filaments) == 1 and filaments[0].get("color"):
        fields["color"] = filaments[0]["color"][:7]
    elif len(filaments) > 1:
        fields["color"] = ""  # multi-colour: see the filament line in the notes
    if len(plates) > 1:
        fields["plate"] = plate["index"]
    return fields, filaments, plate.get("thumbnail")


def material_of(ftype, materials):
    t = (ftype or "").upper()
    for m in sorted(materials, key=len, reverse=True):
        if m != "Other" and t.startswith(m.upper()):
            return m
    return "Other"


def filament_summary(filaments):
    parts = []
    for f in filaments:
        s = f"{f.get('type') or '?'} {f.get('color') or ''}".strip()
        if f.get("used_g"):
            s += f" ({f['used_g']:g} g)"
        if f.get("slot"):
            s = f"slot {f['slot']}: {s}"
        parts.append(s)
    return ", ".join(parts)
