"""Export DRAFT fabrication data only for a hash-validated, fully routed PCB.

Run with KiCad 10's Python after validate_board.py and export_assembly.py.
Never edits the design or the existing reports. All native exports run against
a private snapshot; failed work is discarded and existing draft runs remain.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import csv
import hashlib
import importlib.util
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

import pcbnew as p

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / 'single_detector_usb_master'
BOARD = PROJECT / 'single_detector_usb_master.kicad_pcb'
VALIDATION = ROOT / 'reports/board_validation.json'
DRC = ROOT / 'reports/board_drc.json'
ASSEMBLY = ROOT / 'reports/assembly_export.json'
ASSEMBLY_DIR = ROOT / 'exports/assembly_draft'
OUTPUT = ROOT / 'exports/fabrication_draft'
CLI = Path(sys.executable).with_name('kicad-cli.exe')
ORIGIN = (12.0, 115.0)
DRC_KEYS = ('violations', 'unconnected_items', 'schematic_parity')
PASS = 'NATIVE_ROUTING_DRC_PARITY_PASS_NOT_PRODUCTION_QUALIFICATION'
STATUS = 'DRAFT_FABRICATION_DATA_NOT_APPROVED_FOR_UPLOAD_OR_ORDER'
# Native names accepted by the installed KiCad 10 CLI; no plot-settings fallback.
LAYERS = ('F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu', 'F.Mask', 'B.Mask',
          'F.Silkscreen', 'B.Silkscreen', 'F.Paste', 'B.Paste', 'Edge.Cuts')
FUNCTIONS = (('Copper', 'L1', 'Top'), ('Copper', 'L2', 'Inr'),
             ('Copper', 'L3', 'Inr'), ('Copper', 'L4', 'Bot'),
             ('Soldermask', 'Top'), ('Soldermask', 'Bot'),
             ('Legend', 'Top'), ('Legend', 'Bot'),
             ('Paste', 'Top'), ('Paste', 'Bot'), ('Profile',))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def relative(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


def source_path(name):
    """Accept either Windows or POSIX report keys, never paths outside ROOT."""
    require(isinstance(name, str), 'Hash manifest path is not a string')
    result = (ROOT / name.replace('\\', '/')).resolve()
    require(result.is_relative_to(ROOT) and result.is_file(),
            f'Missing or out-of-project input: {name}')
    return result


def verify_hash_map(mapping):
    require(isinstance(mapping, dict) and mapping, 'Missing input hash manifest')
    result = {}
    for name, digest in mapping.items():
        require(isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest),
                f'Invalid SHA-256 for {name}')
        path = source_path(name)
        require(sha(path) == digest, f'Stale input hash: {name}')
        key = relative(path)
        require(key not in result, f'Duplicate normalized hash path: {name}')
        result[key] = digest
    return result


def routing_folder(routing_run):
    require(isinstance(routing_run, str) and routing_run, 'Validation routing-run evidence is missing')
    path = (ROOT / 'reports/routing' / routing_run).resolve()
    require(path.parent == (ROOT / 'reports/routing').resolve(), 'Invalid routing-run evidence folder')
    return path


def design_sources(routing_run):
    # Same inventory as validate_board.py; also reject added/deleted sources.
    sources = ({path.resolve() for pattern in
                   ('*.kicad_sch', '*.kicad_pro', '*.kicad_dru', '*-lib-table')
                   for path in PROJECT.glob(pattern)} |
                  {path.resolve() for pattern in
                   ('libraries/*.kicad_sym', 'libraries/master.pretty/*.kicad_mod')
                   for path in ROOT.glob(pattern)} |
                  {ROOT / 'tools/validate_board.py', ROOT / 'tools/protected_kelvin.py',
                   routing_folder(routing_run) / 'handoff.json',
                   routing_folder(routing_run) / 'import.json'})
    imported = json.loads((routing_folder(routing_run)/'import.json').read_text())
    amendment = imported.get('position_roundoff_amendment')
    if amendment:
        for key in ('report', 'tool'):
            path = source_path(amendment[key])
            require(sha(path) == amendment[key+'_sha256'], 'Import amendment changed: '+key)
            sources.add(path)
    return sorted(sources)


def require_clean_drc(data):
    require(data.get('coordinate_units') == 'mm', 'Native DRC units must be mm')
    require(set(data.get('included_severities', [])) == {'error', 'warning', 'exclusion'},
            'DRC must include errors, warnings and exclusions')
    for key in DRC_KEYS:
        require(key in data and isinstance(data[key], list) and not data[key],
                f'Native DRC is missing or not clean: {key}')
    require(data.get('source') == BOARD.name, 'DRC source board name differs')
    require(all(row.get('key') == 'tuning_profile_track_geometries'
                for row in data.get('ignored_checks', [])), 'Unexplained ignored native DRC check')


def read_validation():
    require(VALIDATION.is_file() and DRC.is_file(),
            'Run validate_board.py on the fully routed board before fabrication export')
    validation_raw = VALIDATION.read_bytes()
    validation = json.loads(validation_raw)
    require(validation.get('status') == PASS and
            validation.get('native_data_checks_passed') is True and
            validation.get('non_board_inputs_unchanged') is True,
            'Native board validation is incomplete or did not pass')
    require(validation.get('board_after_refill_sha256') == sha(BOARD),
            'PCB changed after native refill/validation')
    require(validation.get('drc_report_sha256') == sha(DRC), 'Native DRC report hash differs')
    sources = verify_hash_map(validation.get('source_sha256'))
    routing_run = validation.get('routing_run')
    require(set(sources) == {relative(path) for path in design_sources(routing_run)},
            'Design source inventory changed after native validation')
    protected = validation.get('protected_kelvin_check', {})
    require(protected.get('exact_locked_seed_retained') is True and
            protected.get('no_extra_conductor_in_protected_corridor') is True,
            'Native validation did not preserve the protected Kelvin path')
    require(validation.get('unexplained_ignored_checks') == [] and
            validation.get('configured_exclusions') == [],
            'Validation contains unexplained ignored checks or configured exclusions')
    route = routing_folder(routing_run)
    handoff = json.loads((route / 'handoff.json').read_bytes())
    imported = json.loads((route / 'import.json').read_bytes())
    require(handoff.get('reviewed_locked_seed') and
            imported.get('status') == 'IMPORTED_NOT_DRC_QUALIFIED',
            'Routing handoff/import provenance does not match the validated pipeline')
    command = validation.get('command', {})
    argv = command.get('argv', [])
    require(command.get('returncode') == 0 and
            {'--severity-all', '--schematic-parity', '--refill-zones', '--save-board',
             '--exit-code-violations'}.issubset(argv),
            'Validation did not perform the required native refill/DRC/parity command')
    require(argv and Path(argv[-1]).resolve() == BOARD.resolve(),
            'Validation command targeted a different PCB')
    finished = datetime.fromisoformat(validation['finished_utc'])
    require(finished.tzinfo is not None and finished <= datetime.now(timezone.utc),
            'Invalid or future validation completion timestamp')
    drc_raw = DRC.read_bytes()
    require(hashlib.sha256(drc_raw).hexdigest() == validation['drc_report_sha256'],
            'Native DRC report changed while being read')
    data = json.loads(drc_raw)
    require_clean_drc(data)
    require(validation.get('drc_counts') == {key: {} for key in DRC_KEYS} and
            validation.get('severity_counts') == {}, 'Validation summary disagrees with clean DRC')
    sources.update({relative(BOARD): validation['board_after_refill_sha256'],
                    relative(VALIDATION): hashlib.sha256(validation_raw).hexdigest(),
                    relative(DRC): hashlib.sha256(drc_raw).hexdigest()})
    return validation, sources


def native_board_checks(board, validation):
    board.BuildConnectivity()
    tracks = list(board.GetTracks())
    zones = [z for z in board.Zones() if not z.GetIsRuleArea()]
    internal = [t for t in tracks if not isinstance(t, p.PCB_VIA)
                and t.GetLayer() not in (p.F_Cu, p.B_Cu)]
    blind = [t for t in tracks if isinstance(t, p.PCB_VIA)
             and (t.TopLayer() != p.F_Cu or t.BottomLayer() != p.B_Cu)]
    require(board.GetCopperLayerCount() == 4, 'Expected exactly four copper layers')
    require(tracks and any(not isinstance(t, p.PCB_VIA) for t in tracks) and zones,
            'A routed board with copper tracks and filled copper zones is required')
    require(not internal and not blind, 'Unexpected inner-layer track or non-through via')
    require(board.GetConnectivity().GetUnconnectedCount(False) == 0,
            'Reopened PCB still has unconnected edges')
    require(all(z.IsFilled() and any(z.HasFilledPolysForLayer(layer)
                                    for layer in z.GetLayerSet().Seq()) for z in zones),
            'Copper zones lack native saved fills; run validate_board.py')
    summary = validation['reopened_native']
    actual = {'copper_layers': board.GetCopperLayerCount(),
              'footprints': len(list(board.GetFootprints())),
              'tracks_and_vias': len(tracks), 'copper_zones': len(zones),
              'unconnected_edges': 0, 'unexpected_internal_tracks': [],
              'unexpected_non_through_vias': []}
    require(all(summary.get(k) == v for k, v in actual.items()),
            'Native reopened counts differ from the saved validation')
    origin = board.GetDesignSettings().GetAuxOrigin()
    actual['aux_origin_native_mm'] = [origin.x / 1e6, origin.y / 1e6]
    require(all(abs(v - e) <= 1e-6 for v, e in zip(actual['aux_origin_native_mm'], ORIGIN)),
            f'Set and validate the drill/place origin {ORIGIN}; exporter does not edit it')
    enabled = {board.GetLayerName(layer) for layer in board.GetEnabledLayers().Seq()}
    require(set(LAYERS).issubset(enabled), 'Required fabrication layers are not enabled')
    return actual


def read_assembly():
    require(ASSEMBLY.is_file(), 'Run export_assembly.py for this validated PCB first')
    assembly_raw = ASSEMBLY.read_bytes()
    report = json.loads(assembly_raw)
    hashes = verify_hash_map(report.get('input_sha256'))
    require(hashes.get(relative(BOARD)) == sha(BOARD), 'Assembly export targets a different PCB')
    require(report.get('BOM_CPL_native_population_exact_match') is True and
            report.get('native_CLI_coordinates_and_angles_match') is True and
            tuple(report.get('placement_origin_native_mm', [])) == ORIGIN,
            'Assembly coordinate/population checks or origin do not match')
    files = report.get('files', {})
    require({'jlc_bom_DRAFT.csv', 'jlc_cpl_DRAFT.csv', 'native_origins.csv'}.issubset(files),
            'Assembly file manifest is incomplete')
    for name, digest in files.items():
        require(Path(name).name == name, 'Assembly manifest contains a non-local file name')
        path = ASSEMBLY_DIR / name
        require(path.is_file() and sha(path) == digest, f'Stale assembly output: {name}')
        hashes[relative(path)] = digest
    rows = list(csv.DictReader((ASSEMBLY_DIR / 'jlc_cpl_DRAFT.csv').open(encoding='utf-8-sig')))
    require(len(rows) == report.get('populated_components'), 'CPL population count differs')
    cpl = {row['Designator']: row for row in rows}
    require(len(cpl) == len(rows), 'CPL contains duplicate references')
    hashes[relative(ASSEMBLY)] = hashlib.sha256(assembly_raw).hexdigest()
    return report, cpl, hashes


def run_native(command, commands):
    result = subprocess.run([str(x) for x in command], capture_output=True,
                            text=True, timeout=300)
    commands.append({'argv': [str(x) for x in command], 'returncode': result.returncode,
                     'stdout': result.stdout, 'stderr': result.stderr})
    require(result.returncode == 0, 'KiCad command failed: ' + result.stdout + result.stderr)


def xy(point):
    return point.x / 1e6 - ORIGIN[0], ORIGIN[1] - point.y / 1e6


def close(a, b, tolerance=2e-5):
    return all(math.isfinite(float(v)) for v in (*a, *b)) and math.dist(a, b) <= tolerance


def check_placements(board, report, cpl, native_csv):
    rows = list(csv.DictReader(native_csv.open(encoding='utf-8-sig')))
    native = {row['Ref']: row for row in rows}
    require(len(native) == len(rows) and set(native) == set(cpl), 'Fresh native CPL population differs')
    corrections = {row['ref']: row for row in report['placement_corrections']}
    require(len(corrections) == len(report['placement_corrections']) and set(corrections) == set(cpl),
            'Assembly correction inventory differs')
    fps = {fp.GetReference(): fp for fp in board.GetFootprints()}
    require(set(cpl) == {ref for ref, fp in fps.items()
                        if not fp.IsDNP() and not fp.IsExcludedFromPosFiles()},
            'Current native fitted position inventory differs')
    for ref, row in cpl.items():
        fp, n, correction = fps[ref], native[ref], corrections[ref]
        require(fp.GetLayer() == p.F_Cu and row['Layer'] == 'Top',
                f'{ref}: bottom placement requires a separately reviewed transform')
        require(close((float(n['PosX']), float(n['PosY'])), xy(fp.GetPosition())),
                f'{ref}: native drill-origin coordinates differ')
        angle = fp.GetOrientationDegrees()
        require(abs((float(n['Rot']) - angle + 180) % 360 - 180) < .001 and
                abs((float(row['Rotation']) - angle + 180) % 360 - 180) < .001,
                f'{ref}: native/CPL rotation differs')
        dx, dy = correction['body_center_local_offset_mm']
        radians = math.radians(-angle)
        px, py = xy(fp.GetPosition())
        expected = (px + dx * math.cos(radians) - dy * math.sin(radians),
                    py - dx * math.sin(radians) - dy * math.cos(radians))
        require(close((float(row['Mid X']), float(row['Mid Y'])), expected),
                f'{ref}: assembly body-center coordinate differs')
    return {'references_checked': len(cpl), 'origin_native_mm': list(ORIGIN),
            'axes': 'positive X right, positive Y up; no mirror',
            'native_plot_origin_and_CPL_body_centers_agree': True,
            'vendor_library_rotations_verified': False}


def gerber_functions(directory):
    result = {}
    for file in directory.glob('*.gbr'):
        text = file.read_text(encoding='utf-8')
        require('%MOMM*%' in text, f'Gerber is not metric: {file.name}')
        match = re.search(r'%TF\.FileFunction,([^*]+)\*%', text)
        require(match, f'Gerber lacks X2 FileFunction: {file.name}')
        function = tuple(match.group(1).split(','))
        matches = [expected for expected in FUNCTIONS if function[:len(expected)] == expected]
        require(len(matches) == 1 and matches[0] not in result,
                f'Unexpected/duplicate Gerber role: {file.name}: {function}')
        result[matches[0]] = file
    require(set(result) == set(FUNCTIONS), 'Missing expected copper/mask/silk/paste/profile output')
    return result


def gerber_path_points(text):
    require('%FSLAX46Y46*%' in text, 'Expected absolute 4.6 Gerber coordinate format')
    points, x, y = [], None, None
    for line in text.splitlines():
        match = re.fullmatch(r'(?:G0[123])?(?:X(-?\d+))?(?:Y(-?\d+))?'
                             r'(?:I-?\d+)?(?:J-?\d+)?D0[12]\*', line.strip())
        if match and (match[1] is not None or match[2] is not None):
            x = int(match[1]) / 1e6 if match[1] is not None else x
            y = int(match[2]) / 1e6 if match[2] is not None else y
            if x is not None and y is not None:
                points.append((x, y))
    return points


def excellon_points(text):
    require(re.search(r'^METRIC[,\r\n]', text, re.M), 'Expected metric Excellon output')
    points, x, y = [], None, None
    for line in text.splitlines():
        # Plain drill hits only; G00/G01/G85 slot-routing commands are excluded.
        match = re.fullmatch(r'(?:X(-?\d+(?:\.\d*)?))?(?:Y(-?\d+(?:\.\d*)?))?', line.strip())
        if match and (match[1] is not None or match[2] is not None):
            x = float(match[1]) if match[1] is not None else x
            y = float(match[2]) if match[2] is not None else y
            if x is not None and y is not None:
                points.append((x, y))
    return points


def native_outline_vertices(board):
    """Return plotted corner/end-point witnesses for supported native outlines.

    A KiCad gr_rect stores two opposite corners, but its profile Gerber plots
    four edges. Arc endpoints are also profile witnesses; their I/J commands
    must not be skipped by the Gerber parser. This verifies coordinates rather
    than claiming a complete CAM comparison of arc interpolation.
    """
    vertices = []
    for item in board.GetDrawings():
        if item.GetLayer() != p.Edge_Cuts:
            continue
        shape = item.GetShape()
        if shape == p.SHAPE_T_RECT:
            require(item.GetCornerRadius() == 0,
                    'Rounded native rectangle needs a reviewed profile-witness transform')
            native = list(item.GetRectCorners())
            require(len(native) == 4, 'Native rectangle did not provide four corners')
        elif shape in (p.SHAPE_T_SEGMENT, p.SHAPE_T_ARC):
            native = (item.GetStart(), item.GetEnd())
        else:
            raise RuntimeError(f'Unsupported native outline primitive for coordinate verification: {shape}')
        vertices.extend(xy(point) for point in native)
    require(len(set(vertices)) >= 3,
            'Need at least three native outline vertices for origin verification')
    return vertices


def check_plot_coordinates(board, functions, drill_dir):
    edge = functions[('Profile',)]
    points = gerber_path_points(edge.read_text(encoding='utf-8'))
    endpoints = native_outline_vertices(board)
    require(all(any(close(point, candidate) for candidate in points) for point in endpoints),
            'Gerber outline endpoints do not match native drill-origin coordinates')
    files = sorted(drill_dir.glob('*.drl'))
    require(files, 'No native Excellon drill files were produced')
    holes = [xy(pad.GetPosition()) for fp in board.GetFootprints() for pad in fp.Pads()
             if pad.GetDrillSize().x > 0 and pad.GetDrillSize().x == pad.GetDrillSize().y]
    holes += [xy(via.GetPosition()) for via in board.GetTracks() if isinstance(via, p.PCB_VIA)]
    require(len(set(holes)) >= 3, 'Need at least three round holes for independent drill-origin verification')
    a = min(holes)
    b = max(holes, key=lambda point: math.dist(a, point))
    area = lambda point: abs((b[0]-a[0])*(point[1]-a[1]) - (b[1]-a[1])*(point[0]-a[0]))
    c = max(holes, key=area)
    require(area(c) > 1, 'Drill coordinate witnesses must not be collinear')
    plotted = [point for file in files for point in excellon_points(file.read_text(encoding='utf-8'))]
    require(all(any(close(point, candidate, .002) for candidate in plotted) for point in (a, b, c)),
            'Native Excellon holes disagree with the PCB origin or axis direction')
    return {'gerber_profile_vertices_checked': len(set(endpoints)),
            'excellon_noncollinear_witnesses_mm': [a, b, c],
            'native_origin_transform_agrees': True,
            'limitation': 'Coordinate witnesses do not replace a complete CAM geometry/slot review'}


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def export():
    validation, inputs = read_validation()
    board = p.LoadBoard(str(BOARD))
    native_summary = native_board_checks(board, validation)
    assembly, cpl, assembly_hashes = read_assembly()
    for name, digest in assembly_hashes.items():
        require(name not in inputs or inputs[name] == digest,
                f'Validation and assembly disagree on source hash: {name}')
        inputs[name] = digest
    for path in (BOARD, VALIDATION, DRC, ASSEMBLY, Path(__file__),
                 ROOT / 'tools/validate_board.py', ROOT / 'tools/export_assembly.py',
                 OUTPUT / 'README.md'):
        require(path.is_file(), f'Required export input missing: {path}')
        name, digest = relative(path), sha(path)
        require(name not in inputs or inputs[name] == digest, f'Input changed during preflight: {name}')
        inputs[name] = digest
    require(CLI.is_file() and p.GetBuildVersion().startswith('10.'), 'Run with installed KiCad 10 Python')
    toolchain_hashes = {str(CLI): sha(CLI), str(Path(sys.executable)): sha(sys.executable)}
    routing_run = validation['routing_run']
    inventory = {relative(path) for path in design_sources(routing_run)}
    started = datetime.now(timezone.utc)
    label = started.strftime('%Y%m%dT%H%M%S_%fZ') + '_' + sha(BOARD)[:12] + '_DRAFT'
    target = OUTPUT / label
    require(not target.exists(), 'This immutable draft export directory already exists')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    commands = []
    with tempfile.TemporaryDirectory(prefix='.work_DRAFT_', dir=OUTPUT) as tmp:
        work = Path(tmp)
        require(work.resolve().is_relative_to(OUTPUT.resolve()), 'Temporary export path escaped output root')
        snapshot, payload = work / 'snapshot', work / 'payload'
        for name, digest in inputs.items():
            destination = snapshot / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
            require(sha(destination) == digest, f'Input changed while snapshotting: {name}')
        source_board = snapshot / relative(BOARD)
        require(sha(source_board) == validation['board_after_refill_sha256'],
                'Snapshot is not the board validated after native zone refill')
        source_native = p.LoadBoard(str(source_board))
        native_board_checks(source_native, validation)
        helper_file = snapshot / 'tools/protected_kelvin.py'
        helper_spec = importlib.util.spec_from_file_location('fabrication_kelvin_snapshot', helper_file)
        helper = importlib.util.module_from_spec(helper_spec)
        helper_spec.loader.exec_module(helper)
        handoff = json.loads((snapshot / relative(routing_folder(routing_run) / 'handoff.json')).read_bytes())
        protected_check = helper.verify(source_native, handoff['reviewed_locked_seed'])
        require(protected_check.get('exact_locked_seed_retained') is True and
                protected_check.get('no_extra_conductor_in_protected_corridor') is True,
                'Snapshot protected Kelvin check failed')
        gerbers, drill, evidence = (payload / name for name in ('gerbers', 'drill', 'evidence'))
        for directory in (gerbers, drill, evidence):
            directory.mkdir(parents=True)
        # Fresh native DRC/parity, without refilling or saving the validated fills.
        # No Gerbers/drills are emitted until this independent current check passes.
        fresh_drc = evidence / 'fresh_native_drc_DRAFT.json'
        run_native([CLI, 'pcb', 'drc', '--format', 'json', '--units', 'mm', '--severity-all',
                    '--schematic-parity', '--exit-code-violations', '--output', fresh_drc,
                    source_board], commands)
        require(fresh_drc.is_file(), 'Fresh native DRC did not create a report')
        fresh = json.loads(fresh_drc.read_text(encoding='utf-8'))
        require_clean_drc(fresh)
        native_csv = evidence / 'native_positions_plot_origin_DRAFT.csv'
        run_native([CLI, 'pcb', 'export', 'pos', '--format', 'csv', '--units', 'mm',
                    '--use-drill-file-origin', '--exclude-dnp', '--output', native_csv,
                    source_board], commands)
        coordinates = check_placements(source_native, assembly, cpl, native_csv)
        run_native([CLI, 'pcb', 'export', 'gerbers', '--layers', ','.join(LAYERS),
                    '--use-drill-file-origin', '--precision', '6', '--no-protel-ext',
                    '--subtract-soldermask', '--output', gerbers, source_board], commands)
        run_native([CLI, 'pcb', 'export', 'drill', '--format', 'excellon', '--drill-origin',
                    'plot', '--excellon-units', 'mm', '--excellon-zeros-format', 'decimal',
                    '--excellon-oval-format', 'route', '--excellon-separate-th',
                    '--generate-map', '--map-format', 'pdf', '--generate-report',
                    '--report-path', evidence / 'native_drill_report_DRAFT.txt',
                    '--output', drill, source_board], commands)
        functions = gerber_functions(gerbers)
        coordinates['fabrication_plot_coordinates'] = check_plot_coordinates(source_native, functions, drill)
        require(any(drill.glob('*.pdf')), 'Native drill PDF map is missing')
        require((evidence / 'native_drill_report_DRAFT.txt').is_file(), 'Native drill report is missing')
        for path in (VALIDATION, DRC, ASSEMBLY):
            shutil.copyfile(snapshot / relative(path), evidence / path.name)
        for name in ('handoff', 'import'):
            path = routing_folder(routing_run) / (name + '.json')
            shutil.copyfile(snapshot / relative(path), evidence / ('routing_' + name + '.json'))
        write_json(evidence / 'protected_kelvin_DRAFT.json', protected_check)
        write_json(evidence / 'coordinate_consistency_DRAFT.json', coordinates)
        (payload / 'README_DRAFT.md').write_text(
            '# Draft fabrication data - do not order\n\n'
            'Generated from the fully routed, saved-fill, native DRC/parity-validated PCB. '
            'The manifest identifies the exact source and assembly hashes. Four copper layers, '
            'both solder masks, both legends, both paste layers and Edge.Cuts are included. '
            'An unused legend/paste layer may contain no artwork. Native Excellon plated and '
            'non-plated holes, routed slots, PDF drill maps and drill report are included.\n\n'
            'Gerber, drill and assembly share native origin (12,115) mm: positive X right and '
            'positive Y up, without mirroring. Three round-hole witnesses and outline vertices '
            'were checked against plotted coordinates; full CAM review remains necessary.\n\n'
            'This is not a manufacturing release. No fabricator stackup, copper weight, '
            'dielectric, finished thickness, impedance, finish, drill process capability, '
            'assembly allocation/rotation, physical fit or electrical/thermal qualification '
            'is asserted. Any KiCad Gerber job stackup fields are design metadata, not a '
            'confirmed fabrication specification. No upload or order occurred.\n\n'
            'fabrication_manifest_DRAFT.json hashes all payload files except itself. '
            'SHA256SUMS_DRAFT.json separately hashes that manifest and the ZIP; neither '
            'checksum document claims to contain its own recursive digest.\n', encoding='utf-8')
        for name, digest in inputs.items():
            require(sha(ROOT / name) == digest and sha(snapshot / name) == digest,
                    f'Input changed during export: {name}')
        require({relative(path) for path in design_sources(routing_run)} == inventory,
                'Source inventory changed during fabrication export')
        manifest = {'status': STATUS, 'started_utc': started.isoformat(),
                    'finished_utc': datetime.now(timezone.utc).isoformat(),
                    'input_sha256': inputs, 'kicad_version': p.GetBuildVersion(),
                    'toolchain_sha256': toolchain_hashes,
                    'native_board': native_summary, 'layers': list(LAYERS),
                    'routing_run': routing_run, 'protected_kelvin_check': protected_check,
                    'gerber_x2_functions': {','.join(k): v.relative_to(payload).as_posix() for k, v in functions.items()},
                    'coordinate_checks': coordinates, 'fresh_drc_counts': {k: 0 for k in DRC_KEYS},
                    'native_drc_ignored_checks': fresh.get('ignored_checks', []),
                    'commands': commands, 'assembly_sourcing_unresolved': assembly.get('unresolved_sourcing', []),
                    'fabricator_stackup_verified': False, 'production_release': False,
                    'output_sha256': {f.relative_to(payload).as_posix(): sha(f)
                                      for f in sorted(payload.rglob('*')) if f.is_file()}}
        write_json(payload / 'fabrication_manifest_DRAFT.json', manifest)
        zip_path = work / (label + '.zip')
        with zipfile.ZipFile(zip_path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(payload.rglob('*')):
                if file.is_file():
                    archive.write(file, arcname=file.relative_to(payload).as_posix())
        with zipfile.ZipFile(zip_path) as archive:
            require(archive.testzip() is None, 'ZIP integrity check failed')
            require(set(archive.namelist()) == {f.relative_to(payload).as_posix()
                                               for f in payload.rglob('*') if f.is_file()},
                    'ZIP file inventory differs')
        zip_digest = sha(zip_path)
        receipt = {'status': STATUS, 'sha256': {f.relative_to(payload).as_posix(): sha(f)
                                              for f in sorted(payload.rglob('*')) if f.is_file()}}
        receipt['sha256'][zip_path.name] = zip_digest
        zip_path.rename(payload / zip_path.name)
        write_json(payload / 'SHA256SUMS_DRAFT.json', receipt)
        for name, digest in inputs.items():
            require(sha(ROOT / name) == digest, f'Input changed before package commit: {name}')
        require({relative(path) for path in design_sources(routing_run)} == inventory,
                'Source inventory changed before package commit')
        require(all(sha(path) == digest for path, digest in toolchain_hashes.items()),
                'KiCad CLI/Python executable changed during fabrication export')
        # Commit only a completed package. No overwrite/delete of previous exports.
        payload.rename(target)
    print(json.dumps({'status': STATUS, 'directory': str(target),
                      'zip': str(target / (label + '.zip')), 'zip_sha256': zip_digest}, indent=2))


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    export()
