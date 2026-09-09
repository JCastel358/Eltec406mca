"""Clean preliminary board silk on a copy; never change electrical geometry.

Default output is reports/silkscreen_cleanup/candidate/. Rerun after placement.
All references remain visible, with >=1.0 mm text and >=0.15 mm strokes. Native
before/after DRC and a non-silk structural identity comparison are recorded.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import pcbnew as p
from route_board import parse

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "single_detector_usb_master"
SOURCE = PROJECT / "single_detector_usb_master.kicad_pcb"
CLI = Path(sys.executable).with_name("kicad-cli.exe")
SILK = {p.F_SilkS, p.B_SilkS}
TEXT_HEIGHT, STROKE, GAP = 1.0, .15, .19


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def mm(value):
    return p.ToMM(value)


def xy(point):
    return (mm(point.x), mm(point.y))


def point(x, y):
    return p.VECTOR2I(p.FromMM(x), p.FromMM(y))


def box(item, extra=0):
    b = item.GetBoundingBox()
    return (mm(b.GetLeft())-extra, mm(b.GetTop())-extra,
            mm(b.GetRight())+extra, mm(b.GetBottom())+extra)


def intersects(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


class Index:
    def __init__(self):
        self.cells = defaultdict(list)
    def keys(self, b):
        for x in range(math.floor(b[0]/5), math.floor(b[2]/5)+1):
            for y in range(math.floor(b[1]/5), math.floor(b[3]/5)+1):
                yield x, y
    def add(self, b, owner=None):
        for key in self.keys(b):
            self.cells[key].append((b, owner))
    def hit(self, b, ignore_owner=None):
        return any(owner != ignore_owner and intersects(b, other)
                   for key in self.keys(b) for other, owner in self.cells.get(key, []))
    def query(self,b):
        seen=set()
        for key in self.keys(b):
            for other,owner in self.cells.get(key,[]):
                if id(owner) not in seen and intersects(b,other):
                    seen.add(id(owner))
                    yield owner


def shape_boxes(shape, extra=0):
    width = mm(shape.GetWidth()) / 2 + extra
    kind = shape.GetShape()
    a, z = xy(shape.GetStart()), xy(shape.GetEnd())
    if kind == p.SHAPE_T_SEGMENT:
        points = [a, z]
    elif kind == p.SHAPE_T_RECT:
        points = [a, (z[0], a[1]), z, (a[0], z[1]), a]
    elif kind == p.SHAPE_T_CIRCLE:
        radius = math.dist(a, z)
        points = [(a[0]+radius*math.cos(i*math.pi/24), a[1]+radius*math.sin(i*math.pi/24)) for i in range(49)]
    else:
        # Conservative for uncommon curved graphics; native DRC checks final.
        return [box(shape, extra)]
    return [(min(a[0],z[0])-width,min(a[1],z[1])-width,
             max(a[0],z[0])+width,max(a[1],z[1])+width) for a,z in zip(points,points[1:])]


def all_items(board):
    for fp in board.GetFootprints():
        yield from fp.GetFields()
        yield from fp.GraphicalItems()
    yield from board.GetDrawings()


def native_identity(board, editable_ids):
    with tempfile.TemporaryDirectory(prefix="eltec_silk_identity_") as tmp:
        path = Path(tmp) / "snapshot.kicad_pcb"
        assert p.SaveBoard(str(path), board, True)
        tree = parse(path.read_text(encoding="utf-8"))
    def filtered(node):
        if not isinstance(node, list):
            return node
        own = [x[1].strip('"') for x in node if isinstance(x,list) and len(x)==2 and x[0]=="uuid"]
        if any(uid in editable_ids for uid in own):
            return None
        return [v for child in node if (v := filtered(child)) is not None]
    return filtered(tree)


def normalize_text(text):
    size = text.GetTextSize()
    text.SetTextSize(point(max(mm(size.x),TEXT_HEIGHT),max(mm(size.y),TEXT_HEIGHT)))
    text.SetTextThickness(p.FromMM(max(mm(text.GetTextThickness()),STROKE)))


def local_copy(fp):
    clone=p.FOOTPRINT(fp)
    clone.SetPosition(point(0,0))
    clone.SetOrientationDegrees(0)
    return clone


def shape_key(item):
    return (int(item.GetShape()),tuple(round(v,5) for v in xy(item.GetStart())),
            tuple(round(v,5) for v in xy(item.GetEnd())),
            tuple(round(v,5) for v in xy(item.GetArcMid())) if item.GetShape()==p.SHAPE_T_ARC else None)


def cleanup(board):
    before = {item.m_Uuid.AsString(): {"layer": board.GetLayerName(item.GetLayer()),
              "position": xy(item.GetPosition()), "text": item.GetText() if isinstance(item,p.PCB_TEXT) else None}
              for item in all_items(board) if item.GetLayer() in SILK}
    editable = set(before)
    identity = native_identity(board, editable)
    footprints = sorted(board.GetFootprints(), key=lambda fp: fp.GetReference())
    refs_before = {fp.GetReference(): (fp.m_Uuid.AsString(),fp.GetPath().AsString()) for fp in footprints}
    pad_index, pad_polygons, body_index = Index(), Index(), Index()
    originals={}
    for fp in footprints:
        ref = fp.GetReference()
        clone=local_copy(fp)
        originals[ref]={item.m_Uuid.AsString():shape_key(item) for item in clone.GraphicalItems() if isinstance(item,p.PCB_SHAPE) and item.GetLayer()==p.F_SilkS}
        for pad in fp.Pads():
            if pad.IsOnLayer(p.F_Mask):
                # The explicit project mask expansion is 0.09 mm. This also
                # bounds standalone pcbnew when it has no loaded project.
                expansion = max(.09, mm(pad.GetSolderMaskExpansion(p.F_Cu)))
                clearance=max(expansion+.03,GAP)
                pad_index.add(box(pad, clearance), ref)
                polygon=p.SHAPE_POLY_SET()
                pad.TransformShapeToPolygon(polygon,p.F_Cu,p.FromMM(clearance),p.FromMM(.005),p.ERROR_OUTSIDE)
                pad_polygons.add(box(pad,clearance),polygon)
        # References may occupy soldering courtyard margin, provided mask
        # clearance is met. Reserve the actual Fab component body instead.
        # JDET/JEM are bare copper wire lands, with no purchased body to cover
        # text; treating their access courtyard as plastic wastes useful space.
        if not ref.startswith(("JDET","JEM")):
            bodies=[box(g,.05) for g in fp.GraphicalItems() if isinstance(g,p.PCB_SHAPE) and g.GetLayer()==p.F_Fab]
            if not bodies:
                bodies=[box(g) for g in fp.GraphicalItems() if g.GetLayer()==p.F_CrtYd]
            if bodies:
                body_index.add((min(b[0] for b in bodies),min(b[1] for b in bodies),
                                max(b[2] for b in bodies),max(b[3] for b in bodies)),ref)
    moves, shapes_to_fab, semantic = [], [], []
    for fp in footprints:
        for item in fp.GraphicalItems():
            if fp.GetReference().startswith("JEM") and isinstance(item,p.PCB_TEXT) and item.GetLayer()==p.F_SilkS and item.GetText()=="GND":
                item.SetText("RETURN")
                semantic.append({"reference":fp.GetReference(),"before":"GND","after":"RETURN","reason":"EMITn_LOW PWM drain return is not ground"})
    # Compare real pad polygons, including rounded corners. Bounding rectangles
    # alone unnecessarily discard tiny standard 0805 identification strokes.
    to_fab=defaultdict(set)
    for fp in footprints:
        for item in fp.GraphicalItems():
            if item.GetLayer()==p.F_SilkS and isinstance(item,p.PCB_SHAPE):
                item.SetWidth(p.FromMM(max(STROKE,mm(item.GetWidth()))))
                polygon=p.SHAPE_POLY_SET()
                item.TransformShapeToPolygon(polygon,p.F_SilkS,0,p.FromMM(.005),p.ERROR_OUTSIDE)
                if any(polygon.Collide(pad) for pad in pad_polygons.query(box(item))):
                    to_fab[str(fp.GetFPID().GetLibItemName())].add(originals[fp.GetReference()][item.m_Uuid.AsString()])
    # Share the same silk geometry by library footprint type; this avoids
    # instance/library mismatches instead of disabling their native DRC check.
    for fp in footprints:
        for item in fp.GraphicalItems():
            if isinstance(item,p.PCB_SHAPE) and item.GetLayer()==p.F_SilkS:
                if originals[fp.GetReference()][item.m_Uuid.AsString()] in to_fab[str(fp.GetFPID().GetLibItemName())]:
                    item.SetLayer(p.F_Fab)
                    shapes_to_fab.append({"reference":fp.GetReference(),"uuid":item.m_Uuid.AsString(),"reason":"shared footprint silk graphic conflicts with pad clearance in at least one placed instance"})
    obstacle = Index()
    for item in all_items(board):
        if item.GetLayer()==p.F_SilkS and isinstance(item,p.PCB_SHAPE):
            for b in shape_boxes(item,GAP):
                obstacle.add(b,"graphic")
    edge = board.GetBoardEdgesBoundingBox()
    edge_bounds = (mm(edge.GetLeft())+.5,mm(edge.GetTop())+.5,mm(edge.GetRight())-.5,mm(edge.GetBottom())-.5)
    tasks = []
    for item in board.GetDrawings():
        if item.GetLayer()==p.F_SilkS and isinstance(item,p.PCB_TEXT) and item.IsVisible():
            tasks.append((0,"BOARD",[(None,item)],False))
    pin_groups=defaultdict(list)
    for fp in footprints:
        for item in fp.GraphicalItems():
            if item.GetLayer()==p.F_SilkS and isinstance(item,p.PCB_TEXT) and item.IsVisible():
                pin_groups[(str(fp.GetFPID().GetLibItemName()),item.GetText())].append((fp,item))
    for key,members in pin_groups.items():
        tasks.append((1,key[0],members,True))
    for fp in footprints:
        text = fp.Reference()
        if text.GetLayer()!=p.F_SilkS or not text.IsVisible():
            raise RuntimeError(f"Expected visible front reference: {fp.GetReference()}")
        tasks.append((1.5 if fp.GetReference().startswith("J") else 2,fp.GetReference(),[(fp,text)],False))
    def freedom(task):
        if task[0]!=2:
            return -len(task[2][0][1].GetText())
        fp,text=task[2][0]
        normalize_text(text)
        original=xy(text.GetPosition())
        old=text.GetTextAngle().AsDegrees()
        score=0
        for angle in (0.,90.):
            text.SetTextAngle(p.EDA_ANGLE(angle,p.DEGREES_T))
            original_box=box(text)
            for dx in range(-4,5):
                for dy in range(-4,5):
                    b=tuple(value+(dx if i%2==0 else dy) for i,value in enumerate(original_box))
                    if edge_bounds[0]<=b[0] and edge_bounds[1]<=b[1] and b[2]<=edge_bounds[2] and b[3]<=edge_bounds[3] and not(pad_index.hit(b) or obstacle.hit(b) or body_index.hit(b)):
                        score+=1
        text.SetTextAngle(p.EDA_ANGLE(old,p.DEGREES_T))
        return score
    # Reserve scarce nearby labeling space first; connectors/pin legends remain
    # higher priority. This uses geometry rather than hard-coded reference IDs.
    tasks.sort(key=lambda t:(t[0],freedom(t),-xy(t[2][0][1].GetPosition())[1],t[1]))
    unresolved=[]
    offsets=[(dx/2,dy/2) for dx in range(-24,25) for dy in range(-24,25)]
    offsets.sort(key=lambda v:(v[0]*v[0]+v[1]*v[1],abs(v[0]),v[1],v[0]))
    for priority,group_name,members,pinlabel in tasks:
        first_fp,first_text=members[0]
        first_rotation=first_fp.GetOrientationDegrees() if first_fp else 0
        first_angle=first_text.GetTextAngle().AsDegrees()
        templates=[]
        originals_text=[]
        for fp,text in members:
            original=xy(text.GetPosition())
            old_angle=text.GetTextAngle().AsDegrees()
            rotation=fp.GetOrientationDegrees() if fp else 0
            normalize_text(text)
            angles=[]
            for angle in dict.fromkeys([first_angle % 180,0.,90.]):
                actual_angle=angle+rotation-first_rotation
                text.SetTextAngle(p.EDA_ANGLE(actual_angle,p.DEGREES_T))
                b=box(text)
                angles.append((actual_angle,tuple(v-original[i%2] for i,v in enumerate(b))))
            templates.append((fp,text,original,old_angle,rotation-first_rotation,angles))
            originals_text.append((original,old_angle))
        found=None
        for dx,dy in offsets:
            # Keep pin legends particularly close to their original pin row.
            if pinlabel and dx*dx+dy*dy>36:
                continue
            for angle_index in range(len(templates[0][-1])):
                group=[]
                for fp,text,original,old_angle,delta,angles in templates:
                    radians=math.radians(-delta)
                    pos=(original[0]+dx*math.cos(radians)-dy*math.sin(radians),original[1]+dx*math.sin(radians)+dy*math.cos(radians))
                    angle,relative=angles[angle_index]
                    candidate=tuple(v+pos[i%2] for i,v in enumerate(relative))
                    if not(edge_bounds[0]<=candidate[0] and edge_bounds[1]<=candidate[1] and candidate[2]<=edge_bounds[2] and candidate[3]<=edge_bounds[3]):
                        break
                    if pad_index.hit(candidate) or obstacle.hit(candidate):
                        break
                    if body_index.hit(candidate,ignore_owner=fp.GetReference() if pinlabel else None):
                        break
                    group.append((pos,angle,candidate))
                if len(group)==len(templates):
                    found=group
                    break
            if found:
                break
        if found:
            for (fp,text,original,old_angle,delta,angles),(pos,angle,candidate) in zip(templates,found):
                text.SetPosition(point(*pos))
                text.SetTextAngle(p.EDA_ANGLE(angle,p.DEGREES_T))
                obstacle.add((candidate[0]-GAP,candidate[1]-GAP,candidate[2]+GAP,candidate[3]+GAP),"placed_text")
                if pos!=original or angle!=old_angle:
                    moves.append({"reference":fp.GetReference() if fp else "BOARD","text":text.GetText(),"from":original,"to":pos,
                                  "angle":angle,"displacement_mm":round(math.dist(original,pos),3),"pin_legend":pinlabel})
        else:
            for fp,text,original,old_angle,delta,angles in templates:
                text.SetPosition(point(*original))
                text.SetTextAngle(p.EDA_ANGLE(old_angle,p.DEGREES_T))
                obstacle.add(box(text,GAP),"unresolved_text")
                unresolved.append({"reference":fp.GetReference() if fp else "BOARD","text":text.GetText(),"position":original})
    assert refs_before=={fp.GetReference():(fp.m_Uuid.AsString(),fp.GetPath().AsString()) for fp in footprints}
    assert all(fp.Reference().IsVisible() and fp.Reference().GetLayer()==p.F_SilkS for fp in footprints)
    assert native_identity(board,editable)==identity,"Non-silk native geometry changed"
    return {"editable_silk_uuids":sorted(editable),"moved_text":moves,"shapes_moved_to_fab":shapes_to_fab,
            "semantic_corrections":semantic,"unplaced_text":unresolved,"reference_count":len(footprints),
            "all_references_visible":True,"non_silk_identity_preserved":True,
            "minimum_text_mm":TEXT_HEIGHT,"minimum_stroke_mm":STROKE,"geometric_clearance_mm":GAP,
            "library_shapes_to_fab":{key:[list(value) for value in values] for key,values in to_fab.items()}},identity


def candidate_libraries(board,report,destination):
    destination.mkdir(parents=True,exist_ok=True)
    source_lib=ROOT/"libraries/master.pretty"
    records=[]
    for path in source_lib.glob("*.kicad_mod"):
        shutil.copyfile(path,destination/path.name)
    by_name={}
    for fp in board.GetFootprints():
        by_name.setdefault(str(fp.GetFPID().GetLibItemName()),fp)
    def frozen(value):
        return tuple(frozen(v) for v in value) if isinstance(value,(list,tuple)) else value
    for name,instance in sorted(by_name.items()):
        fp=p.FootprintLoad(str(source_lib),name)
        clone=local_copy(instance)
        legend={item.GetText():item for item in clone.GraphicalItems() if isinstance(item,p.PCB_TEXT) and item.GetLayer()==p.F_SilkS}
        placed_silk={shape_key(item) for item in clone.GraphicalItems() if isinstance(item,p.PCB_SHAPE) and item.GetLayer()==p.F_SilkS}
        placed_fab={shape_key(item) for item in clone.GraphicalItems() if isinstance(item,p.PCB_SHAPE) and item.GetLayer()==p.F_Fab}
        changed_shapes=set(frozen(v) for v in report["library_shapes_to_fab"].get(name,[]))
        for item in fp.GraphicalItems():
            if item.GetLayer()!=p.F_SilkS:
                continue
            if isinstance(item,p.PCB_SHAPE):
                item.SetWidth(p.FromMM(max(STROKE,mm(item.GetWidth()))))
                if shape_key(item) in changed_shapes or (shape_key(item) not in placed_silk and shape_key(item) in placed_fab):
                    item.SetLayer(p.F_Fab)
            elif isinstance(item,p.PCB_TEXT):
                text=legend.get(item.GetText())
                if text:
                    item.SetPosition(text.GetPosition())
                    item.SetTextAngle(text.GetTextAngle())
                    item.SetTextSize(text.GetTextSize())
                    item.SetTextThickness(text.GetTextThickness())
        normalize_text(fp.Reference())
        before_pads=[(x.GetNumber(),xy(x.GetPosition()),xy(x.GetSize()),xy(x.GetDrillSize()),int(x.GetAttribute()),int(x.GetShape()),tuple(x.GetLayerSet().Seq())) for x in fp.Pads()]
        p.FootprintSave(str(destination),fp)
        assert (destination/f"{name}.kicad_mod").exists()
        reloaded=p.FootprintLoad(str(destination),name)
        after_pads=[(x.GetNumber(),xy(x.GetPosition()),xy(x.GetSize()),xy(x.GetDrillSize()),int(x.GetAttribute()),int(x.GetShape()),tuple(x.GetLayerSet().Seq())) for x in reloaded.Pads()]
        assert before_pads==after_pads
        records.append({"name":name,"source_sha256":sha(source_lib/f"{name}.kicad_mod"),
                        "candidate_sha256":sha(destination/f"{name}.kicad_mod"),"pad_geometry_preserved":True})
    return records


def drc(path,report):
    command=[str(CLI),"pcb","drc","--format","json","--severity-all","--schematic-parity","--output",str(report),str(path)]
    result=subprocess.run(command,capture_output=True,text=True,timeout=120,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    if result.returncode or not report.exists():
        raise RuntimeError(f"Native DRC failed: {result.stdout} {result.stderr}")
    data=json.loads(report.read_text())
    return {"categories":dict(Counter(item["type"] for key in ("violations","unconnected_items","schematic_parity") for item in data.get(key,[]))),
            "command":command,"stdout":result.stdout,"report":str(report)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,default=SOURCE)
    parser.add_argument("--output-dir",type=Path,default=ROOT/"reports/silkscreen_cleanup")
    args=parser.parse_args()
    source=args.source.resolve()
    output=args.output_dir.resolve()
    if output==source.parent or (output/"candidate"/source.name).resolve()==source:
        raise ValueError("Output must be a separate review directory")
    output.mkdir(parents=True,exist_ok=True)
    original_sha=sha(source)
    stage=output/"candidate"
    stage.mkdir(exist_ok=True)
    for pattern in ("*.kicad_pro","*.kicad_dru","*.kicad_sch","*-lib-table"):
        for path in source.parent.glob(pattern):
            dest=stage/path.name
            if path.name.endswith("-lib-table"):
                dest.write_text(path.read_text().replace("${KIPRJMOD}",source.parent.as_posix()),encoding="utf-8")
            else:
                shutil.copyfile(path,dest)
    candidate=stage/source.name
    shutil.copyfile(source,candidate)
    before=drc(candidate,output/"before_drc.json")
    board=p.LoadBoard(str(candidate))
    report,identity=cleanup(board)
    report["candidate_libraries"]=candidate_libraries(board,report,output/"libraries/master.pretty")
    (stage/"fp-lib-table").write_text('(fp_lib_table\n  (lib (name "Eltec_Master")(type "KiCad")(uri "${KIPRJMOD}/../libraries/master.pretty")(options "")(descr "Reviewed candidate silk; original copper geometry"))\n)\n',encoding="utf-8")
    assert p.SaveBoard(str(candidate),board,True)
    reloaded=p.LoadBoard(str(candidate))
    assert native_identity(reloaded,set(report["editable_silk_uuids"]))==identity
    after=drc(candidate,output/"after_drc.json")
    assert sha(source)==original_sha,"Source changed during the copy-only cleanup"
    report.update(source=str(source),source_sha256=original_sha,output=str(candidate),output_sha256=sha(candidate),
                  before_drc=before,after_drc=after,source_pcb_untouched=True,
                  status="REVIEW_CANDIDATE_NOT_FABRICATION_RELEASE")
    (output/"cleanup_report.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({key:report[key] for key in ("status","source_pcb_untouched","reference_count","all_references_visible","unplaced_text","before_drc","after_drc","output")},indent=2))


if __name__=="__main__":
    main()
