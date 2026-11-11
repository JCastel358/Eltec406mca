"""Check source identity and package the editable engineering handoff."""
from pathlib import Path
import csv
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    report = json.loads((ROOT / 'reports/validation.json').read_text())
    assert report['erc_violations'] == 0 and not report['erc_ignored_checks']
    for relative, expected in report['source_sha256'].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected, relative
    parts = list(csv.DictReader((ROOT / 'exports/BOM.csv').open(encoding='utf-8-sig')))
    assert len(parts) == len({p['ref'] for p in parts})
    for part in parts:
        if part['footprint']:
            library, name = part['footprint'].split(':')
            assert (ROOT / 'libraries/gain.pretty' / (name + '.kicad_mod')).exists(), part['ref']
            assert library in (ROOT / 'project/fp-lib-table').read_text()
        else:
            assert part['ref'] == 'SW1', part['ref']
    files = []
    for p in sorted(ROOT.rglob('*')):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if '__pycache__' in rel.parts or p.suffix in ('.kicad_prl', '.lck', '.zip'):
            continue
        if '-backups' in str(rel) or (rel.parts[0] == 'reports' and p.suffix == '.png'):
            continue
        if p.name in ('DELIVERY_SHA256.json', 'validation_run.json'):
            continue
        files.append(p)
    digests = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    manifest = ROOT / 'reports/DELIVERY_SHA256.json'
    manifest.write_text(json.dumps(digests, indent=2) + '\n')
    files.append(manifest)
    target = ROOT.with_suffix('.zip')
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, Path(ROOT.name) / p.relative_to(ROOT))
    with zipfile.ZipFile(target) as z:
        assert z.testzip() is None
        for relative, expected in digests.items():
            assert hashlib.sha256(z.read(ROOT.name + '/' + relative)).hexdigest() == expected
    print(json.dumps({'archive': str(target), 'files': len(files), 'bytes': target.stat().st_size,
                      'bom_parts': len(parts), 'verified_source_hashes': len(report['source_sha256'])}, indent=2))


if __name__ == '__main__':
    main()
