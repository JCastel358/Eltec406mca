"""Read-only routed signal/reference and local bypass-access audit."""
import argparse
import collections
import hashlib
import heapq
import json
import math
from pathlib import Path
import re
import pcbnew as p

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'single_detector_usb_master/single_detector_usb_master.kicad_pcb'
OUT=ROOT/'reports/copper_review'
STEP=.1
def mm(v): return p.ToMM(v)
def xy(v): return (mm(v.x),mm(v.y))
def pt(v): return p.VECTOR2I(p.FromMM(v[0]),p.FromMM(v[1]))
def uid(item): return item.m_Uuid.AsString()
def distance(q,a,b):
    d=(b[0]-a[0],b[1]-a[1]); denom=d[0]**2+d[1]**2
    t=max(0,min(1,((q[0]-a[0])*d[0]+(q[1]-a[1])*d[1])/denom)) if denom else 0
    return math.dist(q,(a[0]+d[0]*t,a[1]+d[1]*t))
def net_kind(net):
    if any(x in net for x in ('SCLK','MOSI','MISO','CS_N','CS_DRV','DRDY','PDWN')): return 'SPI/control'
    if re.fullmatch(r'(SENSOR[0-5]|AMP_(IN|FB|OUT|SERIES)[0-5]|ADC_AIN[0-5]|CLAMP_GND[0-5])',net): return 'analog'
    return None
def describe(item):
    if isinstance(item,p.PAD): return f'{item.GetParentFootprint().GetReference()}.{item.GetNumber()}'
    if isinstance(item,p.PCB_VIA): return 'via '+uid(item)
    return 'track '+uid(item)

def main():
    global SOURCE,OUT
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=SOURCE)
    parser.add_argument('--output-dir',type=Path,default=OUT)
    args=parser.parse_args();SOURCE=args.source.resolve();OUT=args.output_dir.resolve()
    OUT.mkdir(exist_ok=True,parents=True)
    raw=SOURCE.read_bytes(); digest=hashlib.sha256(raw).hexdigest()
    (OUT/'source_snapshot.kicad_pcb').write_bytes(raw)
    board=p.LoadBoard(str(OUT/'source_snapshot.kicad_pcb'))
    board.BuildConnectivity(); conn=board.GetConnectivity()
    tracks=list(board.GetTracks())
    nodes={uid(t):t for t in tracks}
    nodes.update({uid(q):q for f in board.GetFootprints() for q in f.Pads()})
    planes={layer:[] for layer in (p.In1_Cu,p.In2_Cu)}
    for z in board.Zones():
        if z.GetIsRuleArea(): continue
        for layer in planes:
            if z.IsOnLayer(layer): planes[layer].append((str(z.GetNetname()),z.GetFilledPolysList(layer)))
    keepouts=[z.Outline() for z in board.Zones() if z.GetIsRuleArea() and z.GetDoNotAllowTracks()]
    def reference(q,layer):
        if any(k.Contains(pt(q)) for k in keepouts): return 'antenna_keepout'
        for name,poly in planes[layer]:
            if poly.Contains(pt(q)): return name
        return 'void'
    power=[t for t in tracks if not isinstance(t,(p.PCB_VIA,p.PCB_ARC)) and
           re.fullmatch(r'EM_(REG|BAT_SW|BAT_RAW|BAT_FUSED|BAT_PROTECTED)|EMIT[1-3]_LOW',str(t.GetNetname()))]
    selected=[t for t in tracks if not isinstance(t,(p.PCB_VIA,p.PCB_ARC)) and net_kind(str(t.GetNetname()))]
    rows=[]; summary=collections.defaultdict(lambda:collections.Counter())
    proximity=[]
    for t in selected:
        name=str(t.GetNetname()); layer=t.GetLayer()
        if layer not in (p.F_Cu,p.B_Cu): raise RuntimeError('Unexpected internal signal track')
        ref_layer=p.In1_Cu if layer==p.F_Cu else p.In2_Cu
        a,b=xy(t.GetStart()),xy(t.GetEnd()); length=math.dist(a,b)
        n=max(1,math.ceil(length/STEP)); ds=length/n
        samples=[(a[0]+(i+.5)/n*(b[0]-a[0]),a[1]+(i+.5)/n*(b[1]-a[1])) for i in range(n)]
        classes=[reference(q,ref_layer) for q in samples]
        for cls in classes: summary[name][cls]+=ds
        summary[name]['total_mm']+=length
        start=0
        for i in range(1,n+1):
            if i<n and classes[i]==classes[start]: continue
            cls=classes[start]
            if cls!='GND' and (i-start)*ds>=.3:
                rows.append({'net':name,'kind':net_kind(name),'track_uuid':uid(t),'layer':board.GetLayerName(layer),
                             'reference':cls,'span_mm':round((i-start)*ds,3),
                             'from_mm':[round(a[j]+start/n*(b[j]-a[j]),4) for j in (0,1)],
                             'to_mm':[round(a[j]+i/n*(b[j]-a[j]),4) for j in (0,1)]})
            start=i
        near=[None]*n
        for other in power:
            if other.GetLayer()==layer: continue
            c,d=xy(other.GetStart()),xy(other.GetEnd())
            x0,x1=min(a[0],b[0])-1.5,max(a[0],b[0])+1.5
            y0,y1=min(a[1],b[1])-1.5,max(a[1],b[1])+1.5
            if max(c[0],d[0])<x0 or min(c[0],d[0])>x1 or max(c[1],d[1])<y0 or min(c[1],d[1])>y1: continue
            projected_overlap=(mm(t.GetWidth())+mm(other.GetWidth()))/2
            for i,q in enumerate(samples):
                separation=distance(q,c,d)
                if separation<=projected_overlap:
                    proximity.append({'signal':name,'signal_layer':board.GetLayerName(layer),'signal_uuid':uid(t),
                                      'power':str(other.GetNetname()),'power_uuid':uid(other),'xy_mm':[round(v,3) for v in q],
                                      'reference':classes[i],'kind':'opposite-layer copper projection overlap'})
                if separation<1.0 and near[i] is None: near[i]=str(other.GetNetname())
        run=0
        for i in range(n+1):
            if i<n and near[i] is not None: run+=1; continue
            if run*ds>=5:
                proximity.append({'signal':name,'signal_layer':board.GetLayerName(layer),'signal_uuid':uid(t),
                                  'span_mm':round(run*ds,3),'from_mm':list(samples[i-run]),'to_mm':list(samples[i-1]),
                                  'kind':'over 5 mm within 1 mm of opposite-layer emitter power',
                                  'power_nets':sorted({v for v in near[i-run:i] if v})})
            run=0
    # Connected surface-track traversal excludes zones, so a remote same-net
    # plane via is not mistaken for a short local bypass connection.
    def local_access(pad):
        pq=[(0,uid(pad),pad,[])]; best={uid(pad):0}; found=[]
        while pq and len(found)<4:
            cost,_,item,path=heapq.heappop(pq)
            if cost>best[uid(item)]: continue
            if item is not pad and (isinstance(item,p.PCB_VIA) or isinstance(item,p.PAD) and item.GetAttribute()==p.PAD_ATTRIB_PTH):
                pos=xy(item.GetPosition())
                found.append({'terminal':describe(item),'xy_mm':list(pos),'surface_track_length_upper_bound_mm':round(cost,4),
                              'reference_In1':reference(pos,p.In1_Cu),'reference_In2':reference(pos,p.In2_Cu),'path':path})
                continue
            # TRACKS_VEC presents a via as its PCB_TRACK base class. Resolve
            # native UUIDs to the concrete board wrappers before class tests.
            neighbors=[nodes[uid(v)] for v in list(conn.GetConnectedTracks(item))+list(conn.GetConnectedPads(item))]
            for v in neighbors:
                if str(v.GetNetname())!=str(pad.GetNetname()): continue
                weight=0 if isinstance(v,(p.PAD,p.PCB_VIA)) else mm(v.GetLength())
                candidate=cost+weight
                if candidate<best.get(uid(v),math.inf):
                    best[uid(v)]=candidate
                    heapq.heappush(pq,(candidate,uid(v),v,path+[describe(v)]))
        return found
    bypass=[]
    for fp in board.GetFootprints():
        if fp.GetReference() not in ('C11','C13','C39','C36'): continue
        pad=next(q for q in fp.Pads() if q.GetNumber()=='2')
        bypass.append({'reference':fp.GetReference(),'ground_pad_mm':list(xy(pad.GetPosition())),
                       'ground_net':str(pad.GetNetname()),'local_through_connections':local_access(pad)})
    result={'source_sha256':digest,'source_board':str(SOURCE),'sampling_step_max_mm':STEP,
            'non_GND_reference_runs':sorted(rows,key=lambda r:-r['span_mm']),
            'per_net_reference_length_mm':{k:{a:round(b,3) for a,b in v.items()} for k,v in summary.items()},
            'emitter_power_projection_checks':proximity,'bypass_ground_access':bypass,
            'limits':['Centreline sampling bounds locations to about 0.1 mm; pad/via antipads can produce short intentional reference voids.',
                      'Via-to-plane reference check is at its centre; native DRC/connectivity remains authoritative for connection.',
                      'Track-path lengths sum complete touched segments and include copper inside pads, so they are conservative upper bounds.',
                      'No impedance, field, thermal or measured noise qualification is performed.']}
    (OUT/'copper_review.json').write_text(json.dumps(result,indent=2)+'\n')
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest()==digest,'Source changed during read-only audit; results apply only to snapshot'
    print(json.dumps({'source_sha256':digest,'selected_tracks':len(selected),'non_GND_runs':len(rows),'over_2mm':[r for r in rows if r['span_mm']>=2],
                      'projection_hits':len(proximity),'bypass_ground_access':bypass},indent=2))

if __name__=='__main__': main()
