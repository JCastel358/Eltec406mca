"""Enable normally ignored geometry checks on a copy; do not edit source CAD."""
from pathlib import Path
from collections import Counter
import hashlib
import json
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'single_detector_usb_master'
DEST = ROOT/'reports/default_drc_probe/single_detector_usb_master'
DEST.mkdir(parents=True, exist_ok=True)
inputs = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
          for path in SOURCE.iterdir() if path.is_file() and path.suffix in
          {'.kicad_pcb', '.kicad_pro', '.kicad_sch', '.kicad_dru'}}
for name in inputs:
    shutil.copy2(ROOT/name, DEST/Path(name).name)
for name in ('fp-lib-table', 'sym-lib-table'):
    text = (SOURCE/name).read_text().replace('${KIPRJMOD}/../libraries',
                                           (ROOT/'libraries').as_posix())
    (DEST/name).write_text(text, encoding='utf-8')
project = DEST/'single_detector_usb_master.kicad_pro'
data = json.loads(project.read_text())
enabled = ['missing_courtyard', 'track_not_centered_on_via',
           'footprint_filters_mismatch', 'footprint_type_mismatch']
data['board']['design_settings'].setdefault('rule_severities', {}).update(
    {name: 'warning' for name in enabled})
project.write_text(json.dumps(data, indent=2)+'\n', encoding='utf-8')
output = DEST.parent/'drc.json'
if output.exists():
    output.unlink()
argv = [str(Path(sys.executable).with_name('kicad-cli.exe')), 'pcb', 'drc',
        '--format', 'json', '--severity-all', '--schematic-parity',
        '--output', str(output), str(DEST/'single_detector_usb_master.kicad_pcb')]
run = subprocess.run(argv, text=True, capture_output=True, timeout=180)
if run.returncode or not output.exists():
    raise RuntimeError(run.stdout+run.stderr)
report = json.loads(output.read_text())
counts = {key: dict(Counter(v['type'] for v in report.get(key, [])))
          for key in ('violations', 'unconnected_items', 'schematic_parity')}
if any(hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != digest
       for name, digest in inputs.items()):
    raise RuntimeError('Source changed during copy probe')
(DEST.parent/'report.json').write_text(json.dumps({
    'scope': 'Copy-only native DRC with normally ignored applicable checks enabled',
    'input_sha256': inputs, 'enabled_checks': enabled, 'counts': counts,
    'remaining_ignored_checks': report.get('ignored_checks', []),
    'command': argv, 'stdout': run.stdout, 'stderr': run.stderr,
}, indent=2)+'\n', encoding='utf-8')
print(json.dumps(counts, indent=2))
