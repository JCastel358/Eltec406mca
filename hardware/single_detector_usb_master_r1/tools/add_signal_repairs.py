"""Deterministically add two reviewed signal connections to a copy only.

Existing copper is preserved except one explicitly recorded local AMP_OUT4 dogleg; non-routing identity is preserved. A native DRC report,
explicit paths/UUIDs and protected-Kelvin verification accompany the candidate.
The fixed source hash prevents application to another design revision.
"""
from pathlib import Path
import argparse
import hashlib
import heapq
import itertools
import json
import math
import shutil
import subprocess
import sys
import time
import uuid
import pcbnew as p
import route_board as r
import protected_kelvin as k
from repair_track_width_quantization import track_state

SOURCE_SHA = 'b33a97f84bf1f496620f39c85c54ef9b8a860b96ba0045ccac9a89994dcc416f'
NAMESPACE = uuid.UUID('13b056b6-d2b6-42b9-b3bd-94af3fb95b14')
CLI = Path(sys.executable).with_name('kicad-cli.exe')
LAYERS = (p.F_Cu, p.B_Cu)
STEP = .10
CLEARANCE = .205
TASKS = (
    {'net': 'ADC_SCLK', 'ref': 'J5', 'pin': '3', 'width': .20,
     'target': (61.9125, 89.2584), 'target_uuid': '9a83b0ec-3d64-4a63-9229-d07eb34f138a',
     'roi': (40, 65, 96, 105)},
    {'net': 'AMP_OUT5', 'ref': 'U35', 'pin': '7', 'width': .25,
     'target': (31.2263, 29.2357), 'target_uuid': 'f06be6d7-39a7-49ba-89b4-c6aaa56afd61',
     'roi': (22, 17, 39, 33)},
)


def point(xy): return p.VECTOR2I(p.FromMM(xy[0]), p.FromMM(xy[1]))
def mmxy(v): return (v.x/1e6, v.y/1e6)
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def require(value, text):
    if not value: raise RuntimeError(text)


class Obstacles:
    def __init__(self, board, task):
        self.task = task
        self.polygons = {layer: [] for layer in LAYERS}
        self.via_polygons = []
        self.keepouts = list(z for z in board.Zones() if z.GetIsRuleArea())
        self.cache = {}
        self.edge_cache = {}
        self.via_cache = {}
        x0, y0, x1, y1 = task['roi']
        for item in [q for f in board.GetFootprints() for q in f.Pads()] + list(board.GetTracks()):
            b = item.GetBoundingBox()
            if b.GetRight()/1e6 < x0-1 or b.GetLeft()/1e6 > x1+1 or b.GetBottom()/1e6 < y0-1 or b.GetTop()/1e6 > y1+1:
                continue
            if item.GetNetname() != task['net']:
                for layer in LAYERS:
                    if not item.IsOnLayer(layer): continue
                    poly = p.SHAPE_POLY_SET()
                    item.TransformShapeToPolygon(poly, layer, p.FromMM(CLEARANCE+task['width']/2), p.FromMM(.003), p.ERROR_OUTSIDE)
                    self.polygons[layer].append((poly, self.bounds(poly)))
                layer = p.F_Cu if item.IsOnLayer(p.F_Cu) else p.B_Cu
                poly = p.SHAPE_POLY_SET()
                item.TransformShapeToPolygon(poly, layer, p.FromMM(CLEARANCE+.3), p.FromMM(.003), p.ERROR_OUTSIDE)
                self.via_polygons.append((poly, self.bounds(poly)))
            elif isinstance(item, p.PAD):
                # New vias remain off pads, including same-net SMT pads.
                poly = p.SHAPE_POLY_SET()
                item.TransformShapeToPolygon(poly, p.F_Cu, p.FromMM(.31), p.FromMM(.003), p.ERROR_OUTSIDE)
                self.via_polygons.append((poly, self.bounds(poly)))
        self.existing_vias = [mmxy(t.GetPosition()) for t in board.GetTracks() if isinstance(t, p.PCB_VIA)]

    @staticmethod
    def bounds(poly):
        b = poly.BBox()
        return (b.GetLeft(), b.GetTop(), b.GetRight(), b.GetBottom())

    def valid_point(self, xy, layer):
        key = (*xy, layer)
        if key in self.cache: return self.cache[key]
        pt = point(xy)
        good = not any(x0 <= pt.x <= x1 and y0 <= pt.y <= y1 and poly.Collide(pt)
                       for poly, (x0,y0,x1,y1) in self.polygons[layer])
        if good:
            good = not any(z.IsOnLayer(layer) and z.GetDoNotAllowTracks() and
                           z.Outline().Collide(pt, p.FromMM(self.task['width']/2)) for z in self.keepouts)
        self.cache[key] = good
        return good

    def valid_edge(self, a, b, layer):
        key = (min(a,b), max(a,b), layer)
        if key in self.edge_cache: return self.edge_cache[key]
        pa, pb = point(a), point(b)
        shape = p.SHAPE_SEGMENT(pa, pb, 0)
        left, top, right, bottom = min(pa.x,pb.x), min(pa.y,pb.y), max(pa.x,pb.x), max(pa.y,pb.y)
        good = not any(not (x1<left or x0>right or y1<top or y0>bottom) and poly.Collide(shape)
                       for poly,(x0,y0,x1,y1) in self.polygons[layer])
        if good:
            expanded = p.SHAPE_SEGMENT(pa,pb,p.FromMM(self.task['width']))
            good = not any(z.IsOnLayer(layer) and z.GetDoNotAllowTracks() and z.Outline().Collide(expanded) for z in self.keepouts)
        self.edge_cache[key] = good
        return good

    def valid_via(self, xy):
        if xy in self.via_cache: return self.via_cache[xy]
        pt = point(xy)
        good = not any(x0<=pt.x<=x1 and y0<=pt.y<=y1 and poly.Collide(pt)
                       for poly,(x0,y0,x1,y1) in self.via_polygons)
        good = good and all(math.dist(xy, other) >= .8 for other in self.existing_vias)
        good = good and not any(z.GetDoNotAllowVias() and z.Outline().Collide(pt,p.FromMM(.3)) for z in self.keepouts)
        self.via_cache[xy] = good
        return good


def plan(board, task):
    pads = [q for f in board.GetFootprints() if f.GetReference()==task['ref'] for q in f.Pads() if q.GetNumber()==task['pin']]
    require(len(pads)==1 and pads[0].GetNetname()==task['net'], 'Source pad identity changed')
    pad = pads[0]
    targets = [t for t in board.GetTracks() if t.m_Uuid.AsString()==task['target_uuid']]
    require(len(targets)==1 and isinstance(targets[0],p.PCB_VIA) and targets[0].GetNetname()==task['net'] and mmxy(targets[0].GetPosition())==task['target'], 'Existing target via identity changed')
    obs = Obstacles(board,task)
    start, end = mmxy(pad.GetPosition()), task['target']
    lo_x,lo_y,hi_x,hi_y = task['roi']
    nx,ny = round((hi_x-lo_x)/STEP),round((hi_y-lo_y)/STEP)
    def coords(node): return (round(lo_x+node[0]*STEP,6),round(lo_y+node[1]*STEP,6))
    def heuristic(node):
        xy=coords(node);dx,dy=abs(end[0]-xy[0]),abs(end[1]-xy[1]);return max(dx,dy)+(math.sqrt(2)-1)*min(dx,dy)
    start_i,start_j=round((start[0]-lo_x)/STEP),round((start[1]-lo_y)/STEP)
    heap=[];cost={};previous={};counter=itertools.count();deadline=time.monotonic()+150
    for layer in LAYERS:
        n=(start_i,start_j,layer)
        if pad.IsOnLayer(layer) and obs.valid_point(coords(n),layer) and obs.valid_edge(start,coords(n),layer):
            cost[n]=0;previous[n]=None;heapq.heappush(heap,(heuristic(n),0,next(counter),n))
    require(heap, f"{task['net']}: no collision-free escape from source pad")
    moves=((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1))
    visited=0;found=None
    while heap:
        _,g,_,current=heapq.heappop(heap)
        if g!=cost.get(current): continue
        visited+=1
        require(time.monotonic()<deadline, f"{task['net']}: bounded local routing search timed out")
        here=coords(current);layer=current[2]
        if math.dist(here,end)<=STEP*1.5 and obs.valid_edge(here,end,layer):found=current;break
        neighbors=[]
        for di,dj in moves:
            n=(current[0]+di,current[1]+dj,layer)
            if 0<=n[0]<=nx and 0<=n[1]<=ny and obs.valid_point(coords(n),layer) and obs.valid_edge(here,coords(n),layer):
                neighbors.append((n,STEP*(math.sqrt(2) if di and dj else 1)))
        other=p.B_Cu if layer==p.F_Cu else p.F_Cu
        if obs.valid_point(here,other) and obs.valid_via(here): neighbors.append(((current[0],current[1],other),4.0))
        for n,increment in neighbors:
            new=g+increment
            if new+1e-10<cost.get(n,float('inf')):
                cost[n]=new;previous[n]=current;heapq.heappush(heap,(new+heuristic(n),new,next(counter),n))
    require(found is not None, f"{task['net']}: no route in the bounded region, visited {visited}; extent {min(n[0] for n in cost),min(n[1] for n in cost),max(n[0] for n in cost),max(n[1] for n in cost)}")
    nodes=[]
    while found is not None:nodes.append(found);found=previous[found]
    nodes.reverse()
    path=[(start,nodes[0][2])]+[(coords(n),n[2]) for n in nodes]+[(end,nodes[-1][2])]
    compact=[]
    for item in path:
        if compact and compact[-1]==item:continue
        while len(compact)>=2 and compact[-2][1]==compact[-1][1]==item[1]:
            a,b=compact[-2][0],compact[-1][0];c=item[0]
            if abs((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))>1e-8:break
            if not obs.valid_edge(a,c,item[1]):break
            compact.pop()
        compact.append(item)
    # Greedy visible-segment shortcut within a layer reduces grid staircases.
    smooth=[];i=0
    while i<len(compact):
        smooth.append(compact[i]);j=i+1
        while j<len(compact) and compact[j][1]==compact[i][1]:j+=1
        far=i+1
        for candidate in range(j-1,i,-1):
            if obs.valid_edge(compact[i][0],compact[candidate][0],compact[i][1]):far=candidate;break
        i=far
    return {'net':task['net'],'source':task['ref']+'.'+task['pin'],'target_via_uuid':task['target_uuid'],
            'width_mm':task['width'],'visited_nodes':visited,'path':[{'xy_mm':list(xy),'layer':p.LayerName(layer)} for xy,layer in smooth]}


def add(board, proposal):
    result=[];path=proposal['path'];net=board.FindNet(proposal['net'])
    for index,(a,b) in enumerate(zip(path,path[1:])):
        name=proposal['net']+'/'+str(index)+'/'+json.dumps([a,b],sort_keys=True)
        ident=str(uuid.uuid5(NAMESPACE,name))
        if a['layer']!=b['layer']:
            require(a['xy_mm']==b['xy_mm'],'Layer change lacks a shared via coordinate')
            item=p.PCB_VIA(board);item.SetPosition(point(a['xy_mm']));item.SetWidth(p.FromMM(.6));item.SetDrill(p.FromMM(.3));item.SetViaType(p.VIATYPE_THROUGH);item.SetLayerPair(p.F_Cu,p.B_Cu)
            item.SetFrontTentingMode(p.TENTING_MODE_TENTED);item.SetBackTentingMode(p.TENTING_MODE_TENTED)
            detail={'kind':'through_via','diameter_mm':.6,'drill_mm':.3,'xy_mm':a['xy_mm']}
        else:
            require(a['xy_mm']!=b['xy_mm'],'Zero-length track')
            item=p.PCB_TRACK(board);item.SetStart(point(a['xy_mm']));item.SetEnd(point(b['xy_mm']));item.SetWidth(p.FromMM(proposal['width_mm']));item.SetLayer(p.F_Cu if a['layer']=='F.Cu' else p.B_Cu)
            detail={'kind':'track','layer':a['layer'],'width_mm':proposal['width_mm'],'start_mm':a['xy_mm'],'end_mm':b['xy_mm']}
        item.SetUuid(p.KIID(ident));item.SetNet(net);board.Add(item);result.append({'uuid':ident,'net':proposal['net'],**detail})
    return result


def without_zone_fills(identity):
    return [[child for child in node if not (isinstance(child,list) and child and child[0]=='filled_polygon')] if isinstance(node,list) and node and node[0]=='zone' else node for node in identity]


def main(output, only=None):
    source=r.BOARD;digest=sha(source);require(digest==SOURCE_SHA,'Source board differs from reviewed repair revision')
    output=output.resolve();require(output.is_relative_to((r.ROOT/'reports').resolve()) and not output.exists(),'Use a new candidate folder under this project reports/')
    output.mkdir(parents=True);stage=output/'candidate';stage.mkdir();candidate=stage/source.name
    sources=[source,*[f for pattern in ('*.kicad_sch','*.kicad_pro','*.kicad_dru','*-lib-table') for f in source.parent.glob(pattern)]]
    hashes={str(f.relative_to(r.ROOT)):sha(f) for f in sources}
    for f in sources:
        if f.name.endswith('-lib-table'):(stage/f.name).write_text(f.read_text().replace('${KIPRJMOD}',source.parent.as_posix()),encoding='utf-8')
        else:shutil.copyfile(f,stage/f.name)
    board=p.LoadBoard(str(candidate));r.apply_project_netclasses(board,source.with_suffix('.kicad_pro'))
    before_identity,before_tracks=r.identity(board),track_state(board)
    handoff=json.loads((r.RUNS/'trial01/handoff.json').read_text());k.verify(board,handoff['reviewed_locked_seed'])
    proposals=[];added=[];removed={};removed_native=[]
    tasks=[task for task in TASKS if only is None or task['net']==only]
    if any(task['net']=='AMP_OUT5' for task in tasks):
        old_id='127e51c1-4dcf-4589-8924-637594cc2ed9'
        old=next(t for t in board.GetTracks() if t.m_Uuid.AsString()==old_id)
        require(old.GetNetname()=='AMP_OUT4' and old.GetLayer()==p.B_Cu and mmxy(old.GetStart())==(30.4795,20.5627) and mmxy(old.GetEnd())==(32.5332,20.5627), 'Dogleg source identity changed')
        removed[old_id]=before_tracks[old_id];removed_native.append(old);board.Remove(old)
        dogleg={'net':'AMP_OUT4','width_mm':.25,'reason':'Make room for AMP_OUT5 off-pad through via without moving components', 'path':[{'xy_mm':list(xy),'layer':'B.Cu'} for xy in ((30.4795,20.5627),(30.8168,20.9),(32.1959,20.9),(32.5332,20.5627))]}
        proposals.append(dogleg);added+=add(board,dogleg)
    short_file=r.ROOT/'reports/clock_short_path_proposed.json'
    short=json.loads(short_file.read_text()) if short_file.exists() and any(task['net']=='ADC_SCLK' for task in tasks) else None
    if short:
        for uid in short['removed']:
            old=next(t for t in board.GetTracks() if t.m_Uuid.AsString()==uid)
            require(old.GetNetname()=='ESP_MOSI' and old.GetLayer()==p.F_Cu, 'Clock escape source identity changed')
            removed[uid]=before_tracks[uid];removed_native.append(old);board.Remove(old)
        proposals.append(short['reroute']);added+=add(board,short['reroute'])
    for task in tasks:
        if task['net']=='AMP_OUT5':
            proposal=json.loads((r.ROOT/'reports/amp_path_proposed.json').read_text())
        else:
            frozen=r.ROOT/'reports/clock_path_proposed.json'
            proposal=short['path'] if short else (json.loads(frozen.read_text()) if frozen.exists() else plan(board,task))
        proposals.append(proposal);added+=add(board,proposal)
        print(json.dumps(proposal),flush=True)
    (output/'paths.json').write_text(json.dumps({'paths':proposals,'added':added,'removed':removed},indent=2)+'\n')
    require(r.identity(board)==before_identity,'Non-routing identity changed')
    after_tracks=track_state(board)
    require(all(after_tracks.get(uid)==value for uid,value in before_tracks.items() if uid not in removed),'Existing copper changed')
    require(set(after_tracks)-set(before_tracks)=={x['uuid'] for x in added},'Unexpected added copper')
    require(set(before_tracks)-set(after_tracks)==set(removed),'Unexpected removed copper')
    guard=k.verify(board,handoff['reviewed_locked_seed']);r.outer_tracks_only(board)
    require(p.SaveBoard(str(candidate),board,True),'Candidate save failed')
    reopened=p.LoadBoard(str(candidate));require(r.identity(reopened)==before_identity and track_state(reopened)==after_tracks,'Native round-trip changed identity/copper')
    guard=k.verify(reopened,handoff['reviewed_locked_seed'])
    command=[str(CLI),'pcb','drc','--format','json','--severity-all','--schematic-parity','--refill-zones','--save-board','--output',str(output/'native_drc.json'),str(candidate)]
    run=subprocess.run(command,text=True,capture_output=True,timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));require(run.returncode==0,'Native DRC command failed: '+run.stdout+run.stderr)
    drc=json.loads((output/'native_drc.json').read_text())
    filled=p.LoadBoard(str(candidate));require(without_zone_fills(r.identity(filled))==without_zone_fills(before_identity) and track_state(filled)==after_tracks,'Native refill changed non-fill identity or routing');guard=k.verify(filled,handoff['reviewed_locked_seed'])
    remaining=[x for x in drc['unconnected_items'] if any('['+net+']' in item.get('description','') for item in x['items'] for net in [task['net'] for task in tasks])]
    require(all(sha(r.ROOT/name)==value for name,value in hashes.items()),'Main inputs changed during copy repair')
    report={'status':'SIGNAL_COPY_PASS' if not drc['violations'] and not drc['schematic_parity'] and not remaining else 'SIGNAL_COPY_REQUIRES_REVIEW','source_sha256':digest,'candidate_sha256':sha(candidate),'input_sha256':hashes,'tool_sha256':sha(__file__),'candidate':str(candidate.relative_to(r.ROOT)),'paths':proposals,'added':added,'removed':removed,'protected_kelvin':guard,'existing_copper_except_listed_removed_unchanged':True,'nonrouting_identity_unchanged':True,'native_command':command,'native_returncode':run.returncode,'native_stdout':run.stdout,'violation_count':len(drc['violations']),'parity_count':len(drc['schematic_parity']),'unconnected_count':len(drc['unconnected_items']),'target_signals_remaining':remaining,'drc_sha256':sha(output/'native_drc.json')}
    (output/'signal_repairs.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps({key:report[key] for key in ('status','candidate','violation_count','unconnected_count')}),flush=True)
    require(report['status']=='SIGNAL_COPY_PASS','Inspect candidate native DRC failures; main board was not changed')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--only',choices=['ADC_SCLK','AMP_OUT5']);args=parser.parse_args();main(args.output,args.only)
