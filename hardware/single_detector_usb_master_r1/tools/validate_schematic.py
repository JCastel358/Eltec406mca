"""Rebuild and check a stable native schematic snapshot; never route or order.

Run with KiCad's bundled Python. Records exact commands and hashes. A failed
build/export cannot silently leave an old report presented as a fresh pass.
"""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / 'single_detector_usb_master'
REPORTS = ROOT / 'reports'
CLI = Path(sys.executable).with_name('kicad-cli.exe')
SCH = PROJECT / 'single_detector_usb_master.kicad_sch'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--no-build', action='store_true',
                    help='Validate current native sources; preserve reviewed library silk and routed work')
args = parser.parse_args()
report = {'started_utc': datetime.now(timezone.utc).isoformat(),
          'scope': 'Schematic build, ERC, exported connectivity and documented conditional arithmetic',
          'complete': False, 'commands': []}


def run(args, output=None):
    if output is not None:
        output = Path(output)
        assert output.resolve().is_relative_to(ROOT.resolve())
        if output.exists():
            output.unlink()
    result = subprocess.run([str(a) for a in args], cwd=ROOT, text=True,
                            capture_output=True, timeout=180)
    report['commands'].append({'argv': [str(a) for a in args],
                               'returncode': result.returncode,
                               'stdout': result.stdout, 'stderr': result.stderr})
    if result.returncode:
        raise RuntimeError(f'Command failed ({result.returncode}): {args}\n{result.stdout}\n{result.stderr}')
    if output is not None and not output.is_file():
        raise RuntimeError(f'Command did not create expected output: {output}')


try:
    if not args.no_build:
        run([sys.executable, ROOT / 'tools/build_design.py'])
    report['rebuilt_schematic'] = not args.no_build
    snapshot = list(PROJECT.glob('*.kicad_sch')) + list(PROJECT.glob('*.kicad_pro'))
    snapshot += list(PROJECT.glob('*lib-table')) + list((ROOT / 'libraries').rglob('*.kicad_sym'))
    snapshot += list((ROOT / 'libraries').rglob('*.kicad_mod'))
    snapshot += [ROOT / 'tools' / name for name in (
        'cad.py', 'build_design.py', 'power_stage.py', 'spi_stage.py',
        'analog_stage.py', 'emitter_stage.py', 'check_interfaces.py',
        'check_analog.py', 'power_corners.py', 'validate_schematic.py')]
    report['source_sha256'] = {str(f.relative_to(ROOT)): hashlib.sha256(f.read_bytes()).hexdigest()
                               for f in sorted(snapshot)}
    run([CLI, 'sch', 'export', 'netlist', '--format', 'kicadxml', '-o',
         REPORTS / 'interface_netlist.xml', SCH], REPORTS / 'interface_netlist.xml')
    run([CLI, 'sch', 'erc', '--format', 'json', '--severity-all', '--exit-code-violations',
         '-o', REPORTS / 'interface_erc.json', SCH], REPORTS / 'interface_erc.json')
    run([sys.executable, ROOT / 'tools/check_interfaces.py'], REPORTS / 'interface_checks.json')
    run([sys.executable, ROOT / 'tools/check_analog.py'], REPORTS / 'analog_checks.json')
    run([sys.executable, ROOT / 'tools/power_corners.py'], REPORTS / 'power_corners.json')
    erc = json.loads((REPORTS / 'interface_erc.json').read_text())
    violations = [v for sheet in erc['sheets'] for v in sheet.get('violations', [])]
    report['erc_violation_count'] = len(violations)
    report['erc_ignored_checks'] = erc.get('ignored_checks', [])
    report['ignored_check_review'] = {'simulation_model_issue':
        'Native schematic uses electrical pin checks; no complete SPICE simulation model is claimed.'}
    unexplained_ignored = [row for row in report['erc_ignored_checks']
                          if row['key'] not in report['ignored_check_review']]
    report['unexplained_ignored_checks'] = unexplained_ignored
    report['erc_exclusion_count'] = sum(v.get('severity') == 'exclusion' for v in violations)
    report['interface_checks'] = json.loads((REPORTS / 'interface_checks.json').read_text())
    report['analog_checks'] = json.loads((REPORTS / 'analog_checks.json').read_text())
    report['power_checks'] = json.loads((REPORTS / 'power_corners.json').read_text())
    report['netlist_sha256'] = hashlib.sha256((REPORTS / 'interface_netlist.xml').read_bytes()).hexdigest()
    changed = [name for name, digest in report['source_sha256'].items()
               if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest]
    if changed:
        raise RuntimeError(f'Inputs changed during validation: {changed}')
    report['complete'] = (not violations and not unexplained_ignored and report['interface_checks']['all_passed']
                          and report['analog_checks']['all_passed']
                          and report['power_checks']['exported_netlist_checks']['passed'])
    report['does_not_prove'] = ['physical power sequencing or unverified acceptance inputs', 'PCB routing/DRC/parity',
                                'mechanical fit', 'assembly sourcing', 'production qualification']
finally:
    report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    (REPORTS / 'schematic_validation.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')

print('Schematic snapshot passed ERC, connectivity and conditional arithmetic checks.' if report['complete'] else 'Schematic validation incomplete.')
if not report['complete']:
    raise SystemExit(1)
