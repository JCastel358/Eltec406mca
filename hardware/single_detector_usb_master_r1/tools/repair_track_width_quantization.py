"""Restore five 0.2498mm router neckdowns to the required 0.2500mm floor."""
import json
import os
import shutil
import pcbnew as p
import route_board as r
import protected_kelvin as k


def track_state(board):
    result = {}
    for t in board.GetTracks():
        item = [t.GetNetname(), int(t.GetLayer()), k.xy(t.GetStart()),
                k.xy(t.GetEnd()), t.GetWidth(p.F_Cu) if isinstance(t,p.PCB_VIA) else t.GetWidth(), t.IsLocked()]
        if isinstance(t, p.PCB_VIA):
            item += [int(t.GetViaType()), t.GetDrill(), int(t.TopLayer()), int(t.BottomLayer())]
        result[t.m_Uuid.AsString()] = item
    return result


def main():
    folder = r.RUNS/'trial01'
    output = folder/'width_quantization_repair.json'
    assert not output.exists(), 'Repair already recorded; inspect its result'
    board = p.LoadBoard(str(r.BOARD))
    digest = r.sha(r.BOARD)
    drc = json.loads((folder/'native_import_drc.json').read_text())
    rows = drc['violations']
    assert len(rows) == 5 and all(v['type'] == 'track_width' for v in rows)
    ids = {row['items'][0]['uuid'] for row in rows}
    assert len(ids) == 5
    before_identity, before_tracks = r.identity(board), track_state(board)
    expected_tracks = {key:list(value) for key,value in before_tracks.items()}
    handoff = json.loads((folder/'handoff.json').read_text())
    k.verify(board, handoff['reviewed_locked_seed'])
    changed = []
    for track in board.GetTracks():
        uid = track.m_Uuid.AsString()
        if uid not in ids:
            continue
        assert not isinstance(track,(p.PCB_VIA,p.PCB_ARC))
        assert track.GetWidth() == 249800 and track.GetNetname() == 'DET_BAT_PROTECTED'
        assert track.GetLayer() == p.F_Cu
        track.SetWidth(250000)
        expected_tracks[uid][4] = 250000
        changed.append({'uuid':uid,'from_width_mm':.2498,'to_width_mm':.25,
                        'net':track.GetNetname(),'start_nm':k.xy(track.GetStart()),
                        'end_nm':k.xy(track.GetEnd())})
    assert len(changed) == 5 and track_state(board) == expected_tracks
    assert r.identity(board) == before_identity
    guard = k.verify(board, handoff['reviewed_locked_seed'])
    backup = folder/('before_width_repair_'+digest[:16]+'.kicad_pcb')
    if not backup.exists():
        shutil.copyfile(r.BOARD,backup)
    assert r.sha(backup) == digest
    stage = r.BOARD.with_suffix('.width-stage.kicad_pcb')
    assert not stage.exists()
    assert p.SaveBoard(str(stage),board,True)
    reopened = p.LoadBoard(str(stage))
    assert r.identity(reopened) == before_identity and track_state(reopened) == expected_tracks
    assert r.sha(r.BOARD) == digest, 'Source changed concurrently'
    os.replace(stage,r.BOARD)
    output.write_text(json.dumps({'status':'WIDTHS_REPAIRED_NATIVE_DRC_REQUIRED',
        'source_sha256':digest,'output_sha256':r.sha(r.BOARD),
        'backup':str(backup.relative_to(r.ROOT)),'changed_tracks':changed,
        'nonrouting_identity_preserved':True,'only_listed_widths_changed':True,
        'protected_kelvin':guard,'unconnected_count':r.connectivity(reopened),
        'tool_sha256':r.sha(__file__)},indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'widths_repaired':len(changed),'board_sha256':r.sha(r.BOARD),
                      'unconnected':r.connectivity(reopened)}))


if __name__ == '__main__':
    main()
