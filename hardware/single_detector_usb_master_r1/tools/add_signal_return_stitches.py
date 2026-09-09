"""Copy-only CLI / additive helper for three signal return-plane stitches."""
import argparse
import json
import math
from pathlib import Path
import shutil
import uuid
import pcbnew as p
from add_bypass_stitches import geometry_without_fill,sha
from add_reference_planes import point,xy,box,native_drc
from route_board import outer_tracks_only,apply_project_netclasses
import protected_kelvin

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'single_detector_usb_master/single_detector_usb_master.kicad_pcb'
NAMESPACE=uuid.UUID('6caa2310-739e-4edf-bf13-cc942c4a5df8')
SITES=[{'name':'ADC_SCLK_RETURN','xy_mm':[55.9,92.25],'signals':[('ADC_SCLK',[55.8,91.25])]},
       {'name':'AMP_OUT5_RETURN_A','xy_mm':[28.65,19.55],'signals':[('AMP_OUT5',[31.35,20.25])]},
       {'name':'AMP_OUT5_RETURN_BC','xy_mm':[32.6,24.5],'signals':[('AMP_OUT5',[32.5,22.5]),('AMP_OUT5',[31.7,24.4])]}]
def identifier(name):return str(uuid.uuid5(NAMESPACE,name+'/v1'))

def check_site(board,row):
    q=row['xy_mm'];pos=point(*q);fail=[]
    tracks=list(board.GetTracks())
    for net,origin in row['signals']:
        if not any(isinstance(t,p.PCB_VIA) and t.GetNetname()==net and tuple(xy(t.GetPosition()))==tuple(origin) for t in tracks):
            fail.append('reviewed signal-via anchor changed')
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if not(pad.IsOnLayer(p.F_Cu) or pad.IsOnLayer(p.B_Cu)):continue
            b=box(pad)
            if not b[0]-.6<=q[0]<=b[2]+.6 or not b[1]-.6<=q[1]<=b[3]+.6:continue
            poly=p.SHAPE_POLY_SET();pad.TransformShapeToPolygon(poly,p.F_Cu,p.FromMM(.6),p.FromMM(.005),p.ERROR_OUTSIDE)
            if poly.Contains(pos):fail.append('pad-to-annulus clearance: '+fp.GetReference()+'.'+pad.GetNumber())
    for t in tracks:
        if isinstance(t,p.PCB_VIA) and math.dist(q,xy(t.GetPosition()))<.3+p.ToMM(t.GetDrillValue())/2+.25:
            fail.append('existing via drill clearance: '+t.m_Uuid.AsString())
        if t.GetNetname()=='GND':continue
        b=box(t)
        if not b[0]-.55<=q[0]<=b[2]+.55 or not b[1]-.55<=q[1]<=b[3]+.55:continue
        poly=p.SHAPE_POLY_SET();t.TransformShapeToPolygon(poly,p.F_Cu if isinstance(t,p.PCB_VIA) else t.GetLayer(),p.FromMM(.55),p.FromMM(.005),p.ERROR_OUTSIDE)
        if poly.Contains(pos):fail.append('foreign outer copper: '+t.GetNetname())
    for zone in board.Zones():
        if zone.GetIsRuleArea() and zone.GetDoNotAllowVias() and zone.Outline().Collide(pos,p.FromMM(.3)):
            fail.append('via keepout')
    samples=[q]+[[q[0]+.3*math.cos(i*math.pi/8),q[1]+.3*math.sin(i*math.pi/8)] for i in range(16)]
    for layer in (p.In1_Cu,p.In2_Cu):
        polys=[z.GetFilledPolysList(layer) for z in board.Zones() if not z.GetIsRuleArea() and z.GetNetname()=='GND' and z.IsOnLayer(layer)]
        if not all(any(poly.Contains(point(*s)) for poly in polys) for s in samples):fail.append('not inside actual filled GND on '+board.GetLayerName(layer))
    if fail:raise RuntimeError(row['name']+': '+', '.join(fail))
    return {**row,'uuid':identifier(row['name']),'net':'GND','diameter_mm':.6,'drill_mm':.3,
            'signal_via_distances_mm':[round(math.dist(q,origin),4) for _,origin in row['signals']],
            'no_surface_stub':True,'pad_to_annulus_clearance_min_mm':.3,'foreign_copper_clearance_min_mm':.25}

def add_return_stitches(board):
    """Add only three reviewed GND through vias; caller performs refill/save."""
    outer_tracks_only(board)
    expected={identifier(r['name']) for r in SITES};tracks={t.m_Uuid.AsString():t for t in board.GetTracks()}
    present=expected.intersection(tracks)
    if present:
        if present!=expected:raise RuntimeError('Partial return-stitch set; refusing an ambiguous repair')
        for row in SITES:
            t=tracks[identifier(row['name'])]
            assert isinstance(t,p.PCB_VIA) and t.GetViaType()==p.VIATYPE_THROUGH
            assert t.GetNetname()=='GND' and tuple(xy(t.GetPosition()))==tuple(row['xy_mm'])
            assert t.GetWidth(p.F_Cu)==p.FromMM(.6) and t.GetDrillValue()==p.FromMM(.3)
        return {'status':'EXACT_RETURN_STITCHES_ALREADY_PRESENT','added_uuids':[]}
    rows=[check_site(board,row) for row in SITES]
    baseline=geometry_without_fill(board,set())
    for row in rows:
        via=p.PCB_VIA(board);via.SetUuid(p.KIID(row['uuid']));via.SetPosition(point(*row['xy_mm']))
        via.SetWidth(p.FromMM(.6));via.SetDrill(p.FromMM(.3));via.SetViaType(p.VIATYPE_THROUGH);via.SetLayerPair(p.F_Cu,p.B_Cu)
        via.SetNet(board.FindNet('GND'));via.SetFrontTentingMode(p.TENTING_MODE_TENTED);via.SetBackTentingMode(p.TENTING_MODE_TENTED)
        board.Add(via)
    assert geometry_without_fill(board,expected)==baseline
    return {'status':'THREE_SIGNAL_RETURN_STITCHES_ADDED','added_uuids':sorted(expected),'vias':rows,
            'added_through_vias':3,'added_tracks':0,'existing_native_geometry_preserved':True}

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--source',type=Path,default=SOURCE)
    ap.add_argument('--output-dir',type=Path,default=ROOT/'reports/signal_return_stitches');args=ap.parse_args()
    source=args.source.resolve();output=args.output_dir.resolve();initial=sha(source)
    output.mkdir(parents=True,exist_ok=True);stage=output/'candidate';stage.mkdir(exist_ok=True);candidate=stage/source.name
    if candidate.resolve()==source:raise RuntimeError('Copy cannot overwrite source')
    for pattern in ('*.kicad_sch','*.kicad_pro','*.kicad_dru','*-lib-table'):
        for path in source.parent.glob(pattern):
            target=stage/path.name
            if path.name.endswith('-lib-table'):target.write_text(path.read_text().replace('${KIPRJMOD}',source.parent.as_posix()))
            else:shutil.copyfile(path,target)
    shutil.copyfile(source,candidate);before=native_drc(candidate,output/'before_drc.json')
    board=p.LoadBoard(str(candidate));apply_project_netclasses(board,source.with_suffix('.kicad_pro'))
    baseline=geometry_without_fill(board,set());result=add_return_stitches(board);added=set(result['added_uuids'])
    board.BuildConnectivity();assert p.ZONE_FILLER(board).Fill(board.Zones());outer_tracks_only(board)
    assert geometry_without_fill(board,added)==baseline
    spec=json.loads((ROOT/'reports/routing/trial01/handoff.json').read_text())['reviewed_locked_seed']
    kelvin=protected_kelvin.verify(board,spec);assert p.SaveBoard(str(candidate),board,True)
    reloaded=p.LoadBoard(str(candidate));assert geometry_without_fill(reloaded,added)==baseline
    assert protected_kelvin.verify(reloaded,spec)==kelvin
    after=native_drc(candidate,output/'after_drc.json');assert sha(source)==initial
    result.update(source=str(source),source_sha256=initial,candidate=str(candidate),candidate_sha256=sha(candidate),
                  before_drc=before,after_drc=after,protected_kelvin=kelvin,main_board_untouched=True)
    (output/'validation.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))

if __name__=='__main__':main()
