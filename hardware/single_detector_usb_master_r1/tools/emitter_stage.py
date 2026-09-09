"""Isolated emitter regulator and three PWM channels for the replacement PCB.

Entry point: add_emitter_stage(c), using cad.Design. This module never writes
project or footprint files. The master-power stage owns EM_BAT_SW switching;
the connector stage owns the three physical heater connections.

All three 200 mA outputs can switch concurrently electrically. Continuous
aggregate current is also limited by regulator dissipation; see the engineering
envelope in research/emitter_stage.md before claiming 600 mA at every setting.
"""

from cad import effects, prop, q

ADI = 'https://www.analog.com/media/en/technical-documentation/data-sheets/lt1963a.pdf'
AOS = 'https://www.aosmd.com/res/data_sheets/AO3400A.pdf'
LITEON = ('https://optoelectronics.liteon.com/upload/download/DS-70-96-0016/'
          'LTV-8X7%20series%20%20Rev.S.PDF')
BOURNS = 'https://www.bourns.com/docs/product-datasheets/3296.pdf'
AVX = 'https://datasheets.kyocera-avx.com/TPS.pdf'
YAGEO = 'https://yageogroup.com/component-documentation/download/specsheet/'
SAMSUNG = 'https://product.samsungsem.com/mlcc/CL21B104KBCNNN.do'


def _define_polarized_capacitor(c):
    """Explicit + at pin1: tantalum body stripe marks its POSITIVE terminal."""
    name = 'EM_C_Polarized'
    c.define_passive(name, 'C')
    raw = c.symbols[name]['raw']
    marker = (f'(text "+" (at -2.54 2.54 0) {effects(1.27)})')
    raw = raw.replace(f'(symbol {q(name+"_0_1")}',
                      f'(symbol {q(name+"_0_1")} {marker}', 1)
    c.symbols[name]['raw'] = raw


def add_emitter_stage(c):
    """Append complete regulator/PWM circuitry; no board routing or writes.

    Inputs: EM_BAT_SW, EM_GND, GND, EM_LED_RETURN, ESP_EMIT1/2/3.
    Outputs: EM_REG (three heater positives), EMIT1_LOW/2_LOW/3_LOW.
    MASTER_PERMIT is consumed by the separate power stage, which also owns the
    GND-side sink for EM_LED_RETURN. A denied permit blocks all three LEDs even
    if the ESP32 command pins remain high.
    Board coordinates are left for the coordinated layout stage, rather than
    inventing a placement that conflicts with the retained module geometry.
    """
    c.sheets.update({
        'emitter_regulator': 'Isolated adjustable emitter supply',
        'emitter_pwm': 'Three optically isolated emitter PWM channels',
    })
    c.define_passive('EM_R', 'R')
    c.define_passive('EM_C', 'C')
    _define_polarized_capacitor(c)
    c.define('EM_LT1963A_Q', [
        ('2', 'IN', 'power_in', 'L'),
        ('1', 'SHDN_N', 'input', 'L'),
        ('3', 'GND/TAB', 'power_in', 'L'),
        ('4', 'OUT', 'power_out', 'R'),
        ('5', 'ADJ', 'input', 'R'),
    ], width=12.7, description='ADI LT1963A adjustable Q/DD package; tab is pin3 GND')
    c.define('EM_3296W', [
        ('1', 'CCW', 'passive', 'L'),
        ('2', 'WIPER', 'passive', 'L'),
        ('3', 'CW', 'passive', 'R'),
    ], width=7.62, description='Bourns3296W: terminal2 is wiper;1+2 strapped')
    c.define('EM_LTV847S', [
        ('1', 'A1', 'passive', 'L'), ('2', 'K1', 'passive', 'L'),
        ('3', 'A2', 'passive', 'L'), ('4', 'K2', 'passive', 'L'),
        ('5', 'A3', 'passive', 'L'), ('6', 'K3', 'passive', 'L'),
        ('7', 'A4/NC', 'passive', 'L'), ('8', 'K4/NC', 'passive', 'L'),
        ('16', 'C1', 'passive', 'R'), ('15', 'E1', 'passive', 'R'),
        ('14', 'C2', 'passive', 'R'), ('13', 'E2', 'passive', 'R'),
        ('12', 'C3', 'passive', 'R'), ('11', 'E3', 'passive', 'R'),
        ('10', 'C4/NC', 'passive', 'R'), ('9', 'E4/NC', 'passive', 'R'),
    ], width=10.16, description='Four independent phototransistors; input/output grounds remain isolated')
    c.define('EM_AO3400A', [
        ('1', 'G', 'input', 'L'),
        ('3', 'D', 'passive', 'R'),
        ('2', 'S', 'passive', 'R'),
    ], width=5.08, description='AO3400A N-MOS:1gate2source3drain; source referenced to EM_GND')

    def resistor(ref, value, mpn_value, a, b, sheet, xy, description=''):
        return c.add(ref, 'EM_R', value, {1: a, 2: b}, sheet, xy,
                     footprint='Resistor_SMD:R_0805_2012Metric',
                     mpn=f'RC0805FR-07{mpn_value}L', manufacturer='Yageo',
                     datasheet=YAGEO+f'RC0805FR-07{mpn_value}L', description=description)

    s = 'emitter_regulator'
    u = c.add('U100', 'EM_LT1963A_Q', 'LT1963AEQ#PBF',
             {1: 'EM_BAT_SW', 2: 'EM_BAT_SW', 3: 'EM_GND',
              4: 'EM_REG', 5: 'EM_ADJ'}, s, (101.6, 81.28),
             footprint='Package_TO_SOT_SMD:TO-263-5_TabPin3',
             mpn='LT1963AEQ#PBF', manufacturer='Analog Devices', datasheet=ADI,
             description='1.5A adjustable LDO. Thermal design and bench qualification limit actual aggregate output. Pin3/tab needs isolated EM_GND copper.')
    u['lcsc'] = 'C20415348'
    u['assembly_note'] = 'Exact LCSC match identified; live JLCPCBA stock/sourcing must be checked.'
    resistor('R100', '210R 1% 0.125W', '210R', 'EM_ADJ', 'EM_GND',
             s, (101.6, 167.64), 'ADJ-to-ground resistor; ensures >5.5mA divider load even with all emitters off.')
    resistor('R101', '7R5 1% 0.125W', '7R5', 'EM_TRIM_TOP', 'EM_ADJ',
             s, (243.84, 124.46), 'Fixed minimum setting resistor; not an independent output overvoltage clamp.')
    rv = c.add('RV100', 'EM_3296W', '1k 25-turn 0.5W',
               {1: 'EM_REG', 2: 'EM_REG', 3: 'EM_TRIM_TOP'},
               s, (243.84, 81.28),
               footprint='Potentiometer_THT:Potentiometer_Bourns_3296W_Vertical',
               mpn='3296W-1-102LF', manufacturer='Bourns', datasheet=BOURNS,
               description='Populated trimmer. Pins1+2 strapped: open wiper retains full track resistance. CW adjustment decreases output. Set with meter before connecting emitters.')
    rv['lcsc'] = 'C57089'
    for ref, net, xy in [('C100', 'EM_BAT_SW', (60.96, 124.46)),
                         ('C102', 'EM_REG', (330.2, 81.28))]:
        cap = c.add(ref, 'EM_C_Polarized', '22uF 25V 10% TPS',
                    {1: net, 2: 'EM_GND'}, s, xy,
                    footprint='Capacitor_Tantalum_SMD:CP_EIA-7343-31_Kemet-D',
                    mpn='TPSD226K025R0200', manufacturer='Kyocera AVX', datasheet=AVX,
                    description='CaseD7343-31,22uF±10%,25V,200mΩ maximum ESR at100kHz. Pin1 is positive. Place close to LDO.')
        cap['lcsc'] = 'C284839'
        cap['assembly_note'] = 'JLCPCBA Extended/SMT match exists, but live stock and Kyocera AVX manufacturer identity need confirmation (JLC page displays manufacturer --).'
    for ref, net, xy in [('C101', 'EM_BAT_SW', (60.96, 162.56)),
                         ('C103', 'EM_REG', (330.2, 124.46))]:
        c.add(ref, 'EM_C', '100nF 50V X7R 10%', {1: net, 2: 'EM_GND'}, s, xy,
              footprint='Capacitor_SMD:C_0805_2012Metric',
              mpn='CL21B104KBCNNNC', manufacturer='Samsung Electro-Mechanics', datasheet=SAMSUNG,
              description='Local high-frequency bypass; no separate ADJ capacitor needed.')
    c.note(s, 'SUPPLY INTERFACE\nEM_BAT_SW comes from the isolated positive master switch.\nSHDN_N follows EM_BAT_SW; firmware does not bypass the master.\nEM_GND remains separate from GND on every copper layer.\nInput bypass connects directly IN-to-EM_GND; the original D1/C5 error is removed.',
           25.4, 20.32)
    c.note(s, 'VOLTAGE ADJUSTMENT\nVout = 1.21*(1+(7.5+Rtrim)/210) + Iadj*(7.5+Rtrim).\nNominal electrical setpoint: 1.253..7.019V, limited by battery/dropout.\nThis preserves the original broad adjustment; it is NOT a safe heater preset.\nUse the established measured emitter voltage and validated duty/frequency.\nMark the chosen setting; do not turn the pot to its end stop to calibrate.',
           195.58, 172.72)
    c.note(s, 'THERMAL DESIGN\nThree channels: 200mA each electrical design envelope.\n600mA at every trim voltage is NOT thermally qualified.\nP = (Vin-Vout)*Iout + Vin*Ignd. See research/emitter_stage.md.\nTO-263 tab/pin3: broad EM_GND copper and thermal vias.\nNo solid ground plane may bridge the optical isolation boundary.',
           25.4, 210.82)

    s = 'emitter_pwm'
    opto = c.add('U101', 'EM_LTV847S', 'LTV-847S',
                 {1: 'EM_LED1_A', 2: 'EM_LED_RETURN', 3: 'EM_LED2_A', 4: 'EM_LED_RETURN',
                  5: 'EM_LED3_A', 6: 'EM_LED_RETURN',
                  16: 'EM_BAT_SW', 15: 'EM_GATE1_DRIVE',
                  14: 'EM_BAT_SW', 13: 'EM_GATE2_DRIVE',
                  12: 'EM_BAT_SW', 11: 'EM_GATE3_DRIVE'},
                 s, (187.96, 71.12),
                 footprint='Package_DIP:SMDIP-16_W9.53mm',
                 mpn='LTV-847S', manufacturer='Lite-On', datasheet=LITEON,
                 description='Three channels used, fourth all NC. Cathodes return through the MASTER_PERMIT sink owned by power stage. Collectors use switched battery rail rather than low adjustable heater rail. No ground tie across optocoupler.')
    opto['lcsc'] = 'C114599'
    for channel, ypos in [(1, 129.54), (2, 172.72), (3, 215.9)]:
        offset = (channel-1)*5
        control = f'ESP_EMIT{channel}'
        resistor(f'R{110+offset}', '240R 1% 0.125W', '240R', control,
                 f'EM_LED{channel}_A', s, (66.04, ypos),
                 'Opto LED current ~8.75mA at3.3V/1.2V; >5mA with2.64V VOH and1.4V VF.')
        resistor(f'R{111+offset}', '100k 1% 0.125W', '100K', control,
                 'GND', s, (66.04, ypos+17.78), 'Input pull-down keeps emitter command low while ESP32 pin is high impedance.')
        resistor(f'R{112+offset}', '100R 1% 0.125W', '100R',
                 f'EM_GATE{channel}_DRIVE', f'EM_GATE{channel}',
                 s, (193.04, ypos), 'Series gate resistor; place beside MOSFET gate.')
        resistor(f'R{113+offset}', '10k 1% 0.125W', '10K',
                 f'EM_GATE{channel}', 'EM_GND', s, (193.04, ypos+17.78),
                 'Gate-to-source pull-down AFTER series resistor; discharges gate when optical drive is off.')
        mos = c.add(f'Q10{channel}', 'EM_AO3400A', 'AO3400A',
                    {1: f'EM_GATE{channel}', 2: 'EM_GND', 3: f'EMIT{channel}_LOW'},
                    s, (320.04, ypos), footprint='Package_TO_SOT_SMD:SOT-23',
                    mpn='AO3400A', manufacturer='Alpha & Omega Semiconductor',
                    datasheet=AOS, description=f'Emitter{channel} low-side PWM. Gate powered optically from EM_BAT_SW; emitterpositive is EM_REG.')
        mos['lcsc'] = 'C20917'
    c.note(s, 'GPIO33 -> ESP_EMIT1 -> emitter1. GPIO25 -> emitter2. GPIO26 -> emitter3.\nAll outputs default off when control pins float. An asserted master permit alone does not pulse emitters.\nConnect each heater positive to EM_REG; connect its return ONLY to its EMITn_LOW net.\nDo not connect the switched heater return directly to EM_GND, which would bypass PWM.',
           25.4, 20.32)
    c.note(s, 'GND = ESP32/USB side. EM_GND = isolated emitter battery side.\nEM_LED_RETURN is sunk to GND only while the power stage permits operation.\nMASTER_PERMIT therefore blocks all optical drive independently of the three GPIO commands.\nVerify startup/shutdown, PWM timing, low setting gate enhancement, current and temperature.',
           25.4, 257.81)
