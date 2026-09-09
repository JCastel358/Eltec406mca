"""Build the integrated native engineering candidate; validation is separate."""
from cad import Design, ROOT
from spi_stage import add_spi_stage
from analog_stage import add_analog_stage, add_emitter_wire_interfaces
from emitter_stage import add_emitter_stage
from power_stage import add_power_stage

P, I, O, V, W = 'passive', 'input', 'output', 'power_in', 'power_out'


def add_module_interfaces(c):
    c.sheets.update({
        'esp32_interface': 'ESP32 USB power and firmware interface',
        'adc_interface': 'ADS1256 removable module and socket mapping',
    })
    esp_names = [
        '3V3', 'GND', 'GPIO15', 'GPIO2', 'GPIO4/DRDY', 'GPIO16/RX2',
        'GPIO17/TX2', 'GPIO5/CS', 'GPIO18/SCLK', 'GPIO19/MISO', 'GPIO21',
        'GPIO3/RX0', 'GPIO1/TX0', 'GPIO22', 'GPIO23/MOSI',
        'VIN/USB5V', 'GND', 'GPIO13', 'GPIO12', 'GPIO14', 'GPIO27',
        'GPIO26/EMIT3', 'GPIO25/EMIT2', 'GPIO33/EMIT1', 'GPIO32',
        'GPIO35', 'GPIO34', 'GPIO39/VN', 'GPIO36/VP', 'EN',
    ]
    esp_nets = {1: 'ESP_3V3', 2: 'GND', 5: 'ESP_DRDY', 8: 'ESP_CS_N',
                9: 'ESP_SCLK', 10: 'ESP_MISO', 15: 'ESP_MOSI',
                16: 'USB_PRESENT_5V', 17: 'GND', 22: 'ESP_EMIT3',
                23: 'ESP_EMIT2', 24: 'ESP_EMIT1', 26: 'ESP_POWER_NOT_READY'}
    esp_types = {1: W, 2: V, 5: I, 8: O, 9: O, 10: I, 15: O,
                 16: W, 17: V, 22: O, 23: O, 24: O,
                 26: I, 27: I, 28: I, 29: I, 30: I}
    c.define('ESP32_USB_Module', [(n, label, esp_types.get(n, 'bidirectional'),
                                  'L' if n <= 15 else 'R')
                                 for n, label in enumerate(esp_names, 1)],
             width=17.78, description='30-pin USB-powered module; original native pin mapping')
    c.define('ESP_Socket_A', [(n, esp_names[n-1], P, 'L') for n in range(1, 16)], width=12.7)
    c.define('ESP_Socket_B', [(n, esp_names[n+14], P, 'L') for n in range(1, 16)], width=12.7)
    s = 'esp32_interface'
    c.add('U1', 'ESP32_USB_Module', 'ESP32 DevKit 30-pin USB-C', esp_nets, s,
          (86.36, 106.68), populated=False,
          description='Customer-inserted module. J3 and J4 are separately populated female sockets. USB is its only power source.')
    c.add('J3', 'ESP_Socket_A', '1x15 female socket - row A',
          {n: esp_nets.get(n) for n in range(1, 16)}, s, (233.68, 106.68),
          footprint='Eltec_Master:Socket_Kinghelm_1x15_P2.54_H8.5', pcb=(86.08, 87.0, 180),
          mpn='KH-2.54FH-1X15P-H8.5', manufacturer='Kinghelm',
          datasheet='https://www.kinghelm.net/upload/file/20221115/KH-2.54FH-1X15P-H8.5.pdf',
          description='Physical socket, populated. Pin1 mates to U1.1; pin15 mates to U1.15. Height/fit remains preliminary.')
    c.add('J4', 'ESP_Socket_B', '1x15 female socket - row B',
          {n: esp_nets.get(n+15) for n in range(1, 16)}, s, (350.52, 106.68),
          footprint='Eltec_Master:Socket_Kinghelm_1x15_P2.54_H8.5', pcb=(86.08, 112.4, 180),
          mpn='KH-2.54FH-1X15P-H8.5', manufacturer='Kinghelm',
          datasheet='https://www.kinghelm.net/upload/file/20221115/KH-2.54FH-1X15P-H8.5.pdf',
          description='Physical socket, populated. Pin1 mates to U1.16; pin15 mates to U1.30. Height/fit remains preliminary.')
    c.note(s, 'USB POWER DOMAIN\nUSB cable -> existing ESP32 USB-C receptacle -> module VIN pin.\nNo battery rail is connected to USB_PRESENT_5V or ESP_3V3.', 25.4, 20.32)
    c.note(s, 'Firmware mapping retained: SCLK18, MOSI23, MISO19, CS5, DRDY4.\nEmitter outputs: GPIO33, GPIO25, GPIO26. New GPIO35 input: LOW=power ready.\nSPI crosses to the switched ADC supply through powered-off-protected buffers.\nUse the separate R1 firmware to wait/retry initialization and reject supply-loss captures.', 25.4, 162.56)
    c.note(s, 'Socket row separation: 25.40 mm. Pitch: 2.54 mm.\nOriginal duplicate pad28 is not reproduced.\nBoth module ground pins are explicitly connected to GND.\nOnly the plug-in module is omitted from PCBA; both sockets are populated.', 203.2, 162.56)

    adc_names = ['5V_IN', 'DGND', 'SCLK', 'DIN', 'DOUT', 'DRDY_N', 'CS_N', 'PDWN_N']
    adc_names += [f'AIN{i}' for i in range(8)] + [f'AGND{i}' for i in range(8)]
    adc_nets = {1: 'ADC_5V_HELD', 2: 'GND', 3: 'ADC_SCLK', 4: 'ADC_MOSI',
                5: 'ADC_MISO', 6: 'ADC_DRDY_N', 7: 'ADC_CS_N', 8: 'ADC_PDWN_N'}
    adc_nets.update({9+i: f'ADC_AIN{i}' if i < 6 else 'GND' for i in range(8)})
    adc_nets.update({17+i: 'GND' for i in range(8)})
    adc_types = {1: V, 2: V, 3: I, 4: I, 5: O, 6: O, 7: I, 8: I}
    adc_types.update({9+i: I for i in range(8)})
    adc_types.update({17+i: V for i in range(8)})
    c.define('ADS1256_GY_Module', [(n, label, adc_types[n], 'L' if n <= 8 else 'R')
                                 for n, label in enumerate(adc_names, 1)], width=15.24,
             description='GY-220722-V1 header map from original carrier; seller schematic shows10uH between5V header andAVDD')
    c.define('ADC_Digital_Socket', [(n, adc_names[n-1], P, 'L') for n in range(1, 9)], width=12.7)
    analog_pins, analog_nets = [], {}
    for channel in range(8):
        for number, label, module_pin in [(2*channel+1, f'AIN{channel}', 9+channel),
                                          (2*channel+2, f'AGND{channel}', 17+channel)]:
            analog_pins.append((number, label, P, 'L' if number % 2 else 'R'))
            analog_nets[number] = adc_nets[module_pin]
    c.define('ADC_Analog_Socket', analog_pins, width=12.7)
    s = 'adc_interface'
    c.add('U2', 'ADS1256_GY_Module', 'ADS1256 GY-220722-V1 module', adc_nets, s,
          (88.9, 116.84), populated=False,
          description='Customer-inserted module. Seller circuit recovered; confirm installed module agrees and measure input-filterDCR/AVDD. Physical sockets areJ5/J6.')
    c.add('J5', 'ADC_Digital_Socket', '1x8 female ADC digital socket',
          {n: adc_nets[n] for n in range(1, 9)}, s, (238.76, 83.82),
          footprint='Eltec_Master:Socket_Kinghelm_1x08_P2.54_H8.5', pcb=(53.21, 80.975, 0),
          mpn='KH-2.54FH-1X8P-H8.5', manufacturer='Kinghelm',
          description='Physical populated socket. Pin1=module5V; native centers retained; candidate drawing verified, assembly matching pending.')
    c.add('J6', 'ADC_Analog_Socket', '2x8 female ADC analog socket', analog_nets, s,
          (238.76, 157.48), footprint='Eltec_Master:Socket_FG_2x08_P2.54_H8.5',
          pcb=(53.21, 23.825, 0), mpn='FG-PM2.54-2-08P-H8.5', manufacturer='FG',
          description='Physical populated socket. Odd pins AIN0..7; even pins GND0..7. Pad geometry checked against native module.')
    c.note(s, 'ADC BATTERY DOMAIN - seller schematic, ASIN B0DBSWRQZS\nHeader5V ->10uH -> AVDD with22uF/1uF/100nF bypass; AVDD -> onboard3.3V regulator.\nEach analog input has100R series and100nF at the ADC.\nVerify installed module revision, filterDCR and actualAVDD before release.', 25.4, 20.32)
    c.note(s, 'AIN0/1: test/reference pair1; AIN2/3: pair2; AIN4/5: pair3.\nUnused AIN6 and AIN7 are tied to GND.\nThe legacy AIN7 battery divider is removed: firmware does not use it.\nAll eight analog grounds and digital ground are joined explicitly on the carrier.', 25.4, 213.36)
    c.note(s, 'J6 physical numbering is interleaved by channel.\nThis is a socket footprint, not the module body.\nThe build validates each socket pad against the source module coordinates.\nNeither module nor barrel jacks will be included in JLC population.', 203.2, 213.36)


def build():
    c = Design()
    add_module_interfaces(c)
    add_power_stage(c)
    add_spi_stage(c)
    add_analog_stage(c)
    add_emitter_stage(c)
    add_emitter_wire_interfaces(c)
    c.define('External_Power_Source', [(1, 'POWER', 'power_out', 'L')], width=2.54)
    # Explicit ERC sources: battery/USB returns and rails reached through passive
    # protection/switch components. These flags do not bypass electrical review.
    for index, (net, sheet, pos) in enumerate([
        ('GND', 'power_inputs', (45.72, 238.76)),
        ('EM_GND', 'power_inputs', (106.68, 238.76)),
        ('DET_BAT_PROTECTED', 'adc_supply', (50.8, 210.82)),
        ('EM_BAT_SW', 'emitter_regulator', (106.68, 195.58)),
        ('PWR_REF_4V096', 'power_permission', (76.2, 218.44)),
    ], 1):
        c.add(f'#FLG{index:02}', 'External_Power_Source', 'External power source',
              {1: net}, sheet, pos, physical=False,
              description='ERC declaration of external source through passive protection. See schematic power path; no physical component.')
    # The power bay grows toward decreasing native Y, away from the original
    # ESP32 row. Preliminary automatic placement replaces stage seed coordinates.
    for part in c.parts:
        if part['sheet'] in ('power_inputs', 'adc_supply', 'power_supervision', 'power_permission') and part['ref'] not in ('J1', 'J2'):
            part['pcb'] = None
    c.note('root', 'R1 ENGINEERING CANDIDATE - NOT FOR FABRICATION\nSeparate replacement project; original design preserved.\nUSB-controlled battery power, protected ADC interfaces and isolated emitter supply.\nElectrical corner review, PCB routing, assembly sourcing and physical qualification remain open.',
           25.4, 20.32, 1.524)
    c.write()
    return c


if __name__ == '__main__':
    design = build()
    print(f'Wrote {len(design.sheets)} interface sheets and {len(design.parts)} symbols to {ROOT}')
