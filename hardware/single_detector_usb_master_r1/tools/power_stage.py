"""USB-master power candidate for electrical review, not fabrication release.

The module seller schematic establishes an LC-filtered AVDD path, not a direct
header-to-AVDD short. See research/power_architecture.md for the remaining
component/module corner bounds and required bench evidence.
"""

P, I, O, V, W = 'passive', 'input', 'output', 'power_in', 'power_out'
TI = 'Texas Instruments'
SOT23 = 'Package_TO_SOT_SMD:SOT-23'
SOT235 = 'Package_TO_SOT_SMD:SOT-23-5'
R0603 = 'Resistor_SMD:R_0603_1608Metric'
C0603 = 'Capacitor_SMD:C_0603_1608Metric'
C1206 = 'Capacitor_SMD:C_1206_3216Metric'
C1210 = 'Capacitor_SMD:C_1210_3225Metric'
C0G_MPN = 'GRM31C5C1H104JA01K'
C0G_DS = 'https://www.mouser.ca/datasheet/3/76/1/GRM31C5C1H104JA01-01A.pdf'
BULK_CER_MPN = 'GRM32ER71E226KE15L'
BULK_CER_DS = 'https://static.chipdip.ru/lib/973/DOC031973527.pdf'
SUP_DS = 'https://www.ti.com/lit/ds/symlink/tps3840.pdf'
NMOS_DS = 'https://www.onsemi.com/pdf/datasheet/2n7002l-d.pdf'
PMOS_DS = 'https://www.aosmd.com/sites/default/files/res/datasheets/AO3401A.pdf'


def add_power_stage(c):
    c.sheets.update({
        'power_inputs': 'Battery inputs, polarity protection and master switches',
        'adc_supply': 'Quiet held ADC supply and local digital supply',
        'power_supervision': 'USB and ESP32 supervision with battery-referenced enable',
        'power_permission': 'Power readiness and isolation',
    })
    c.define_passive('PWR_R', 'R')
    c.define_passive('PWR_C', 'C')
    c.define_passive('PWR_D', 'D')
    c.define_passive('PWR_F', 'F')
    c.define('PWR_CPOL', [(1, '+', P, 'L'), (2, '-', P, 'R')], width=2.54)
    c.define('PWR_NMOS', [(1, 'G', I, 'L'), (2, 'S', P, 'R'), (3, 'D', P, 'R')], width=5.08)
    c.define('PWR_PMOS', [(1, 'G', I, 'L'), (2, 'S', P, 'R'), (3, 'D', P, 'R')], width=5.08)
    c.define('PWR_JACK', [(1, 'CENTER+', P, 'L'), (2, 'SLEEVE-', P, 'R'),
                          (3, 'SWITCH_NC', P, 'R')], width=7.62)
    c.define('PWR_LT3041', [(1, 'IN', V, 'L'), (2, 'IN', V, 'L'),
                           (3, 'IN', V, 'L'), (5, 'EN/UV', I, 'L'),
                           (4, 'VIOC/NC', O, 'L'), (8, 'PGFB', I, 'L'),
                           (10, 'GND', V, 'L'), (11, 'GND', V, 'L'),
                           (15, 'EP/GND', V, 'L'), (13, 'OUT', W, 'R'),
                           (14, 'OUT/COMMON13', P, 'R'), (12, 'OUTS', I, 'R'),
                           (9, 'SET', P, 'R'), (7, 'ILIM', P, 'R'),
                           (6, 'PG/NC', 'open_collector', 'R')], width=12.7)
    c.define('PWR_AP2112', [(1, 'VIN', V, 'L'), (3, 'EN', I, 'L'),
                           (2, 'GND', V, 'L'), (5, 'VOUT', W, 'R'),
                           (4, 'NC', P, 'R')], width=10.16)
    c.define('PWR_SUP_PP', [(2, 'VDD/SENSE', V, 'L'), (4, 'MR_N', I, 'L'),
                           (3, 'GND', V, 'L'), (1, 'RESET_N', O, 'R'),
                           (5, 'CT', P, 'R')], width=10.16)
    c.define('PWR_SUP_OD', [(2, 'VDD/SENSE', V, 'L'), (4, 'MR_N', I, 'L'),
                           (3, 'GND', V, 'L'), (1, 'RESET_N', 'open_collector', 'R'),
                           (5, 'CT', P, 'R')], width=10.16)
    c.define('PWR_SCHMITT_INV', [(2, 'A', I, 'L'), (3, 'GND', V, 'L'),
                                (5, 'VCC', V, 'L'), (4, 'Y', O, 'R'),
                                (1, 'NC', P, 'R')], width=10.16)
    c.define('PWR_OPTO', [(1, 'LED_A', P, 'L'), (2, 'LED_K', P, 'L'),
                          (4, 'COLLECTOR', P, 'R'), (3, 'EMITTER', P, 'R')], width=10.16)
    c.define('PWR_SHUNT', [(1, 'K/4.096V', P, 'L'), (2, 'A/GND', P, 'R'),
                           (3, 'NC/GND', P, 'R')], width=7.62)
    c.define('PWR_COMPARATOR', [(3, 'IN+', I, 'L'), (4, 'IN-', I, 'L'),
                                (2, 'VEE', V, 'L'), (5, 'VCC', V, 'L'),
                                (1, 'OUT', O, 'R')], width=10.16)
    c.define('PWR_NAND', [(1, 'A', I, 'L'), (2, 'B', I, 'L'),
                          (3, 'GND', V, 'L'), (5, 'VCC', V, 'L'),
                          (4, 'Y', O, 'R')], width=10.16)

    # Seed placements occupy only the new power bay. They are not a routed or
    # thermally approved layout; the board integration step assigns final sites.
    seed = [0]
    def place():
        n = seed[0]
        seed[0] += 1
        return (18.0 + (n % 12) * 6.0, 144.0 + (n // 12) * 5.0, 0)

    def add(ref, symbol, value, nets, sheet, xy, fp, **kw):
        kw.setdefault('pcb', place())
        return c.add(ref, symbol, value, nets, sheet, xy, footprint=fp, **kw)

    def resistor(ref, value, a, b, sheet, xy, **kw):
        if 'mpn' not in kw:
            if '0.1%' in value:
                code = {'300': '300R', '221': '221R', '1.00k': '1K'}[value.split()[0]]
                kw.update(mpn='RT0603BRD07'+code+'L', manufacturer='Yageo',
                          datasheet='https://www.yageo.com/upload/media/product/productsearch/datasheet/rchip/PYu-RT_51_RoHS_L.pdf')
            else:
                code = {'100k':'100K','10k':'10K','1k':'1K','4.7k':'4K7',
                        '1.5k':'1K5','390':'390R','100':'100R','150':'150R','0':'0R'}[value.split()[0]]
                kw.update(mpn=('RC0603JR-07' if code=='0R' else 'RC0603FR-07')+code+'L',
                          manufacturer='Yageo',
                          datasheet='https://www.yageo.com/upload/media/product/productsearch/datasheet/rchip/PYu-RC_Group_51_RoHS_L.pdf')
        return add(ref, 'PWR_R', value, {1: a, 2: b}, sheet, xy, R0603, **kw)

    def cap(ref, value, a, b, sheet, xy, fp=C0603, **kw):
        if 'mpn' not in kw and value.startswith('100nF'):
            if 'C0G' in value:
                kw.update(mpn=C0G_MPN, manufacturer='Murata', datasheet=C0G_DS)
                fp = C1206
            else:
                value = '100nF 50V X7R'
                kw.update(mpn='CL10B104KB8NNNC', manufacturer='Samsung Electro-Mechanics',
                          datasheet='https://product.samsungsem.com/mlcc/CL10B104KB8NNN.do')
        elif 'mpn' not in kw and value.startswith('10nF'):
            kw.update(mpn='GRM1885C1H103JA01D', manufacturer='Murata',
                      datasheet='https://www.farnell.com/datasheets/2048016.pdf')
        return add(ref, 'PWR_C', value, {1: a, 2: b}, sheet, xy, fp, **kw)

    def nfet(ref, gate, source, drain, sheet, xy, **kw):
        return add(ref, 'PWR_NMOS', '2N7002LT1G', {1: gate, 2: source, 3: drain},
                   sheet, xy, SOT23, mpn='2N7002LT1G', manufacturer='onsemi',
                   datasheet=NMOS_DS, **kw)

    def pfet(ref, gate, source, drain, sheet, xy, **kw):
        return add(ref, 'PWR_PMOS', 'AO3401A', {1: gate, 2: source, 3: drain},
                   sheet, xy, SOT23, mpn='AO3401A', manufacturer='Alpha & Omega Semiconductor',
                   datasheet=PMOS_DS, **kw)

    s = 'power_inputs'
    for ref, plus, minus, xy, pcb in [
            ('J1', 'DET_BAT_RAW', 'GND', (48.26, 43.18), (82.2, 26.5, 90)),
            ('J2', 'EM_BAT_RAW', 'EM_GND', (48.26, 142.24), (82.2, 74.5, 90))]:
        add(ref, 'PWR_JACK', 'GCT DCJ200-10-A / customer installed',
            {1: plus, 2: minus}, s, xy, 'Eltec_Master:BarrelJack_GCT_DCJ200_10_A',
            pcb=pcb, populated=False, mpn='DCJ200-10-A', manufacturer='GCT',
            datasheet='https://gct.co/files/drawings/dcj200.pdf',
            description='Original jack position and assigned footprint retained. Physical pad3 is intentionally unconnected.')
    for ref, value, a, b, xy, mpn in [
            ('F1', '1A Slo-Blo', 'DET_BAT_RAW', 'DET_BAT_FUSED', (111.76, 43.18), '0468001.NRHF'),
            ('F2', '1.5A Slo-Blo', 'EM_BAT_RAW', 'EM_BAT_FUSED', (111.76, 142.24), '046801.5NRHF')]:
        add(ref, 'PWR_F', value, {1: a, 2: b}, s, xy, 'Fuse:Fuse_1206_3216Metric',
            mpn=mpn, manufacturer='Littelfuse',
            datasheet='https://www.littelfuse.com/~/media/electronics/datasheets/fuses/littelfuse_fuse_468_datasheet.pdf.pdf',
            description='Input fault protection candidate; current rating, interrupt rating and startup I2t must be approved against the actual battery.')
    # Reversed PFET orientation: initial forward body-diode conduction raises
    # source, then negative VGS turns it on with low drop. Not an ideal diode.
    pfet('Q16', 'DET_RPOL_GATE', 'DET_BAT_PROTECTED', 'DET_BAT_FUSED', s, (185.42, 43.18),
         description='Reverse-polarity protection. Drain faces jack, source faces load; gate referenced to detector ground.')
    resistor('R28', '100k 1%', 'DET_RPOL_GATE', 'GND', s, (182.88, 68.58))
    pfet('Q17', 'DET_MASTER_GATE', 'DET_BAT_PROTECTED', 'SENSOR_BAT_SW', s, (304.8, 43.18))
    resistor('R21', '10k 1%', 'DET_MASTER_GATE', 'DET_BAT_PROTECTED', s, (279.4, 71.12))
    resistor('R22', '1k 1%', 'DET_MASTER_GATE', 'DET_MASTER_SINK', s, (345.44, 71.12))
    cap('C26', '100nF 25V X7R', 'DET_MASTER_GATE', 'DET_BAT_PROTECTED', s, (279.4, 93.98))
    nfet('Q18', 'ADC_READY_RAW', 'GND', 'DET_MASTER_SINK', s, (345.44, 96.52))
    cap('C28', '100nF 25V X7R', 'SENSOR_BAT_SW', 'GND', s, (205.74, 96.52))

    pfet('Q19', 'EM_RPOL_GATE', 'EM_BAT_PROTECTED', 'EM_BAT_FUSED', s, (185.42, 142.24),
         description='Emitter-domain reverse-polarity protection. EM_GND remains isolated from detector/USB ground.')
    resistor('R29', '100k 1%', 'EM_RPOL_GATE', 'EM_GND', s, (182.88, 167.64))
    pfet('Q20', 'EM_MASTER_GATE', 'EM_BAT_PROTECTED', 'EM_BAT_SW', s, (304.8, 142.24))
    resistor('R23', '10k 1%', 'EM_MASTER_GATE', 'EM_BAT_PROTECTED', s, (279.4, 170.18))
    resistor('R24', '1k 1%', 'EM_MASTER_GATE', 'EM_OPTO_C', s, (345.44, 170.18))
    cap('C27', '100nF 25V X7R', 'EM_MASTER_GATE', 'EM_BAT_PROTECTED', s, (279.4, 193.04))
    add('U16', 'PWR_OPTO', 'LTV-817S-TA1-B', {1: 'EM_MASTER_LED_A', 2: 'EM_MASTER_LED_K',
                                       3: 'EM_GND', 4: 'EM_OPTO_C'}, s, (340.36, 213.36),
        'Package_DIP:SMDIP-4_W7.62mm', mpn='LTV-817S-TA1-B', manufacturer='Lite-On',
        datasheet='https://optoelectronics.liteon.com/upload/download/DS-70-96-0016/LTV-8X7%20series%20%20Rev.T.PDF',
        description='Isolated emitter master enable. TA1 tape packaging of SMDIP4, B CTR bin130..260% atIF5mA/VCE5V. JLC catalog C109226; verify assembly orientation.')
    resistor('R25', '390 1%', 'ADC_IO_3V3', 'EM_MASTER_LED_A', s, (205.74, 210.82))
    nfet('Q21', 'ADC_READY_RAW', 'GND', 'EM_MASTER_LED_K', s, (208.28, 238.76))
    c.note(s, 'Detector and emitter batteries: confirmed 6.4V nominal LiFePO4 packs. Endpoints are not yet verified.\nPositive leads are switched; detector ground remains continuous with USB. EM_GND stays optically isolated.\nQ16/Q19 protect reversed battery polarity. They do not provide reverse-current isolation.\nQ17/Q20 gate capacitors slow edges; they are not precision current limiters. Check inrush and MOSFET SOA.',
           25.4, 261.62, 1.016)

    s = 'adc_supply'
    add('U10', 'PWR_LT3041', 'LT3041ADE#TRPBF',
        {1: 'DET_BAT_PROTECTED', 2: 'DET_BAT_PROTECTED', 3: 'DET_BAT_PROTECTED',
         5: 'ADC_REG_EN', 7: 'ADC_LDO_ILIM', 8: 'DET_BAT_PROTECTED',
         9: 'ADC_LDO_SET', 10: 'GND', 11: 'GND', 12: 'ADC_5V_HELD',
         13: 'ADC_5V_HELD', 14: 'ADC_5V_HELD', 15: 'GND'}, s, (152.4, 58.42),
        'Package_DFN_QFN:DFN-14-1EP_3x4mm_P0.5mm_EP1.7x3.3mm',
        mpn='LT3041ADE#TRPBF', manufacturer='Analog Devices',
        datasheet='https://www.analog.com/media/en/technical-documentation/data-sheets/lt3041.pdf',
        description='Reverse-current protected quiet LDO. PGFB tied IN disables fast start; VIOC and PG float. OUTS Kelvin at local output capacitors. LCSC C7452883.')
    resistor('R10', '51.1k 0.1% 10ppm/K', 'ADC_LDO_SET', 'ADC_LDO_SET_RETURN', s, (279.4, 45.72),
             mpn='RN73C1J51K1BTDF', manufacturer='TE Connectivity',
             datasheet='https://www.te.com/en/product-6-1879134-9.html')
    resistor('R39', '698 0.1% 10ppm/K', 'ADC_LDO_SET_RETURN', 'GND', s, (345.44, 45.72),
             mpn='RN73C1J698RBTDF', manufacturer='TE Connectivity',
             datasheet='https://www.te.com/en/product-9-1676970-4.html')
    resistor('R11', '300 0.1%', 'ADC_LDO_ILIM', 'GND', s, (279.4, 68.58),
             description='Nominal500mA current limit. Datasheet450..550mA example is atVIN2.2V/OUT0; do not misstate this as a guaranteed6.4V battery inrush limit.')
    add('C10', 'PWR_CPOL', '22uF 25V tantalum +/-10%',
        {1: 'DET_BAT_PROTECTED', 2: 'GND'}, s, (50.8, 109.22),
        'Capacitor_Tantalum_SMD:CP_EIA-7343-31_Kemet-D',
        mpn='TPSD226K025R0200', manufacturer='Kyocera AVX',
        datasheet='https://datasheets.kyocera-avx.com/TPS.pdf',
        description='19.8uF initial minimum without MLCC DC-bias loss;200mohm maximumESR at100kHz. Parallel C36 local ceramic follows LT3041 input decoupling guidance; actual battery lead damping must be checked.')
    cap('C36', '22uF 25V X7R +/-10%', 'DET_BAT_PROTECTED', 'GND', s, (50.8, 139.7),
        fp=C1210, mpn=BULK_CER_MPN, manufacturer='Murata', datasheet=BULK_CER_DS,
        description='Local high-frequency input bypass in parallel with C10 tantalum. C10 independently supplies the LDO minimum input capacitance.')
    for ref, xy in [('C11', (152.4, 109.22)), ('C13', (228.6, 109.22)), ('C39', (304.8, 109.22))]:
        cap(ref, '22uF 25V X7R +/-10%', 'ADC_5V_HELD', 'GND', s, xy,
            fp=C1210, mpn=BULK_CER_MPN, manufacturer='Murata', datasheet=BULK_CER_DS,
            description='Three local ceramics;66uF nominal. Approval target>=20uF combined at5.25V, eachESR<20mohm and mountedESL<2nH. Published bias/ESR curves are typical, not guaranteed limits.')
    for ref, xy in [('C15', (152.4, 139.7)), ('C37', (228.6, 139.7)), ('C38', (228.6, 160.02))]:
        cap(ref, '100nF 50V 5% C0G', 'ADC_LDO_SET', 'GND', s, xy,
            description='Three parallel C0G SET capacitors total300nF; no4.7uF low-frequency noise claim. Qualified reference timer/firmware controls startup. Manufacturer IR bounds included in power_corners.py.')
    add('C12', 'PWR_CPOL', '820uF 6.3V polymer +/-20%', {1: 'ADC_5V_HELD', 2: 'GND'},
        s, (48.26, 160.02), 'Capacitor_SMD:CP_Elec_8x11.9', pcb=(23, 131, 0),
        mpn='PCJ0J821MCL4GS', manufacturer='Nichicon',
        datasheet='https://www.nichicon.com/getmedia/dac1415c-535b-498d-bb65-4c9bced98c46/e-pcj.pdf',
        description='Polymer 820uF: initial minimum656uF; endurance minimum524.8uF at20C. ESR10mohm max at20C/100kHz; max impedance ratio1.25 at-55/+105C. Verify effectiveC>=300uF and total rail step<=25mV over required operating conditions.')
    resistor('R12', '4.7k 1%', 'ADC_5V_HELD', 'GND', s, (279.4, 134.62),
             description='Held-rail bleeder~1.08mA; does not by itself prove all off-state analog paths safe.')
    add('U11', 'PWR_AP2112', 'AP2112K-3.3TRG1',
        {1: 'ADC_5V_HELD', 2: 'GND', 3: 'ADC_5V_HELD', 5: 'ADC_IO_3V3'},
        s, (289.56, 180.34), SOT235, mpn='AP2112K-3.3TRG1', manufacturer='Diodes Incorporated',
        datasheet='https://www.diodes.com/assets/Datasheets/AP2112.pdf',
        description='Carrier-local3.3V for receiver-powered SPI buffers/control; not tied to the module internal AMS1117 output.')
    cap('C17', '4.7uF 25V X7R', 'ADC_5V_HELD', 'GND', s, (248.92, 220.98),
        fp='Capacitor_SMD:C_0805_2012Metric', mpn='CL21B475KAFNNNE',
        manufacturer='Samsung Electro-Mechanics', datasheet='https://product.samsungsem.com/mlcc/CL21B475KAFNNN.do',
        description='4.7uF25V X7R selected with substantial nominal margin to AP2112 effective1uF minimum; qualify combined DC/AC/temperature capacitance at the local rail.')
    cap('C18', '4.7uF 25V X7R', 'ADC_IO_3V3', 'GND', s, (340.36, 220.98),
        fp='Capacitor_SMD:C_0805_2012Metric', mpn='CL21B475KAFNNNE',
        manufacturer='Samsung Electro-Mechanics', datasheet='https://product.samsungsem.com/mlcc/CL21B475KAFNNN.do',
        description='4.7uF25V X7R selected with substantial nominal margin to AP2112 effective1uF minimum; qualify combined DC/AC/temperature capacitance at the local rail.')
    c.note(s, 'Nominal ADC_5V_HELD =100uA x(51.1k+698)=5.1798V. Static resistor corner5.1157..5.2441V; with qualified SET leakage5.1108..5.2454V.\nSET resistor specifications0.1%/10ppm are mandatory; generic25ppm substitutions can exceed the ADC limit.\nLT3041 permits OUT above IN and blocks output-to-input current. Allow15mA OUT-to-ground recovery current.\nSeller module schematic:5V header ->10uH L1 ->AVDD. Header-to-actual-AVDD drop must remain<=100mV.\n820uF polymer reservoir: qualification target effectiveC>=300uF, held-domain discharge<=120mA; see research for full bounds.',
           25.4, 254, 1.016)

    s = 'power_supervision'
    for ref, mpn, rail, good, ct, xy in [
            ('U12', 'TPS3840PL42DBVR', 'USB_PRESENT_5V', 'USB_VALID_RAW', 'USB_SUP_CT', (81.28, 58.42)),
            ('U13', 'TPS3840PL30DBVR', 'ESP_3V3', 'ESP_VALID_RAW', 'ESP_SUP_CT', (284.48, 58.42))]:
        add(ref, 'PWR_SUP_PP', mpn, {1: good, 2: rail, 3: 'GND', 4: rail, 5: ct},
            s, xy, SOT235, mpn=mpn, manufacturer=TI, datasheet=SUP_DS,
            description='Push-pull active-low supervisor; output means valid after threshold+hysteresis and CT delay. Supplied only from the monitored USB domain.')
    cap('C19', '100nF 25V X7R', 'USB_PRESENT_5V', 'GND', s, (55.88, 101.6))
    cap('C20', '10nF 5% C0G', 'USB_SUP_CT', 'GND', s, (129.54, 101.6))
    cap('C21', '100nF 25V X7R', 'ESP_3V3', 'GND', s, (259.08, 101.6))
    cap('C22', '10nF 5% C0G', 'ESP_SUP_CT', 'GND', s, (332.74, 101.6))
    for ref, gate, source, drain, xy in [
            ('Q10', 'USB_VALID_RAW', 'ENABLE_AND_MID', 'POWER_OFF_NODE', (63.5, 157.48)),
            ('Q11', 'ESP_VALID_RAW', 'GND', 'ENABLE_AND_MID', (63.5, 198.12))]:
        add(ref, 'PWR_NMOS', 'AO3414', {1: gate, 2: source, 3: drain}, s, xy, SOT23,
            mpn='AO3414', manufacturer='Alpha & Omega Semiconductor',
            datasheet='https://www.aosmd.com/sites/default/files/res/data_sheets/AO3414.pdf',
            description='RDS(on) specified atVGS1.8V; used only forUSB/ESP gate voltages, since absoluteVGS is8V.')
    for ref, anode, xy in [('D10', 'DET_BAT_PROTECTED', (157.48, 109.22)),
                           ('D11', 'ADC_5V_HELD', (223.52, 109.22))]:
        add(ref, 'PWR_D', '1N4148W', {1: 'OFF_BIAS_RAIL', 2: anode}, s, xy,
            'Diode_SMD:D_SOD-123', mpn='1N4148W-7-F', manufacturer='Diodes Incorporated',
            datasheet='https://www.diodes.com/assets/Datasheets/ds30086.pdf',
            description='Diode OR maintains default-OFF control from either battery or heldrail without backfeeding battery when disconnected.')
    resistor('R13', '10k 1%', 'OFF_BIAS_RAIL', 'POWER_OFF_NODE', s, (157.48, 157.48))
    nfet('Q12', 'POWER_OFF_NODE', 'GND', 'ADC_REG_EN', s, (251.46, 160.02))
    resistor('R14', '100k 1%', 'DET_BAT_PROTECTED', 'ADC_REG_EN', s, (330.2, 160.02))
    nfet('Q13', 'POWER_OFF_NODE', 'GND', 'ADC_SUP_MR_N', s, (251.46, 208.28))
    resistor('R15', '100k 1%', 'PWR_REF_4V096', 'ADC_SUP_MR_N', s, (330.2, 208.28))
    add('Q24', 'PWR_NMOS', 'AO3414',
        {1: 'POWER_OFF_NODE', 2: 'GND', 3: 'ADC_READY_RAW'}, s, (157.48, 208.28), SOT23,
        mpn='AO3414', manufacturer='Alpha & Omega Semiconductor',
        datasheet='https://www.aosmd.com/sites/default/files/res/data_sheets/AO3414.pdf',
        description='Direct hardware kill, specifiedON at1.8V even when onlyheldrail suppliesOFFbias. Comparator isolatedthroughR16. D12protects8V gate rating.')
    add('D12', 'PWR_D', 'BZT52C6V8GW', {1: 'POWER_OFF_NODE', 2: 'GND'},
        s, (157.48, 236.22), 'Diode_SMD:D_SOD-123',
        mpn='BZT52C6V8GW', manufacturer='Diotec Semiconductor',
        datasheet='https://diotec.com/request/datasheet/bzt52c2v4gw.pdf',
        description='Active SOD1236.8V gate clamp behindR13; datasheet6.40..7.20V at5mA and max+0.07%/K imply7.704V at125C, belowAO3414 8V gate rating. Do not connect gate directly to battery.')
    c.note(s, 'USB and ESP3V3 must both be valid to pull POWER_OFF_NODE low.\nOtherwise Q12 disables the ADC regulator, Q13 restarts reference qualification, Q24 directly removes permission.\nDiode-OR OFF bias also works when detector battery is removed beforeUSB and the heldrail remains charged.\nUSB and ESP touch only MOSFET gates or their own rail supervisors.\nNo battery, bulk capacitor or regulator output is connected to USB_PRESENT_5V or ESP_3V3.',
           25.4, 251.46, 1.016)

    s = 'power_permission'
    add('U17', 'PWR_SHUNT', 'LM4050AIM3-4.1/NOPB',
        {1: 'PWR_REF_4V096', 2: 'GND', 3: 'GND'}, s, (50.8, 43.18), SOT23,
        mpn='LM4050AIM3-4.1/NOPB', manufacturer=TI,
        datasheet='https://www.ti.com/lit/ds/symlink/lm4050-n.pdf',
        description='4.096V shunt reference, A grade -40..85C tolerance18mV plus1.2mV current change below1mA. LCSC C2156509. This component limits analyzed electronics temperature to85C.')
    resistor('R30', '1.5k 1%', 'ADC_5V_HELD', 'PWR_REF_4V096', s, (50.8, 76.2))
    cap('C14', '100nF 5% C0G', 'PWR_REF_4V096', 'GND', s, (50.8, 104.14),
        fp='Capacitor_SMD:C_0805_2012Metric')
    add('U14', 'PWR_SUP_PP', 'TPS3840PL34DBVR',
        {1: 'REF_READY_RAW', 2: 'PWR_REF_4V096', 3: 'GND', 4: 'ADC_SUP_MR_N', 5: 'ADC_SUP_CT'},
        s, (170.18, 45.72), SOT235, mpn='TPS3840PL34DBVR', manufacturer=TI, datasheet=SUP_DS,
        description='Delayed reference startup qualification; not the precision falling-rail threshold. Low-POR push-pull output releases default-on ADC_READY clamp throughQ26. LCSC C2871627.')
    cap('C24', '100nF 5% C0G', 'ADC_SUP_CT', 'GND', s, (170.18, 81.28),
        fp='Capacitor_SMD:C_0805_2012Metric',
        description='~61.9ms nominal; initial minimum34.0ms using RevE Eq6, RCT350kohm and C95nF;31.4ms with post-humidity capacitance envelope. This timer qualifies reference startup and restarts afterUSB/ESP faults.')
    add('U18', 'PWR_COMPARATOR', 'TLV3601DCKR',
        {1: 'ADC_COMPARATOR_GOOD', 2: 'GND', 3: 'ADC_RAIL_SENSE',
         4: 'PWR_REF_4V096', 5: 'ADC_5V_HELD'}, s, (299.72, 43.18),
        'Package_TO_SOT_SMD:SOT-353_SC-70-5', mpn='TLV3601DCKR', manufacturer=TI,
        datasheet='https://www.ti.com/lit/ds/symlink/tlv3601.pdf',
        description='Known-low POR comparator. Full-temperature offset5mV, hysteresis5mV,4.5ns max detect at50mV overdrive/5pF. C2974371. See aggregate gate-load bound; do not apply5pF timing directly to allMOSgates.')
    resistor('R31', '221 0.1% 25ppm/K', 'ADC_5V_HELD', 'ADC_RAIL_SENSE', s, (287.02, 78.74))
    resistor('R32', '1.00k 0.1% 25ppm/K', 'ADC_RAIL_SENSE', 'GND', s, (350.52, 78.74))
    cap('C16', '100nF 25V X7R', 'ADC_5V_HELD', 'GND', s, (350.52, 104.14))
    resistor('R16', '1k 1%', 'ADC_COMPARATOR_GOOD', 'ADC_READY_RAW', s, (287.02, 104.14),
             description='Limits direct USB-fault clamp current throughQ24 and isolates comparator from MOSFET gate capacitance.')
    resistor('R35', '100k 1%', 'ADC_READY_RAW', 'GND', s, (226.06, 104.14))
    add('Q23', 'PWR_NMOS', 'AO3414', {1: 'REF_NOT_READY', 2: 'GND', 3: 'ADC_READY_RAW'},
        s, (170.18, 116.84), SOT23, mpn='AO3414', manufacturer='Alpha & Omega Semiconductor',
        datasheet='https://www.aosmd.com/sites/default/files/res/data_sheets/AO3414.pdf',
        description='Default-on qualification clamp. RDS specified at1.8V; holds all enable gates low untilREF_READY releasesQ23 throughQ26.')
    add('Q26', 'PWR_NMOS', 'AO3414', {1: 'REF_READY_RAW', 2: 'GND', 3: 'REF_CLAMP_RELEASE'},
        s, (109.22, 142.24), SOT23, mpn='AO3414', manufacturer='Alpha & Omega Semiconductor',
        datasheet='https://www.aosmd.com/sites/default/files/res/data_sheets/AO3414.pdf',
        description='RDS specified at1.8V so reference-supervisor HIGH safely releases the qualification clamp.')
    resistor('R40', '150 1%', 'REF_NOT_READY', 'REF_CLAMP_RELEASE', s, (109.22, 163.83),
             description='Limits100nF feed-forward capacitor release pulse below36mA; approximately15us startup-only discharge. Independent Q24/comparator shutdown path is unaffected.')
    resistor('R38', '10k 1%', 'ADC_5V_HELD', 'REF_NOT_READY', s, (170.18, 144.78))
    cap('C23', '100nF 5% C0G', 'ADC_5V_HELD', 'REF_NOT_READY', s, (50.8, 142.24),
        fp='Capacitor_SMD:C_0805_2012Metric',
        description='Feed-forward makes default-on clamp track abruptheldrailstartup instead ofwaiting forR38/Ciss. Clamp must be fullyon before comparatorcanproduceHIGH.')
    resistor('R34', '100k 1%', 'REF_READY_RAW', 'GND', s, (109.22, 114.3))
    nfet('Q14', 'ADC_READY_RAW', 'GND', 'ISOLATE_H', s, (274.32, 144.78))
    resistor('R17', '1k 1%', 'ADC_5V_HELD', 'ISOLATE_H', s, (345.44, 144.78),
             description='12 ADG control pins may draw216uA total;1k keeps HIGH valid down to3V, whileQ14 sinks~5mA duringmeasurement.')
    add('U15', 'PWR_SCHMITT_INV', 'SN74LVC1G14DBVR',
        {2: 'ISOLATE_H', 3: 'GND', 4: 'MASTER_PERMIT', 5: 'ADC_IO_3V3'},
        s, (81.28, 172.72), SOT235, mpn='SN74LVC1G14DBVR', manufacturer=TI,
        datasheet='https://www.ti.com/lit/ds/symlink/sn74lvc1g14.pdf',
        description='3.3V low-impedance hardware permit. Battery masters separately require raw supervisor state, not this gate during undervoltage.')
    cap('C25', '100nF 25V X7R', 'ADC_IO_3V3', 'GND', s, (48.26, 195.58))
    resistor('R18', '10k 1%', 'MASTER_PERMIT', 'GND', s, (139.7, 195.58))
    resistor('R19', '0', 'MASTER_PERMIT', 'SPI_PERMIT', s, (139.7, 226.06))
    nfet('Q15', 'ISOLATE_H', 'GND', 'ADC_PDWN_N', s, (274.32, 175.26))
    resistor('R20', '4.7k 1%', 'ADC_IO_3V3', 'ADC_PDWN_N', s, (345.44, 175.26))
    add('Q22', 'PWR_NMOS', 'AO3400A', {1: 'EM_LED_ENABLE_GATE', 2: 'GND', 3: 'EM_LED_RETURN'},
        s, (274.32, 213.36), SOT23, mpn='AO3400A', manufacturer='Alpha & Omega Semiconductor',
        datasheet='https://www.aosmd.com/sites/default/files/res/datasheets/AO3400A.pdf',
        description='Hardware return gate for all3 existing emitter PWM optocoupler LEDs; this is USB/logic ground, not EM_GND.')
    resistor('R26', '100 1%', 'MASTER_PERMIT', 'EM_LED_ENABLE_GATE', s, (345.44, 195.58))
    resistor('R27', '100k 1%', 'EM_LED_ENABLE_GATE', 'GND', s, (345.44, 226.06))
    add('U19', 'PWR_NAND', 'SN74LVC1G132DBVR',
        {1: 'ADC_READY_RAW', 2: 'REF_READY_RAW', 3: 'GND',
         4: 'ESP_POWER_NOT_READY', 5: 'ESP_3V3'}, s, (81.28, 254), SOT235,
        mpn='SN74LVC1G132DBVR', manufacturer=TI,
        datasheet='https://www.ti.com/lit/ds/symlink/sn74lvc1g132.pdf',
        description='ESP-powered Schmitt NAND:LOW means both rawpermissionsvalid.5.5V-tolerant inputs andIoff avoid cross-power. No reliance on undervoltageADC-domainMASTER_PERMIT logic.')
    cap('C29', '100nF 25V X7R', 'ESP_3V3', 'GND', s, (170.18, 251.46))
    resistor('R33', '10k 1%', 'ESP_POWER_NOT_READY', 'GND', s, (233.68, 251.46),
             description='Limits outputIoff10uA to0.1V whileESP unpowered; poweredNAND activelydrivesHIGH=fault.')
    c.note(s, 'Default-on Q23 clamps all permission gates until reference qualification; Q24 independently clampsUSBfaults.\nISOLATE_H=HIGH: signal paths open, ADCground paths close, PDWN asserted. LOW: measurement enabled.\nComparatornominaltrip=4.096x(1+221/1000)=5.001216V. Fulltemperature/calibrationbounds in research.\nUSBfault Q24 removesADC_READY_RAW independentlyof MRdelay; powerstatus LOW=ready onESP_POWER_NOT_READY.\nFirmware mustwaitfor hardwareREADY and reinitializeADC after everydrop; hardware protection needsnofirmware.',
           25.4, 274.32, 1.016)
