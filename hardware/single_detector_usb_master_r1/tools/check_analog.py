"""Check actual exported analog connectivity and reproduce bounded calculations.

This is analytical verification, not SPICE or bench characterization. Device
limits and module assumptions are explicit; source hashes link the result to CAD.
"""
from pathlib import Path
import hashlib
import itertools
import json
import math
import re
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'reports/interface_netlist.xml'
doc = ET.parse(SOURCE).getroot()
parts = {p.get('ref'): p for p in doc.findall('components/comp')}
nets = {(p.get('ref'), p.get('pin')): n.get('name')
        for n in doc.findall('nets/net') for p in n.findall('node')}
checks = []


def check(label, ok, detail=None):
    checks.append(dict(description=label, passed=bool(ok), detail=detail))


def same(label, *pins):
    actual = [nets.get((r, str(p))) for r, p in pins]
    check(label, None not in actual and len(set(actual)) == 1, actual)


def ohms(ref):
    text = parts[ref].findtext('value')
    match = re.fullmatch(r'([0-9.]+)(R|k|M)\s+([0-9.]+)%', text)
    if not match:
        raise ValueError(f'Unknown resistance/tolerance: {ref} {text}')
    return float(match[1]) * {'R': 1, 'k': 1000, 'M': 1e6}[match[2]], float(match[3])/100


for ch in range(6):
    amp = f'U{33+ch//2}'
    sw = f'U{30+ch//2}'
    plus, minus, out = (3, 2, 1) if ch % 2 == 0 else (5, 6, 7)
    signal, ground, dsignal, dground = (14, 3, 15, 2) if ch % 2 == 0 else (11, 6, 10, 7)
    rload, rin, rout, rground, rbleed, rf, rg = [f'R{base+ch}' for base in (200,210,220,230,240,250,260)]
    same(f'Ch{ch} sensor sees retained100k and input resistor', (f'JDET{ch+1}',2), (rload,1), (rin,1))
    same(f'Ch{ch} opamp positive input behind10k', (rin,2), (amp,plus))
    same(f'Ch{ch} feedback before output isolation', (amp,out), (rf,1), (rout,1))
    same(f'Ch{ch} noninverting feedback junction', (amp,minus), (rf,2), (rg,1))
    same(f'Ch{ch} buffer reaches correct signal switch', (rout,2), (sw,signal))
    same(f'Ch{ch} tied drains at ADC with permanent bleed', (sw,dsignal), (sw,dground), (rbleed,1), ('J6',2*ch+1), ('U2',9+ch))
    same(f'Ch{ch} grounding switch has220R path', (sw,ground), (rground,1))
    same(f'Ch{ch} local ground paths common', (rload,2), (rg,2), (rground,2), (rbleed,2), (amp,4), (sw,4), (sw,5), ('U2',2))
    same(f'Ch{ch} amp and switch share held ADC rail', (amp,8), (sw,13), ('U2',1))
    same(f'Ch{ch} all selector controls common', (sw,1),(sw,8),(sw,9),(sw,16))
    check(f'Ch{ch} selector does not load sensor directly', nets[(sw,str(signal))] != nets[(f'JDET{ch+1}','2')])
    for ref, expected, tolerance in [(rload,100000,.01),(rin,10000,.01),(rout,499,.001),(rground,220,.01),(rbleed,33000,.001),(rf,499,.001),(rg,33000,.001)]:
        check(ref+' value/tolerance matches analysis', ohms(ref)==(expected,tolerance), ohms(ref))
    check(f'Ch{ch} qualified opamp MPN', parts[amp].findtext('value')=='OPA2325IDR')
    check(f'Ch{ch} qualified switch MPN', parts[sw].findtext('value')=='ADG4613BRUZ')


def transfer(rf, rg, rs, rb, ron, z_adc, frequency=0):
    zcap = complex(z_adc) if frequency == 0 else 1/(1/z_adc+2j*math.pi*frequency*100e-9)
    branch = 100 + zcap
    load = 1/(1/rb + 1/branch)
    return (1+rf/rg)*load/(rs+ron+load)*zcap/branch


def original(z_adc, frequency=0):
    zcap = complex(z_adc) if frequency == 0 else 1/(1/z_adc+2j*math.pi*frequency*100e-9)
    return zcap/(500+100+zcap)


def adc_input_with_bias(vin, avdd, revised):
    # ADS1256 Fig11/Table10 buffer-off PGA1 typical input model. Za reaches
    # AVDD/2 and Zb reaches AINCOM=0; this is not a simple load to ground.
    za, zb = 260000, 220000
    z = 1/(1/za+1/zb)
    vthev = (avdd/2)/za*z
    if not revised:
        return (vin*z+vthev*600)/(z+600)
    path, rb, rm = 499+17, 33000, 100
    gain = 1+499/33000
    header = (gain*vin/path+vthev/(rm+z))/(1/path+1/rb+1/(rm+z))
    return header*z/(rm+z)+vthev*rm/(rm+z)


nominal = []
corners = []
# The device-table Ron maximum applies through85C. Precision RT resistors have
# 25ppm/C;65C is the largest displacement from25C in the-40..85C range.
precision_tolerance = .001 + 25e-6*65
for z in (75000, 125000, 150000, 80000000):
    for f in (0, 1, 10, 18, 80):
        for ron in (12.5, 17):
            t = transfer(499,33000,499,33000,ron,z,f)
            rel = t/original(z,f)
            nominal.append(dict(adc_impedance_ohm=z, frequency_hz=f, ron_ohm=ron,
                                magnitude=abs(t), relative_change_pct=(abs(rel)-1)*100,
                                phase_deg=math.degrees(math.atan2(t.imag,t.real))))
    results = []
    for signs in itertools.product((-1,1), repeat=4):
        rs = [value*(1+sign*precision_tolerance) for value,sign in zip((499,33000,499,33000),signs)]
        for ron in (0,17):
            results.append((abs(transfer(*rs,ron,z)/original(z))-1)*100)
    corners.append(dict(adc_impedance_ohm=z, relative_change_pct_min=min(results),
                        relative_change_pct_max=max(results)))

rb_max = 33000*(1+precision_tolerance)
off_voltage = 2*3e-6*rb_max
# Both clamp polarities included: leak can source or sink. This bound applies
# at VDD=0/floating, not an unspecified intermediate supply voltage.
check('Off-state leakage at specifiedVDD0 below +/-0.3V', off_voltage < .3, off_voltage)
discharge_r = 220*1.01 + 17 + 100*1.01
module_c = 100e-9*1.20
discharge_s = module_c*discharge_r*math.log(5.23/.2)
analysis = dict(
    method='linear network arithmetic from checked netlist; not simulation or measurements',
    nominal_transfer=nominal, precision_tolerance_with_temperature=precision_tolerance,
    resistor_ron_corners=corners,
    off_state_voltage_magnitude_v=off_voltage,
    module_filter_discharge=dict(resistance_ohm=discharge_r, capacitance_f=module_c,
                                initial_v=5.23,target_v=.2,time_s=discharge_s,
                                excludes='control/logic propagation, switch charge injection and unknown module parasitics'),
    opamp_current_max_a=6*.0008,
    opamp_bias_error_v_through85C=500e-12*110000,
    opamp_bias_error_v_through125C=10e-9*110000,
    normal_switch_leak_sensitivity_v=(120e-9+80e-9)*(499+17),
    buffer_off_bias_model=dict(
        source='ADS1256 Fig11/Table10, typical PGA1 Za260k toAVDD/2 and Zb220k toAINCOM=0',
        notes='This model exposes existing external-resistor DC error that a150k-to-ground model omits. Values are typical, not guaranteed; source-module AVDD was not measured.',
        examples=[dict(detector_v=v,original_at_nominal5V=adc_input_with_bias(v,5,False),
                       revised_at_nominal5_1798V=adc_input_with_bias(v,5.1798,True),
                       change_at_same_supply_v=adc_input_with_bias(v,5.1798,True)-adc_input_with_bias(v,5.1798,False))
                  for v in (0,.8,1.5,3)]),
    limits=[
        'Full circuit operating temperature is limited by its least-rated component; ADG4613 data used here spans-40..85C.',
        'ADC150/PGAk differential impedance is typical and frequency dependent;75k/125k/150k/80M are simple sensitivity cases, not guaranteed resistance bounds or an exact single-ended model. See separate Za/Zb bias model.',
        'Module100R/100nF values are seller evidence;1%/20% tolerances are explicit provisional assumptions pending installed-module verification.',
        'Power-off3uA/drain specification is forVDD0/floating under stated datasheet test conditions. Intermediate-rail collapse requires separate power analysis.',
        'Input-current limiting protects the opamp but does not alone prevent phantom power; all connected detector supplies are switched together.',
        'Transfer calculation excludes opamp offset/drift/nonlinearity, ADC switched-cap transients and reference accuracy; bench correlation remains required.',
        'No claim of zero additional noise or exact unity gain; failed high-offset detectors can saturate the held-powered amplifier.'
    ],
    sources=[
        'https://www.ti.com/lit/ds/symlink/opa325.pdf',
        'https://www.analog.com/media/en/technical-documentation/data-sheets/adg4612_4613.pdf',
        'https://www.ti.com/lit/ds/symlink/ads1256.pdf',
        'research/adc_seller_image_05.jpg',
        'reports/original_netlist.xml'
    ])
report = dict(netlist_sha256=hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
              all_passed=all(c['passed'] for c in checks), checks=checks, analysis=analysis)
(ROOT/'reports/analog_checks.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
print(f'{sum(c["passed"] for c in checks)}/{len(checks)} analog connectivity/value/defined-bound checks passed.')
print(f'Off-state leakage bound: +/-{off_voltage:.6f}V atVDD0; module discharge after control: {discharge_s*1e6:.1f}us.')
if not report['all_passed']:
    for c in checks:
        if not c['passed']:
            print(c)
    raise SystemExit(1)
