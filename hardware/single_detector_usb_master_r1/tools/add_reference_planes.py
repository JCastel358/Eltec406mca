"""Propose reference planes/thermal vias; optionally fill and DRC a review copy.

Default invocation only writes a geometric plan. --build-copy adds native zones
and off-pad through vias to a separate project copy. Never writes the main PCB.
Run again after trial routing: candidate vias are checked against those routes.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid
import pcbnew as p
from route_board import parse, apply_project_netclasses, outer_tracks_only

ROOT=Path(__file__).resolve().parents[1]
PROJECT=ROOT/"single_detector_usb_master"
SOURCE=PROJECT/"single_detector_usb_master.kicad_pcb"
CLI=Path(sys.executable).with_name("kicad-cli.exe")
NAMESPACE=uuid.UUID("ce486f74-24c5-4e27-bc7a-5487a4290871")
MOAT=1.0
CLEARANCE=.25
VIA_DIAMETER=.6
VIA_DRILL=.3
PAD_VIA_EDGE_GAP=.3


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def mm(value): return p.ToMM(value)
def point(x,y): return p.VECTOR2I(p.FromMM(x),p.FromMM(y))
def xy(value): return (mm(value.x),mm(value.y))
def snap_down(value,step=.5): return math.floor(value/step)*step
def snap_up(value,step=.5): return math.ceil(value/step)*step
def box(item):
    b=item.GetBoundingBox()
    return [mm(b.GetLeft()),mm(b.GetTop()),mm(b.GetRight()),mm(b.GetBottom())]
def union(boxes):
    return [min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)]
def rectangle(b): return [[b[0],b[1]],[b[2],b[1]],[b[2],b[3]],[b[0],b[3]]]
def expanded(b,amount): return [b[0]-amount,b[1]-amount,b[2]+amount,b[3]+amount]
def boxes_intersect(a,b): return not (a[2]<b[0] or b[2]<a[0] or a[3]<b[1] or b[3]<a[1])
def inside(pt,poly):
    x,y=pt
    result=False
    for a,b in zip(poly,poly[1:]+poly[:1]):
        if (a[1]>y)!=(b[1]>y) and x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]: result=not result
    return result
def copper_pads(fp): return [q for q in fp.Pads() if q.IsOnLayer(p.F_Cu) or q.IsOnLayer(p.B_Cu)]
def named_pads(fp): return [q for q in copper_pads(fp) if q.GetNumber() and not q.GetNetname().startswith("unconnected-(")]
def is_emitter(fp):
    names=[q.GetNetname() for q in named_pads(fp)]
    return bool(names) and all(name.startswith(("EM_","EMIT")) and not name.startswith(("EM_LED","EM_MASTER_LED")) for name in names)
def uid(name): return p.KIID(str(uuid.uuid5(NAMESPACE,name)))


def distance_segment(pt,a,b):
    dx,dy=b[0]-a[0],b[1]-a[1]
    if dx*dx+dy*dy==0: return math.dist(pt,a)
    t=max(0,min(1,((pt[0]-a[0])*dx+(pt[1]-a[1])*dy)/(dx*dx+dy*dy)))
    return math.dist(pt,(a[0]+t*dx,a[1]+t*dy))


def via_proposals(board,tab):
    b=box(tab)
    proposals=[]
    # Two five-via rows and two four-via columns outside the exposed tab.
    for y in (b[1]-.8,b[3]+.8):
        for fraction in (.08,.29,.50,.71,.92):
            proposals.append((round(b[0]+fraction*(b[2]-b[0]),3),round(y,3)))
    for x in (b[0]-.8,b[2]+.8):
        for fraction in (.15,.38,.62,.85):
            proposals.append((round(x,3),round(b[1]+fraction*(b[3]-b[1]),3)))
    pads=[]
    for fp in board.GetFootprints():
        for pad in copper_pads(fp):
            polygon=p.SHAPE_POLY_SET()
            # Expanded real copper shape, not just its unrotated nominal size.
            pad.TransformShapeToPolygon(polygon,p.F_Cu,p.FromMM(VIA_DIAMETER/2+PAD_VIA_EDGE_GAP),p.FromMM(.005),p.ERROR_OUTSIDE)
            pads.append((fp.GetReference(),pad.GetNumber(),polygon))
    accepted,rejected=[],[]
    for pt in proposals:
        why=[]
        for ref,pin,polygon in pads:
            if polygon.Collide(point(*pt)): why.append(f"pad clearance: {ref}.{pin}")
        for track in board.GetTracks():
            if isinstance(track,p.PCB_VIA):
                separation=math.dist(pt,xy(track.GetPosition()))
                if separation<.05: why.append("existing via at this position")
                elif track.GetNetname()!="EM_GND" and separation<(VIA_DIAMETER+mm(track.GetWidth(p.F_Cu)))/2+CLEARANCE:
                    why.append(f"via clearance: {track.GetNetname()}")
            elif track.GetNetname()!="EM_GND":
                if isinstance(track,p.PCB_ARC):
                    btrack=expanded(box(track),VIA_DIAMETER/2+CLEARANCE)
                    conflict=btrack[0]<=pt[0]<=btrack[2] and btrack[1]<=pt[1]<=btrack[3]
                else:
                    conflict=distance_segment(pt,xy(track.GetStart()),xy(track.GetEnd()))<VIA_DIAMETER/2+CLEARANCE+mm(track.GetWidth())/2
                if conflict: why.append(f"track clearance: {track.GetNetname()}")
        for zone in board.Zones():
            if zone.GetIsRuleArea() and zone.GetDoNotAllowVias() and zone.Outline().Collide(point(*pt),p.FromMM(VIA_DIAMETER/2+CLEARANCE)):
                why.append("existing via keepout")
        record={"xy_mm":list(pt),"diameter_mm":VIA_DIAMETER,"drill_mm":VIA_DRILL,
                "net":"EM_GND","pad_to_annulus_clearance_min_mm":PAD_VIA_EDGE_GAP,
                "via_in_pad":False,"layers":["F.Cu","B.Cu"]}
        if why: rejected.append({**record,"reasons":why})
        else: accepted.append(record)
    return accepted,rejected


def local_ground_stitch(board,region,anchor,reference,allow_region_growth=False,
                        net="EM_GND",width=1.0,skip_existing_region=True,
                        candidates=None,required=True):
    origin=xy(anchor.GetPosition())
    if candidates is None:
        candidates=[]
        for distance in (1.,1.2,1.5,1.8,2.1,2.4):
            candidates += [(origin[0]+dx*distance,origin[1]+dy*distance) for dx,dy in ((0,-1),(0,1),(-1,0),(1,0))]
    # Conservative bounding-box prefilter only: every proposed annulus/stub is
    # contained by this search box, and native shape collision remains final.
    search=expanded(union([[x,y,x,y] for x,y in [origin]+list(candidates)]),max(width/2,VIA_DIAMETER/2)+CLEARANCE+PAD_VIA_EDGE_GAP+.01)
    existing=[{"kind":"existing_through_via","xy_mm":list(xy(t.GetPosition()))}
              for t in board.GetTracks() if isinstance(t,p.PCB_VIA) and t.GetNetname()==net and inside(xy(t.GetPosition()),region)]
    existing += [{"kind":"existing_ground_PTH","xy_mm":list(xy(q.GetPosition()))}
                 for f in board.GetFootprints() for q in f.Pads() if q.GetNetname()==net and q.GetAttribute()==p.PAD_ATTRIB_PTH and inside(xy(q.GetPosition()),region)]
    if existing and skip_existing_region:
        return {"reference":reference,"add":False,"existing_connections":existing}
    pad_for_via=[]
    foreign=[]
    foreign_vias=[]
    for f in board.GetFootprints():
        for q in copper_pads(f):
            if not boxes_intersect(box(q),search): continue
            poly=p.SHAPE_POLY_SET()
            q.TransformShapeToPolygon(poly,p.F_Cu,p.FromMM(VIA_DIAMETER/2+PAD_VIA_EDGE_GAP),p.FromMM(.005),p.ERROR_OUTSIDE)
            pad_for_via.append(poly)
            if q.GetNetname()!=net:
                poly=p.SHAPE_POLY_SET()
                q.TransformShapeToPolygon(poly,p.F_Cu,p.FromMM(CLEARANCE),p.FromMM(.005),p.ERROR_OUTSIDE)
                foreign.append(poly)
    for track in board.GetTracks():
        if not boxes_intersect(box(track),search): continue
        if track.GetNetname()!=net:
            poly=p.SHAPE_POLY_SET()
            track.TransformShapeToPolygon(poly,track.GetLayer(),p.FromMM(CLEARANCE),p.FromMM(.005),p.ERROR_OUTSIDE)
            foreign_vias.append(poly)
            if track.IsOnLayer(p.F_Cu) or isinstance(track,p.PCB_VIA): foreign.append(poly)
    rejected=[]
    for pt in candidates:
        why=[]
        if not allow_region_growth and not inside(pt,region): why.append("outside assigned ground region")
        if any(poly.Collide(point(*pt)) for poly in pad_for_via): why.append("pad-to-annulus clearance")
        if any(zone.GetIsRuleArea() and zone.GetDoNotAllowVias() and zone.Outline().Collide(point(*pt),p.FromMM(.55)) for zone in board.Zones()): why.append("existing via keepout")
        if any(isinstance(t,p.PCB_VIA) and math.dist(pt,xy(t.GetPosition()))<(VIA_DRILL+mm(t.GetDrillValue()))/2+.25 for t in board.GetTracks()): why.append("existing via drill clearance")
        via_shape=p.SHAPE_CIRCLE(point(*pt),p.FromMM(VIA_DIAMETER/2))
        if any(poly.Collide(via_shape) for poly in foreign+foreign_vias): why.append("foreign copper at through-via site")
        stub=p.PCB_TRACK(board)
        stub.SetStart(anchor.GetPosition())
        stub.SetEnd(point(*pt))
        stub.SetWidth(p.FromMM(width))
        stub.SetLayer(p.F_Cu)
        polygon=p.SHAPE_POLY_SET()
        stub.TransformShapeToPolygon(polygon,p.F_Cu,0,p.FromMM(.005),p.ERROR_OUTSIDE)
        if any(polygon.Collide(poly) for poly in foreign): why.append("foreign front copper on ground stub")
        if why:
            rejected.append({"xy_mm":[round(v,4) for v in pt],"reasons":why})
            continue
        return {"reference":reference,"add":True,"net":net,"xy_mm":[round(v,4) for v in pt],
                "diameter_mm":VIA_DIAMETER,"drill_mm":VIA_DRILL,"via_in_pad":False,
                "pad_to_annulus_clearance_min_mm":PAD_VIA_EDGE_GAP,
                "rejected_alternatives":rejected,
                "ground_stub":{"layer":"F.Cu","width_mm":width,"from_mm":list(origin),"to_mm":[round(v,4) for v in pt]}}
    if required: raise RuntimeError(f"No clear off-pad ground stitch and {width} mm F.Cu connection near {reference}; review routed geometry")
    return {"reference":reference,"net":net,"add":False,"review_required":True,"rejected_alternatives":rejected}


def quiet_regulator_stitches(board,fp,ground,holes):
    """Separate local plane access; no shared surface neck or via-in-pad.

    A pre-existing GND via elsewhere on the broad plane is not evidence of a
    short local thermal/quiet return, so each named anchor is reviewed locally.
    Failure remains an explicit proposal rejection, and prevents copy build.
    """
    ep=next(q for q in fp["U10"].Pads() if q.GetNumber()=="15" and q.GetNetname()=="GND")
    b=box(ep)
    center=xy(ep.GetPosition())
    if b[3]-b[1] <= b[2]-b[0]: raise RuntimeError("U10 EP orientation changed; review end-via construction")
    rows=[]
    for end,y in (("top",b[1]-.8),("bottom",b[3]+.8)):
        sites=[(center[0]+dx,y+dy) for dy in (0, -.2 if end=="top" else .2) for dx in (0,-.2,.2)]
        rows.append(local_ground_stitch(board,ground,ep,"U10.15_"+end,net="GND",width=1.0,
                    skip_existing_region=False,candidates=sites,required=False))
    for ref,width,purpose in (("C11",.6,"output capacitor ground"),("C15",.3,"SET capacitor quiet ground"),
                               ("C37",.3,"SET capacitor quiet ground"),("C38",.3,"SET capacitor quiet ground")):
        pad=next(q for q in fp[ref].Pads() if q.GetNumber()=="2" and q.GetNetname()=="GND")
        row=local_ground_stitch(board,ground,pad,ref+".2",net="GND",width=width,skip_existing_region=False,required=False)
        row["purpose"]=purpose
        rows.append(row)
    for row in rows:
        row.setdefault("purpose","U10 exposed-pad thermal ground")
        if row["add"] and any(inside(row["xy_mm"],h) for h in holes):
            row["add"]=False
            row["review_required"]=True
            row["rejection"]="candidate lies inside GND-plane hole"
    return {"U10_EP_copper_bbox_mm":b,"stitches":rows,
            "separate_surface_returns":True,"shared_connection":"broad GND internal planes",
            "thermal_via_count_qualified":False,"requires_routed_copy_DRC":True}


def regulator_ground_bridges(board,fp):
    """Short explicit U10 GND-pin links to its EP, subject to routed clearance."""
    # These 0.5 mm pitch lead escapes use the project's 0.20 mm track
    # clearance. Zone/via proposals elsewhere retain their 0.25 mm margin.
    bridge_clearance=.20
    ep=next(q for q in fp["U10"].Pads() if q.GetNumber()=="15" and q.GetNetname()=="GND")
    result=[]
    for pin in ("10","11"):
        pad=next(q for q in fp["U10"].Pads() if q.GetNumber()==pin and q.GetNetname()=="GND")
        start=xy(pad.GetPosition())
        end=(mm(ep.GetPosition().x),start[1])
        if not box(ep)[1]<end[1]<box(ep)[3]: raise RuntimeError("U10 ground pin is not aligned with its exposed pad")
        stub=p.PCB_TRACK(board)
        stub.SetStart(point(*start)); stub.SetEnd(point(*end))
        stub.SetWidth(p.FromMM(.25)); stub.SetLayer(p.F_Cu)
        shape=p.SHAPE_POLY_SET()
        stub.TransformShapeToPolygon(shape,p.F_Cu,0,p.FromMM(.005),p.ERROR_OUTSIDE)
        search=expanded(box(stub),bridge_clearance+.01)
        blockers=[]
        for f in board.GetFootprints():
            for q in copper_pads(f):
                if not q.IsOnLayer(p.F_Cu) or q.GetNetname()=="GND" or not boxes_intersect(box(q),search): continue
                other=p.SHAPE_POLY_SET()
                q.TransformShapeToPolygon(other,p.F_Cu,p.FromMM(bridge_clearance),p.FromMM(.005),p.ERROR_OUTSIDE)
                if shape.Collide(other): blockers.append(f"pad {f.GetReference()}.{q.GetNumber()} ({q.GetNetname()})")
        for track in board.GetTracks():
            if not track.IsOnLayer(p.F_Cu) or track.GetNetname()=="GND" or not boxes_intersect(box(track),search): continue
            other=p.SHAPE_POLY_SET()
            track.TransformShapeToPolygon(other,p.F_Cu,p.FromMM(bridge_clearance),p.FromMM(.005),p.ERROR_OUTSIDE)
            if shape.Collide(other): blockers.append(f"track/via {track.m_Uuid.AsString()} ({track.GetNetname()})")
        for zone in board.Zones():
            if zone.GetIsRuleArea() and zone.IsOnLayer(p.F_Cu) and zone.GetDoNotAllowTracks() and shape.Collide(zone.Outline()):
                blockers.append("front track keepout")
        result.append({"reference":"U10."+pin+"_to_EP15","net":"GND","add":not blockers,
                       "review_required":bool(blockers),"blockers":blockers,
                       "layer":"F.Cu","width_mm":.25,"track_clearance_checked_mm":bridge_clearance,"from_mm":list(start),"to_mm":list(end)})
    return result


def surface_return_stitches(board,fp,ground,em_right):
    """Give the four trial-route surface return groups local plane access.

    These SMD groups were independently identified by native connectivity,
    not inferred to connect merely because an inner zone lies underneath.
    """
    rows=[]
    for ref,net,width,region in (("R121","GND",.3,ground),("C101","EM_GND",1.0,em_right),
                                 ("R29","EM_GND",1.0,em_right),("C100","EM_GND",1.0,em_right)):
        pad=next(q for q in fp[ref].Pads() if q.GetNumber()=="2" and q.GetNetname()==net)
        # R111/R116/R121 already share a routed GND island. The R121 site
        # accesses that group from the straight 1.35 mm corridor between its
        # pad and the antenna boundary; coarser 1.2/1.5 mm sites do not fit.
        sites=[(mm(pad.GetPosition().x)+1.35,mm(pad.GetPosition().y))] if ref=="R121" else None
        row=local_ground_stitch(board,region,pad,ref+".2",net=net,width=width,skip_existing_region=False,required=False,candidates=sites)
        row["purpose"]="local plane access for separate surface return group"
        rows.append(row)
    return rows


def make_plan(board):
    outer_tracks_only(board)
    if board.GetCopperLayerCount()!=4: raise RuntimeError("Expected four copper layers")
    if any(not z.GetIsRuleArea() for z in board.Zones()): raise RuntimeError("Existing copper zones require a separate merge review; refusing duplicate pours")
    fp={f.GetReference():f for f in board.GetFootprints()}
    required=("U100","U101","U16","U10","C11","C15","C37","C38","R121","C101","R29","C100","J1","J2","J3","J4","J5","J6")
    if any(ref not in fp for ref in required): raise RuntimeError("Required reference anchors missing")
    edge=board.GetBoardEdgesBoundingBox()
    xmin,ymin,xmax,ymax=[mm(edge.GetLeft())+.5,mm(edge.GetTop())+.5,mm(edge.GetRight())-.5,mm(edge.GetBottom())-.5]
    # Edge line stroke can expand GetBoardEdgesBoundingBox; snap to the intended
    # 0.5 mm inset, while checking the source's verified rectangular outline.
    xmin,ymin,xmax,ymax=[round(v,1) for v in (xmin,ymin,xmax,ymax)]
    pure=[f for f in fp.values() if is_emitter(f)]
    top=[q for f in pure if f.GetPosition().y<0 for q in named_pads(f)]
    reg=[q for f in pure if 20<mm(f.GetPosition().y)<85 for q in named_pads(f)]
    pwm=[q for f in pure if mm(f.GetPosition().y)>95 for q in named_pads(f)]
    if not top or not reg or not pwm: raise RuntimeError("Cannot derive emitter placement clusters")
    top_b,reg_b,pwm_b=[union([box(q) for q in group]) for group in (top,reg,pwm)]
    left_top=max(xmin,snap_down(top_b[0]-1))
    bottom_top=snap_up(top_b[3]+1)
    left_reg=max(xmin,snap_down(reg_b[0]-.75))
    top_reg=snap_down(reg_b[1]-1)
    bottom_reg=min(ymax,snap_up(reg_b[3]+1))
    # A narrow right-edge bridge connects the input/regulator EM plane around
    # the detector jack and detector-domain power parts, not underneath them.
    detector_middle=[q for f in fp.values() if not is_emitter(f) and bottom_top<mm(f.GetPosition().y)<top_reg
                     for q in named_pads(f) if mm(q.GetPosition().x)>75 and q.GetNetname() not in ("EM_GND","EM_OPTO_C")]
    bridge=snap_up(max(box(q)[2] for q in detector_middle)+2)
    if bridge>xmax-1.0: raise RuntimeError("No adequate right-edge EM bridge; adjust placement before planes")
    em_right=[[left_top,ymin],[xmax,ymin],[xmax,bottom_reg],[left_reg,bottom_reg],
              [left_reg,top_reg],[bridge,top_reg],[bridge,bottom_top],[left_top,bottom_top]]
    opto=fp["U101"]
    upper=[mm(q.GetPosition().y) for q in opto.Pads() if q.GetNumber() in ("1","2","3","4","5","6")]
    lower=[mm(q.GetPosition().y) for q in opto.Pads() if q.GetNumber() in ("11","12","13","14","15","16")]
    midpoint=(sum(upper)/len(upper)+sum(lower)/len(lower))/2
    pwm_top=round(midpoint+1,3)
    # Bound the return plane from actual EM_GND pads. The low-current gate
    # resistor ends may occupy the moat, rather than pushing the plane beneath
    # the adjacent USB/GND LED-drive column at x40 mm.
    pwm_ground=[q for f in fp.values() for q in named_pads(f) if q.GetNetname()=="EM_GND" and mm(q.GetPosition().y)>95]
    pwm_right=snap_up(max(box(q)[2] for q in pwm_ground)+.75)
    em_pwm=rectangle([xmin,pwm_top,pwm_right,ymax])
    ground=[[xmin,ymin],[left_top-MOAT,ymin],[left_top-MOAT,bottom_top+MOAT],
            [bridge-MOAT,bottom_top+MOAT],[bridge-MOAT,top_reg-MOAT],
            [left_reg-MOAT,top_reg-MOAT],[left_reg-MOAT,bottom_reg+MOAT],
            [xmax,bottom_reg+MOAT],[xmax,ymax],[pwm_right+MOAT,ymax],
            [pwm_right+MOAT,midpoint-1],[xmin,midpoint-1]]
    # U16 straddles the domains beside detector switches. Derive a small pocket
    # from only its emitter pins, preserving the surrounding GND-side circuitry.
    em_opto=[q for q in fp["U16"].Pads() if q.GetNumber() in ("3","4")]
    pocket_b=union([box(q) for q in em_opto])
    pocket_b=[snap_down(pocket_b[0]-.5,.25),snap_down(pocket_b[1]-.5,.25),snap_up(pocket_b[2]+.5,.25),snap_up(pocket_b[3]+.5,.25)]
    pocket=rectangle(pocket_b)
    pocket_in_right=all(inside(v,em_right) for v in pocket)
    em_regions=[("EM_INPUT_REGULATOR",em_right),("EM_PWM",em_pwm)]
    holes=[]
    if not pocket_in_right:
        em_regions.append(("EM_OPTO_LOCAL",pocket))
        holes.append(rectangle(expanded(pocket_b,MOAT)))
    pwm_anchor=next(q for q in fp["Q101"].Pads() if q.GetNumber()=="2" and q.GetNetname()=="EM_GND")
    stitches=[local_ground_stitch(board,em_pwm,pwm_anchor,"Q101.2")]
    if not pocket_in_right:
        opto_anchor=next(q for q in fp["U16"].Pads() if q.GetNumber()=="3" and q.GetNetname()=="EM_GND")
        stitch=local_ground_stitch(board,pocket,opto_anchor,"U16.3",True)
        stitches.append(stitch)
        if stitch["add"]:
            x,y=stitch["xy_mm"]
            pocket_b=union([pocket_b,[x-.55,y-.55,x+.55,y+.55]])
            pocket_b=[snap_down(pocket_b[0],.25),snap_down(pocket_b[1],.25),snap_up(pocket_b[2],.25),snap_up(pocket_b[3],.25)]
            pocket=rectangle(pocket_b)
            em_regions[-1]=("EM_OPTO_LOCAL",pocket)
            holes=[rectangle(expanded(pocket_b,MOAT))]
    ground_pads=[]
    conflicts=[]
    for f in fp.values():
        for q in named_pads(f):
            pt=xy(q.GetPosition())
            if q.GetNetname()=="GND":
                fits=inside(pt,ground) and not any(inside(pt,hole) for hole in holes)
                ground_pads.append({"reference":f.GetReference(),"pin":q.GetNumber(),"xy_mm":list(pt),"over_GND_region":fits})
                if not fits: conflicts.append(f"GND pad lacks its planned reference region: {f.GetReference()}.{q.GetNumber()}")
                if any(inside(pt,poly) for name,poly in em_regions): conflicts.append(f"GND pad over emitter plane: {f.GetReference()}.{q.GetNumber()}")
            if q.GetNetname()=="EM_GND" and not any(inside(pt,poly) for name,poly in em_regions):
                conflicts.append(f"Emitter ground pad outside proposed EM region: {f.GetReference()}.{q.GetNumber()}")
    if conflicts: raise RuntimeError("Plane placement review required: "+"; ".join(conflicts))
    quiet=quiet_regulator_stitches(board,fp,ground,holes)
    stitches.extend(quiet["stitches"])
    stitches.extend(surface_return_stitches(board,fp,ground,em_right))
    bridges=regulator_ground_bridges(board,fp)
    tab=max((q for q in fp["U100"].Pads() if q.GetNumber()=="3" and q.GetNetname()=="EM_GND"),key=lambda q:q.GetSize().x*q.GetSize().y)
    accepted,rejected=via_proposals(board,tab)
    # Keep the thermal spreader clear of the separate small lead pads below.
    thermal=rectangle(expanded(box(tab),1.25))
    planes=[]
    for layer in ("In1.Cu","In2.Cu"):
        planes.append({"name":"GND_REFERENCE_"+layer,"net":"GND","layer":layer,"polygon_mm":ground,"holes_mm":holes,"connection":"thermal through-hole; direct SMD"})
        for name,poly in em_regions:
            planes.append({"name":name+"_"+layer,"net":"EM_GND","layer":layer,"polygon_mm":poly,"holes_mm":[],"connection":"thermal through-hole; direct SMD"})
    for layer in ("F.Cu","B.Cu"):
        planes.append({"name":"U100_THERMAL_"+layer,"net":"EM_GND","layer":layer,"polygon_mm":thermal,"holes_mm":[],"connection":"solid"})
    # The central ADC/SPI ground route must remain in one GND region. The
    # all-layer antenna rule area intentionally remains outside this corridor.
    corridor=[[57,30],[77,30],[77,108],[57,108]]
    assert all(inside(pt,ground) and not any(inside(pt,h) for h in holes) for pt in corridor)
    return {"status":"GEOMETRIC_PROPOSAL_NOT_APPLIED_TO_MAIN_BOARD","source_phase":"routed" if list(board.GetTracks()) else "unrouted geometry precheck",
            "board_inset_mm":[xmin,ymin,xmax,ymax],"copper_edge_inset_mm":.5,"zone_clearance_mm":CLEARANCE,
            "domain_moat_min_mm":MOAT,"opto_U101_midline_y_mm":midpoint,"opto_U101_plane_gap_mm":2,
            "right_edge_bridge_x_mm":bridge,"planes":planes,"ground_pad_coverage":ground_pads,
            "quiet_ADC_SPI_GND_corridor_mm":corridor,"U100_tab_copper_bbox_mm":box(tab),
            "proposed_thermal_vias":accepted,"rejected_thermal_vias":rejected,"via_count":len(accepted),
            "local_ground_stitches":stitches,"quiet_regulator_ground":quiet,
            "local_ground_bridges":bridges,
            "ground_stitch_review_required":any(row.get("review_required",False) for row in stitches+bridges),
            "U16_local_emitter_pocket_required":not pocket_in_right,"no_new_signal_tracks":True,
            "thermal_performance_qualified":False,"bench_note":"Copper area and via count do not establish regulator temperature; verify worst-case dissipation/airflow/assembly."}


def zone_from_plan(board,row):
    zone=p.ZONE(board)
    zone.SetUuid(uid("zone/"+row["name"]))
    zone.SetZoneName(row["name"])
    layer=board.GetLayerID(row["layer"])
    zone.SetLayer(layer)
    zone.SetNet(board.FindNet(row["net"]))
    zone.SetLocalClearance(p.FromMM(CLEARANCE))
    zone.SetMinThickness(p.FromMM(.25))
    zone.SetPadConnection(p.ZONE_CONNECTION_FULL if row["connection"]=="solid" else p.ZONE_CONNECTION_THT_THERMAL)
    zone.SetThermalReliefGap(p.FromMM(.3))
    zone.SetThermalReliefSpokeWidth(p.FromMM(.5))
    zone.SetIslandRemovalMode(p.ISLAND_REMOVAL_MODE_ALWAYS)
    zone.SetAssignedPriority(1)
    poly=zone.Outline()
    poly.NewOutline()
    for x,y in row["polygon_mm"]: poly.Append(p.FromMM(x),p.FromMM(y))
    for hole in row["holes_mm"]:
        index=poly.NewHole(0)
        for x,y in hole: poly.Append(p.FromMM(x),p.FromMM(y),0,index)
    board.Add(zone)
    return zone


def render_plan(board,plan,destination):
    from PIL import Image,ImageDraw,ImageFont
    scale=7
    origin=(plan["board_inset_mm"][0]-.5,plan["board_inset_mm"][1]-.5)
    width=plan["board_inset_mm"][2]-origin[0]+.5
    height=plan["board_inset_mm"][3]-origin[1]+.5
    im=Image.new("RGB",(round(width*scale)+140,round(height*scale)+150),"white")
    draw=ImageDraw.Draw(im)
    try: font=ImageFont.truetype("arial.ttf",13)
    except OSError: font=ImageFont.load_default()
    def pt(value): return ((value[0]-origin[0])*scale+65,(value[1]-origin[1])*scale+60)
    for row in plan["planes"]:
        if row["layer"]!="In1.Cu": continue
        color="#bbdfcd" if row["net"]=="GND" else "#f5c48c"
        draw.polygon([pt(x) for x in row["polygon_mm"]],fill=color,outline="#555555")
        for hole in row["holes_mm"]: draw.polygon([pt(x) for x in hole],fill="white",outline="#555555")
    for f in board.GetFootprints():
        for q in copper_pads(f):
            b=box(q)
            draw.rectangle([pt(b[:2]),pt(b[2:])],outline="#9b9b9b",width=1)
    for zone in board.Zones():
        if not zone.GetIsRuleArea(): continue
        outline=zone.Outline().Outline(0)
        poly=[pt(xy(outline.CPoint(i))) for i in range(outline.PointCount())]
        draw.polygon(poly,fill="white",outline="#a33a3a",width=2)
        b=box(zone)
        draw.text(pt((b[0]+.4,b[1]+1)),"ANTENNA\nKEEPOUT",fill="#a33a3a",font=font)
    for row in plan["planes"]:
        if row["layer"]=="F.Cu": draw.polygon([pt(x) for x in row["polygon_mm"]],outline="#a45b00",width=2)
    for row in plan["proposed_thermal_vias"]+[x for x in plan["local_ground_stitches"] if x["add"]]:
        x,y=pt(row["xy_mm"])
        draw.ellipse((x-2,y-2,x+2,y+2),fill="#8d3b13")
    draw.rectangle([pt(plan["quiet_ADC_SPI_GND_corridor_mm"][0]),pt(plan["quiet_ADC_SPI_GND_corridor_mm"][2])],outline="#216092",width=2)
    draw.text((28,15),"REFERENCE PLANE PROPOSAL - COPY REVIEW ONLY",fill="#202020",font=font)
    y=im.height-65
    draw.rectangle((30,y,44,y+14),fill="#bbdfcd"); draw.text((50,y),"GND",fill="black",font=font)
    draw.rectangle((130,y,144,y+14),fill="#f5c48c"); draw.text((150,y),"EM_GND (separate electrical domain)",fill="black",font=font)
    draw.text((30,y+24),"Blue: ADC/SPI corridor. Dots: proposed off-pad through vias.",fill="#216092",font=font)
    im.save(destination)


def structural_identity(board,added):
    with tempfile.TemporaryDirectory(prefix="eltec_planes_identity_") as tmp:
        path=Path(tmp)/"identity.kicad_pcb"
        assert p.SaveBoard(str(path),board,True)
        tree=parse(path.read_text(encoding="utf-8"))
    def visit(node):
        if not isinstance(node,list): return node
        if any(isinstance(x,list) and len(x)==2 and x[0]=="uuid" and x[1].strip('"') in added for x in node): return None
        return [value for child in node if (value:=visit(child)) is not None]
    return visit(tree)


def native_drc(path,output):
    args=[str(CLI),"pcb","drc","--format","json","--severity-all","--schematic-parity","--output",str(output),str(path)]
    result=subprocess.run(args,capture_output=True,text=True,timeout=120,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    if result.returncode: raise RuntimeError(result.stdout+result.stderr)
    data=json.loads(output.read_text())
    return {"counts":dict(Counter(item["type"] for key in ("violations","unconnected_items","schematic_parity") for item in data.get(key,[]))),"stdout":result.stdout,"command":args}


def build_copy(source,output,plan):
    if plan["via_count"]<8: raise RuntimeError("Fewer than eight off-pad thermal via sites survive routing; review before constructing the thermal candidate")
    if plan["ground_stitch_review_required"]: raise RuntimeError("A required local regulator ground candidate conflicts with routed geometry; inspect plane_plan.json before copy construction")
    sites=plan["proposed_thermal_vias"]+[r for r in plan["local_ground_stitches"] if r["add"]]
    if any(math.dist(a["xy_mm"],b["xy_mm"])<VIA_DRILL+.25 for index,a in enumerate(sites) for b in sites[index+1:]):
        raise RuntimeError("Proposed ground via holes conflict with each other; review their sites")
    stage=output/"candidate"
    stage.mkdir(parents=True,exist_ok=True)
    candidate=stage/source.name
    if candidate.resolve()==source.resolve(): raise RuntimeError("Copy output would overwrite source")
    for pattern in ("*.kicad_sch","*.kicad_pro","*.kicad_dru","*-lib-table"):
        for path in source.parent.glob(pattern):
            dest=stage/path.name
            if path.name.endswith("-lib-table"):
                dest.write_text(path.read_text().replace("${KIPRJMOD}",source.parent.as_posix()),encoding="utf-8")
            else: shutil.copyfile(path,dest)
    shutil.copyfile(source,candidate)
    before=native_drc(candidate,output/"before_drc.json")
    board=p.LoadBoard(str(candidate))
    apply_project_netclasses(board,source.with_suffix(".kicad_pro"))
    baseline=structural_identity(board,set())
    tracks_before=len(list(board.GetTracks()))
    added=set()
    for row in plan["planes"]:
        added.add(zone_from_plan(board,row).m_Uuid.AsString())
    for row in plan["proposed_thermal_vias"]:
        via=p.PCB_VIA(board)
        via.SetUuid(uid("via/"+str(row["xy_mm"])))
        via.SetPosition(point(*row["xy_mm"]))
        via.SetWidth(p.FromMM(VIA_DIAMETER))
        via.SetDrill(p.FromMM(VIA_DRILL))
        via.SetViaType(p.VIATYPE_THROUGH)
        via.SetLayerPair(p.F_Cu,p.B_Cu)
        via.SetNet(board.FindNet(row["net"]))
        via.SetFrontTentingMode(p.TENTING_MODE_TENTED)
        via.SetBackTentingMode(p.TENTING_MODE_TENTED)
        board.Add(via)
        added.add(via.m_Uuid.AsString())
    stitch_count=0
    for row in plan["local_ground_stitches"]:
        if not row["add"]: continue
        via=p.PCB_VIA(board)
        via.SetUuid(uid("stitch/"+row["reference"]))
        via.SetPosition(point(*row["xy_mm"]))
        via.SetWidth(p.FromMM(VIA_DIAMETER))
        via.SetDrill(p.FromMM(VIA_DRILL))
        via.SetViaType(p.VIATYPE_THROUGH)
        via.SetLayerPair(p.F_Cu,p.B_Cu)
        via.SetNet(board.FindNet(row["net"]))
        via.SetFrontTentingMode(p.TENTING_MODE_TENTED)
        via.SetBackTentingMode(p.TENTING_MODE_TENTED)
        board.Add(via)
        added.add(via.m_Uuid.AsString())
        stub=p.PCB_TRACK(board)
        stub.SetUuid(uid("stitch_ground_stub/"+row["reference"]))
        stub.SetStart(point(*row["ground_stub"]["from_mm"]))
        stub.SetEnd(point(*row["ground_stub"]["to_mm"]))
        stub.SetWidth(p.FromMM(row["ground_stub"]["width_mm"]))
        stub.SetLayer(p.F_Cu)
        stub.SetNet(board.FindNet(row["net"]))
        board.Add(stub)
        added.add(stub.m_Uuid.AsString())
        stitch_count+=1
    for row in plan["local_ground_bridges"]:
        if not row["add"]: continue
        stub=p.PCB_TRACK(board)
        stub.SetUuid(uid("ground_bridge/"+row["reference"]))
        stub.SetStart(point(*row["from_mm"]))
        stub.SetEnd(point(*row["to_mm"]))
        stub.SetWidth(p.FromMM(row["width_mm"]))
        stub.SetLayer(p.F_Cu)
        stub.SetNet(board.FindNet(row["net"]))
        board.Add(stub)
        added.add(stub.m_Uuid.AsString())
    bridge_count=sum(row["add"] for row in plan["local_ground_bridges"])
    board.BuildConnectivity()
    assert p.ZONE_FILLER(board).Fill(board.Zones())
    assert structural_identity(board,added)==baseline,"Existing native geometry changed"
    assert len(list(board.GetTracks()))==tracks_before+plan["via_count"]+2*stitch_count+bridge_count
    outer_tracks_only(board)
    assert p.SaveBoard(str(candidate),board,True)
    reloaded=p.LoadBoard(str(candidate))
    assert structural_identity(reloaded,added)==baseline
    areas=[{"name":z.GetZoneName(),"net":z.GetNetname(),"area_mm2":round(z.GetFilledArea()/1e12,3)} for z in reloaded.Zones() if not z.GetIsRuleArea()]
    after=native_drc(candidate,output/"after_drc.json")
    return {"status":"FILLED_REVIEW_COPY_ONLY","output":str(candidate),"output_sha256":sha(candidate),
            "before_drc":before,"after_drc":after,"filled_copper_areas":areas,
            "existing_native_geometry_preserved":True,"added_uuid_count":len(added),
            "added_thermal_through_vias":plan["via_count"],"added_local_ground_stitches":stitch_count,
            "added_ground_stitches_by_net":dict(Counter(r["net"] for r in plan["local_ground_stitches"] if r["add"])),
            "added_outer_ground_stubs":stitch_count+bridge_count,"added_ground_bridges":bridge_count,"new_internal_signal_tracks":0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,default=SOURCE)
    parser.add_argument("--output-dir",type=Path,default=ROOT/"reports/reference_planes")
    parser.add_argument("--build-copy",action="store_true")
    args=parser.parse_args()
    source=args.source.resolve()
    output=args.output_dir.resolve()
    output.mkdir(parents=True,exist_ok=True)
    initial=sha(source)
    board=p.LoadBoard(str(source))
    plan=make_plan(board)
    plan.update(source=str(source),source_sha256=initial)
    (output/"plane_plan.json").write_text(json.dumps(plan,indent=2)+"\n",encoding="utf-8")
    render_plan(board,plan,output/"plane_plan.png")
    if args.build_copy:
        copy_report=build_copy(source,output,plan)
        (output/"copy_validation.json").write_text(json.dumps(copy_report,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(copy_report,indent=2))
    assert sha(source)==initial,"Main source changed during review; regenerate the plan from its new state"
    print(json.dumps({"plan":str(output/"plane_plan.json"),"via_count":plan["via_count"],"plane_count":len(plan["planes"]),"main_board_untouched":True,"source_phase":plan["source_phase"]}))


if __name__=="__main__": main()
