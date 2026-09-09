"""Receiver-powered SPI buffers with specified partial-power-down behavior.

Pin map is for TI SN74LVC2G125 DCT/SM8, not the similarly named quad LVC125A.
"""


def add_spi_stage(c):
    s = 'spi_protection'
    c.sheets[s] = 'SPI interface across independently switched supplies'
    c.define('SN74LVC2G125_DCT', [
        (1, '1OE_N', 'input', 'L'), (2, '1A', 'input', 'L'),
        (5, '2A', 'input', 'L'), (7, '2OE_N', 'input', 'L'),
        (6, '1Y', 'tri_state', 'R'), (3, '2Y', 'tri_state', 'R'),
        (8, 'VCC', 'power_in', 'R'), (4, 'GND', 'power_in', 'R')], width=10.16)
    c.define('MMBT3904', [(1, 'B', 'input', 'L'), (3, 'C', 'open_collector', 'R'),
                          (2, 'E', 'passive', 'R')], width=5.08)
    c.define_passive('SPI_R', 'R')
    c.define_passive('SPI_C', 'C')
    fp = 'Eltec_Master:TI_DCT0008A_SSOP8'
    rfp = 'Resistor_SMD:R_0805_2012Metric'
    cfp = 'Capacitor_SMD:C_0805_2012Metric'
    ds = 'https://www.ti.com/lit/ds/symlink/sn74lvc2g125.pdf'

    def resistor(ref, value, a, b, x, y):
        mpn = {'10k': 'RC0805FR-0710KL', '100k': 'RC0805FR-07100KL',
               '33': 'RC0805FR-0733RL'}[value]
        return c.add(ref, 'SPI_R', value, {1: a, 2: b}, s, (x, y),
                     footprint=rfp, mpn=mpn, manufacturer='Yageo',
                     description='0805, 1%, 0.125W; final purchasing match in assembly BOM')

    def capacitor(ref, rail, x, y):
        return c.add(ref, 'SPI_C', '100nF 50V X7R', {1: rail, 2: 'GND'}, s,
                     (x, y), footprint=cfp, mpn='CL21B104KBCNNNC',
                     manufacturer='Samsung Electro-Mechanics',
                     datasheet='https://product.samsungsem.com/mlcc/CL21B104KBCNNN.do',
                     description='Local buffer decoupling; place at VCC/GND pins')

    buffers = [
        ('U50', {1: 'ADC_IF_OE_N', 2: 'ESP_SCLK', 6: 'ADC_SCLK_DRV',
                 7: 'ADC_IF_OE_N', 5: 'ESP_MOSI', 3: 'ADC_MOSI_DRV',
                 8: 'ADC_IO_3V3', 4: 'GND'}, (63.5, 73.66)),
        ('U51', {1: 'ADC_IF_OE_N', 2: 'ESP_CS_N', 6: 'ADC_CS_DRV',
                 7: 'ADC_IO_3V3', 5: 'GND', 8: 'ADC_IO_3V3', 4: 'GND'},
         (63.5, 139.7)),
        ('U52', {1: 'ESP_IF_OE_N', 2: 'ADC_MISO', 6: 'ESP_MISO_DRV',
                 7: 'ESP_IF_OE_N', 5: 'ADC_DRDY_N', 3: 'ESP_DRDY_DRV',
                 8: 'ESP_3V3', 4: 'GND'}, (238.76, 73.66)),
    ]
    for ref, nets, pos in buffers:
        part = c.add(ref, 'SN74LVC2G125_DCT', 'SN74LVC2G125DCTR', nets, s, pos,
                     footprint=fp, mpn='SN74LVC2G125DCTR', manufacturer='Texas Instruments',
                     datasheet=ds, description='Dual buffer; receiver-powered; specified Ioff <=10uA at VCC=0')
        part['lcsc'] = 'C206035'
    # Separate collectors/pull-ups prevent the two supply rails being joined.
    for ref, base, oe, pos in [('Q50', 'ADC_OE_BASE', 'ADC_IF_OE_N', (238.76, 144.78)),
                               ('Q51', 'ESP_OE_BASE', 'ESP_IF_OE_N', (350.52, 144.78))]:
        part = c.add(ref, 'MMBT3904', 'MMBT3904LT1G', {1: base, 2: 'GND', 3: oe},
                     s, pos, footprint='Package_TO_SOT_SMD:SOT-23',
                     mpn='MMBT3904LT1G', manufacturer='onsemi',
                     datasheet='https://www.onsemi.com/download/data-sheet/pdf/mmbt3904lt1-d.pdf',
                     description='Default-disabled output-enable driver; B1 E2 C3')
        part['lcsc'] = 'C81464'
    resistor('R50', '10k', 'SPI_PERMIT', 'ADC_OE_BASE', 208.28, 190.5)
    resistor('R51', '100k', 'ADC_OE_BASE', 'GND', 208.28, 213.36)
    resistor('R52', '10k', 'ADC_IO_3V3', 'ADC_IF_OE_N', 208.28, 236.22)
    resistor('R53', '10k', 'SPI_PERMIT', 'ESP_OE_BASE', 335.28, 190.5)
    resistor('R54', '100k', 'ESP_OE_BASE', 'GND', 335.28, 213.36)
    resistor('R55', '10k', 'ESP_3V3', 'ESP_IF_OE_N', 335.28, 236.22)
    # Damping at each output; receiver defaults follow its own supply.
    for ref, a, b, pos in [
        ('R56', 'ADC_SCLK_DRV', 'ADC_SCLK', (124.46, 55.88)),
        ('R57', 'ADC_MOSI_DRV', 'ADC_MOSI', (124.46, 86.36)),
        ('R58', 'ADC_CS_DRV', 'ADC_CS_N', (124.46, 137.16)),
        ('R59', 'ESP_MISO_DRV', 'ESP_MISO', (335.28, 55.88)),
        ('R60', 'ESP_DRDY_DRV', 'ESP_DRDY', (335.28, 86.36)),
    ]:
        resistor(ref, '33', a, b, *pos)
    # Input/output defaults are separated intentionally. No live-side pull-up
    # is allowed to bias an unpowered receiver pin.
    defaults = [('R61', 'ESP_SCLK', 'GND'), ('R62', 'ESP_MOSI', 'GND'),
                ('R63', 'ESP_CS_N', 'ESP_3V3'), ('R64', 'ADC_MISO', 'GND'),
                ('R65', 'ADC_DRDY_N', 'GND'), ('R66', 'ADC_SCLK', 'GND'),
                ('R67', 'ADC_MOSI', 'GND'), ('R68', 'ADC_CS_N', 'ADC_IO_3V3'),
                ('R69', 'ESP_MISO', 'GND'), ('R70', 'ESP_DRDY', 'GND')]
    # Defaults/decoupling get their own sheet to keep the signal page legible.
    c.sheets['spi_defaults'] = 'SPI defaults, decoupling and power-off leakage bounds'
    for index, (ref, a, b) in enumerate(defaults):
        part = resistor(ref, '10k', a, b, 63.5 + (index % 3)*124.46,
                        63.5 + (index // 3)*38.1)
        part['sheet'] = 'spi_defaults'
    for ref, rail, x in [('C50', 'ADC_IO_3V3', 63.5),
                          ('C51', 'ADC_IO_3V3', 187.96), ('C52', 'ESP_3V3', 312.42)]:
        part = capacitor(ref, rail, x, 228.6)
        part['sheet'] = 'spi_defaults'
    c.note(s, 'FORWARD: ADC-powered outputs. RETURN: ESP32-powered outputs.\nSPI_PERMIT must be LOW until both supplies are valid; power stage provides this.\nEach OE defaults HIGH through a pull-up to its own supply.', 25.4, 20.32)
    c.note(s, 'U51 spare channel: input tied LOW; OE tied HIGH; output NC.\nDCT pinout: 1OE=1, 1A=2, 1Y=6, 2OE=7, 2A=5, 2Y=3, GND=4, VCC=8.\nDo not substitute the quad SN74LVC125A: its cited data sheet does not specify Ioff.', 25.4, 190.5)
    c.note('spi_defaults', 'RECEIVER DEFAULTS\n10k pull resistors are connected to the receiver\nsupply or GND; CS defaults HIGH. GPIO5 boot strap remains pulled HIGH.', 25.4, 20.32)
    c.note('spi_defaults', 'Ioff <=10uA with VCC=0; 10k +1% bounds pull-down rise to0.101V per output.\nAt VCC>=3.0V and <=0.36mA load, check ADS VIH=0.8*DVDD using the actual module DVDD.\n33 ohm output damping is a starting layout value; verify SPI waveform/timing on hardware.\nEach100nF capacitor belongs next to its respective U50/U51/U52 supply pins.', 25.4, 251.46)
