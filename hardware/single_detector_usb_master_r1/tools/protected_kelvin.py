"""One reviewed native Kelvin seed; no generic incremental-routing permission.

The DSN-only netless obstacle is deliberately occupied by the fixed seed.
It discourages all new conductors. A stricter native geometric postcondition
independently refuses any branch there, irrespective of net name.
"""
from __future__ import annotations
import uuid
import pcbnew as p

PROFILE = "LT3041_U10_12_TO_C11_1_V1"
WIDTH_NM = 200_000
GUARD_NAME = "ELTEC_KELVIN_U10_12_NO_BRANCH"


def xy(point):
    return [int(point.x), int(point.y)]


def bbox(item):
    b = item.GetBoundingBox()
    return [int(b.GetLeft()), int(b.GetTop()), int(b.GetRight()), int(b.GetBottom())]


def pad(board, ref, number):
    found = [x for f in board.GetFootprints() if f.GetReference() == ref
             for x in f.Pads() if x.GetNumber() == number]
    if len(found) != 1:
        raise RuntimeError(f"Kelvin profile needs exactly one real pad {ref}.{number}")
    return found[0]


def manifest(board, source_board_sha256):
    a, z = pad(board, "U10", "12"), pad(board, "C11", "1")
    related = [a, z, pad(board,"U10","13"),pad(board,"U10","14")]
    if any(x.GetNetname() != "ADC_5V_HELD" or not x.IsOnLayer(p.F_Cu) for x in related):
        raise RuntimeError("Reviewed Kelvin pads changed net or copper layer")
    start, end = xy(a.GetPosition()), xy(z.GetPosition())
    ab, zb = bbox(a), bbox(z)
    if end[0] <= start[0]+1_000_000 or abs(end[1]-start[1]) > 100_000:
        raise RuntimeError("Kelvin profile only supports this reviewed near-horizontal placement")
    # Destination aperture begins 0.25mm inside C11's left copper boundary.
    # The complete trace beyond that boundary lies inside the capacitor pad.
    right = zb[0]+250_000
    if not start[0]+500_000 < right < end[0]-250_000:
        raise RuntimeError("No reviewed capacitor-pad join aperture")
    low_y = min(ab[1], start[1]-WIDTH_NM//2, end[1]-WIDTH_NM//2)-100_000
    high_y = max(ab[3], start[1]+WIDTH_NM//2, end[1]+WIDTH_NM//2)+100_000
    if low_y < zb[1]+100_000 or high_y > zb[3]-100_000:
        raise RuntimeError("Guard does not terminate safely inside the C11 copper pad")
    track_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, PROFILE+source_board_sha256))
    return {"profile":PROFILE,"source_board_sha256":source_board_sha256,
            "track":{"uuid":track_uuid,"start_nm":start,"end_nm":end,
                     "width_nm":WIDTH_NM,"layer":"F.Cu","net":"ADC_5V_HELD","locked":True},
            "protected_rectangle_nm":[ab[0]-100_000,low_y,right,high_y],
            "start_pad_bbox_nm":ab,"destination_pad_bbox_nm":zb,
            "join_policy":"Only inside C11.1 beyond protected rectangle; no intermediate junction or U10.12 branch",
            "copper_zone_policy":"Recheck after plane work; no F.Cu copper zone may overlap protected rectangle"}


def check_manifest(board, spec):
    if spec != manifest(board, spec.get("source_board_sha256", "")):
        raise RuntimeError("Kelvin manifest does not match the single reviewed profile/pad geometry")


def track_signature(track):
    return {"uuid":track.m_Uuid.AsString(),"start_nm":xy(track.GetStart()),
            "end_nm":xy(track.GetEnd()),"width_nm":int(track.GetWidth()),
            "layer":"F.Cu" if track.GetLayer()==p.F_Cu else str(track.GetLayer()),
            "net":str(track.GetNetname()),"locked":bool(track.IsLocked())}


def seed_board(board, spec):
    check_manifest(board,spec)
    existing=list(board.GetTracks())
    if existing:
        if len(existing)!=1 or track_signature(existing[0])!=spec["track"]:
            raise RuntimeError("Only the exact explicitly reviewed locked seed is accepted")
        verify(board,spec)
        return
    a=pad(board,"U10","12")
    t=p.PCB_TRACK(board)
    t.SetUuid(p.KIID(spec["track"]["uuid"]))
    t.SetStart(p.VECTOR2I(*spec["track"]["start_nm"]))
    t.SetEnd(p.VECTOR2I(*spec["track"]["end_nm"]))
    t.SetWidth(WIDTH_NM);t.SetLayer(p.F_Cu);t.SetNetCode(a.GetNetCode());t.SetLocked(True)
    board.Add(t)
    verify(board,spec)


def boxes_intersect(a,b):
    return not (a[2]<b[0] or b[2]<a[0] or a[3]<b[1] or b[3]<a[1])


def segment_hits_rectangle(start,end,rect,radius=0):
    # Slab clipping against the expanded rectangle is conservative at corners:
    # false positives are possible; false-negative conductor overlaps are not.
    lo,hi=0.0,1.0
    for axis in (0,1):
        mn,mx=rect[axis]-radius,rect[axis+2]+radius
        d=end[axis]-start[axis]
        if not d:
            if not mn <= start[axis] <= mx:return False
        else:
            a,b=(mn-start[axis])/d,(mx-start[axis])/d
            if a>b:a,b=b,a
            lo,hi=max(lo,a),min(hi,b)
            if lo>hi:return False
    return True


def verify(board,spec):
    check_manifest(board,spec)
    tracks=list(board.GetTracks())
    exact=[t for t in tracks if not isinstance(t,(p.PCB_VIA,p.PCB_ARC))
           and track_signature(t)==spec["track"]]
    if len(exact)!=1:
        raise RuntimeError("Locked Kelvin seed was deleted, changed, duplicated or lost its native identity")
    rect=spec["protected_rectangle_nm"]
    failures=[]
    for t in tracks:
        if t.m_Uuid.AsString()==spec["track"]["uuid"]:continue
        if isinstance(t,p.PCB_VIA):
            hit=segment_hits_rectangle(xy(t.GetPosition()),xy(t.GetPosition()),rect,t.GetWidth(p.F_Cu)/2)
        elif t.GetLayer()!=p.F_Cu:continue
        elif isinstance(t,p.PCB_ARC):hit=boxes_intersect(bbox(t),rect)
        else:hit=segment_hits_rectangle(xy(t.GetStart()),xy(t.GetEnd()),rect,t.GetWidth()/2)
        if hit:failures.append("track/via "+t.m_Uuid.AsString()+" net="+str(t.GetNetname()))
    for f in board.GetFootprints():
        for a in f.Pads():
            if (str(f.GetReference()),str(a.GetNumber())) in (("U10","12"),("C11","1")):continue
            if a.IsOnLayer(p.F_Cu) and a.GetNumber() and boxes_intersect(bbox(a),rect):
                failures.append("foreign pad "+f.GetReference()+"."+a.GetNumber())
    for zone in board.Zones():
        if not zone.GetIsRuleArea() and zone.IsOnLayer(p.F_Cu) and boxes_intersect(bbox(zone),rect):
            failures.append("F.Cu copper zone overlaps protected corridor")
    for drawing in board.GetDrawings():
        if drawing.GetLayer()==p.F_Cu and boxes_intersect(bbox(drawing),rect):
            failures.append("copper drawing overlaps protected corridor")
    if failures:
        raise RuntimeError("Protected Kelvin path acquired a branch/obstacle: "+"; ".join(failures))
    return {"profile":PROFILE,"exact_locked_seed_retained":True,
            "no_extra_conductor_in_protected_corridor":True,
            "protected_rectangle_nm":rect,"trace_width_mm":WIDTH_NM/1e6}


def children(tree,name):
    return [x for x in tree if isinstance(x,list) and x and x[0]==name]


def guard_dsn(tree,spec):
    if children(tree,"unit") != [["unit","um"]]:
        raise RuntimeError("Kelvin DSN profile requires native micrometer units")
    wiring=children(tree,"wiring")
    wires=children(wiring[0],"wire") if len(wiring)==1 else []
    if len(wires)!=1:raise RuntimeError("Seed export must contain exactly one native wire")
    wire=wires[0]
    path=children(wire,"path")
    expected=[WIDTH_NM/1000,*[v/1000*(-1 if i%2 else 1)
              for i,v in enumerate(spec["track"]["start_nm"]+spec["track"]["end_nm"])]]
    if len(path)!=1 or path[0][1]!="F.Cu" or [float(x) for x in path[0][2:]]!=expected:
        raise RuntimeError("Native DSN seed geometry changed")
    if children(wire,"net") != [["net","ADC_5V_HELD"]] or children(wire,"type") != [["type","fix"]]:
        raise RuntimeError("KiCad did not serialize the seed as a fixed wire on the reviewed net")
    st=children(tree,"structure")
    if len(st)!=1:raise RuntimeError("Expected one DSN structure")
    if any(GUARD_NAME in str(x) for x in st[0]):raise RuntimeError("Duplicate Kelvin DSN obstacle")
    x0,y0,x1,y1=spec["protected_rectangle_nm"]
    # No net descriptor: the obstacle shares no net, including ADC_5V_HELD.
    st[0].append(["keepout",GUARD_NAME,["rect","F.Cu",str(x0/1000),str(-y1/1000),str(x1/1000),str(-y0/1000)]])
    return tree
