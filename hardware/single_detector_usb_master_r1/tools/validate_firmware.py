"""Compile the separate carrier firmware with installed tools; never upload."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CLI = Path(os.environ['LOCALAPPDATA'])/'Programs/Arduino IDE/resources/app/lib/backend/resources/arduino-cli.exe'
SOURCE = ROOT.parents[1]/'Arduino/Eltec/Eltec.ino'
FIRMWARE = ROOT/'firmware/Eltec_USB_Master/Eltec_USB_Master.ino'
BUILD = ROOT/'tmp/firmware_build'
REPORT = ROOT/'reports/firmware_validation.json'
LOG = ROOT/'reports/firmware_compile.log'
source_hash = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
report = dict(started_utc=datetime.now(timezone.utc).isoformat(), complete=False,
              flashed=False, fqbn='esp32:esp32:esp32', commands=[])


def run(args):
    args = [str(a) for a in args]
    result = subprocess.run(args,cwd=ROOT,text=True,capture_output=True,timeout=600)
    record = dict(argv=args,returncode=result.returncode,stdout=result.stdout,stderr=result.stderr)
    report['commands'].append(record)
    if result.returncode:
        raise RuntimeError(f'Command failed:{args}\n{result.stdout}\n{result.stderr}')
    return result


try:
    run([sys.executable,ROOT/'tools/prepare_firmware.py'])
    run([CLI,'version'])
    run([CLI,'core','list'])
    result = run([CLI,'compile','--fqbn',report['fqbn'],'--warnings','all',
                  '--build-path',BUILD,FIRMWARE.parent])
    LOG.write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
    report['firmware_sha256'] = hashlib.sha256(FIRMWARE.read_bytes()).hexdigest()
    report['original_source_sha256'] = source_hash
    report['original_source_unchanged'] = hashlib.sha256(SOURCE.read_bytes()).hexdigest()==source_hash
    report['build_outputs'] = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted(BUILD.glob('*.bin'))}
    run([sys.executable,ROOT/'tools/check_firmware_protocol.py','--require-current-sketch'])
    report['protocol_checks'] = json.loads((ROOT/'reports/firmware_protocol_checks.json').read_text())
    report['complete'] = report['original_source_unchanged'] and bool(report['build_outputs'])
    report['limitations'] = [
        'Compile uses generic ESP32 target and installed core; actual module/flash configuration must be confirmed before flashing.',
        'Compile is not a hardware power-cycle, serial/app, noise or fault-injection test.',
        'This validation never selects a serial port and never uploads firmware.'
    ]
finally:
    report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    REPORT.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print('Separate R1 firmware compiled; no upload performed.' if report['complete'] else 'Firmware validation incomplete.')
if not report['complete']:
    raise SystemExit(1)
