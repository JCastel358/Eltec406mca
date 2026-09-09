"""Align native drill/place origin with the documented candidate CPL origin."""
from pathlib import Path
import json
import os
import shutil
import pcbnew as p
from route_board import BOARD, ROOT, identity, sha


def geometry_identity(board):
    tree = identity(board)
    for node in tree:
        if isinstance(node, list) and node and node[0] == 'setup':
            node[:] = [child for child in node if not
                       (isinstance(child, list) and child and child[0] == 'aux_axis_origin')]
    return tree


def main():
    board = p.LoadBoard(str(BOARD))
    assert not list(board.GetTracks()), 'Set the fabrication origin before routing'
    assert all(zone.GetIsRuleArea() for zone in board.Zones()), 'Set origin before adding copper planes'
    before_identity = geometry_identity(board)
    before_sha = sha(BOARD)
    settings = board.GetDesignSettings()
    old = settings.GetAuxOrigin()
    record = {'board_before_sha256': before_sha,
              'origin_before_native_mm': [old.x/1e6, old.y/1e6],
              'origin_after_native_mm': [12.0, 115.0]}
    if old != p.VECTOR2I(p.FromMM(12), p.FromMM(115)):
        backup = ROOT/'reports/origin_update'/('before_'+before_sha[:16]+'.kicad_pcb')
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            assert sha(backup) == before_sha
        else:
            shutil.copyfile(BOARD, backup)
        assert sha(backup) == before_sha
        settings.SetAuxOrigin(p.VECTOR2I(p.FromMM(12), p.FromMM(115)))
        stage = BOARD.with_suffix('.origin-stage.kicad_pcb')
        assert not stage.exists(), 'Inspect leftover origin stage before retrying'
        assert p.SaveBoard(str(stage), board, True)
        reopened = p.LoadBoard(str(stage))
        assert geometry_identity(reopened) == before_identity, 'Origin change altered geometry/net identities'
        origin = reopened.GetDesignSettings().GetAuxOrigin()
        assert origin == p.VECTOR2I(p.FromMM(12), p.FromMM(115))
        assert sha(BOARD) == before_sha, 'Main board changed concurrently'
        os.replace(stage, BOARD)
        record['backup'] = str(backup.relative_to(ROOT))
    record['board_after_sha256'] = sha(BOARD)
    record['native_geometry_and_nets_preserved'] = True
    (ROOT/'reports/fabrication_origin.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
