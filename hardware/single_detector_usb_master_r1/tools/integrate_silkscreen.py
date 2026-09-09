"""Install the checked copy-only silk result and matching project footprints.

Refuses changed input or candidate hashes; backs up every replaced file first.
Never copies candidate library tables or any files into the original project.
"""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import shutil

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT/'reports/silkscreen_cleanup'
BOARD = ROOT/'single_detector_usb_master/single_detector_usb_master.kicad_pcb'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def integrate():
    data = json.loads((WORK/'cleanup_report.json').read_text())
    candidate = WORK/'candidate'/BOARD.name
    if sha(BOARD) != data['source_sha256'] or sha(candidate) != data['output_sha256']:
        raise RuntimeError('PCB input or silk candidate changed; regenerate/review the cleanup')
    categories = data['after_drc']['categories']
    if any(count for kind, count in categories.items() if kind != 'unconnected_items'):
        raise RuntimeError('Silk candidate still has unresolved native violations/parity')
    if not data['all_references_visible'] or not data['non_silk_identity_preserved']:
        raise RuntimeError('Silk identity/visibility guard failed')
    replacements = [(BOARD, candidate)]
    for row in data['candidate_libraries']:
        filename = row['name']+'.kicad_mod'
        if Path(filename).name != filename:
            raise ValueError('Invalid library filename')
        target, changed = ROOT/'libraries/master.pretty'/filename, WORK/'libraries/master.pretty'/filename
        if sha(target) != row['source_sha256'] or sha(changed) != row['candidate_sha256']:
            raise RuntimeError(f'Library source/candidate changed: {filename}')
        replacements.append((target, changed))
    backup = WORK/('integrated_from_'+sha(BOARD)[:16])
    backup.mkdir(exist_ok=False)
    records=[]
    for target, changed in replacements:
        relative = target.relative_to(ROOT)
        original = backup/relative
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target, original)
        if sha(original) != sha(target):
            raise RuntimeError('Backup verification failed')
        records.append({'target': str(relative), 'before_sha256': sha(target),
                        'after_sha256': sha(changed), 'backup': str(original.relative_to(ROOT))})
    # Every destination is an existing, project-local file checked above.
    for (target, changed), row in zip(replacements, records):
        if sha(target) != row['before_sha256'] or sha(changed) != row['after_sha256']:
            raise RuntimeError('Source changed immediately before integration')
        shutil.copyfile(changed, target)
        if sha(target) != row['after_sha256']:
            raise RuntimeError('Copy verification failed')
    result={'completed_utc': datetime.now(timezone.utc).isoformat(), 'files': records,
            'matching_native_board_and_libraries_installed': True,
            'project_library_tables_untouched': True, 'original_source_project_untouched': True,
            'status': 'SILK_INTEGRATED_RECHECK_MAIN_PROJECT_DRC'}
    (ROOT/'reports/silkscreen_integration.json').write_text(json.dumps(result,indent=2)+'\n')
    print(f'Integrated checked silk in board and {len(records)-1} matching footprint files; verified backups retained.')


if __name__ == '__main__':
    integrate()
