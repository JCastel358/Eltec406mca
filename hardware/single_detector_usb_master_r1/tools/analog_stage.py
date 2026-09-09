"""Six detector buffers with protected signal/ground selectors at the ADC.

Candidate protection architecture. Hold-up/current/filter corner analysis and
late power-collapse behavior are qualification gates, not an ERC claim.
"""
import json
from cad import ROOT


def add_analog_stage(c):
    c.define_passive('AN_R', 'R')
    c.define_passive('AN_C', 'C')
    c.define('ADG4613_TSSOP16', [
        (14, 'S2/SIGNAL_A', 'passive', 'L'), (3, 'S1/GROUND_A', 'passive', 'L'),
        (11, 'S3/SIGNAL_B', 'passive', 'L'), (6, 'S4/GROUND_B', 'passive', 'L'),
        (1, 'IN1', 'input', 'L'), (16, 'IN2', 'input', 'L'),
        (9, 'IN3', 'input', 'L'), (8, 'IN4', 'input', 'L'),
        (15, 'D2/A', 'passive', 'R'), (2, 'D1/A', 'passive', 'R'),
        (10, 'D3/B', 'passive', 'R'), (7, 'D4/B', 'passive', 'R'),
        (13, 'VDD', 'power_in', 'R'), (4, 'VSS', 'power_in', 'R'),
        (5, 'GND', 'power_in', 'R'), (12, 'NC', 'no_connect', 'R')], width=15.24)
    c.define('OPA2325_Dual', [
        (3, '+INA', 'input', 'L'), (2, '-INA', 'input', 'L'),
        (5, '+INB', 'input', 'L'), (6, '-INB', 'input', 'L'),
        (1, 'OUTA', 'output', 'R'), (7, 'OUTB', 'output', 'R'),
        (8, 'V+', 'power_in', 'R'), (4, 'V-', 'power_in', 'R')], width=10.16)
    c.define('Detector_Wire_Interface', [
        (1, 'V+', 'passive', 'R'), (2, 'SIGNAL', 'passive', 'R'),
        (3, 'GND/CASE', 'passive', 'R')], width=7.62)
    geometry = {g['reference']: g for g in json.loads(
        (ROOT / 'reports/interface_geometry.json').read_text())['placements']}

    def r(ref, value, code, a, b, sheet, pos, description='', precision=False):
        part = c.add(ref, 'AN_R', value, {1: a, 2: b}, sheet, pos,
                     footprint='Resistor_SMD:R_0805_2012Metric',
                     mpn=('RT0805BRD07' if precision else 'RC0805FR-07')+code+'L',
                     manufacturer='Yageo', description=description)
        if part['mpn'] == 'RT0805BRD0733KL':
            part['lcsc'] = 'C728650'
        return part

    for pair in range(3):
        a, b = pair*2, pair*2+1
        s = f'analog_pair_{pair+1}'
        c.sheets[s] = f'Detector/reference pair {pair+1} - buffered ADC isolation'
        for channel, pos in [(a, (45.72, 66.04)), (b, (45.72, 139.7))]:
            ref = f'JDET{channel+1}'
            g = geometry[ref]
            c.add(ref, 'Detector_Wire_Interface', 'TEST' if channel % 2 == 0 else 'REFERENCE',
                  {1: 'SENSOR_BAT_SW', 2: f'SENSOR{channel}', 3: 'GND'}, s, pos,
                  footprint=g['library_id'],
                  pcb=(*g['position_mm'], g['rotation_deg']), physical=False,
                  description='Existing wire solder lands: copper feature, no purchased component. V+1 SIGNAL2 GND/case3. Preserve harness polarity.')
            r(f'R{200+channel}', '100k 1%', '100K', f'SENSOR{channel}', 'GND', s,
              (48.26, 96.52 if channel == a else 170.18),
              'Original detector source resistor; remains at the sensor. Do not omit or parallel with another sensor load.')
        u = c.add(f'U{30+pair}', 'ADG4613_TSSOP16', 'ADG4613BRUZ',
                  {1: 'ISOLATE_H', 8: 'ISOLATE_H', 9: 'ISOLATE_H', 16: 'ISOLATE_H',
                   14: f'AMP_SERIES{a}', 11: f'AMP_SERIES{b}',
                   3: f'CLAMP_GND{a}', 6: f'CLAMP_GND{b}',
                   15: f'ADC_AIN{a}', 2: f'ADC_AIN{a}',
                   10: f'ADC_AIN{b}', 7: f'ADC_AIN{b}',
                   13: 'ADC_5V_HELD', 4: 'GND', 5: 'GND'},
                  s, (294.64, 104.14),
                  footprint='Package_SO:TSSOP-16_4.4x5mm_P0.65mm',
                  mpn='ADG4613BRUZ', manufacturer='Analog Devices',
                  datasheet='https://www.analog.com/media/en/technical-documentation/data-sheets/adg4612_4613.pdf',
                  description='Selector after low-impedance buffer: HIGH grounds ADC inputs through220R; LOW selects buffered signals. Power-off leakage is discharged by33k ADC bleeders.')
        for channel, ypos in [(a, 66.04), (b, 139.7)]:
            r(f'R{210+channel}', '10k 1%', '10K', f'SENSOR{channel}', f'AMP_IN{channel}',
              s, (104.14, ypos), 'Limits input clamp current. OPA2325 rail-clamp operation requires input current within10mA.')
            r(f'R{220+channel}', '499R 0.1%', '499R', f'AMP_OUT{channel}', f'AMP_SERIES{channel}',
              s, (236.22, ypos), 'Isolation before selector. Gain network compensates nominal33k bleed load; module100R/100nF and ADC impedance remain in transfer calculation.', precision=True)
            r(f'R{230+channel}', '220R 1%', '220R', f'CLAMP_GND{channel}', 'GND',
              s, (292.1, 185.42 if channel == a else 215.9), 'Direct ADC grounding path when ISOLATE_H is HIGH; includes internal module100R/100nF filter.')
            r(f'R{240+channel}', '33k 0.1%', '33K', f'ADC_AIN{channel}', 'GND',
              s, (355.6, ypos), 'Permanent ADC-side bleed. Two tied switch drain leaks at3uA each produce at most0.199V with33k0.1%; not82k.', precision=True)
            r(f'R{250+channel}', '499R 0.1%', '499R', f'AMP_OUT{channel}', f'AMP_FB{channel}',
              s, (114.3, 195.58 if channel == a else 220.98), 'Noninverting feedback. Rf/Rg=499/33000 offsets ADC-side bleed loading; calculate residual switch/module transfer error.', precision=True)
            r(f'R{260+channel}', '33k 0.1%', '33K', f'AMP_FB{channel}', 'GND',
              s, (200.66, 195.58 if channel == a else 220.98), 'Noninverting gain resistor. Gain is1.0151212 nominal before output network.', precision=True)
        c.add(f'U{33+pair}', 'OPA2325_Dual', 'OPA2325IDR',
              {3: f'AMP_IN{a}', 2: f'AMP_FB{a}', 1: f'AMP_OUT{a}',
               5: f'AMP_IN{b}', 6: f'AMP_FB{b}', 7: f'AMP_OUT{b}',
               8: 'ADC_5V_HELD', 4: 'GND'}, s, (154.94, 104.14),
              footprint='Package_SO:SOIC-8_3.9x4.9mm_P1.27mm',
              mpn='OPA2325IDR', manufacturer='Texas Instruments',
              datasheet='https://www.ti.com/lit/ds/symlink/opa325.pdf',
              description='Zero-crossover input stage. Powered by held ADC supply so its output is bounded by that supply. Gain1+499/33k; see transfer and fault-input analysis.')['lcsc'] = 'C2058909'
        for ref, pos in [(f'C{30+pair}', (236.22, 241.3)), (f'C{33+pair}', (340.36, 241.3))]:
            c.add(ref, 'AN_C', '100nF 50V X7R', {1: 'ADC_5V_HELD', 2: 'GND'}, s, pos,
                  footprint='Capacitor_SMD:C_0805_2012Metric',
                  mpn='CL21B104KBCNNNC', manufacturer='Samsung Electro-Mechanics',
                  datasheet='https://product.samsungsem.com/mlcc/CL21B104KBCNNN.do',
                  description='Local analog IC bypass; one each at switch and dual opamp.')
        c.note(s, 'CONTROL: ISOLATE_H=1 disconnects buffers and grounds ADC inputs;0selects buffered signals.\nThe100k detector loads are retained. Switch leakage acts on the low-impedance buffer output.\nADC bleeders remain connected when the switches lose supply; gain compensates their nominal load.', 25.4, 20.32)
        c.note(s, f'CHANNEL MAP: TEST -> AIN{a}; REFERENCE -> AIN{b}.\nHeld supply must outlast switch operation and module100R/100nF discharge.\nThe transfer includes module input impedance: this is not exact unity gain.\nHigh faulty-detector outputs may clip; valid405 offsets0.8..3.0V remain within range.', 25.4, 256.54)


def add_emitter_wire_interfaces(c):
    c.define('Emitter_Wire_Interface', [(1, 'HEATER+', 'passive', 'L'),
                                        (2, 'SWITCHED_RETURN', 'passive', 'R')], width=15.24)
    s = 'emitter_connections'
    c.sheets[s] = 'Existing emitter wire connections'
    geometry = {g['reference']: g for g in json.loads(
        (ROOT / 'reports/interface_geometry.json').read_text())['placements']}
    for channel in range(1, 4):
        ref = f'JEM{channel}'
        g = geometry[ref]
        c.add(ref, 'Emitter_Wire_Interface', f'EMITTER {channel}',
              {1: 'EM_REG', 2: f'EMIT{channel}_LOW'}, s, (101.6, 71.12+(channel-1)*50.8),
              footprint=g['library_id'], pcb=(*g['position_mm'], g['rotation_deg']),
              physical=False, description='Existing two wire copper lands. Pin1heaterpositive; pin2PWMreturn. No purchased connector.')
    c.note(s, 'Preserve the existing harness. These are solder lands, not on-board emitter bodies.\nA heater return must not be shorted to EM_GND because that bypasses PWM.\nAny separate emitter case/shield lead is not established by this two-wire footprint.\nVerify its existing connection independently; do not bridge isolated grounds by assumption.', 25.4, 25.4)
