"""Deterministic, unrouted placement seeds using native footprint courtyards.

Run with KiCad's Python. This module never writes a board or library. The public
plan_placements(manifest, interface_geometry=None) returns ref: (x,y,angle).
Only actual interfaces are fixed; other coordinates are functional seeds that
must converge with routing, thermal, isolation and actual module-fit review.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import sys

import pcbnew as pcb

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "libraries/master.pretty"
STOCK = Path(sys.executable).parents[1] / "share/kicad/footprints"
SOURCE = ROOT / "source_reference/ESP32_test_board/ESP32_test_board.kicad_pcb"
BOARD_OUTLINE = (12.0, -30.0, 98.0, 115.0)
GAP = 0.20  # In addition to the manufacturer's/stock courtyard allowance.

# These are source graphic envelopes, not vendor-qualified keepouts.
MODULES = {
    "ESP32": (44.17, 85.73, 96.24, 113.67),
    "ADS1256": (44.32, 21.285, 79.88, 83.515),
}
ANTENNA_GRAPHIC = (45.44, 93.35, 53.06, 106.05)
ANTENNA_NO_NEW_COMPONENTS = (42.0, 89.0, 55.5, 110.0)
USB_GRAPHIC = (92.43, 94.62, 96.24, 104.78)
USB_NO_NEW_COMPONENTS = (91.9, 94.1, 98.0, 105.3)

REGIONS = {
    "analog1": (25.0, 59.0, 48.0, 85.0),
    "analog2": (25.0, 34.5, 48.0, 60.5),
    "analog3": (25.0, 9.5, 49.0, 35.5),
    "spi": (56.0, 89.5, 90.5, 110.0),
    "supervision": (51.0, 31.0, 78.5, 78.0),
    "adc_supply": (15.0, -27.0, 72.0, 8.0),
    "detector_switch": (74.0, -12.0, 96.5, 18.0),
    "emitter_regulator": (80.1, 32.0, 97.0, 67.8),
    "emitter_pwm": (13.0, 85.0, 41.5, 107.5),
    "emitter_switch": (80.0, -27.0, 96.5, -12.5),
    "emitter_control": (65.0, -11.0, 96.5, 15.0),
}


def mm(value):
    return value / 1e6


def overlap(a, b, gap=0.0):
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


def inside(a, b):
    return a[0] >= b[0] and a[1] >= b[1] and a[2] <= b[2] and a[3] <= b[3]


def rotate_box(box, angle):
    # pcbnew positive rotation is counterclockwise in screen coordinates.
    radians = math.radians(-angle)
    c, s = math.cos(radians), math.sin(radians)
    corners = [(x*c-y*s, x*s+y*c) for x in (box[0], box[2]) for y in (box[1], box[3])]
    return (min(x for x,y in corners), min(y for x,y in corners), max(x for x,y in corners), max(y for x,y in corners))


def move_box(box, x, y):
    return (round(box[0]+x,6), round(box[1]+y,6), round(box[2]+x,6), round(box[3]+y,6))


def footprint_info(identifier):
    library, name = identifier.split(":", 1)
    local = LIB / (name + ".kicad_mod")
    if local.exists():
        directory = LIB
    else:
        choices = list(STOCK.glob("*.pretty/" + name + ".kicad_mod"))
        if len(choices) != 1:
            raise ValueError(f"No unique native footprint for {identifier}: {choices}")
        directory, local = choices[0].parent, choices[0]
    fp = pcb.FootprintLoad(str(directory), name)
    if fp is None:
        raise ValueError(f"Cannot load footprint {identifier}")
    corners = []
    for graphic in fp.GraphicalItems():
        if isinstance(graphic, pcb.PCB_SHAPE) and graphic.GetLayer() == pcb.F_CrtYd:
            box = graphic.GetBoundingBox()
            corners.extend([(mm(box.GetLeft()),mm(box.GetTop())),(mm(box.GetRight()),mm(box.GetBottom()))])
    if not corners:
        raise ValueError(f"{identifier} lacks a real courtyard; do not guess its body")
    return {
        "box": (min(x for x,y in corners),min(y for x,y in corners),max(x for x,y in corners),max(y for x,y in corners)),
        "source": str(local.relative_to(ROOT)) if local.is_relative_to(ROOT) else str(local),
        "sha256": hashlib.sha256(local.read_bytes()).hexdigest(),
    }


def source_positions():
    board = pcb.LoadBoard(str(SOURCE))
    return {fp.GetReference(): (mm(fp.GetPosition().x),mm(fp.GetPosition().y),fp.GetOrientationDegrees())
            for fp in board.GetFootprints()}


def group_for(part):
    ref, sheet = part["ref"], part["sheet"]
    if sheet.startswith("analog_pair_"):
        return "analog" + sheet.rsplit("_",1)[-1]
    if sheet.startswith("spi_"):
        return "spi"
    if sheet in {"power_supervision", "power_permission"}:
        return "supervision"
    if sheet == "adc_supply":
        return "adc_supply"
    if sheet == "emitter_regulator":
        return "emitter_regulator"
    if sheet == "emitter_pwm":
        return "emitter_pwm"
    if ref in {"Q19", "Q20", "R23", "R24", "R29", "C27", "F2"}:
        return "emitter_switch"
    if ref in {"U16", "Q21", "R25"}:
        return "emitter_control"
    return "detector_switch"


def preferences(old):
    preferred = {
        "U33": old["O1"], "U34": old["O2"], "U35": old["O3"],
        "C33": old["C2"], "C34": old["C3"], "C35": old["C4"],
        "U30": (41.0,72.734,90), "U31": (41.0,48.0,90), "U32": (41.0,24.184,90),
        "U101": old["U4"], "Q101": old["Q2"], "Q102": old["Q3"], "Q103": old["Q4"],
        "U100": (88.75,45.0,90), "RV100": old["RV1"],
        "C100": (88.75,35.5,0), "C102": (88.75,58.25,0),
        "R100": (81.5,47,90), "R101": (81.5,52,90),
        "C101": (81.5,36,90), "C103": (81.5,58.5,90),
        "C12": (23,-22,0), "C13": (38.5,-13,0),
        "U10": (34,-7,0), "U11": (54,-5,0),
        "D10": (65,-16,0), "D11": (65,-9,0),
        # Local LT3041 capacitor cluster. The router must keep OUTS Kelvin at
        # C11 positive, with SET return at the same local capacitor ground.
        "C10": (22,-7,0), "C36": (29.5,-8,0),
        "C11": (38.5,-9,0), "C39": (43.5,-9,0),
        "C15": (34,-1.25,270), "C37": (37.3,-1.25,270), "C38": (40.6,-1.25,270),
        "R10": (38.3,-4.75,0), "R39": (41.0,-4.75,0), "R11": (29,-3.5,0),
        "C17": (50.5,-5,90), "C18": (57.5,-5,90),
        "U12": (60,66,0), "U13": (69,66,0), "U14": (61,45,0), "U15": (70,45,0),
        "U17": (55,37,0), "C14": (55,33,0),
        "U18": (63,37,0), "C16": (65.5,37,270),
        "R30": (55,41,0), "R31": (61,33,90), "R32": (63.5,33,90),
        "C19": (60,69.5,0), "C21": (69,69.5,0), "C23": (61,41.5,0), "C25": (70,41.5,0),
        "U50": (61,94,0), "U51": (71,94,0), "U52": (82,102,0),
        "C50": (61,90.5,0), "C51": (71,90.5,0), "C52": (82,98.5,0),
        "Q50": (62,105,0), "Q51": (73,105,0),
        "U16": (79,5,0), "Q21": (70,5,0), "R25": (68,10,0),
        "F1": (88,14,0), "Q16": (87,7,0), "Q17": (87,0,0), "Q18": (80,-5,0),
        "F2": (89,-16,0), "Q19": (83,-21,0), "Q20": (90,-22,0),
    }
    # Keep the same local ordering as the original three optical/PWM channels.
    for new, original in {"R110":"R16","R115":"R17","R120":"R18", "R112":"R19","R117":"R21","R122":"R23", "R113":"R20","R118":"R22","R123":"R24"}.items():
        preferred[new] = old[original]
    for pair in range(3):
        cy = [72.734,48.0,24.184][pair]
        for ref, dx, dy in [(200+pair*2,-2,-6),(201+pair*2,-2,6), (210+pair*2,4,-6),(211+pair*2,4,6),
                           (220+pair*2,13,-6),(221+pair*2,13,6),(230+pair*2,14,-3),(231+pair*2,14,3)]:
            preferred[f"R{ref}"] = (29+dx,cy+dy,0)
        preferred[f"C{30+pair}"] = (45.5,cy,90)
    return preferred


def build_plan(manifest, interface_geometry=None):
    if interface_geometry is None:
        interface_geometry = json.loads((ROOT/"reports/interface_geometry.json").read_text())
    entries = {part["ref"]: part for part in manifest if part.get("footprint")}
    info = {ident: footprint_info(ident) for ident in sorted({p["footprint"] for p in entries.values()})}
    fixed = {item["reference"]: (item["position_mm"][0], item["position_mm"][1], item["rotation_deg"])
             for item in interface_geometry["placements"] if item["reference"] in entries}
    old = source_positions()
    preferred = preferences(old)
    placed, records = {}, {}
    occupied = []
    useful_nets = defaultdict(list)
    for ref, part in entries.items():
        for net in set(part.get("nets",{}).values()):
            if net and net not in {"GND","EM_GND","ADC_5V_HELD","ADC_IO_3V3","ESP_3V3","USB_PRESENT_5V","EM_REG"}:
                useful_nets[net].append(ref)
    blockers = [("unverified ESP antenna corridor",ANTENNA_NO_NEW_COMPONENTS),
                ("source USB graphic/access corridor",USB_NO_NEW_COMPONENTS)]

    def install(ref, placement, is_fixed=False):
        part = entries[ref]
        x,y,angle = placement
        box = move_box(rotate_box(info[part["footprint"]]["box"],angle),x,y)
        collisions = [other for other,bounds in occupied if overlap(box,bounds,GAP)]
        if collisions:
            raise ValueError(f"Placement collision: {ref} with {collisions}")
        if not inside(box,BOARD_OUTLINE):
            raise ValueError(f"Placement outside fixed width/outline: {ref}: {box}")
        if not is_fixed and any(overlap(box,b) for _,b in blockers):
            raise ValueError(f"{ref} occupies reserved antenna/USB corridor")
        placed[ref] = (round(x,6),round(y,6),angle)
        occupied.append((ref,box))
        records[ref] = {"xy_angle":list(placed[ref]),"courtyard_bbox_mm":list(box),"group":"fixed_interface" if is_fixed else group_for(part),
                        "fixed":is_fixed,"footprint":part["footprint"],"under_module":[name for name,b in MODULES.items() if overlap(box,b)]}

    for ref in sorted(fixed):
        install(ref,fixed[ref],True)

    # ICs and bulk/mechanical parts receive functional anchors first. The rest
    # fill their own circuit region nearest the anchor and already placed nets.
    priority = {ref:i for i,ref in enumerate(["C12","U10","C11","C13","C39","C36","C10","C15","C37","C38",
        "U100","RV100","C100","C102","U101", "U33","U34","U35","U30","U31","U32","U50","U51","U52",
        "U11","U17","U18","C14","C16","R30","R31","R32","U12","U13","U14","U15","U16"])}
    def order(ref):
        box = info[entries[ref]["footprint"]]["box"]
        area=(box[2]-box[0])*(box[3]-box[1])
        return (0,priority[ref],0,ref) if ref in priority else (1,0,-area,ref)

    for ref in sorted(set(entries)-set(fixed),key=order):
        part=entries[ref]
        group=group_for(part)
        region=REGIONS[group]
        target=preferred.get(ref)
        if target is None:
            anchors=[placed[other] for net in part.get("nets",{}).values() if net for other in useful_nets[net] if other in placed and records[other]["group"]==group]
            target=((sum(q[0] for q in anchors)/len(anchors),sum(q[1] for q in anchors)/len(anchors),0)
                    if anchors else ((region[0]+region[2])/2,(region[1]+region[3])/2,0))
        neighbors=[placed[other] for net in part.get("nets",{}).values() if net for other in useful_nets[net] if other in placed]
        best=None
        angles=list(dict.fromkeys([target[2],0,90,180,270]))
        seen_rotations=set()
        for angle in angles:
            local=rotate_box(info[part["footprint"]]["box"],angle)
            rotation_key=tuple(round(value,6) for value in local)
            if rotation_key in seen_rotations:
                continue
            seen_rotations.add(rotation_key)
            xlo,xhi=region[0]-local[0],region[2]-local[2]
            ylo,yhi=region[1]-local[1],region[3]-local[3]
            if xhi < xlo or yhi < ylo:
                continue
            points=[(target[0],target[1])]
            points.extend((xi/2,yi/2) for xi in range(math.ceil(xlo*2),math.floor(xhi*2)+1)
                          for yi in range(math.ceil(ylo*2),math.floor(yhi*2)+1))
            for x,y in points:
                distance=math.hypot(x-target[0],y-target[1])
                if best is not None and distance > best[0]:
                    continue
                box=move_box(local,x,y)
                if not inside(box,region) or any(overlap(box,b,GAP) for _,b in occupied) or any(overlap(box,b) for _,b in blockers):
                    continue
                # The exact source preference dominates, but this still brings
                # unanchored decouplers and gate components near known nets.
                score=distance + (0 if angle==target[2] else 0.15)
                if ref not in preferred and neighbors:
                    score += 0.15*sum(math.hypot(x-q[0],y-q[1]) for q in neighbors)/len(neighbors)
                option=(round(score,9),x,y,angle)
                if best is None or option<best:
                    best=option
                if best[0] == 0:
                    break
            if best is not None and best[0] == 0:
                break
        if best is None:
            raise ValueError(f"No legal preliminary position for {ref} in {group}; expand/rework that region explicitly")
        install(ref,(best[1],best[2],best[3]))

    # Tall parts are prohibited beneath either module, independent of 2-D fit.
    tall_names={"CP_Elec_10x10.5", "Potentiometer_Bourns_3296W_Vertical"}
    tall_conflicts=[ref for ref,record in records.items() if not record["fixed"] and record["under_module"] and record["footprint"].split(":")[-1] in tall_names]
    assert not tall_conflicts, f"Tall components below module: {tall_conflicts}"
    report={
        "status":"PRELIMINARY PLACEMENT SEEDS ONLY — NO ROUTING OR FABRICATION APPROVAL",
        "source_manifest_sha256":hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest(),
        "physical_footprints":len(placed),"fixed_interfaces":len(fixed),"courtyard_overlap_count":0,
        "minimum_additional_courtyard_gap_mm":GAP,"outline_mm":list(BOARD_OUTLINE),
        "copper_layer_plan":"Four layers possible: F/B signal; inner continuous GND in detector/USB domain plus strictly separate EM_GND islands. No planes generated by this task.",
        "regions_mm":{k:list(v) for k,v in REGIONS.items()},
        "placement":records,"footprint_evidence":info,
        "provisional_keepouts":{
            "source_antenna_like_silk_mm":list(ANTENNA_GRAPHIC),"no_new_component_corridor_mm":list(ANTENNA_NO_NEW_COMPONENTS),
            "source_usb_graphic_mm":list(USB_GRAPHIC),"usb_no_new_component_corridor_mm":list(USB_NO_NEW_COMPONENTS),
            "qualification":"Source silk supports approximate antenna/USB location only. Exact module revision and vendor RF copper keepout must be verified. Existing fixed interfaces are not moved; no RF-compliance claim is made."},
        "mechanical_gates":[
            "Socket nominal height8.5mm is not a guarantee of8.5mm available clearance; verify actual module underside components and header seating.",
            "Every noninterface part listed under_module is provisional until its maximum package height and actual module clearance are checked.",
            "C12–C14 current10.5mm-high cans remain in the new power bay; trimmer remains outside module envelopes.",
            "Future LT3042/300uF revision may reuse the ADC supply bay but requires new footprint/courtyard and height checks.",
            "No enclosure fit, thermal qualification, antenna performance, creepage, routing congestion, or PCB DRC result follows from this placement check.",
        ],
        "routing_gates":[
            "Emitter switches are provisionally in the upper-right bay. J2-to-switch and switch-to-regulator current paths need deliberate wide routing along a separate EM domain; placement may need convergence.",
            "U101 retains the original optical barrier orientation and output FET ordering. Do not join EM_GND to GND through any plane or via.",
            "ADG selectors retain per-pair regions. If revised to follow buffers, optimize pad orientation and ADC header routing after the new netlist is frozen.",
            "Reserve low-noise ADC return paths; no global ground-plane split may cut the analog signal return accidentally.",
        ],
    }
    return placed,report


def plan_placements(manifest, interface_geometry=None):
    """Return every on-board manifest reference as (x_mm,y_mm,angle_degrees)."""
    return build_plan(manifest,interface_geometry)[0]


def verify_native_transforms(report):
    """Independent native placement of unsaved footprints; this is not DRC."""
    footprints=[]
    transform_differences=[]
    for ref,record in report["placement"].items():
        identifier=record["footprint"]
        evidence=report["footprint_evidence"][identifier]
        source=Path(evidence["source"])
        if not source.is_absolute():
            source=ROOT/source
        fp=pcb.FootprintLoad(str(source.parent),source.stem)
        x,y,angle=record["xy_angle"]
        fp.SetOrientationDegrees(angle)
        fp.SetPosition(pcb.VECTOR2I(round(x*1e6),round(y*1e6)))
        corners=[]
        for graphic in fp.GraphicalItems():
            if isinstance(graphic,pcb.PCB_SHAPE) and graphic.GetLayer()==pcb.F_CrtYd:
                box=graphic.GetBoundingBox()
                corners.extend([(mm(box.GetLeft()),mm(box.GetTop())),(mm(box.GetRight()),mm(box.GetBottom()))])
        bounds=(min(x for x,y in corners),min(y for x,y in corners),max(x for x,y in corners),max(y for x,y in corners))
        delta=max(abs(a-b) for a,b in zip(bounds,record["courtyard_bbox_mm"]))
        if delta>0.000002:
            transform_differences.append({"ref":ref,"difference_mm":delta})
        footprints.append((ref,bounds))
    intersections=[(ref,other) for i,(ref,a) in enumerate(footprints) for other,b in footprints[i+1:] if overlap(a,b)]
    result={"native_footprints_checked":len(footprints),"transform_differences":transform_differences,
            "native_courtyard_bbox_intersections":intersections,"scope":"Unsaved native footprints; no electrical/routing DRC performed."}
    if transform_differences or intersections:
        raise ValueError(f"Independent native placement check failed: {result}")
    return result


def write_preview(report, path):
    """Annotated plan of courtyards, not rendered component bodies or copper."""
    from PIL import Image, ImageDraw, ImageFont
    scale,offset_x,offset_y=10,15,40
    canvas=Image.new("RGB",(940,1550),"white")
    draw=ImageDraw.Draw(canvas)
    font=ImageFont.truetype("C:/Windows/Fonts/arial.ttf",11)
    point=lambda x,y:(round((x-12)*scale+offset_x),round((y+30)*scale+offset_y))
    rectangle=lambda b:[point(b[0],b[1]),point(b[2],b[3])]
    colors={"fixed_interface":"#ccd0d5","analog1":"#a5dab4","analog2":"#a5dab4","analog3":"#a5dab4",
            "spi":"#b2d9ef","supervision":"#c5bbed","adc_supply":"#eddca5","detector_switch":"#ddd38a",
            "emitter_regulator":"#eeac9d","emitter_pwm":"#eeac9d","emitter_switch":"#eeac9d","emitter_control":"#ead0a5"}
    draw.text((15,10),f'PRELIMINARY COURTYARD PLACEMENT - {report["physical_footprints"]} footprints - UNROUTED',fill="black",font=font)
    draw.rectangle(rectangle(report["outline_mm"]),outline="black",width=2)
    for bounds in MODULES.values():
        draw.rectangle(rectangle(bounds),outline="#555555",width=2)
    for key in ["no_new_component_corridor_mm","usb_no_new_component_corridor_mm"]:
        draw.rectangle(rectangle(report["provisional_keepouts"][key]),fill="#eeeeee",outline="#999999")
    for ref,record in report["placement"].items():
        bounds=record["courtyard_bbox_mm"]
        draw.rectangle(rectangle(bounds),fill=colors[record["group"]],outline="#333333")
        x,y=point((bounds[0]+bounds[2])/2,(bounds[1]+bounds[3])/2)
        draw.text((x,y),ref,fill="#111111",font=font,anchor="mm")
    canvas.save(path)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest",type=Path,default=ROOT/"reports/design_manifest.json")
    parser.add_argument("--report",type=Path,default=ROOT/"reports/placement_plan.json")
    parser.add_argument("--preview",type=Path,default=ROOT/"reports/placement_preview.png")
    args=parser.parse_args()
    manifest=json.loads(args.manifest.read_text(encoding="utf-8"))
    placements,report=build_plan(manifest)
    report["independent_native_validation"]=verify_native_transforms(report)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    args.preview.parent.mkdir(parents=True,exist_ok=True)
    write_preview(report,args.preview)
    print(json.dumps({"report":str(args.report),"placements":len(placements),"fixed_interfaces":report["fixed_interfaces"],"courtyard_overlap_count":report["courtyard_overlap_count"],"outline_mm":report["outline_mm"]}))


if __name__ == "__main__":
    main()
