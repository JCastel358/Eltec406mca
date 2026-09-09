"""Package a checked native candidate and review evidence without ordering/uploading."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]
BOARD=ROOT/'single_detector_usb_master/single_detector_usb_master.kicad_pcb'
CLI=Path(sys.executable).with_name('kicad-cli.exe')

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text())

def main(review_name,fabrication_name):
    review=(ROOT/'exports/board_review'/review_name).resolve()
    fab=(ROOT/'exports/fabrication_draft'/fabrication_name).resolve()
    assert review.parent==(ROOT/'exports/board_review').resolve()
    assert fab.parent==(ROOT/'exports/fabrication_draft').resolve()
    board=read(ROOT/'reports/board_validation.json')
    assert board['native_data_checks_passed'] and board['board_after_refill_sha256']==sha(BOARD)
    schematic=read(ROOT/'reports/schematic_validation.json')
    assert schematic['complete']
    for rel,digest in schematic['source_sha256'].items():assert sha(ROOT/rel)==digest
    assert sha(ROOT/'exports/review/schematic_candidate.pdf')=='0bcee143453598c67100fae68172a60e036c274a5193b948fb6fc26bc12ff1ec'
    assert read(ROOT/'reports/copper_review_complete/complete_validation.json')['source_sha256']==sha(BOARD)
    assembly=read(ROOT/'reports/assembly_export.json')
    assert assembly['input_sha256'][str(BOARD.relative_to(ROOT))]==sha(BOARD)
    assert read(review/'visual_review.json')['status']=='ALL_SEVEN_PAGES_VISUALLY_REVIEWED'
    assert read(fab/'visual_review.json')['status']=='ALL_DRILL_MAP_PAGES_VISUALLY_REVIEWED'
    fabrication=read(fab/'fabrication_manifest_DRAFT.json')
    assert fabrication['input_sha256'][str(BOARD.relative_to(ROOT)).replace('\\','/')]==sha(BOARD)
    for path,digest in read(review/'review_manifest.json')['source_hashes'].items():
        assert sha(ROOT/path)==digest, 'Review source changed: '+path
    files=set()
    def include(folder,suffixes=None):
        for path in folder.rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and (suffixes is None or path.suffix in suffixes):files.add(path)
    for folder in ('libraries','research','source_reference','firmware'):include(ROOT/folder)
    include(ROOT/'single_detector_usb_master',{'.kicad_pcb','.kicad_sch','.kicad_pro','.kicad_dru'})
    files.update((ROOT/'single_detector_usb_master').glob('*-lib-table'))
    files.update(ROOT.glob('*.md'));files.update((ROOT/'tools').glob('*.py'))
    files.update(p for p in (ROOT/'reports').iterdir() if p.is_file() and p.suffix in ('.json','.xml','.md','.log'))
    # Keep the provenance, paths and native check evidence; omit old candidate
    # boards and diagnostic images so there is one editable current project.
    for folder in (ROOT/'reports').iterdir():
        if folder.is_dir():include(folder,{'.json','.md','.log','.ses','.dsn'})
    include(ROOT/'exports/assembly_draft')
    files.add(ROOT/'exports/review/schematic_candidate.pdf')
    files.update(p for p in review.iterdir() if p.is_file())
    include(fab)
    files.add(ROOT/'exports/fabrication_draft/README.md')
    hashes={path.relative_to(ROOT).as_posix():sha(path) for path in sorted(files)}
    out=ROOT/'exports/review_package';out.mkdir(exist_ok=True)
    name='usb_master_r1_REVIEW_'+sha(BOARD)[:12]
    target=out/(name+'.zip');assert not target.exists()
    with tempfile.TemporaryDirectory(prefix='usb_master_review_') as tmp:
        stage=Path(tmp)
        for path in files:
            dest=stage/path.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True)
            dest.write_bytes(path.read_bytes())
        # Reopen from the portable directory tree; local tables must resolve
        # their ../libraries paths without the original workspace location.
        drc_path=stage/'portable_check.json'
        command=[str(CLI),'pcb','drc','--format','json','--severity-all',
                 '--schematic-parity','--exit-code-violations','--output',str(drc_path),
                 str(stage/BOARD.relative_to(ROOT))]
        run=subprocess.run(command,capture_output=True,text=True,timeout=180)
        assert run.returncode==0,run.stdout+run.stderr
        drc=read(drc_path)
        assert all(not drc[key] for key in ('violations','unconnected_items','schematic_parity'))
        for rel,digest in hashes.items():
            assert sha(ROOT/rel)==digest and sha(stage/rel)==digest
        manifest={'status':'PORTABLE_ENGINEERING_REVIEW_PACKAGE_NOT_PRODUCTION_RELEASE',
                  'created_utc':datetime.now(timezone.utc).isoformat(),'board_sha256':sha(BOARD),
                  'source_files':hashes,'portable_native_drc':{'violations':0,'unconnected':0,'parity':0,
                   'stdout':run.stdout,'stderr':run.stderr,'command':command},
                  'review_bundle':review_name,'fabrication_bundle':fabrication_name,
                  'unperformed':'Physical acceptance and JLC allocation/rotation/process confirmation',
                  'no_upload_or_order':True}
        (stage/'PACKAGE_MANIFEST.json').write_text(json.dumps(manifest,indent=2)+'\n')
        with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED) as z:
            for rel in sorted(hashes):z.write(stage/rel,ROOT.name+'/'+rel)
            z.write(stage/'PACKAGE_MANIFEST.json',ROOT.name+'/PACKAGE_MANIFEST.json')
        with zipfile.ZipFile(target) as z:
            assert z.testzip() is None
            for rel,digest in hashes.items():assert hashlib.sha256(z.read(ROOT.name+'/'+rel)).hexdigest()==digest
    receipt={'zip':target.name,'sha256':sha(target),'board_sha256':sha(BOARD),
             'included_files':len(hashes)+1,'portable_native_drc_passed':True}
    (out/(name+'.json')).write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,indent=2))

if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--review',required=True);ap.add_argument('--fabrication',required=True)
    args=ap.parse_args();main(args.review,args.fabrication)
