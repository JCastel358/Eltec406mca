"""Build project-owned interface footprints and verify original pad coordinates.

Run with KiCad 10.0 bundled Python. Writes only libraries/master.pretty/*.kicad_mod
and reports/interface_geometry.json. Does not create or alter any circuit board.
Manufacturer dimensions are transcribed from visually reviewed drawings cited
below; original module pin centers come from the preserved native source PCB.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import sys
import uuid

import pcbnew as p

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "libraries/master.pretty"
ORIGINAL = ROOT / "source_reference/ESP32_test_board"
STANDARD = Path(sys.executable).parents[1] / "share/kicad/footprints"
NS = uuid.UUID("f4a5bc01-2ee5-4686-9e7c-468da98fd824")
NICKNAME = "Eltec_Master"

SOURCES = {
    "kinghelm_1x15": {
        "manufacturer": "Kinghelm", "mpn": "KH-2.54FH-1X15P-H8.5", "lcsc": "C2932676",
        "url": "https://www.kinghelm.net/upload/file/20221115/KH-2.54FH-1X15P-H8.5.pdf",
        "sha256": "32903659a23f398202008f3866f0412a46830c1f031a41ca1dd7b48c8d04a811",
        "review": "Single-page manufacturer drawing visually inspected 2026-09-09. Table B=38.10; actual body callout is B+0.4 +/-0.3.",
        "nominal_body_mm": [38.50, 2.50, 8.50], "body_tolerance_mm": [0.30, 0.15, 0.15],
        "pitch_mm": 2.54, "recommended_hole_mm": 1.02, "tail_mm": 3.0,
        "assembly_note": "PBT; drawing states 200 C for30-60s or220 C for5s. Require suitable THT assembly process; do not assume reflow compatibility."},
    "kinghelm_1x8": {
        "manufacturer": "Kinghelm", "mpn": "KH-2.54FH-1X8P-H8.5", "lcsc": "C2905417",
        "url": "https://www.kinghelm.net/upload/file/20221115/KH-2.54FH-1X8P-H8.5.pdf",
        "sha256": "f0e377b795a9d1a35efa537285ac4fa2062ed6d091894f33582d023abc3667e7",
        "review": "Single-page manufacturer drawing visually inspected 2026-09-09. Table B=20.32; actual body is B+0.4 +/-0.3.",
        "nominal_body_mm": [20.72, 2.50, 8.50], "body_tolerance_mm": [0.30, 0.15, 0.15],
        "pitch_mm": 2.54, "recommended_hole_mm": 1.02, "tail_mm": 3.0,
        "assembly_note": "PBT; drawing states 200 C for30-60s or220 C for5s. THT process confirmation remains required."},
    "fg_2x8": {
        "manufacturer": "FG / Shenzhen Fugang Technology", "mpn": "FG-PM2.54-2-08P-H8.5", "lcsc": "C25687193",
        "url": "https://datasheet.lcsc.com/datasheet/pdf/b4bb61f34b6ccf0067248c7fcdef65c0.pdf?productCode=C25687193",
        "product_url": "https://www.lcsc.com/product-detail/C25687193.html",
        "sha256": "0747faace15dd0e6f0dad725a2d6fae6981dbc679f89b7ef5c69a425e425c6b6",
        "review": "Manufacturer-authored single-page drawing supplied by LCSC, visually inspected2026-09-09. Title PM2.54-2*nP H=8.5mm, date19.12.31. Table B20.32+0.5 nominal body length.",
        "nominal_body_mm": [20.82, 5.00, 8.50], "body_tolerance_mm": [0.25, 0.15, 0.20],
        "pitch_mm": 2.54, "row_pitch_mm": 2.54, "recommended_hole_mm": 1.02,
        "recommended_hole_tolerance_mm": 0.05, "tail_mm": 3.0,
        "assembly_note": "PBT and gold-plated H62 brass. Actual module mating and assembly-library acceptance remain unconfirmed."},
}


def mm(x):
    return p.FromMM(float(x))


def point(x, y):
    return p.VECTOR2I(mm(x), mm(y))


def xy(pt):
    return [round(p.ToMM(pt.x), 6), round(p.ToMM(pt.y), 6)]


def shape(fp, a, b, layer, width=0.1, kind=None):
    s = p.PCB_SHAPE(fp)
    s.SetShape(p.SHAPE_T_SEGMENT if kind is None else kind)
    s.SetStart(point(*a))
    s.SetEnd(point(*b))
    s.SetLayer(layer)
    s.SetWidth(mm(width))
    fp.Add(s)
    return s


def text(fp, content, pos, layer=p.F_Fab, size=0.8):
    t = p.PCB_TEXT(fp)
    t.SetText(content)
    t.SetPosition(point(*pos))
    t.SetTextSize(point(size, size))
    t.SetTextThickness(mm(0.12))
    t.SetLayer(layer)
    fp.Add(t)


def bounds(fp, layer):
    positions = []
    for obj in fp.GraphicalItems():
        if isinstance(obj, p.PCB_SHAPE) and obj.GetLayer() == layer:
            positions += [xy(obj.GetStart()), xy(obj.GetEnd())]
    return [min(q[0] for q in positions), min(q[1] for q in positions),
            max(q[0] for q in positions), max(q[1] for q in positions)] if positions else None


def prepare(fp, name, desc, attrs):
    fp.SetFPID(p.LIB_ID(NICKNAME, name))
    fp.SetReference("REF**")
    fp.SetValue(name)
    fp.SetAttributes(attrs)
    fp.SetLibDescription(desc)
    fp.Models().clear()  # No dependency on machine-local 3D-model paths.
    fp.SetPath(p.KIID_PATH())
    return fp


def save(fp, name):
    # Native objects acquire random UUIDs. Normalize all UUIDs deterministically
    # after serialization; this affects identity, not native geometry semantics.
    # Empty .pretty directories cannot always be auto-detected by KiCad10's
    # FootprintSave wrapper; select the installed native writer explicitly.
    p.PCB_IO_MGR.FindPlugin(p.PCB_IO_MGR.KICAD_SEXP).FootprintSave(str(LIB), fp)
    filename = LIB / (name + ".kicad_mod")
    data = filename.read_text(encoding="utf-8")
    seen = {}
    def repl(m):
        old = m.group(1)
        if old not in seen:
            seen[old] = str(uuid.uuid5(NS, f"{name}/{len(seen)}"))
        return '(uuid "' + seen[old] + '")'
    data = re.sub(r'\(uuid "([0-9a-f-]+)"\)', repl, data)
    filename.write_text(data, encoding="utf-8", newline="\n")
    loaded = p.FootprintLoad(str(LIB), name)
    assert loaded is not None
    numbers = [pad.GetNumber() for pad in loaded.Pads()]
    assert len(numbers) == len(set(numbers)), f"Duplicate pads in {name}"
    assert all(pad.GetNetCode() == 0 for pad in loaded.Pads()), "Library pads must not carry board nets"
    return {"name": name, "library_id": f"{NICKNAME}:{name}",
            "path": str(filename.relative_to(ROOT)).replace("\\", "/"),
            "sha256": hashlib.sha256(filename.read_bytes()).hexdigest(),
            "pad_count": len(numbers), "unique_pad_numbers": True,
            "attributes": loaded.GetAttributes(),
            "fab_bounds_local_mm": bounds(loaded, p.F_Fab),
            "courtyard_bounds_local_mm": bounds(loaded, p.F_CrtYd),
            "pads_local": [{"number": q.GetNumber(), "xy_mm": xy(q.GetPosition()),
                            "size_mm": xy(q.GetSize()), "drill_mm": xy(q.GetDrillSize())}
                           for q in loaded.Pads()]}


def socket(name, n, rows, source_key):
    source = SOURCES[source_key]
    length, width, height = source["nominal_body_mm"]
    tlength, twidth, _ = source["body_tolerance_mm"]
    endpin = (n - 1) * 2.54
    center_y = (rows - 1) * 1.27
    overhang = (length - endpin) / 2
    nominal = [-overhang, center_y - width / 2, endpin + overhang, center_y + width / 2]
    courtyard = [math.floor((nominal[0] - tlength / 2 - 0.5) / 0.05 + 1e-8) * 0.05,
                 math.floor((nominal[1] - twidth / 2 - 0.5) / 0.05 + 1e-8) * 0.05,
                 math.ceil((nominal[2] + tlength / 2 + 0.5) / 0.05 - 1e-8) * 0.05,
                 math.ceil((nominal[3] + twidth / 2 + 0.5) / 0.05 - 1e-8) * 0.05]
    fp = prepare(p.FOOTPRINT(None), name,
                 f'{source["manufacturer"]} {source["mpn"]}; {height}mm body height; '
                 f'candidate mating not physically verified; {source["url"]}', p.FP_THROUGH_HOLE)
    fp.SetKeywords(f"female socket {rows}x{n} 2.54mm 8.5mm")
    fp.Reference().SetPosition(point(endpin / 2, courtyard[1] - 0.9))
    fp.Reference().SetTextSize(point(0.8, 0.8))
    fp.Reference().SetTextThickness(mm(0.12))
    fp.Value().SetVisible(False)
    for col in range(n):
        for row in range(rows):
            pad = p.PAD(fp)
            number = col + 1 if rows == 1 else col * 2 + row + 1
            pad.SetNumber(str(number))
            pad.SetPosition(point(col * 2.54, row * 2.54))
            pad.SetAttribute(p.PAD_ATTRIB_PTH)
            pad.SetShape(p.PAD_SHAPE_RECT if number == 1 else p.PAD_SHAPE_CIRCLE)
            pad.SetSize(point(2, 2))
            pad.SetDrillSize(point(1.02, 1.02))
            pad.SetLayerSet(p.PAD.PTHMask())
            fp.Add(pad)
    shape(fp, nominal[:2], nominal[2:], p.F_Fab, 0.1, p.SHAPE_T_RECT)
    shape(fp, courtyard[:2], courtyard[2:], p.F_CrtYd, 0.05, p.SHAPE_T_RECT)
    # Silk lies outside pads/mask. Line width0.12 with nominal body boundary
    # expanded0.2 gives adequate clearance to the 2mm pads at the end pins.
    silk = [nominal[0]-0.2, nominal[1]-0.2, nominal[2]+0.2, nominal[3]+0.2]
    shape(fp, silk[:2], silk[2:], p.F_SilkS, 0.12, p.SHAPE_T_RECT)
    text(fp, "1", (-1.85, center_y), p.F_Fab, 0.7)
    text(fp, "${REFERENCE}", (endpin / 2, center_y), p.F_Fab, 0.8)
    # External pin1 mark is a graphical marker, not a fictitious keyed housing.
    shape(fp, (silk[0]-0.3, -0.4), (silk[0]-0.3, 0.4), p.F_SilkS, 0.18)
    result = save(fp, name)
    result.update({"source_key": source_key, "body_height_mm": height,
                   "courtyard_rule": "maximum drawing body envelope plus0.5mm outward-rounded to0.05mm grid",
                   "assembly": "populated", "mating_fit_verified": False})
    return result


def copy_jack():
    name = "BarrelJack_GCT_DCJ200_10_A"
    source_name = "BarrelJack_GCT_DCJ200-10-A_Horizontal"
    fp = p.FootprintLoad(str(STANDARD / "Connector_BarrelJack.pretty"), source_name)
    assert fp is not None
    prepare(fp, name, "GCT DCJ200-10-A assigned original footprint; customer-installed; "
            "actual existing jack part remains unconfirmed; https://gct.co/files/drawings/dcj200-10.pdf",
            p.FP_THROUGH_HOLE | p.FP_DNP | p.FP_EXCLUDE_FROM_POS_FILES)
    result = save(fp, name)
    result.update({"source": str(STANDARD / "Connector_BarrelJack.pretty" / (source_name + ".kicad_mod")),
                   "assembly": "customer-installed DNP", "models": "removed for portability; no unverified3D shape added"})
    return result


def copy_wire_land(original_name, new_name):
    fp = p.FootprintLoad(str(ORIGINAL / "Eltec Footprints.pretty"), original_name)
    assert fp is not None
    prepare(fp, new_name,
            "Original copper wire attachment lands; no purchased or placed component. "
            "Pad1=positive; pad2=" + ("signal; pad3=ground/shield." if original_name == "Sensor" else "emitter return."),
            p.FP_THROUGH_HOLE | p.FP_EXCLUDE_FROM_BOM | p.FP_EXCLUDE_FROM_POS_FILES)
    fp.Value().SetVisible(False)
    # Correct original surface shield land to have no paste opening. These are
    # hand-soldered wires, not SMT parts; preserve every copper/drill coordinate.
    for pad in fp.Pads():
        if pad.GetAttribute() == p.PAD_ATTRIB_SMD:
            pad.SetLayerSet(p.PAD.ConnSMDMask())
    if original_name == "Emitter":
        # Pad2 is the PWM MOSFET drain return (EMITn_LOW), not EM_GND.
        # Correct only this project-owned legend; original source stays intact.
        for item in fp.GraphicalItems():
            if isinstance(item, p.PCB_TEXT) and item.GetText() == "GND":
                item.SetText("RETURN")
    # Retain original wire-area silk, copy its outline as a labeled fabrication
    # aid. The courtyard describes wire soldering clearance, not a sensor body.
    silkbox = bounds(fp, p.F_SilkS)
    if silkbox:
        shape(fp, silkbox[:2], silkbox[2:], p.F_Fab, 0.1, p.SHAPE_T_RECT)
        shape(fp, (silkbox[0]-0.5, silkbox[1]-0.5),
              (silkbox[2]+0.5, silkbox[3]+0.5), p.F_CrtYd, 0.05, p.SHAPE_T_RECT)
    result = save(fp, new_name)
    result.update({"assembly": "nonpurchased copper feature; wire soldering",
                   "source_original_footprint": f"Eltec Footprints:{original_name}",
                   "courtyard_rule": "original wire-area outline plus0.5mm assembly access; not optical component envelope"})
    return result


def main():
    LIB.mkdir(parents=True, exist_ok=True)
    footprints = [socket("Socket_Kinghelm_1x15_P2.54_H8.5", 15, 1, "kinghelm_1x15"),
                  socket("Socket_Kinghelm_1x08_P2.54_H8.5", 8, 1, "kinghelm_1x8"),
                  socket("Socket_FG_2x08_P2.54_H8.5", 8, 2, "fg_2x8"),
                  copy_jack(), copy_wire_land("Sensor", "Detector_Wire_Lands"),
                  copy_wire_land("Emitter", "Emitter_Wire_Lands")]
    original = p.LoadBoard(str(ORIGINAL / "ESP32_test_board.kicad_pcb"))
    old = {f.GetReference(): f for f in original.GetFootprints()}
    placements = []
    def placed(ref, name, position, angle, mapping, oldref):
        fp = p.FootprintLoad(str(LIB), name)
        fp.SetPosition(point(*position))
        fp.SetOrientationDegrees(angle)
        oldpads = {}
        for pad in old[oldref].Pads():
            oldpads.setdefault(pad.GetNumber(), []).append(pad)
        pad_report = []
        for pad in fp.Pads():
            oldnumber = str(mapping[int(pad.GetNumber())])
            errors = [(math.dist(xy(pad.GetPosition()), xy(candidate.GetPosition())), candidate)
                      for candidate in oldpads[oldnumber]]
            distance, nearest = min(errors, key=lambda item: item[0])
            assert distance <= 0.000002, f"{ref}.{pad.GetNumber()} drift {distance}mm"
            pad_report.append({"pad": pad.GetNumber(), "xy_mm": xy(pad.GetPosition()),
                               "original": f"{oldref}.{oldnumber}", "error_mm": round(distance, 9)})
        placements.append({"reference": ref, "library_id": f"{NICKNAME}:{name}",
                           "position_mm": position, "rotation_deg": angle,
                           "coordinates": "Original board global coordinates; subtract(12,12) for original outline-relative coordinates.",
                           "pads": pad_report})
    placed("J3", footprints[0]["name"], [86.08, 87.0], 180, {i:i for i in range(1,16)}, "U1")
    placed("J4", footprints[0]["name"], [86.08, 112.4], 180, {i:i+15 for i in range(1,16)}, "U1")
    placed("J5", footprints[1]["name"], [53.21, 80.975], 0, {i:i for i in range(1,9)}, "U2")
    mapping = {2*i+1:9+i for i in range(8)} | {2*i+2:17+i for i in range(8)}
    placed("J6", footprints[2]["name"], [53.21, 23.825], 0, mapping, "U2")
    for ref in ["J1", "J2"]:
        placed(ref, footprints[3]["name"], xy(old[ref].GetPosition()),
               old[ref].GetOrientationDegrees(), {1:1,2:2,3:3}, ref)
    for i, ref in enumerate(["Test1","Ref1","Test2","Ref2","Test3","Ref3"], 1):
        placed(f"JDET{i}", footprints[4]["name"], xy(old[ref].GetPosition()),
               old[ref].GetOrientationDegrees(), {1:1,2:2,3:3}, ref)
    for i in range(1,4):
        ref = f"E{i}"
        placed(f"JEM{i}", footprints[5]["name"], xy(old[ref].GetPosition()),
               old[ref].GetOrientationDegrees(), {1:1,2:2}, ref)
    report = {
        "schema":1, "status":"PRELIMINARY MECHANICAL FIT - candidate socket drawings checked, no hardware fit approved",
        "native_format":"KiCad10; files reopened through FootprintLoad",
        "library_nickname":NICKNAME, "sources":SOURCES, "footprints":footprints,
        "placements":placements,
        "verified": {"library_reopen_count":len(footprints), "placement_pad_count":sum(len(x["pads"]) for x in placements),
                     "all_pad_centers_match_original_within_mm":0.000002, "all_footprints_have_unique_pad_numbers":True,
                     "old_esp32_duplicate_pad_removed":"Original U1 had duplicate28 coincident with27. J3+J4 implement exactly30 unique contacts."},
        "outline_constraint":{"original_min_mm":[12,12],"original_max_mm":[98,115],"fixed_width_x_mm":86,
                              "growth_axis":"Y only, perpendicular to ESP32 long axisX; replacement height not chosen by this script"},
        "module_envelope_plans":[
            {"module":"U1 ESP32", "render_as":"board User.Drawings/F.Fab outline and text only; not a physical BOM/CPL footprint",
             "source_silk_bbox_global_mm":[44.17,85.73,96.24,113.67],
             "source_usb_graphic_bbox_global_mm":[92.43,94.62,96.24,104.78],
             "confidence":"Original library graphics only, not manufacturer mechanical drawing. Actual USB overhang, antenna clearance, underside parts and connector height unverified.",
             "sockets":["J3","J4"]},
            {"module":"U2 ADS1256", "render_as":"board User.Drawings/F.Fab outline and text only; not a physical BOM/CPL footprint",
             "source_silk_bbox_global_mm":[44.32,21.285,79.88,83.515],
             "confidence":"Original library body graphic only. Electrical pin centers confirmed from original carrier; module underside and mating engagement unmeasured.",
             "sockets":["J5","J6"]}],
        "remaining_gates":["Verify selected socket engagement with actual male pins, nominal8.5mm bodies, underside clearance and USB/antenna clearance.",
                           "Confirm JLC assembly-library match and suitable through-hole soldering for all4socket components.",
                           "Confirm actual customer-installed barrel jacks fit originalGCT-assigned slots; identity remains unverified.",
                           "Module envelopes are documentation only, not confirmed mechanical fit or automatic PCB courtyard exceptions.",
                           "Board placement and DRC still required; this script does not generate a board."]}
    target = ROOT / "reports/interface_geometry.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(f"Created {len(footprints)} portable footprints; verified {report['verified']['placement_pad_count']} pad centers")
    print(target)


if __name__ == "__main__":
    main()
