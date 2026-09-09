"""Export hash-linked draft engineering/JLC BOMs and native-board placements.

No upload, sourcing reservation, fabrication or ordering. Includes fitted THT
sockets/trimmer. Customer-installed modules/jacks and copper wire lands are
separate. Placement offsets are explicit; JLC library rotation review is pending.
"""
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
import csv
import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile

import pcbnew as p

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'single_detector_usb_master/single_detector_usb_master'
BOARD = BASE.with_suffix('.kicad_pcb')
MANIFEST = ROOT / 'reports/design_manifest.json'
CATALOG = ROOT / 'research/verified_catalog.json'
OUTPUT = ROOT / 'exports/assembly_draft'
CLI = Path(sys.executable).with_name('kicad-cli.exe')
# Manufacturer-derived body centers of the project-owned socket drawings and
# Bourns3296W native Fab body. Rotation is applied in KiCad's screen coordinates.
LOCAL_OFFSETS = {'J3': (17.78, 0), 'J4': (17.78, 0), 'J5': (8.89, 0),
                 'J6': (8.89, 1.27), 'RV100': (-2.54, .005)}
ORIGIN = (12.0, 115.0)  # Fixed bottom-left corner in original native coordinates.


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def natural(ref):
    return tuple(int(x) if x.isdigit() else x for x in re.split(r'(\d+)', ref))


def manufacturer_key(value):
    normalized = re.sub(r'[^a-z0-9]', '', value.casefold())
    return {'shenzhenkinghelmelec': 'kinghelm', 'texasinstrumentsti': 'texasinstruments',
            'kyoceraavxcomponents': 'kyoceraavx'}.get(normalized, normalized)


def write_csv(path, columns, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def export():
    hashes = {str(f.relative_to(ROOT)): sha(f) for f in
              [BOARD, MANIFEST, BASE.with_suffix('.kicad_pro'),
               *BASE.parent.glob('*.kicad_sch')]}
    catalog = json.loads(CATALOG.read_text()) if CATALOG.exists() else {}
    if CATALOG.exists():
        hashes[str(CATALOG.relative_to(ROOT))] = sha(CATALOG)
    # Accept the documented direct MPN dictionary or a provenance wrapper.
    catalog = catalog.get('parts', catalog)
    manifest = {entry['ref']: entry for entry in json.loads(MANIFEST.read_text())}
    board = p.LoadBoard(str(BOARD))
    footprints = {fp.GetReference(): fp for fp in board.GetFootprints()}
    physical = {ref for ref, row in manifest.items() if row.get('footprint')}
    if physical != set(footprints):
        raise ValueError(f'Board/manifest population differs: {physical ^ set(footprints)}')
    fitted, omitted, copper = [], [], []
    for ref, row in manifest.items():
        if not row.get('physical', True):
            if row.get('footprint'):
                copper.append(ref)
            continue
        if not row.get('populated', True):
            omitted.append(ref)
        else:
            fitted.append(ref)
    if set(omitted) != {'U1', 'U2', 'J1', 'J2'}:
        raise ValueError(f'Customer-install boundary changed: {omitted}')
    if not set(fitted).issubset(footprints):
        raise ValueError('Populated purchase is missing a real board footprint')
    engineering, placements, offsets, shortages = [], [], [], []
    grouped = defaultdict(list)
    for ref in sorted(fitted, key=natural):
        row, fp = manifest[ref], footprints[ref]
        if fp.IsDNP() or fp.IsExcludedFromBOM() or fp.IsExcludedFromPosFiles():
            raise ValueError(f'{ref}: native flags incorrectly omit required populated component')
        if fp.GetValue() != row['value'] or str(fp.GetFPID().GetLibItemName()) != row['footprint'].split(':')[-1]:
            raise ValueError(f'{ref}: stale native value or footprint')
        if not fp.GetPath().AsString().endswith('/'+row['uuid']):
            raise ValueError(f'{ref}: native schematic relationship changed')
        selected = catalog.get(row['mpn'], {})
        if selected and manufacturer_key(selected['manufacturer']) != manufacturer_key(row['manufacturer']):
            raise ValueError(f'{ref}: catalog manufacturer differs from the selected manufacturer')
        lcsc = selected.get('lcsc') or row.get('lcsc', '')
        if lcsc and not re.fullmatch(r'C\d+', lcsc):
            raise ValueError(f'{ref}: invalid catalog identity {lcsc}')
        if selected.get('lcsc') and row.get('lcsc') and selected['lcsc'] != row['lcsc']:
            raise ValueError(f'{ref}: conflicting catalog identities')
        tech = 'THT' if fp.GetAttributes() & p.FP_THROUGH_HOLE else 'SMT'
        entry = {'Designator': ref, 'Value': row['value'], 'Manufacturer': row['manufacturer'],
                 'MPN': row['mpn'], 'Footprint': row['footprint'], 'JLCPCB Part #': lcsc,
                 'Assembly': tech, 'Catalog source': selected.get('verified_url', ''),
                 'Assembly status': selected.get('assembly_status', 'Allocation and library rotation unverified')}
        engineering.append(entry)
        if not row['mpn'] or not lcsc:
            shortages.append({'reference': ref, 'mpn': row['mpn'],
                              'missing': [name for name, val in [('exact MPN', row['mpn']), ('catalog identity', lcsc)] if not val]})
        grouped[(row['mpn'], row['value'], row['footprint'], lcsc)].append(ref)
        if fp.GetLayer() != p.F_Cu:
            raise ValueError('Bottom-side placement requires separately reviewed rotation transform')
        x, y = fp.GetPosition().x/1e6, fp.GetPosition().y/1e6
        angle = fp.GetOrientationDegrees()
        dx, dy = LOCAL_OFFSETS.get(ref, (0, 0))
        radians = math.radians(-angle)
        cx, cy = x+dx*math.cos(radians)-dy*math.sin(radians), y+dx*math.sin(radians)+dy*math.cos(radians)
        midx, midy = cx-ORIGIN[0], ORIGIN[1]-cy
        if not all(math.isfinite(v) for v in (midx, midy, angle)) or not (0 <= midx <= 86 and 0 <= midy <= 145):
            raise ValueError(f'{ref}: placement outside candidate outline')
        placements.append({'Designator': ref, 'Mid X': f'{midx:.6f}', 'Mid Y': f'{midy:.6f}',
                           'Rotation': f'{angle % 360:.6f}', 'Layer': 'Top'})
        offsets.append({'ref': ref, 'native_origin_mm': [x, y], 'body_center_local_offset_mm': [dx, dy],
                        'body_center_native_mm': [cx, cy], 'native_rotation_deg': angle,
                        'JLC_rotation_offset_deg': 0, 'JLC_rotation_verified': False,
                        'basis': 'Manufacturer-drawn THT body center' if ref in LOCAL_OFFSETS else
                                 'Native SMT footprint body origin; vendor library orientation review pending'})
    bom = [{'Comment': key[0] or key[1], 'Designator': ','.join(sorted(refs, key=natural)),
            'Footprint': key[2].split(':')[-1], 'JLCPCB Part #': key[3]}
           for key, refs in sorted(grouped.items(), key=lambda item: natural(item[1][0]))]
    bom_refs = [ref for row in bom for ref in row['Designator'].split(',')]
    cpl_refs = [row['Designator'] for row in placements]
    assert len(bom_refs) == len(set(bom_refs)) == len(cpl_refs) == len(set(cpl_refs))
    assert set(bom_refs) == set(cpl_refs) == set(fitted)
    for ref in copper:
        fp = footprints[ref]
        if not fp.IsExcludedFromBOM() or not fp.IsExcludedFromPosFiles():
            raise ValueError(f'Copper-only interface {ref} has purchased component flags')
    # Native CLI placement extraction is an independent consumer of the PCB.
    # It retains native footprint origins; compare before our explicit offsets.
    with tempfile.TemporaryDirectory(prefix='eltec_cpl_') as tmp:
        raw = Path(tmp)/'native.csv'
        command = [str(CLI), 'pcb', 'export', 'pos', '--format', 'csv', '--units', 'mm',
                   '--exclude-dnp', '--output', str(raw), str(BOARD)]
        result = subprocess.run(command, text=True, capture_output=True, timeout=180)
        if result.returncode:
            raise RuntimeError(result.stdout+result.stderr)
        native = {row['Ref']: row for row in csv.DictReader(raw.open(encoding='utf-8-sig'))}
        if set(native) != set(fitted):
            raise ValueError(f'Native CLI placement population differs: {set(native) ^ set(fitted)}')
        for row in offsets:
            n = native[row['ref']]
            if abs(float(n['PosX'])-row['native_origin_mm'][0]) > 2e-5 or abs(float(n['PosY'])+row['native_origin_mm'][1]) > 2e-5:
                raise ValueError(f"Native CLI origin differs: {row['ref']}")
            if abs((float(n['Rot'])-row['native_rotation_deg']+180) % 360-180) > .001:
                raise ValueError(f"Native CLI angle differs: {row['ref']}")
        raw_text = raw.read_text(encoding='utf-8-sig')
    changed = [name for name, digest in hashes.items() if sha(ROOT/name) != digest]
    if changed:
        raise ValueError(f'Inputs changed during assembly export: {changed}')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    write_csv(OUTPUT/'engineering_bom.csv', list(engineering[0]), engineering)
    write_csv(OUTPUT/'jlc_bom_DRAFT.csv', ['Comment', 'Designator', 'Footprint', 'JLCPCB Part #'], bom)
    write_csv(OUTPUT/'jlc_cpl_DRAFT.csv', ['Designator', 'Mid X', 'Mid Y', 'Rotation', 'Layer'], placements)
    customer = [{'Designator': ref, 'Item': manifest[ref]['value'], 'Installation': 'Customer installs after PCBA'}
                for ref in sorted(omitted, key=natural)]
    write_csv(OUTPUT/'customer_install.csv', ['Designator', 'Item', 'Installation'], customer)
    (OUTPUT/'native_origins.csv').write_text(raw_text, encoding='utf-8')
    report = {'status': 'DRAFT_ASSEMBLY_DATA_NOT_APPROVED_FOR_UPLOAD_OR_ORDER',
              'created_utc': datetime.now(timezone.utc).isoformat(), 'input_sha256': hashes,
              'populated_components': len(fitted), 'bom_groups': len(bom),
              'customer_install': sorted(omitted), 'copper_not_purchased': sorted(copper),
              'BOM_CPL_native_population_exact_match': True,
              'native_CLI_coordinates_and_angles_match': True,
              'native_CLI_command': command, 'placement_origin_native_mm': ORIGIN,
              'placement_corrections': offsets, 'unresolved_sourcing': shortages,
              'limitations': ['All JLC package rotations/allocations still need vendor preview review',
                              'Mechanical mating/underside clearances unconfirmed',
                              'This export does not certify routing, DRC, thermal or electrical performance'],
              'files': {path.name: sha(path) for path in OUTPUT.glob('*.csv')}}
    (ROOT/'reports/assembly_export.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    (OUTPUT/'README.md').write_text(
        '# Draft assembly data — do not order\n\n'
        f'{len(fitted)} fitted components, including sockets and trimmer. '
        'Customer installs only the ESP32, ADS1256 module and two barrel jacks. '
        'Detector/emitter wire lands are copper features.\n\n'
        f'{len(shortages)} references have unresolved sourcing fields; see each entry in assembly_export.json. '
        'The engineering BOM and sourcing report identify them. No substitution is authorized.\n\n'
        'CPL coordinates are millimeters from the candidate board bottom-left corner; '
        'positive X is right, positive Y is up, rotation is counterclockwise. '
        'THT connector/trimmer body-center offsets are explicit in assembly_export.json. '
        'Every vendor library rotation, allocation and assembly process still needs review.\n\n'
        'Generated from the native KiCad PCB; BOM/CPL/native fitted reference sets and native origins agree. '
        'Native routing checks are recorded separately in board_validation.json. '
        'Mechanical fit, electrical qualification and release remain separate gates.\n\n'
        '[JLC BOM/CPL guidance](https://jlcpcb.com/help/article/how-to-generate-the-bom-and-centroid-file-from-kicad), '
        '[placement format](https://jlcpcb.com/help/article/pick-place-file-for-pcb-assembly).\n', encoding='utf-8')
    print(f'Draft assembly: {len(fitted)} parts, {len(bom)} BOM groups, {len(shortages)} unresolved sourcing references.')


if __name__ == '__main__':
    export()
