"""Create an explicitly unrouted preliminary board from KiCad's actual XML netlist.

Inputs: reports/interface_netlist.xml, reports/design_manifest.json, and
reports/interface_geometry.json. Outputs: native project PCB, project-owned
copies of required stock footprints, and reports/placement_manifest.json.

Only run while a placement-only rebuild is intended: this command replaces this
new project's unrouted PCB and does not preserve hand placement. It refuses to
replace tracks, vias, or copper zones unless --replace-routed-board is supplied;
that explicit destructive-regeneration flag first creates a hash-named backup.
It never
modifies the original design, schematic files, or project rules. No autorouting,
copper fill, fabrication export, or production-release claim is performed.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET

import pcbnew as p

from build_interface_footprints import prepare, save, shape, text
from placement_plan import (build_plan, verify_native_transforms, write_preview,
                            BOARD_OUTLINE, ANTENNA_GRAPHIC, ANTENNA_NO_NEW_COMPONENTS)

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "single_detector_usb_master"
BASE = PROJECT / "single_detector_usb_master"
LIB = ROOT / "libraries/master.pretty"
STANDARD = Path(sys.executable).parents[1] / "share/kicad/footprints"
CLI = Path(sys.executable).with_name("kicad-cli.exe")
NAMESPACE = uuid.UUID("55337bdb-9d9d-49a2-ad3c-78805e0be9d0")
TI_DCT_NAME = "TI_DCT0008A_SSOP8"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def identity(key):
    return str(uuid.uuid5(NAMESPACE, key))


def point(x, y):
    return p.VECTOR2I(round(float(x) * 1e6), round(float(y) * 1e6))


def xy(q):
    return [round(q.x / 1e6, 6), round(q.y / 1e6, 6)]


def board_net_name(xml_name):
    # Native XML exposes literal slashes in generated pin-derived names, while
    # KiCad's board/parity reader uses {slash} to distinguish them from hierarchy.
    # Preserve actual sheet-path separators; only normalize generated pin names.
    if xml_name.startswith(("unconnected-(", "Net-(")):
        return xml_name.replace("/", "{slash}")
    return xml_name


def new_ti_dct():
    """Use TI DCT0008A land drawing4220784/D10/2025, not a guessed SM8 alias."""
    fp = prepare(p.FOOTPRINT(None), TI_DCT_NAME,
                 "Texas Instruments DCT0008A SSOP8; land pattern4220784/D10/2025; "
                 "pitch0.65mm, pad1.1x0.4mm, row center span3.8mm; "
                 "https://www.ti.com/lit/ds/symlink/sn74lvc2g125.pdf", p.FP_SMD)
    fp.Reference().SetPosition(point(0, -2.65))
    fp.Reference().SetTextSize(point(0.8, 0.8))
    fp.Reference().SetTextThickness(p.FromMM(0.12))
    fp.Value().SetVisible(False)
    for number in range(1, 9):
        left = number <= 4
        x = -1.9 if left else 1.9
        y = -0.975 + (number - 1) * 0.65 if left else 0.975 - (number - 5) * 0.65
        pad = p.PAD(fp)
        pad.SetNumber(str(number))
        pad.SetPosition(point(x, y))
        pad.SetSize(point(1.1, 0.4))
        pad.SetAttribute(p.PAD_ATTRIB_SMD)
        pad.SetShape(p.PAD_SHAPE_ROUNDRECT)
        pad.SetRoundRectRadiusRatio(0.125)  #0.05mm radius, as drawn by TI.
        pad.SetLayerSet(p.PAD.SMDMask())
        fp.Add(pad)
    # Nominal body midpoint3.0x3.0; latest package drawing limits2.9..3.1mm.
    # Courtyard includes0.25mm allowance beyond pads and max body+flash envelope.
    shape(fp, (-1.5,-1.5), (1.5,1.5), p.F_Fab, 0.1, p.SHAPE_T_RECT)
    shape(fp, (-2.75,-1.95), (2.75,1.95), p.F_CrtYd, 0.05, p.SHAPE_T_RECT)
    shape(fp, (-1.5,-1.65), (1.5,-1.65), p.F_SilkS, 0.12)
    shape(fp, (-1.5,1.65), (1.5,1.65), p.F_SilkS, 0.12)
    shape(fp, (-2.6,-1.2), (-2.6,-0.75), p.F_SilkS, 0.15)
    text(fp, "1", (-1.1,-1.05), p.F_Fab, 0.65)
    text(fp, "${REFERENCE}", (0,0), p.F_Fab, 0.7)
    result = save(fp, TI_DCT_NAME)
    result["manufacturer_drawing"] = {
        "url":"https://www.ti.com/lit/ds/symlink/sn74lvc2g125.pdf",
        "pages":"21 package outline;22 land pattern (one-based)",
        "drawing":"DCT0008A,4220784/D10/2025",
        "visual_review":"2026-09-09; nominal pad dimensions1.1x0.4mm, pitch0.65mm, row span3.8mm, radius0.05mm",
        "package_body_limits_mm":[[2.9,3.1],[2.9,3.1]],"maximum_height_mm":1.3,
        "why_custom":"Requested SM8 name absent; stock SSOP footprint uses a different land pattern. This custom copy follows the current TI drawing."}
    return result


def resolve_footprint(requested, records):
    nickname, name = requested.split(":", 1)
    if name in {"SM8_2.95x2.8mm_P0.65mm", TI_DCT_NAME}:
        if TI_DCT_NAME not in records:
            records[TI_DCT_NAME] = new_ti_dct()
        return "Eltec_Master:" + TI_DCT_NAME
    local = LIB / (name + ".kicad_mod")
    if local.exists():
        records.setdefault(name, {"name":name,"local_path":str(local.relative_to(ROOT)),
                                  "sha256":sha(local),"source":"existing project-owned library; retained"})
        return "Eltec_Master:" + name
    source = STANDARD / (nickname + ".pretty") / (name + ".kicad_mod")
    if not source.exists():
        matches = list(STANDARD.glob("*.pretty/" + name + ".kicad_mod"))
        if len(matches) != 1:
            raise ValueError(f"Cannot uniquely resolve footprint {requested}: {[str(x) for x in matches]}")
        source = matches[0]
    fp = p.FootprintLoad(str(source.parent), name)
    if fp is None:
        raise ValueError(f"KiCad failed to load footprint {source}")
    description = fp.GetLibDescription()
    attributes = fp.GetAttributes()
    prepare(fp, name, description, attributes)
    record = save(fp, name)
    record.update({"source":str(source),"source_sha256":sha(source),
                   "changes":"Native geometry retained; namespace localized; machine-specific3D model references removed; UUIDs made deterministic."})
    records[name] = record
    return "Eltec_Master:" + name


def drawing(board, start, end, layer, width=0.1, rect=False, key=""):
    item = p.PCB_SHAPE()
    item.SetUuid(p.KIID(identity("drawing/" + key)))
    item.SetShape(p.SHAPE_T_RECT if rect else p.SHAPE_T_SEGMENT)
    item.SetStart(point(*start))
    item.SetEnd(point(*end))
    item.SetWidth(p.FromMM(width))
    item.SetLayer(layer)
    board.Add(item)


def board_text(board, value, position, layer=p.Dwgs_User, size=1.2, key=""):
    item = p.PCB_TEXT(board)
    item.SetUuid(p.KIID(identity("text/" + key)))
    item.SetText(value)
    item.SetPosition(point(*position))
    item.SetTextSize(point(size,size))
    item.SetTextThickness(p.FromMM(0.15))
    item.SetLayer(layer)
    board.Add(item)


def rectangular_bounds(fp):
    corners=[]
    for item in fp.GraphicalItems():
        if isinstance(item,p.PCB_SHAPE) and item.GetLayer()==p.F_CrtYd:
            corners += [xy(item.GetStart()),xy(item.GetEnd())]
    if not corners:
        for pad in fp.Pads():
            center,size=xy(pad.GetPosition()),xy(pad.GetSize())
            corners += [[center[0]-size[0]/2,center[1]-size[1]/2],
                        [center[0]+size[0]/2,center[1]+size[1]/2]]
    return [min(a[0] for a in corners),min(a[1] for a in corners),
            max(a[0] for a in corners),max(a[1] for a in corners)]


def overlaps(a,b,clearance=0.0):
    return a[0]<b[2]+clearance and b[0]<a[2]+clearance and a[1]<b[3]+clearance and b[1]<a[3]+clearance


def inspect_existing_routing(path):
    """Treat unfilled copper zones as routing work; rule areas alone are safe."""
    if not path.exists():
        return {"exists":False,"tracks_and_vias":0,"copper_zones":0,"rule_areas":0}
    existing=p.LoadBoard(str(path))
    if existing is None:
        raise ValueError(f"Cannot inspect existing board; refusing to overwrite {path}")
    zones=list(existing.Zones())
    return {"exists":True,"sha256":sha(path),"tracks_and_vias":len(list(existing.GetTracks())),
            "copper_zones":sum(not zone.GetIsRuleArea() for zone in zones),
            "rule_areas":sum(zone.GetIsRuleArea() for zone in zones)}


def guard_routing_overwrite(path, explicit_replace=False):
    existing=inspect_existing_routing(path)
    if existing["tracks_and_vias"] or existing["copper_zones"]:
        if not explicit_replace:
            raise ValueError(
                f"Refusing to discard routing in {path}: {existing['tracks_and_vias']} tracks/vias and "
                f"{existing['copper_zones']} copper zones. Continue from the routed board or explicitly use "
                "--replace-routed-board to regenerate placement; the latter first saves a hash-named backup.")
        backup=ROOT/"reports/board_backups"/(path.stem+"_"+existing["sha256"][:16]+".kicad_pcb")
        backup.parent.mkdir(parents=True,exist_ok=True)
        if backup.exists() and sha(backup)!=existing["sha256"]:
            raise ValueError(f"Backup hash collision; refusing to replace {backup}")
        if not backup.exists():
            shutil.copy2(path,backup)
        assert sha(backup)==existing["sha256"]
        existing["preserved_backup"]=str(backup.relative_to(ROOT))
    return existing


def configure_four_layer_candidate(board):
    """Layer purposes only; dielectric/copper thickness needs fab stack approval."""
    board.SetCopperLayerCount(4)
    board.GetDesignSettings().SetBoardThickness(p.FromMM(1.6))
    for layer in (p.F_Cu,p.B_Cu):
        board.SetLayerType(layer,p.LT_SIGNAL)
    for layer in (p.In1_Cu,p.In2_Cu):
        board.SetLayerType(layer,p.LT_POWER)
    return {"copper_layers":4,"nominal_board_thickness_mm":1.6,
            "layers":[{"name":"F.Cu","purpose":"Signal and power routing"},
                      {"name":"In1.Cu","purpose":"GND reference; separate isolated EM_GND region where required"},
                      {"name":"In2.Cu","purpose":"GND reference/return copper; separate isolated EM_GND region where required"},
                      {"name":"B.Cu","purpose":"Signal and power routing"}],
            "status":"Candidate layer purposes; no copper zones or fabrication dielectric/copper-thickness stack generated."}


def add_antenna_rule_area(board):
    """Actual KiCad exclusion on every enabled copper layer, not just a note."""
    bounds=ANTENNA_NO_NEW_COMPONENTS
    area=p.ZONE(board)
    area.SetUuid(p.KIID(identity("rule-area/esp-antenna")))
    area.SetZoneName("ESP ANTENNA — PROVISIONAL ALL-COPPER KEEPOUT; VERIFY MODULE")
    area.SetIsRuleArea(True)
    area.SetLayerSet(p.LSET.AllCuMask(4))
    area.SetDoNotAllowTracks(True)
    area.SetDoNotAllowVias(True)
    area.SetDoNotAllowPads(True)
    area.SetDoNotAllowZoneFills(True)
    area.SetDoNotAllowFootprints(True)
    polygon=area.Outline()
    polygon.NewOutline()
    for x,y in [(bounds[0],bounds[1]),(bounds[2],bounds[1]),(bounds[2],bounds[3]),(bounds[0],bounds[3])]:
        polygon.Append(round(x*1e6),round(y*1e6))
    board.Add(area)
    board_text(board,"ANTENNA KEEPOUT\nALL COPPER LAYERS\nMODULE FIT/RF UNVERIFIED",(48.75,99.5),p.Dwgs_User,0.8,"antenna-keepout-note")
    return {"source_silk_antenna_graphic_mm":list(ANTENNA_GRAPHIC),"rule_area_mm":list(bounds),
            "layers":["F.Cu","In1.Cu","In2.Cu","B.Cu"],
            "prohibited":["tracks","vias","pads","copper pours","footprints"],
            "basis":"Conservative carrier corridor around source antenna-like graphic; no new components or copper. Exact ESP32 module antenna boundary, vendor clearance and enclosure RF/physical qualification remain open.",
            "limitation":"This preserves the fixed socket/harness geometry and is not a vendor RF-compliance claim."}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--netlist",type=Path,default=ROOT/"reports/interface_netlist.xml")
    parser.add_argument("--manifest",type=Path,default=ROOT/"reports/design_manifest.json")
    parser.add_argument("--no-drc",action="store_true",help="Skip preliminary CLI report only; board remains unrouted")
    parser.add_argument("--replace-routed-board",action="store_true",
                        help="Explicitly discard routing/copper zones after creating a hash-named backup; default refuses")
    args=parser.parse_args()
    output=BASE.with_suffix(".kicad_pcb")
    overwrite_record=guard_routing_overwrite(output,args.replace_routed_board)
    inputs={"netlist":sha(args.netlist),"design_manifest":sha(args.manifest),
            "interface_geometry":sha(ROOT/"reports/interface_geometry.json"),
            "schematic":sha(BASE.with_suffix(".kicad_sch")),
            "project":sha(BASE.with_suffix(".kicad_pro"))}
    schematic_snapshot={str(path.relative_to(PROJECT)):sha(path) for path in PROJECT.glob("*.kicad_sch")}
    original_paths=[ROOT/"source_reference/ESP32_test_board"/("ESP32_test_board"+suffix)
                    for suffix in (".kicad_sch",".kicad_pcb",".kicad_pro")]
    original_hashes={str(path.relative_to(ROOT)):sha(path) for path in original_paths}
    manifest=json.loads(args.manifest.read_text(encoding="utf-8"))
    entries={part["ref"]:part for part in manifest}
    xml=ET.parse(args.netlist).getroot()
    components={c.attrib["ref"]:c for c in xml.find("components")}
    source=Path(xml.findtext("design/source"))
    assert source.resolve()==BASE.with_suffix(".kicad_sch").resolve(), "Netlist belongs to another design"
    root_uuid=re.search(r'\(uuid "([0-9a-f-]+)"\)',source.read_text(encoding="utf-8")).group(1)
    geom=json.loads((ROOT/"reports/interface_geometry.json").read_text(encoding="utf-8"))
    interface={entry["reference"]:entry for entry in geom["placements"]}
    LIB.mkdir(parents=True,exist_ok=True)
    board=p.BOARD()
    board.GetDesignSettings().SetAuxOrigin(point(12,115))
    board.SetFileName(str(BASE.with_suffix(".kicad_pcb")))
    stackup=configure_four_layer_candidate(board)
    title=p.TITLE_BLOCK()
    title.SetTitle("Single detector USB master - PRELIMINARY UNROUTED")
    title.SetCompany("Eltec Instruments")
    title.SetRevision("R1-PLACEMENT-DRAFT")
    title.SetDate("2026-09-09")
    title.SetComment(0,"NO FABRICATION RELEASE - electrical design not frozen")
    title.SetComment(1,"86mm width fixed; trial outline expanded only along Y")
    board.SetTitleBlock(title)
    drawing(board,BOARD_OUTLINE[:2],BOARD_OUTLINE[2:],p.Edge_Cuts,0.05,True,"outline")
    board_text(board,"PRELIMINARY - UNROUTED - DO NOT FABRICATE",(55,-27),p.F_SilkS,1.2,"draft")
    board_text(board,"Candidate power bay; placement/routing and physical fit unqualified",(55,8),p.Dwgs_User,1,"power-bay")
    drawing(board,(14,-25),(96,6),p.Dwgs_User,0.1,True,"power-bay")
    for module in geom["module_envelope_plans"]:
        box=module["source_silk_bbox_global_mm"]
        drawing(board,box[:2],box[2:],p.Dwgs_User,0.15,True,module["module"])
        board_text(board,module["module"]+"\nREFERENCE ENVELOPE - FIT UNVERIFIED",
                   [(box[0]+box[2])/2,(box[1]+box[3])/2],p.Dwgs_User,1,module["module"])
        if "source_usb_graphic_bbox_global_mm" in module:
            box=module["source_usb_graphic_bbox_global_mm"]
            drawing(board,box[:2],box[2:],p.Dwgs_User,0.1,True,"source-usb-graphic")
    antenna_keepout=add_antenna_rule_area(board)
    net_objects={}
    padnets={}
    for index,net_xml in enumerate(sorted(xml.find("nets"),key=lambda x:x.attrib["name"]),1):
        name=net_xml.attrib["name"]
        net=p.NETINFO_ITEM(board,board_net_name(name),index)
        board.Add(net)
        net_objects[name]=net
        for node in net_xml.findall("node"):
            key=(node.attrib["ref"],node.attrib["pin"])
            assert key not in padnets, f"Netlist assigns duplicate net to {key}"
            padnets[key]=name
    placements=[]
    excluded=[]
    library_records={}
    footprint_remaps={}
    occupied=[]
    planned=[]
    for ref,entry in entries.items():
        comp=components.get(ref)
        if comp is None:
            if not entry.get("physical",True):
                excluded.append({"reference":ref,"reason":"nonphysical schematic feature"})
                continue
            raise ValueError(f"Physical manifest component missing from native XML: {ref}")
        properties={x.attrib["name"]:x.attrib.get("value","") for x in comp.findall("property")}
        if "exclude_from_board" in properties:
            excluded.append({"reference":ref,"reason":"explicit native XML exclude_from_board property"})
            continue
        requested=comp.findtext("footprint") or ""
        if not entry.get("physical",True):
            # Copper wire lands are board features, not purchased components.
            # Their native symbols are on-board but explicitly excluded from BOM.
            if not ref.startswith(("JDET", "JEM")) or "exclude_from_bom" not in properties:
                raise ValueError(f"Unexplained non-purchased on-board feature: {ref}")
        if not requested:
            raise ValueError(f"On-board component {ref} has no physical footprint")
        assert comp.findtext("tstamps")==entry["uuid"], f"Stale manifest UUID for {ref}"
        assert comp.findtext("value")==entry["value"], f"Stale manifest value for {ref}"
        for pin,name in entry["nets"].items():
            if name is not None:
                assert padnets.get((ref,str(pin)))==name, f"Manifest/XML net differs for {ref}.{pin}"
        actual=resolve_footprint(requested,library_records)
        if requested!=actual:
            footprint_remaps[requested]=actual
        fp=p.FootprintLoad(str(LIB),actual.split(":",1)[1])
        fp.SetFPID(p.LIB_ID("Eltec_Master",actual.split(":",1)[1]))
        fp.SetReference(ref)
        fp.SetValue(comp.findtext("value") or "")
        fp.SetUuid(p.KIID(identity("footprint/"+ref)))
        sheet=comp.find("sheetpath")
        path="/"+root_uuid+sheet.attrib["tstamps"]+comp.findtext("tstamps")
        fp.SetPath(p.KIID_PATH(path))
        fp.SetSheetname(properties.get("Sheetname",""))
        fp.SetSheetfile(properties.get("Sheetfile",""))
        for field in comp.findall("fields/field"):
            if field.attrib["name"] not in {"Footprint"}:
                fp.SetField(field.attrib["name"],field.text or "")
        fp.SetDNP("dnp" in properties or not entry.get("populated",True))
        if "exclude_from_bom" in properties:
            fp.SetExcludedFromBOM(True)
        if fp.IsDNP():
            fp.SetExcludedFromPosFiles(True)
        if ref.startswith(("JDET","JEM")):
            fp.SetExcludedFromBOM(True)
            fp.SetExcludedFromPosFiles(True)
        board.Add(fp)
        native_pins={x.attrib["num"] for x in comp.findall("units/unit/pins/pin")}
        physical_pins={pad.GetNumber() for pad in fp.Pads() if pad.GetNumber()}
        assert native_pins==physical_pins, f"{ref} symbol/footprint pin set mismatch: {native_pins ^ physical_pins}"
        for pad in fp.Pads():
            pad.SetUuid(p.KIID(identity("pad/"+ref+"/"+pad.GetNumber()+"/"+str(xy(pad.GetPosition())))))
            name=padnets.get((ref,pad.GetNumber()))
            if name:
                pad.SetNet(net_objects[name])
        planned.append((fp,entry,properties,requested,actual))
    # Only actual on-board XML components reach the planner. Resolve portable
    # footprints first, so future new packages use their actual courtyard.
    plan_input=[dict(entry,footprint=actual) for fp,entry,properties,requested,actual in planned]
    positions,placement_plan=build_plan(plan_input,geom)
    placement_plan["independent_native_validation"]=verify_native_transforms(placement_plan)
    assert set(positions)=={entry["ref"] for fp,entry,properties,requested,actual in planned}
    for fp,entry,properties,requested,actual in planned:
        ref=entry["ref"]
        pos=list(positions[ref])
        origin="preserved original interface geometry" if ref in interface else "functional candidate from placement_plan.py; routing and physical qualification pending"
        fp.SetOrientationDegrees(pos[2])
        fp.SetPosition(point(*pos[:2]))
        for field in fp.GetFields():
            field.SetVisible(False)
        fp.Reference().SetVisible(True)
        fp.Reference().SetTextSize(point(0.8,0.8))
        fp.Reference().SetTextThickness(p.FromMM(0.12))
        fp.Value().SetVisible(False)
        if entry["ref"]=="J4":
            # Original socket row lies near the lower edge; its rotated library
            # reference would fall outside the unchanged Y=115 edge.
            fp.Reference().SetPosition(point(68.3,109.65))
        # FootprintLoad deliberately allocates fresh instance UUIDs. Preserve
        # reproducible board identity by naming every field/graphic per ref.
        seen_items=set()
        for index,item in enumerate(list(fp.GetFields())+list(fp.GraphicalItems())):
            old_id=item.m_Uuid.AsString()
            if old_id in seen_items:
                continue
            seen_items.add(old_id)
            item.SetUuid(p.KIID(identity(f"footprint-item/{entry['ref']}/{index}")))
        placements.append({"reference":entry["ref"],"footprint_requested":requested,"footprint_actual":actual,
                           "position_mm":pos[:2],"rotation_deg":pos[2],"placement_basis":origin,
                           "schematic_path":fp.GetPath().AsString(),"footprint_uuid":fp.m_Uuid.AsString(),
                           "populated":not fp.IsDNP(),"exclude_bom":fp.IsExcludedFromBOM(),
                           "exclude_cpl":fp.IsExcludedFromPosFiles(),"courtyard_bbox_mm":rectangular_bounds(fp),
                           "pads":[{"number":q.GetNumber(),"position_mm":xy(q.GetPosition()),"net":q.GetNetname()}
                                   for q in fp.Pads()]})
    board.BuildConnectivity()
    assert len(list(board.GetTracks()))==0
    assert all(zone.GetIsRuleArea() for zone in board.Zones())
    assert sha(args.netlist)==inputs["netlist"] and sha(args.manifest)==inputs["design_manifest"],"Netlist/manifest changed during build; refusing to save against changing inputs"
    assert {str(path.relative_to(PROJECT)):sha(path) for path in PROJECT.glob("*.kicad_sch")}==schematic_snapshot,"Schematic sheets changed during build; refusing to save against changing inputs"
    assert sha(BASE.with_suffix(".kicad_pro"))==inputs["project"],"Project changed during build; refusing to save against changing inputs"
    current_board=inspect_existing_routing(output)
    assert current_board.get("sha256")==overwrite_record.get("sha256"),"Existing board changed during build; refusing to overwrite concurrent edits"
    assert p.SaveBoard(str(output),board,True)
    reloaded=p.LoadBoard(str(output))
    assert len(list(reloaded.GetFootprints()))==len(placements)
    assert reloaded.GetCopperLayerCount()==4
    assert len(list(reloaded.Zones()))==1 and all(zone.GetIsRuleArea() for zone in reloaded.Zones())
    assert sha(BASE.with_suffix(".kicad_pro"))==inputs["project"],"Project settings unexpectedly changed"
    assert sha(BASE.with_suffix(".kicad_sch"))==inputs["schematic"],"Schematic changed during build; regenerate against a stable input snapshot"
    assert all(sha(ROOT/path)==hash_value for path,hash_value in original_hashes.items()), "Preserved original files changed"
    expected_paths={x["reference"]:x["schematic_path"] for x in placements}
    for fp in reloaded.GetFootprints():
        assert fp.GetPath().AsString()==expected_paths[fp.GetReference()]
        for pad in fp.Pads():
            assert pad.GetNetname()==board_net_name(padnets.get((fp.GetReference(),pad.GetNumber()),""))
    reloaded.BuildConnectivity()
    report={"schema":1,"status":"PRELIMINARY UNROUTED PLACEMENT - NOT A FABRICATION RELEASE",
            "inputs":inputs,"output":str(output.relative_to(ROOT)),"output_sha256":sha(output),
            "original_native_file_hashes_before_and_after":original_hashes,
            "existing_board_overwrite_guard":overwrite_record,"candidate_stackup":stackup,
            "antenna_keepout":antenna_keepout,
            "outline_mm":{"min":[12,-30],"max":[98,115],"size":[86,145],"growth_axis":"Y only"},
            "module_envelopes":"Reference drawings on Dwgs.User only; physical heights and fit not approved",
            "placements":placements,"excluded_from_board":excluded,"library_records":library_records,
            "required_schematic_footprint_remaps":footprint_remaps,
            "native_xml_netname_encoding":{name:board_net_name(name) for name in net_objects if name!=board_net_name(name)},
            "checks":{"native_save_and_reopen":True,"all_footprint_paths_preserved":True,
                      "all_pad_net_assignments_match_kicad_xml":True,"original_design_untouched":True,
                      "project_settings_unchanged":True,"new_schematic_unchanged":True,
                      "tracks":len(list(reloaded.GetTracks())),"zones":len(list(reloaded.Zones())),
                      "copper_zones":sum(not zone.GetIsRuleArea() for zone in reloaded.Zones()),
                      "rule_areas":sum(zone.GetIsRuleArea() for zone in reloaded.Zones()),
                      "copper_layers":reloaded.GetCopperLayerCount(),
                      "independent_placement_validation":placement_plan["independent_native_validation"],
                      "unconnected_count":reloaded.GetConnectivity().GetUnconnectedCount(False)},
            "limitations":["The native XML netlist and manifest define this candidate; electrical revision may require regeneration.",
                           "Functional placements remain provisional. Module heights, RF clearance, thermal layout and routing require qualification.",
                           "Four-layer purpose assignment is not a fabrication-approved material/thickness stackup.",
                           "Unrouted connections are expected and are not accepted as a final design."]}
    if not args.no_drc:
        with tempfile.TemporaryDirectory(prefix="eltec_placement_drc_") as tmp:
            path=Path(tmp)/"placement_drc.json"
            command=[str(CLI),"pcb","drc","--format","json","--severity-all","--schematic-parity",
                     "--exit-code-violations","--output",str(path),str(output)]
            result=subprocess.run(command,text=True,capture_output=True,timeout=90)
            data=json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            report["preliminary_drc"]={"command":command,"returncode":result.returncode,
                                       "stdout":result.stdout,"stderr":result.stderr,"report":data}
            if data:
                report["preliminary_drc"]["type_counts"]={name:dict(Counter(v.get("type","unknown") for v in data.get(name,[])))
                                                           for name in ["violations","unconnected_items","schematic_parity"]}
    target=ROOT/"reports/placement_manifest.json"
    target.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    (ROOT/"reports/placement_plan.json").write_text(json.dumps(placement_plan,indent=2)+"\n",encoding="utf-8")
    write_preview(placement_plan,ROOT/"reports/placement_preview.png")
    print(f"Saved and reopened {len(placements)} footprints on4copper layers, no tracks/copper zones,1antenna rule area, {report['checks']['unconnected_count']} unconnected edges")
    print(f"Footprint remaps still needed in schematic: {len(footprint_remaps)}")
    if "preliminary_drc" in report:
        print("Preliminary DRC:",report["preliminary_drc"]["returncode"],report["preliminary_drc"].get("type_counts",{}))
    print(target)


if __name__=="__main__":
    main()
