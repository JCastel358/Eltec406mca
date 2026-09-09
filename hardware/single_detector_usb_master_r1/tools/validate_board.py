"""Refill/save the current native PCB, then require clean DRC and parity.

This verifies digital design data. It never certifies physical fit, transient
operation, noise, calibration, sourcing or manufacturing release.
"""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import argparse
import hashlib
import json
import subprocess
import sys

import pcbnew as p
import protected_kelvin

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT/'single_detector_usb_master'
BOARD = PROJECT/'single_detector_usb_master.kicad_pcb'
REPORTS = ROOT/'reports'
CLI = Path(sys.executable).with_name('kicad-cli.exe')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(routing_run):
    routing_folder = (REPORTS/'routing'/routing_run).resolve()
    if routing_folder.parent != (REPORTS/'routing').resolve():
        raise ValueError('Expected a direct routing-run folder name')
    handoff_path = routing_folder/'handoff.json'
    import_path = routing_folder/'import.json'
    handoff = json.loads(handoff_path.read_text())
    imported = json.loads(import_path.read_text())
    if imported.get('status') != 'IMPORTED_NOT_DRC_QUALIFIED' or not handoff.get('reviewed_locked_seed'):
        raise ValueError('Validation requires a recorded native import with the reviewed Kelvin seed')
    sources = [*PROJECT.glob('*.kicad_sch'), *PROJECT.glob('*.kicad_pro'),
               *PROJECT.glob('*.kicad_dru'), *PROJECT.glob('*-lib-table'),
               *ROOT.glob('libraries/*.kicad_sym'), *ROOT.glob('libraries/master.pretty/*.kicad_mod'),
               handoff_path, import_path, Path(__file__), Path(protected_kelvin.__file__)]
    amendment = imported.get('position_roundoff_amendment')
    if amendment:
        for key in ('report', 'tool'):
            path = (ROOT/amendment[key]).resolve()
            if not path.is_relative_to(ROOT) or not path.is_file() or sha(path) != amendment[key+'_sha256']:
                raise ValueError('Import rounding amendment is missing or changed: '+key)
            sources.append(path)
    before = {str(f.relative_to(ROOT)): sha(f) for f in sources}
    result = {'started_utc': datetime.now(timezone.utc).isoformat(),
              'status': 'INCOMPLETE', 'source_sha256': before,
              'position_roundoff_amendment_verified': bool(amendment),
              'board_before_refill_sha256': sha(BOARD)}
    report_path = REPORTS/'board_drc.json'
    command = [str(CLI), 'pcb', 'drc', '--format', 'json', '--severity-all',
               '--schematic-parity', '--refill-zones', '--save-board',
               '--exit-code-violations', '--output', str(report_path), str(BOARD)]
    # Remove only this known report to avoid interpreting an old pass as fresh.
    if report_path.exists():
        report_path.unlink()
    try:
        run = subprocess.run(command, text=True, capture_output=True, timeout=240)
        result['command'] = {'argv': command, 'returncode': run.returncode,
                             'stdout': run.stdout, 'stderr': run.stderr}
        if not report_path.exists():
            raise RuntimeError('Native DRC failed to create its report')
        data = json.loads(report_path.read_text())
        ignored = data.get('ignored_checks', [])
        allowed_ignored = {'tuning_profile_track_geometries':
                           'No length-tuning profiles are present in this board.'}
        unexpected_ignored = [row for row in ignored if row['key'] not in allowed_ignored]
        project_data = json.loads(BOARD.with_suffix('.kicad_pro').read_text())
        exclusions = project_data.get('board', {}).get('design_settings', {}).get('drc_exclusions', [])
        result['ignored_checks'] = ignored
        result['ignored_check_review'] = allowed_ignored
        result['unexplained_ignored_checks'] = unexpected_ignored
        result['configured_exclusions'] = exclusions
        result['drc_counts'] = {key: dict(Counter(x['type'] for x in data.get(key, [])))
                                for key in ('violations', 'unconnected_items', 'schematic_parity')}
        result['severity_counts'] = dict(Counter(x.get('severity', 'unspecified')
                                                 for key in ('violations', 'unconnected_items', 'schematic_parity')
                                                 for x in data.get(key, [])))
        board = p.LoadBoard(str(BOARD))
        board.BuildConnectivity()
        tracks = list(board.GetTracks())
        zones = [zone for zone in board.Zones() if not zone.GetIsRuleArea()]
        internal = [str(track.m_Uuid.AsString()) for track in tracks
                    if not isinstance(track, p.PCB_VIA) and track.GetLayer() not in (p.F_Cu, p.B_Cu)]
        blind = [str(track.m_Uuid.AsString()) for track in tracks
                 if isinstance(track, p.PCB_VIA) and (track.TopLayer()!=p.F_Cu or track.BottomLayer()!=p.B_Cu)]
        result['reopened_native'] = {'kicad_version': p.GetBuildVersion(),
                                    'copper_layers': board.GetCopperLayerCount(),
                                    'footprints': len(list(board.GetFootprints())),
                                    'tracks_and_vias': len(tracks), 'copper_zones': len(zones),
                                    'unconnected_edges': board.GetConnectivity().GetUnconnectedCount(False),
                                    'unexpected_internal_tracks': internal, 'unexpected_non_through_vias': blind}
        result['routing_run'] = routing_run
        result['protected_kelvin_check'] = protected_kelvin.verify(board, handoff['reviewed_locked_seed'])
        changed = [name for name, digest in before.items() if sha(ROOT/name) != digest]
        result['non_board_inputs_unchanged'] = not changed
        if changed:
            raise RuntimeError(f'Inputs changed during DRC/refill: {changed}')
        result['board_after_refill_sha256'] = sha(BOARD)
        result['drc_report_sha256'] = sha(report_path)
        clean = (run.returncode == 0 and all(not data.get(key) for key in
                  ('violations', 'unconnected_items', 'schematic_parity'))
                 and board.GetConnectivity().GetUnconnectedCount(False) == 0
                 and board.GetCopperLayerCount() == 4 and bool(tracks) and bool(zones)
                 and not internal and not blind and not unexpected_ignored and not exclusions)
        result['native_data_checks_passed'] = clean
        result['status'] = 'NATIVE_ROUTING_DRC_PARITY_PASS_NOT_PRODUCTION_QUALIFICATION' if clean else 'INCOMPLETE_NATIVE_BOARD_CHECKS'
        result['does_not_prove'] = ['mechanical mating/enclosure fit', 'power transient and input-limit waveforms',
                                    'analog noise/gain/calibration or EMI', 'thermal qualification',
                                    'JLC allocation/rotation/process acceptance', 'production release']
        print(json.dumps({'status': result['status'], 'drc_counts': result['drc_counts'],
                          'reopened_native': result['reopened_native']}, indent=2))
        if not clean:
            raise RuntimeError('Board is not yet fully routed, refilled and clean in native KiCad')
    finally:
        result['finished_utc'] = datetime.now(timezone.utc).isoformat()
        (REPORTS/'board_validation.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--routing-run', required=True,
                        help='Imported run folder under reports/routing, for the locked sense-path evidence')
    validate(parser.parse_args().routing_run)
