"""Integrate the reviewed ground-only candidate with exact preservation guards."""
import json
import os
import shutil
import pcbnew as p
import route_board as r
import protected_kelvin as k
from add_reference_planes import structural_identity


def main():
    folder = r.ROOT/'reports/reference_planes_routed'
    report = json.loads((folder/'copy_validation.json').read_text())
    plan = json.loads((folder/'plane_plan.json').read_text())
    candidate = folder/'candidate'/r.BOARD.name
    assert r.sha(candidate) == report['output_sha256']
    assert r.sha(r.BOARD) == plan['source_sha256'], 'Main board changed after plane planning'
    drc = json.loads((folder/'after_drc.json').read_text())
    assert not drc['violations'] and not drc['schematic_parity']
    assert len(drc['unconnected_items']) == 5
    assert all(' [GND]' not in item['description'] and ' [EM_GND]' not in item['description']
               for row in drc['unconnected_items'] for item in row['items'])
    before, after = p.LoadBoard(str(r.BOARD)), p.LoadBoard(str(candidate))
    existing = {t.m_Uuid.AsString() for t in [*before.GetTracks(),*before.Zones()]}
    all_items = {t.m_Uuid.AsString():t for t in [*after.GetTracks(),*after.Zones()]}
    assert existing.issubset(all_items)
    added = set(all_items)-existing
    assert added
    for uid in added:
        item = all_items[uid]
        assert item.GetNetname() in ('GND','EM_GND'), 'Unexpected non-ground addition'
        if isinstance(item,p.ZONE):
            assert not item.GetIsRuleArea()
        elif isinstance(item,p.PCB_VIA):
            assert item.TopLayer()==p.F_Cu and item.BottomLayer()==p.B_Cu
        else:
            assert item.GetLayer()==p.F_Cu and not isinstance(item,p.PCB_ARC)
    assert structural_identity(before,set()) == structural_identity(after,added)
    handoff = json.loads((r.RUNS/'trial01/handoff.json').read_text())
    guard = k.verify(after,handoff['reviewed_locked_seed'])
    r.outer_tracks_only(after)
    old_sha = r.sha(r.BOARD)
    backup = folder/('before_integration_'+old_sha[:16]+'.kicad_pcb')
    if not backup.exists():shutil.copyfile(r.BOARD,backup)
    assert r.sha(backup)==old_sha
    stage = r.BOARD.with_suffix('.planes-stage.kicad_pcb')
    assert not stage.exists()
    shutil.copyfile(candidate,stage)
    assert r.sha(stage)==report['output_sha256'] and r.sha(r.BOARD)==old_sha
    os.replace(stage,r.BOARD)
    result = {'status':'REVIEWED_PLANES_INTEGRATED_FIVE_NONGROUND_OPENS_REMAIN',
              'before_sha256':old_sha,'after_sha256':r.sha(r.BOARD),
              'backup':str(backup.relative_to(r.ROOT)),
              'existing_native_items_preserved':True,
              'added_ground_item_uuids':sorted(added),'protected_kelvin':guard,
              'unconnected_count':r.connectivity(after),
              'input_sha256':{str(path.relative_to(r.ROOT)):r.sha(path) for path in
                  [folder/'copy_validation.json',folder/'plane_plan.json',folder/'after_drc.json',
                   candidate,r.ROOT/'tools/add_reference_planes.py',r.ROOT/'tools/integrate_reference_planes.py']}}
    (folder/'integration.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'board_sha256':r.sha(r.BOARD),'added_ground_items':len(added),
                      'unconnected_count':r.connectivity(after)}))


if __name__ == '__main__':main()
