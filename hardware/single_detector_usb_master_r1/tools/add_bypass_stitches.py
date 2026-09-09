"""Add three reviewed local bypass-ground stitches; standalone use is copy-only.

add_stitches(board) adds only three through vias and three F.Cu GND stubs.
It does not fill, save, change existing routes, or edit project data. The CLI
copies the complete review project, fills zones, and runs DRC and Kelvin checks.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import uuid
import pcbnew as p
from add_reference_planes import local_ground_stitch, structural_identity, native_drc, point, xy
from route_board import outer_tracks_only, apply_project_netclasses
import protected_kelvin

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'single_detector_usb_master/single_detector_usb_master.kicad_pcb'
NAMESPACE=uuid.UUID('f5280368-2293-41d6-b873-0d0469d938f1')
SITES={'C13':([39.975,-13.5],[38.775,-13.5]),
       'C39':([44.975,-9.0],[43.775,-9.0]),
       'C36':([29.5,-9.475],[29.5,-10.675])}
def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def identifier(ref,kind): return str(uuid.uuid5(NAMESPACE,f'{ref}.2/{kind}/v1'))
def geometry_without_fill(board,added):
    tree=structural_identity(board,added)
    def visit(node):
        if not isinstance(node,list): return node
        if node and node[0]=='zone':
            node=[v for v in node if not (isinstance(v,list) and v and v[0] in ('filled_polygon','fill_segments'))]
        return [visit(v) for v in node]
    return visit(tree)

def add_stitches(board):
    """Validate exact reviewed geometry, then add all six fixed-UUID items."""
    outer_tracks_only(board)
    expected={identifier(ref,kind) for ref in SITES for kind in ('via','stub')}
    tracks={t.m_Uuid.AsString():t for t in board.GetTracks()}
    present=expected.intersection(tracks)
    if present:
        if present!=expected: raise RuntimeError('Only part of the bypass-stitch set is present; refusing ambiguous repair')
        for ref,(start,end) in SITES.items():
            v,t=tracks[identifier(ref,'via')],tracks[identifier(ref,'stub')]
            assert isinstance(v,p.PCB_VIA) and tuple(xy(v.GetPosition()))==tuple(end)
            assert v.GetViaType()==p.VIATYPE_THROUGH
            assert v.GetWidth(p.F_Cu)==p.FromMM(.6) and v.GetDrillValue()==p.FromMM(.3)
            assert tuple(xy(t.GetStart()))==tuple(start) and tuple(xy(t.GetEnd()))==tuple(end)
            assert t.GetWidth()==p.FromMM(.6) and t.GetLayer()==p.F_Cu
            assert v.GetNetname()==t.GetNetname()=='GND'
        return {'status':'EXACT_STITCH_SET_ALREADY_PRESENT','added_uuids':[],'expected_uuids':sorted(expected)}
    fp={f.GetReference():f for f in board.GetFootprints()}
    bounds=board.GetBoardEdgesBoundingBox()
    region=[[p.ToMM(bounds.GetLeft()),p.ToMM(bounds.GetTop())],
            [p.ToMM(bounds.GetRight()),p.ToMM(bounds.GetTop())],
            [p.ToMM(bounds.GetRight()),p.ToMM(bounds.GetBottom())],
            [p.ToMM(bounds.GetLeft()),p.ToMM(bounds.GetBottom())]]
    grounds={layer:[z.GetFilledPolysList(layer) for z in board.Zones()
                    if not z.GetIsRuleArea() and z.GetNetname()=='GND' and z.IsOnLayer(layer)]
             for layer in (p.In1_Cu,p.In2_Cu)}
    rows=[]
    for ref,(start,end) in SITES.items():
        pad=next(q for q in fp[ref].Pads() if q.GetNumber()=='2' and q.GetNetname()=='GND')
        if tuple(xy(pad.GetPosition()))!=tuple(start): raise RuntimeError(f'{ref} placement changed; re-review this local bypass connection')
        if not all(any(poly.Contains(point(*end)) for poly in grounds[layer]) for layer in grounds):
            raise RuntimeError(f'{ref} via site is not over filled GND on both internal layers')
        row=local_ground_stitch(board,region,pad,ref+'.2',net='GND',width=.6,
                                skip_existing_region=False,candidates=[end],required=True)
        row.update(via_uuid=identifier(ref,'via'),stub_uuid=identifier(ref,'stub'))
        rows.append(row)
    baseline=geometry_without_fill(board,set())
    for row in rows:
        via=p.PCB_VIA(board)
        via.SetUuid(p.KIID(row['via_uuid']));via.SetPosition(point(*row['xy_mm']))
        via.SetWidth(p.FromMM(.6));via.SetDrill(p.FromMM(.3))
        via.SetViaType(p.VIATYPE_THROUGH);via.SetLayerPair(p.F_Cu,p.B_Cu)
        via.SetNet(board.FindNet('GND'))
        via.SetFrontTentingMode(p.TENTING_MODE_TENTED);via.SetBackTentingMode(p.TENTING_MODE_TENTED)
        board.Add(via)
        stub=p.PCB_TRACK(board)
        stub.SetUuid(p.KIID(row['stub_uuid']))
        stub.SetStart(point(*row['ground_stub']['from_mm']));stub.SetEnd(point(*row['ground_stub']['to_mm']))
        stub.SetWidth(p.FromMM(.6));stub.SetLayer(p.F_Cu);stub.SetNet(board.FindNet('GND'))
        board.Add(stub)
    assert geometry_without_fill(board,expected)==baseline,'Existing native geometry changed'
    return {'status':'THREE_LOCAL_BYPASS_STITCHES_ADDED','added_uuids':sorted(expected),
            'added_through_vias':3,'added_front_ground_stubs':3,'stitches':rows,
            'existing_native_geometry_preserved':True}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=SOURCE)
    parser.add_argument('--output-dir',type=Path,default=ROOT/'reports/bypass_stitches')
    args=parser.parse_args();source=args.source.resolve();output=args.output_dir.resolve()
    initial=sha(source);output.mkdir(parents=True,exist_ok=True);stage=output/'candidate';stage.mkdir(exist_ok=True)
    candidate=stage/source.name
    if candidate.resolve()==source: raise RuntimeError('Review output cannot overwrite source')
    for pattern in ('*.kicad_sch','*.kicad_pro','*.kicad_dru','*-lib-table'):
        for path in source.parent.glob(pattern):
            dest=stage/path.name
            if path.name.endswith('-lib-table'):dest.write_text(path.read_text().replace('${KIPRJMOD}',source.parent.as_posix()))
            else:shutil.copyfile(path,dest)
    shutil.copyfile(source,candidate)
    before=native_drc(candidate,output/'before_drc.json')
    board=p.LoadBoard(str(candidate));apply_project_netclasses(board,source.with_suffix('.kicad_pro'))
    baseline=geometry_without_fill(board,set())
    report=add_stitches(board);added=set(report['added_uuids'])
    board.BuildConnectivity();assert p.ZONE_FILLER(board).Fill(board.Zones())
    assert geometry_without_fill(board,added)==baseline
    outer_tracks_only(board)
    spec=json.loads((ROOT/'reports/routing/trial01/handoff.json').read_text())['reviewed_locked_seed']
    kelvin=protected_kelvin.verify(board,spec)
    assert p.SaveBoard(str(candidate),board,True)
    reloaded=p.LoadBoard(str(candidate));assert geometry_without_fill(reloaded,added)==baseline
    assert protected_kelvin.verify(reloaded,spec)==kelvin
    after=native_drc(candidate,output/'after_drc.json')
    assert sha(source)==initial,'Source changed during copy review'
    report.update(source=str(source),source_sha256=initial,candidate=str(candidate),candidate_sha256=sha(candidate),
                  before_drc=before,after_drc=after,protected_kelvin=kelvin,main_board_untouched=True)
    (output/'validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
