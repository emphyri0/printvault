"""Tests for slicer.py with hand-built files in the Bambu Studio structure.

Run:  python3 -m unittest discover -s tests
"""
import base64
import json
import os
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import slicer  # noqa: E402

MATERIALS = ["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PC", "Other"]


def png(rgb=(255, 0, 0)):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + bytes(rgb))) + chunk(b"IEND", b""))


PROJECT_SETTINGS = {
    "layer_height": "0.16",
    "sparse_infill_density": "15%",
    "nozzle_diameter": ["0.4"],
    "curr_bed_type": "Textured PEI Plate",
    "nozzle_temperature": ["220", "250"],
    "textured_plate_temp": ["65", "70"],
    "hot_plate_temp": ["55", "80"],
    "filament_type": ["PLA", "PETG"],
    "filament_colour": ["#FF6600", "#000000"],
}

SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header><header_item key="X-BBL-Client-Type" value="slicer"/></header>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="nozzle_diameters" value="0.4"/>
    <metadata key="prediction" value="5025"/>
    <metadata key="weight" value="23.45"/>
    <filament id="1" type="PLA" color="#FF6600" used_m="7.5" used_g="23.45"/>
  </plate>
  <plate>
    <metadata key="index" value="2"/>
    <metadata key="prediction" value="12000"/>
    <metadata key="weight" value="80.1"/>
    <filament id="1" type="PLA" color="#FF6600" used_m="10" used_g="30.1"/>
    <filament id="2" type="PETG" color="#000000" used_m="16" used_g="50.0"/>
  </plate>
</config>"""

MODEL = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <metadata name="Application">BambuStudio-02.00.00.00</metadata>
 <metadata name="Title">Kabelhouder bureau</metadata>
 <resources/>
</model>"""


def make_3mf(path, sliced=True):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", MODEL)
        z.writestr("Metadata/project_settings.config", json.dumps(PROJECT_SETTINGS))
        z.writestr("Metadata/plate_1.png", png((255, 0, 0)))
        z.writestr("Metadata/plate_2.png", png((0, 0, 255)))
        if sliced:
            z.writestr("Metadata/slice_info.config", SLICE_INFO)
            z.writestr("Metadata/plate_1.gcode", "G28\n")


BAMBU_GCODE = """; HEADER_BLOCK_START
; BambuStudio 02.00.00.00
; model printing time: 1h 20m 5s; total estimated time: 1h 25m 30s
; total layer number: 120
; total filament length [mm] : 3200.12
; total filament weight [g] : 9.87
; HEADER_BLOCK_END

; THUMBNAIL_BLOCK_START
; thumbnail begin 1x1 {n}
; {b64}
; thumbnail end
; THUMBNAIL_BLOCK_END

; CONFIG_BLOCK_START
; layer_height = 0.2
; sparse_infill_density = 20%
; nozzle_diameter = 0.4
; curr_bed_type = Cool Plate
; cool_plate_temp = 35
; nozzle_temperature = 230
; filament_type = PETG
; filament_colour = #00AE42
; CONFIG_BLOCK_END
G28
"""

PRUSA_GCODE = """G28
G1 X10
; estimated printing time (normal mode) = 2h 3m 4s
; filament used [g] = 15.20, 4.80
; fill_density = 25%
; layer_height = 0.28
; nozzle_diameter = 0.6,0.6
; temperature = 215,240
; bed_temperature = 60,80
; filament_type = PLA;PLA
; filament_colour = #FFFFFF;#FF0000
"""


class SlicerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_sliced_3mf_plate_1(self):
        path = os.path.join(self.dir, "kabelhouder.gcode.3mf")
        make_3mf(path)
        parsed = slicer.parse(path, "kabelhouder.gcode.3mf")
        self.assertEqual(parsed["name"], "Kabelhouder bureau")
        self.assertEqual([p["index"] for p in parsed["plates"]], [1, 2])
        fields, fil, thumb = slicer.plate_to_print_fields(parsed, 1, MATERIALS)
        self.assertEqual(fields["print_time_min"], 84)
        self.assertEqual(fields["filament_g"], 23.45)
        self.assertEqual(fields["layer_height"], 0.16)
        self.assertEqual(fields["infill"], "15%")
        self.assertEqual(fields["nozzle"], 0.4)
        self.assertEqual(fields["nozzle_temp"], 220)
        self.assertEqual(fields["bed_temp"], 65)
        self.assertEqual(fields["plate_type"], "Textured PEI Plate")
        self.assertEqual(fields["material"], "PLA")
        self.assertEqual(fields["color"], "#FF6600")
        self.assertEqual(fields["plate"], 1)
        self.assertEqual(thumb[0], png((255, 0, 0)))

    def test_sliced_3mf_plate_2_multicolor(self):
        path = os.path.join(self.dir, "x.gcode.3mf")
        make_3mf(path)
        fields, fil, thumb = slicer.plate_to_print_fields(slicer.parse(path, "x.gcode.3mf"), 2, MATERIALS)
        self.assertEqual(fields["print_time_min"], 200)
        self.assertEqual(fields["filament_g"], 80.1)
        self.assertEqual(fields["material"], "Other")
        self.assertEqual(fields["color"], "")
        self.assertEqual(len(fil), 2)
        self.assertEqual(thumb[0], png((0, 0, 255)))
        self.assertIn("slot 2: PETG #000000 (50 g)", slicer.filament_summary(fil))

    def test_project_3mf_without_slice_info(self):
        path = os.path.join(self.dir, "project.3mf")
        make_3mf(path, sliced=False)
        parsed = slicer.parse(path, "project.3mf")
        fields, fil, thumb = slicer.plate_to_print_fields(parsed, 1, MATERIALS)
        self.assertNotIn("print_time_min", fields)
        self.assertNotIn("filament_g", fields)
        self.assertEqual(fields["layer_height"], 0.16)
        self.assertIsNotNone(thumb)

    def test_bambu_gcode(self):
        thumb = png((0, 255, 0))
        b64 = base64.b64encode(thumb).decode()
        path = os.path.join(self.dir, "a.gcode")
        with open(path, "w") as fh:
            fh.write(BAMBU_GCODE.format(n=len(b64), b64=b64))
        fields, fil, t = slicer.plate_to_print_fields(slicer.parse(path, "a.gcode"), 1, MATERIALS)
        self.assertEqual(fields["print_time_min"], 86)
        self.assertEqual(fields["filament_g"], 9.87)
        self.assertEqual(fields["layer_height"], 0.2)
        self.assertEqual(fields["infill"], "20%")
        self.assertEqual(fields["plate_type"], "Cool Plate")
        self.assertEqual(fields["bed_temp"], 35)
        self.assertEqual(fields["nozzle_temp"], 230)
        self.assertEqual(fields["material"], "PETG")
        self.assertEqual(fields["color"], "#00AE42")
        self.assertNotIn("plate", fields)
        self.assertEqual(t[0], thumb)

    def test_prusa_style_gcode(self):
        path = os.path.join(self.dir, "b.gcode")
        with open(path, "w") as fh:
            fh.write(PRUSA_GCODE)
        fields, fil, t = slicer.plate_to_print_fields(slicer.parse(path, "b.gcode"), 1, MATERIALS)
        self.assertEqual(fields["print_time_min"], 123)
        self.assertEqual(fields["filament_g"], 20.0)
        self.assertEqual(fields["infill"], "25%")
        self.assertEqual(fields["nozzle"], 0.6)
        self.assertEqual(fields["nozzle_temp"], 215)
        self.assertEqual(fields["bed_temp"], 60)
        self.assertEqual(fields["material"], "PLA")
        self.assertIsNone(t)

    def test_bad_zip(self):
        path = os.path.join(self.dir, "kapot.3mf")
        with open(path, "wb") as fh:
            fh.write(b"not a zip")
        with self.assertRaises(slicer.ParseError):
            slicer.parse(path, "kapot.3mf")

    def test_duration(self):
        self.assertEqual(slicer.parse_duration("1d 2h 3m 30s"), 1564)
        self.assertIsNone(slicer.parse_duration(""))


if __name__ == "__main__":
    unittest.main()
