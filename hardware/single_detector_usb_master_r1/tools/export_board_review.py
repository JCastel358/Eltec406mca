"""Native KiCad copper/Fab review PDFs from copies; never a fabrication release.

Run with KiCad Python after the desired routing/plane stage is saved. Generates
four PDFs (seven pages), renders every page, and records exact source/output
hashes. Images still require human/agent visual review before delivery.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import pcbnew as p
from route_board import parse, emit

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "single_detector_usb_master"
BOARD = PROJECT / "single_detector_usb_master.kicad_pcb"
MANIFEST = ROOT / "reports/design_manifest.json"
CLI = Path(sys.executable).with_name("kicad-cli.exe")
RUNTIME = Path(os.environ["USERPROFILE"]) / ".cache/codex-runtimes/codex-primary-runtime/dependencies"
PDF_PYTHON = RUNTIME / "python/python.exe"
NODE = RUNTIME / "node/bin/node.exe"
POPLER = RUNTIME / "native/poppler/Library/bin/pdftoppm.exe"
MARKER = Path(os.environ["USERPROFILE"]) / ".codex/plugins/cache/openai-primary-runtime/pdf/26.904.11930/skills/pdf/container_tools/mark_artifact_operation_started.mjs"
PHASES = {
    "placement_only": "PLACEMENT ONLY - UNROUTED",
    "routed_planes_pending": "TRIAL ROUTE - PLANES PENDING",
    "routed_planes_filled": "ROUTED WITH PLANES - REVIEW PENDING",
    "review_candidate": "ENGINEERING REVIEW CANDIDATE",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inputs():
    paths = [BOARD, MANIFEST, Path(__file__), Path(__file__).with_name("route_board.py"),
             *PROJECT.glob("*.kicad_pro"), *PROJECT.glob("*.kicad_dru"),
             *PROJECT.glob("*.kicad_sch"), *PROJECT.glob("*-lib-table"),
             *ROOT.glob("libraries/master.pretty/*.kicad_mod")]
    return {str(x.relative_to(ROOT)): sha(x) for x in sorted(set(paths))}


def run(command, timeout=60):
    result = subprocess.run([str(x) for x in command], capture_output=True, text=True,
                            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return {"command": [str(x) for x in command], "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip()}


def bbox(item):
    b = item.GetBoundingBox()
    return [b.GetLeft(), b.GetTop(), b.GetRight(), b.GetBottom()]


def fab_center(fp):
    layer = p.F_Fab if fp.GetLayer() == p.F_Cu else p.B_Fab
    boxes = [bbox(g) for g in fp.GraphicalItems()
             if g.GetLayer() == layer and not isinstance(g, p.PCB_TEXT)]
    if not boxes:
        raise RuntimeError(f"No native fabrication body drawing for {fp.GetReference()}")
    bounds = [min(x[0] for x in boxes), min(x[1] for x in boxes),
              max(x[2] for x in boxes), max(x[3] for x in boxes)]
    return p.VECTOR2I(round((bounds[0]+bounds[2])/2), round((bounds[1]+bounds[3])/2)), bounds


def annotate_assembly(board, manifest):
    """Only text visibility/placement changes; pad/polarity drawings untouched."""
    rows = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        if ref not in manifest:
            raise RuntimeError("Footprint missing from the source manifest: " + ref)
        center, bounds = fab_center(fp)
        layer = p.F_Fab if fp.GetLayer() == p.F_Cu else p.B_Fab
        for item in fp.GraphicalItems():
            if isinstance(item, p.PCB_TEXT) and item.GetText() in ("${REFERENCE}", "%R"):
                # Native footprint user-text visibility is not persisted here;
                # clear the duplicate text on this review copy only.
                item.SetText("")
        field = fp.Reference()
        field.SetPosition(center); field.SetLayer(layer); field.SetVisible(True)
        field.SetTextAngleDegrees(0)
        field.SetTextSize(p.VECTOR2I(p.FromMM(.55), p.FromMM(.55)))
        field.SetTextThickness(p.FromMM(.09))
        fp.Value().SetVisible(False)
        customer = not manifest[ref].get("populated", True)
        if customer:
            if not fp.IsDNP():
                raise RuntimeError("Customer-installed physical footprint lacks native DNP flag: " + ref)
            note = p.PCB_TEXT(board)
            note.SetText("CUSTOMER"); note.SetLayer(layer)
            note.SetPosition(p.VECTOR2I(center.x, center.y+p.FromMM(1.2)))
            note.SetTextSize(p.VECTOR2I(p.FromMM(.55),p.FromMM(.55)))
            note.SetTextThickness(p.FromMM(.09)); board.Add(note)
        rows.append({"reference":ref,"body_center_nm":[center.x,center.y],
                     "body_basis":"Bounding center of existing non-text Fab body geometry",
                     "native_fab_bbox_nm":bounds,"visible":bool(field.IsVisible()),
                     "customer_installed_dnp":customer})
    modules=[]
    for g in board.GetDrawings():
        if isinstance(g,p.PCB_TEXT) and g.GetLayer()==p.Dwgs_User:
            for ref,kind in (("U1","ESP32"),("U2","ADS1256")):
                if g.GetText().startswith(ref+" "):
                    g.SetText(f"{ref} {kind}\nCUSTOMER-INSTALLED MODULE\nFIT UNVERIFIED")
                    g.SetPosition(p.VECTOR2I(p.FromMM(101),p.FromMM(96 if ref=='U1' else 37)))
                    g.SetHorizJustify(p.GR_TEXT_H_ALIGN_LEFT)
                    g.SetTextAngleDegrees(0)
                    g.SetTextSize(p.VECTOR2I(p.FromMM(.9),p.FromMM(.9)))
                    modules.append(ref)
            if g.GetText().startswith('ANTENNA KEEPOUT'):
                g.SetText('ANTENNA\nKEEPOUT\nALL COPPER LAYERS')
                g.SetPosition(p.VECTOR2I(p.FromMM(48.75),p.FromMM(99.5)))
                g.SetHorizJustify(p.GR_TEXT_H_ALIGN_CENTER)
                g.SetTextAngleDegrees(0)
                g.SetTextSize(p.VECTOR2I(p.FromMM(.65),p.FromMM(.65)))
            elif g.GetText().startswith('Candidate power bay'):
                g.SetText('')
    if sorted(modules)!=["U1","U2"]:
        raise RuntimeError("Expected both existing customer-module envelope labels")
    return {"footprints":rows,"customer_module_envelopes":modules,
            "pads_and_polarity_graphics_changed":False,
            "text_placement_only_on_copy":True}


def make_copy(path, title, phase, digest, assembly, manifest):
    board=p.LoadBoard(str(BOARD))
    titleblock=board.GetTitleBlock()
    titleblock.SetTitle(title)
    titleblock.SetRevision("REVIEW ONLY")
    titleblock.SetDate(datetime.now(timezone.utc).strftime("%Y-%m-%d UTC"))
    titleblock.SetComment(0,PHASES[phase]+" - NO FABRICATION RELEASE")
    titleblock.SetComment(1,"Source SHA256: "+digest[:32])
    titleblock.SetComment(2,"               "+digest[32:])
    titleblock.SetComment(3,"Source: single_detector_usb_master.kicad_pcb")
    board.SetTitleBlock(titleblock)
    for g in board.GetDrawings():
        if isinstance(g,p.PCB_TEXT) and g.GetText()=="PRELIMINARY - UNROUTED - DO NOT FABRICATE":
            g.SetText(PHASES[phase]+" - DO NOT FABRICATE")
    annotation=annotate_assembly(board,manifest) if assembly else None
    if not p.SaveBoard(str(path),board,True):
        raise RuntimeError("Native review-copy save failed")
    # PAGE_INFO is not exposed by installed SWIG. Change only the copy's native
    # paper expression, then require a complete structural round-trip.
    tree=parse(path.read_text(encoding="utf-8"))
    paper=[x for x in tree if isinstance(x,list) and x and x[0]=="paper"]
    if len(paper)!=1:raise RuntimeError("No unique native page setting")
    paper[0][:]=["paper",'"A3"',"portrait"]
    rendered=emit(tree)+"\n"
    if parse(rendered)!=tree:raise RuntimeError("Review-copy serialization changed structure")
    path.write_text(rendered,encoding="utf-8")
    return annotation


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("name",help="New review bundle name under exports/board_review")
    ap.add_argument("--phase",required=True,choices=PHASES)
    args=ap.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}",args.name):
        ap.error("Use a short plain output identifier")
    output=ROOT/"exports/board_review"/args.name
    output.mkdir(parents=True,exist_ok=False)
    copies=output/"native_copies";copies.mkdir()
    previews=output/"rendered_pages";previews.mkdir()
    source=inputs(); digest=sha(BOARD)
    board=p.LoadBoard(str(BOARD));board.BuildConnectivity()
    tracks=len(list(board.GetTracks()))
    copper_zones=[z for z in board.Zones() if not z.GetIsRuleArea()]
    zones=len(copper_zones)
    filled_zones=sum(bool(z.IsFilled()) for z in copper_zones)
    if args.phase=="placement_only" and tracks:
        raise RuntimeError("Placement-only label contradicts source copper")
    if args.phase!="placement_only" and not tracks:
        raise RuntimeError("Routing-stage label requires a source with routed copper")
    if args.phase=="routed_planes_filled" and (not zones or filled_zones!=zones):
        raise RuntimeError("Plane-stage label requires all actual copper zones to report filled")
    manifest={x["ref"]:x for x in json.loads(MANIFEST.read_text(encoding="utf-8"))}
    jobs=[
        ("copper_top","Top copper - viewed from top","F.Cu,F.Silkscreen,Edge.Cuts",False,False,False),
        ("copper_bottom","Bottom copper - mirrored bottom view","B.Cu,B.Silkscreen,Edge.Cuts",True,False,False),
        ("copper_layers","Individual copper layers - viewed from top","F.Cu,In1.Cu,In2.Cu,B.Cu",False,True,False),
        ("assembly_top","Top assembly - centered references","F.Fab,Edge.Cuts,Dwgs.User",False,False,True),
    ]
    logs=[];outputs=[];annotations=None
    # The calling authoring workflow runs the skill marker exactly once before
    # its first PDF creation. It has already done so for this project; repeated
    # review exports must not duplicate that operation marker.
    for name,title,layers,mirror,multipage,assembly in jobs:
        copy=copies/(name+".kicad_pcb")
        info=make_copy(copy,title,args.phase,digest,assembly,manifest)
        if info is not None:annotations=info
        pdf=output/(name+".pdf")
        command=[CLI,"pcb","export","pdf","--layers",layers,"--exclude-value",
                 "--include-border-title","--black-and-white","--no-property-popups",
                 "--scale","2.0","--drill-shape-opt","2","--output",pdf]
        command += ["--mode-multipage","--common-layers","Edge.Cuts"] if multipage else ["--mode-single"]
        if mirror:command.append("--mirror")
        if assembly:command += ["--sketch-pads-on-fab-layers","--crossout-DNP-footprints-on-fab-layers"]
        command.append(copy);logs.append(run(command))
        if not pdf.exists() or not pdf.stat().st_size:
            raise RuntimeError("Native PDF exporter produced no nonempty file")
        count=run([PDF_PYTHON,"-c","import sys;from pypdf import PdfReader;print(len(PdfReader(sys.argv[1]).pages))",pdf])
        pages=int(count["stdout"])
        if pages!=(4 if multipage else 1):raise RuntimeError("Unexpected PDF page count")
        logs.append(run([POPLER,"-r","140","-png",pdf,previews/name]))
        pngs=sorted(previews.glob(name+"-*.png"))
        if len(pngs)!=pages:raise RuntimeError("Not all PDF pages were rendered")
        outputs.append({"file":str(pdf.relative_to(output)),"sha256":sha(pdf),"pages":pages,
                        "layers":layers.split(","),"mirrored":mirror,
                        "source_copy_sha256":sha(copy),
                        "page_images":[str(x.relative_to(output)) for x in pngs]})
    if inputs()!=source:raise RuntimeError("Source changed during PDF export; bundle is stale")
    record={"status":"EXPORTED_AND_RENDERED_VISUAL_REVIEW_PENDING","phase":args.phase,
            "phase_is_not_a_route_or_drc_pass":True,"source_hashes":source,
            "source_tracks_and_vias":tracks,"source_copper_zones":zones,
            "source_zones_reporting_filled":filled_zones,
            "source_unconnected_count":int(board.GetConnectivity().GetUnconnectedCount(False)),
            "source_unchanged":True,"kicad_version":p.GetBuildVersion(),
            "created_utc":datetime.now(timezone.utc).isoformat(),"outputs":outputs,
            "assembly_annotations":annotations,"commands":logs,
            "limitations":"Review only. No zone refill, DRC execution or main-CAD/library mutation. Rendered images require visual inspection before delivery. DNP modules have no duplicate fabricated footprint; their native envelope drawings are labeled customer-installed."}
    (output/"review_manifest.json").write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"output":str(output),"status":record["status"],"pdfs":outputs},indent=2))


if __name__=="__main__":main()
