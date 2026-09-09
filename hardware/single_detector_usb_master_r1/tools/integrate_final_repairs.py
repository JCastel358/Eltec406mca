"""Merge explicit, independently reviewed route repairs and install only a clean board.

Inputs are frozen reports, not a fresh autorouter run. The only non-copper edit
updates the obsolete UNROUTED title/annotation to PRELIMINARY REVIEW ONLY.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import pcbnew as p
import route_board as r
import protected_kelvin as k
from add_signal_repairs import add, without_zone_fills
from repair_track_width_quantization import track_state


def normalized(value):
    return json.loads(json.dumps(value))


def main():
    folder = r.ROOT/'reports/final_route_integration'
    assert not folder.exists(), 'Use the preserved integration report; do not overwrite it'
    signal_file = r.ROOT/'reports/signal_repairs_trial01_f/signal_repairs.json'
    power_file = r.ROOT/'reports/power_bypass_combined/integration_copy.json'
    signal, power = (json.loads(f.read_text()) for f in (signal_file,power_file))
    assert signal['status']=='SIGNAL_COPY_PASS'
    assert r.sha(r.BOARD)==signal['source_sha256']
    assert r.sha(r.ROOT/signal['candidate'])==signal['candidate_sha256']
    assert r.sha(r.ROOT/power['candidate'])==power['candidate_sha256']
    assert r.sha(r.ROOT/'tools/add_signal_repairs.py')==signal['tool_sha256']
    assert power['input_sha256'][str(r.BOARD.relative_to(r.ROOT))]==r.sha(r.BOARD)
    inputs=[r.BOARD,signal_file,power_file,r.ROOT/signal['candidate'],r.ROOT/power['candidate'],
            Path(__file__),r.ROOT/'tools/add_signal_repairs.py',r.ROOT/'tools/protected_kelvin.py']
    inputs += [f for pattern in ('*.kicad_sch','*.kicad_pro','*.kicad_dru','*-lib-table') for f in r.PROJECT.glob(pattern)]
    hashes={str(f.relative_to(r.ROOT)):r.sha(f) for f in inputs}
    folder.mkdir();stage=folder/'candidate';stage.mkdir()
    for f in inputs:
        if f.parent != r.PROJECT or f == r.BOARD:continue
        target=stage/f.name
        if f.name.endswith('-lib-table'):
            target.write_text(f.read_text().replace('${KIPRJMOD}',r.PROJECT.as_posix()),encoding='utf-8')
        else:shutil.copyfile(f,target)
    candidate=stage/r.BOARD.name
    shutil.copyfile(r.ROOT/power['candidate'],candidate)
    board=p.LoadBoard(str(candidate));r.apply_project_netclasses(board)
    before_identity=without_zone_fills(r.identity(board))
    before_tracks=track_state(board)
    removed_native=[]
    for uid,state in signal['removed'].items():
        assert normalized(before_tracks[uid])==state, 'Removed segment differs from reviewed original'
        item=next(t for t in board.GetTracks() if t.m_Uuid.AsString()==uid)
        removed_native.append(item);board.Remove(item)
    added=[]
    for proposal in signal['paths']:added+=add(board,proposal)
    assert added==signal['added'], 'Replay differs from reviewed route additions'
    after_tracks=track_state(board)
    added_ids={row['uuid'] for row in added};removed_ids=set(signal['removed'])
    assert set(after_tracks)-set(before_tracks)==added_ids
    assert set(before_tracks)-set(after_tracks)==removed_ids
    assert all(after_tracks[uid]==state for uid,state in before_tracks.items() if uid not in removed_ids)
    assert without_zone_fills(r.identity(board))==before_identity
    assert r.connectivity(board)==0
    # Documented annotation-only update after all nets are connected.
    annotations=[]
    for item in board.GetDrawings():
        if isinstance(item,p.PCB_TEXT) and item.GetText()=='PRELIMINARY - UNROUTED - DO NOT FABRICATE':
            annotations.append({'uuid':item.m_Uuid.AsString(),'before':item.GetText(),
                                'after':'PRELIMINARY R1 - REVIEW ONLY'})
            item.SetText(annotations[-1]['after'])
    assert len(annotations)==1
    title=board.GetTitleBlock()
    assert title.GetTitle()=='Single detector USB master - PRELIMINARY UNROUTED'
    title.SetTitle('Single detector USB master - PRELIMINARY ROUTED REVIEW')
    final_identity=without_zone_fills(r.identity(board))
    assert p.SaveBoard(str(candidate),board,True)
    command=[str(Path(sys.executable).with_name('kicad-cli.exe')),'pcb','drc',
             '--format','json','--severity-all','--schematic-parity','--refill-zones','--save-board',
             '--output',str(folder/'native_drc.json'),str(candidate)]
    run=subprocess.run(command,text=True,capture_output=True,timeout=240)
    assert run.returncode==0,run.stdout+run.stderr
    drc=json.loads((folder/'native_drc.json').read_text())
    assert not drc['violations'] and not drc['schematic_parity'] and not drc['unconnected_items'],drc
    reopened=p.LoadBoard(str(candidate))
    assert track_state(reopened)==after_tracks
    assert without_zone_fills(r.identity(reopened))==final_identity
    assert r.connectivity(reopened)==0
    handoff=json.loads((r.RUNS/'trial01/handoff.json').read_text())
    guard=k.verify(reopened,handoff['reviewed_locked_seed']);r.outer_tracks_only(reopened)
    assert all(r.sha(r.ROOT/name)==digest for name,digest in hashes.items()), 'An input changed during review'
    backup=folder/'before_integration.kicad_pcb';shutil.copyfile(r.BOARD,backup)
    target=r.BOARD.with_suffix('.final-stage.kicad_pcb')
    assert not target.exists();shutil.copyfile(candidate,target)
    assert r.sha(target)==r.sha(candidate) and r.sha(backup)==r.sha(r.BOARD)
    os.replace(target,r.BOARD)
    report={'status':'COMPLETE_ROUTING_INTEGRATED_NATIVE_DRC_PASS','input_sha256':hashes,
            'board_sha256':r.sha(r.BOARD),'backup':str(backup.relative_to(r.ROOT)),
            'native_command':command,'native_stdout':run.stdout,'native_stderr':run.stderr,
            'drc_sha256':r.sha(folder/'native_drc.json'),'violations':0,'unconnected':0,'parity':0,
            'added_signal_items':added,'removed_signal_items':signal['removed'],
            'all_other_tracks_preserved':True,'nonrouting_changes':{'annotations':annotations,
              'title':'Single detector USB master - PRELIMINARY ROUTED REVIEW','zones':'fill only'},
            'protected_kelvin':guard}
    (folder/'integration.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('status','board_sha256','violations','unconnected','parity')}))


if __name__=='__main__':main()
