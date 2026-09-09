"""Combine reviewed power/bypass repairs on a project copy, never the main PCB."""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
import pcbnew as p
import route_board as r
import protected_kelvin as k
from add_power_repairs import add_repairs
from add_reviewed_power_stubs import add_out_stub
from add_bypass_stitches import add_stitches, geometry_without_fill


def main(name):
    folder = (r.ROOT/'reports'/name).resolve()
    assert folder.parent == (r.ROOT/'reports').resolve() and not folder.exists()
    folder.mkdir()
    stage = folder/'candidate'
    stage.mkdir()
    source_files = [r.BOARD,*[f for pattern in ('*.kicad_sch','*.kicad_pro','*.kicad_dru','*-lib-table')
                                  for f in r.PROJECT.glob(pattern)],
                    *[r.ROOT/'tools'/f for f in ('prepare_combined_repairs.py','add_power_repairs.py',
                       'add_reviewed_power_stubs.py','add_bypass_stitches.py','protected_kelvin.py')]]
    hashes = {str(f.relative_to(r.ROOT)):r.sha(f) for f in source_files}
    for f in source_files:
        if f.parent != r.PROJECT:continue
        target = stage/f.name
        if f.name.endswith('-lib-table'):
            target.write_text(f.read_text().replace('${KIPRJMOD}',r.PROJECT.as_posix()),encoding='utf-8')
        else:shutil.copyfile(f,target)
    candidate = stage/r.BOARD.name
    board = p.LoadBoard(str(candidate))
    r.apply_project_netclasses(board)
    before = geometry_without_fill(board,set())
    previous = {t.m_Uuid.AsString() for t in board.GetTracks()}
    assert r.connectivity(board)==5
    power = add_repairs(board)
    out = add_out_stub(board)
    bypass = add_stitches(board)
    added = {t.m_Uuid.AsString() for t in board.GetTracks()}-previous
    assert geometry_without_fill(board,added)==before
    assert r.connectivity(board)==2
    assert p.SaveBoard(str(candidate),board,True)
    command=[str(Path(sys.executable).with_name('kicad-cli.exe')),'pcb','drc',
             '--format','json','--severity-all','--schematic-parity','--refill-zones','--save-board',
             '--output',str(folder/'native_drc.json'),str(candidate)]
    run=subprocess.run(command,text=True,capture_output=True,timeout=240)
    assert run.returncode==0, run.stdout+run.stderr
    drc=json.loads((folder/'native_drc.json').read_text())
    assert not drc['violations'] and not drc['schematic_parity'], drc['violations']
    assert len(drc['unconnected_items'])==2
    reopened=p.LoadBoard(str(candidate))
    assert geometry_without_fill(reopened,added)==before
    handoff=json.loads((r.RUNS/'trial01/handoff.json').read_text())
    guard=k.verify(reopened,handoff['reviewed_locked_seed'])
    assert all(r.sha(r.ROOT/name)==digest for name,digest in hashes.items())
    report={'status':'POWER_AND_BYPASS_INTEGRATION_COPY_PASS_TWO_SIGNAL_OPENS',
            'input_sha256':hashes,'candidate':str(candidate.relative_to(r.ROOT)),
            'candidate_sha256':r.sha(candidate),'added_uuids':sorted(added),
            'power_repairs':power,'regulator_output_stub':out,'bypass_stitches':bypass,
            'native_drc_command':command,'native_stdout':run.stdout,'native_stderr':run.stderr,
            'drc_sha256':r.sha(folder/'native_drc.json'),'protected_kelvin':guard,
            'preexisting_geometry_preserved':True,'only_zone_fill_changed_after_additions':True,
            'unconnected_count':2,'main_unchanged':True}
    (folder/'integration_copy.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'candidate':report['candidate'],'sha256':report['candidate_sha256'],
                      'violations':0,'unconnected':2,'parity':0}))


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('name')
    main(ap.parse_args().name)
