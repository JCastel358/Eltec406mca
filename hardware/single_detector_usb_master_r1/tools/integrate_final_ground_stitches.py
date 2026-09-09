"""Integrate reviewed GND-only return improvements with exact native guards."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import pcbnew as p
import route_board as r
import protected_kelvin as k
from add_bypass_stitches import geometry_without_fill
from add_final_power_ground_stitches import add_stitches
from add_signal_return_stitches import add_return_stitches


def main():
    folder=r.ROOT/'reports/final_ground_integration';assert not folder.exists()
    reports=[r.ROOT/'reports/power_review_final/ground_candidate_v3/validation.json',
             r.ROOT/'reports/signal_return_stitches/validation.json']
    source_sha=r.sha(r.BOARD)
    for file in reports:
        report=json.loads(file.read_text());assert report['source_sha256']==source_sha
    inputs=[r.BOARD,*reports,Path(__file__),r.ROOT/'tools/add_final_power_ground_stitches.py',
            r.ROOT/'tools/add_signal_return_stitches.py',r.ROOT/'tools/protected_kelvin.py']
    inputs += [f for pattern in ('*.kicad_sch','*.kicad_pro','*.kicad_dru','*-lib-table') for f in r.PROJECT.glob(pattern)]
    hashes={str(f.relative_to(r.ROOT)):r.sha(f) for f in inputs}
    folder.mkdir();stage=folder/'candidate';stage.mkdir()
    for f in inputs:
        if f.parent!=r.PROJECT:continue
        target=stage/f.name
        if f.name.endswith('-lib-table'):target.write_text(f.read_text().replace('${KIPRJMOD}',r.PROJECT.as_posix()))
        else:shutil.copyfile(f,target)
    candidate=stage/r.BOARD.name;board=p.LoadBoard(str(candidate));r.apply_project_netclasses(board)
    baseline=geometry_without_fill(board,set());old={t.m_Uuid.AsString() for t in board.GetTracks()}
    power=add_stitches(board);signals=add_return_stitches(board)
    added={t.m_Uuid.AsString() for t in board.GetTracks()}-old
    assert len(added)==7
    assert all(t.GetNetname()=='GND' for t in board.GetTracks() if t.m_Uuid.AsString() in added)
    assert geometry_without_fill(board,added)==baseline
    assert p.SaveBoard(str(candidate),board,True)
    command=[str(Path(sys.executable).with_name('kicad-cli.exe')),'pcb','drc','--format','json',
             '--severity-all','--schematic-parity','--refill-zones','--save-board','--exit-code-violations',
             '--output',str(folder/'native_drc.json'),str(candidate)]
    run=subprocess.run(command,capture_output=True,text=True,timeout=240)
    assert run.returncode==0,run.stdout+run.stderr
    drc=json.loads((folder/'native_drc.json').read_text())
    assert all(not drc[key] for key in ('violations','unconnected_items','schematic_parity'))
    filled=p.LoadBoard(str(candidate));assert geometry_without_fill(filled,added)==baseline
    assert r.connectivity(filled)==0;r.outer_tracks_only(filled)
    handoff=json.loads((r.RUNS/'trial01/handoff.json').read_text())
    guard=k.verify(filled,handoff['reviewed_locked_seed'])
    assert all(r.sha(r.ROOT/name)==digest for name,digest in hashes.items())
    backup=folder/'before_integration.kicad_pcb';shutil.copyfile(r.BOARD,backup)
    target=r.BOARD.with_suffix('.ground-stage.kicad_pcb');assert not target.exists()
    shutil.copyfile(candidate,target);assert r.sha(target)==r.sha(candidate)
    assert r.sha(r.BOARD)==source_sha;os.replace(target,r.BOARD)
    report={'status':'FINAL_GROUND_IMPROVEMENTS_INTEGRATED_NATIVE_DRC_PASS','input_sha256':hashes,
            'board_sha256':r.sha(r.BOARD),'added_uuids':sorted(added),'power':power,'signals':signals,
            'all_preexisting_geometry_preserved':True,'zones_refilled':True,'violations':0,'unconnected':0,
            'parity':0,'protected_kelvin':guard,'command':command,'stdout':run.stdout,'stderr':run.stderr,
            'drc_sha256':r.sha(folder/'native_drc.json')}
    (folder/'integration.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({key:report[key] for key in ('status','board_sha256','violations','unconnected','parity')}))

if __name__=='__main__':main()
