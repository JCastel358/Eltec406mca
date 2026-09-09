"""Independent checks against the KiCad-exported interface netlist.

The expected socket/module pin relationships come from original CAD and the
receiver-buffer datasheet, not from re-reading the generator's part manifest.
These checks prove interface connectivity only; they do not certify sequencing.
"""
from pathlib import Path
import hashlib
import json
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
NETLIST = ROOT / 'reports/interface_netlist.xml'
data = ET.parse(NETLIST).getroot()
pin_net = {}
for net in data.findall('nets/net'):
    for node in net.findall('node'):
        key = (node.get('ref'), node.get('pin'))
        if key in pin_net:
            raise ValueError(f'Pin appears in more than one net: {key}')
        pin_net[key] = net.get('name')

checks = []


def check(description, result, detail=''):
    checks.append(dict(description=description, passed=bool(result), detail=detail))


def same(description, *pins):
    nets = [pin_net.get((ref, str(pin))) for ref, pin in pins]
    check(description, None not in nets and len(set(nets)) == 1,
          repr(list(zip(pins, nets))))


# Original module pad numbering and socket physical remapping.
for pin in (1, 2, 5, 8, 9, 10, 15, 16, 17, 22, 23, 24, 26):
    same(f'ESP32 used pin{pin} mates to correct socket',
         ('U1', pin), ('J3' if pin <= 15 else 'J4', pin if pin <= 15 else pin-15))
for pin in range(1, 9):
    same(f'ADC digital pin{pin} mates to J5', ('U2', pin), ('J5', pin))
for channel in range(8):
    same(f'ADC AIN{channel} mates to J6 odd pin', ('U2', 9+channel), ('J6', 2*channel+1))
    same(f'ADC ground{channel} mates to J6 even pin', ('U2', 17+channel), ('J6', 2*channel+2))
same('All carrier module grounds explicitly common', ('U1', 2), ('U1', 17),
     ('U2', 2), *[('U2', 17+i) for i in range(8)])
same('Unused AIN6 grounded', ('U2', 15), ('U2', 2))
same('Unused AIN7 grounded; no legacy battery divider', ('U2', 16), ('U2', 2))

paths = [
    ('SCLK', ('U1', 9), ('U50', 2), ('U50', 6), ('R56', 1), ('R56', 2), ('U2', 3)),
    ('MOSI', ('U1', 15), ('U50', 5), ('U50', 3), ('R57', 1), ('R57', 2), ('U2', 4)),
    ('CS', ('U1', 8), ('U51', 2), ('U51', 6), ('R58', 1), ('R58', 2), ('U2', 7)),
    ('MISO', ('U2', 5), ('U52', 2), ('U52', 6), ('R59', 1), ('R59', 2), ('U1', 10)),
    ('DRDY', ('U2', 6), ('U52', 5), ('U52', 3), ('R60', 1), ('R60', 2), ('U1', 5)),
]
for name, origin, buf_in, buf_out, resistor_in, resistor_out, destination in paths:
    same(name+' enters correct buffer input', origin, buf_in)
    same(name+' buffer output enters damping resistor', buf_out, resistor_in)
    same(name+' damping resistor reaches correct receiver', resistor_out, destination)
    check(name+' does not bypass its buffer', pin_net.get((origin[0], str(origin[1]))) !=
          pin_net.get((destination[0], str(destination[1]))))

same('Forward buffers use ADC 3.3V domain', ('U50', 8), ('U51', 8), ('R52', 1))
same('Return buffer uses ESP32 3.3V domain', ('U52', 8), ('U1', 1), ('R55', 1))
check('Buffer supply rails not shorted', pin_net[('U50', '8')] != pin_net[('U52', '8')])
check('OE collectors not shorted across supplies', pin_net[('Q50', '3')] != pin_net[('Q51', '3')])
same('Forward OE separate local pull-up', ('U50', 1), ('U50', 7), ('U51', 1), ('Q50', 3), ('R52', 2))
same('Return OE separate local pull-up', ('U52', 1), ('U52', 7), ('Q51', 3), ('R55', 2))
same('Spare buffer input grounded', ('U51', 5), ('U2', 2))
same('Spare buffer disabled', ('U51', 7), ('U51', 8))

report = dict(scope='Interface connectivity only; power/analog/emitter design not certified',
              netlist_sha256=hashlib.sha256(NETLIST.read_bytes()).hexdigest(),
              all_passed=all(c['passed'] for c in checks), checks=checks)
(ROOT / 'reports/interface_checks.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
print(f'{sum(c["passed"] for c in checks)}/{len(checks)} interface checks passed')
if not report['all_passed']:
    for result in checks:
        if not result['passed']:
            print(result)
    raise SystemExit(1)
