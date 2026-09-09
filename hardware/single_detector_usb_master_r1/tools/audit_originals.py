"""Verify original supplied archive members, retained copies and firmware hashes."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT=Path(__file__).resolve().parents[1]

def digest(data):return hashlib.sha256(data).hexdigest()

def main():
    source=ROOT/'source_reference'
    records=json.loads((source/'source_provenance.json').read_text())
    checks=[]
    for row in records:
        if row.get('archive','').lower().endswith('.zip'):
            with zipfile.ZipFile(row['archive']) as archive:data=archive.read(row['member'])
            retained=source/row['member']
            assert digest(data).lower()==row['sha256'].lower()
            assert digest(retained.read_bytes()).lower()==row['sha256'].lower()
            checks.append({'source':row['archive'],'member':row['member'],
                           'sha256':digest(data),'archive_and_retained_copy_match':True})
        else:
            assert row['member']=='original bytes (extension added for JPEG display)'
            path=Path(row['archive'])
            retained=source/(path.name if path.suffix else path.name+'.jpg')
            assert digest(path.read_bytes()).lower()==row['sha256'].lower()
            assert digest(retained.read_bytes()).lower()==row['sha256'].lower()
            checks.append({'source':str(path),'sha256':digest(path.read_bytes()),
                           'original_and_retained_copy_match':True})
    original=ROOT.parents[1]/'Arduino/Eltec/Eltec.ino'
    sketch=ROOT/'firmware/Eltec_USB_Master/Eltec_USB_Master.ino'
    expected={'original':'ded93d6270e217e0b35a20470bb8e9490321be4eca5b9ffb6a42077f57c0fb54',
              'r1':'753437ecdca873d92a8e545318515d49c33812aac60535c2385d4d08cb5bdd0c'}
    assert digest(original.read_bytes())==expected['original']
    assert digest(sketch.read_bytes())==expected['r1']
    report={'status':'ORIGINAL_ARCHIVE_MEMBERS_AND_RETAINED_COPIES_UNCHANGED',
            'checks':checks,'firmware_sha256':expected,'no_original_written':True}
    (ROOT/'reports/original_preservation_final.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'checked_records':len(checks),'firmware_hashes_match':True}))

if __name__=='__main__':main()
