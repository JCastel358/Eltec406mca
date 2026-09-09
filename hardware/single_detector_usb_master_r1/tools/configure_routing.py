"""Write explicit candidate routing rules, preserving unrelated project settings.

Run after the schematic netlist export and before the board/DSN build. These are
manufacturing geometry and preferred widths, not a current/thermal qualification.
Actual routed power paths still require resistance/return-path review.
"""
from pathlib import Path
from collections import Counter
import hashlib
import json
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / 'single_detector_usb_master/single_detector_usb_master.kicad_pro'
NETLIST = ROOT / 'reports/interface_netlist.xml'


def classify(name):
    if name == 'EM_GND':
        return 'Emitter return'
    if name == 'GND':
        return 'Quiet return'
    if (name in {'EM_REG', 'EM_BAT_RAW', 'EM_BAT_FUSED', 'EM_BAT_PROTECTED', 'EM_BAT_SW',
                 'DET_BAT_RAW', 'DET_BAT_FUSED', 'DET_BAT_PROTECTED', 'SENSOR_BAT_SW'}
            or name.startswith('EMIT') and name.endswith('_LOW')):
        return 'Battery and emitter power'
    if name in {'ADC_5V_HELD', 'ADC_IO_3V3', 'ESP_3V3', 'USB_PRESENT_5V'}:
        return 'Local supply'
    if (name.startswith(('SENSOR', 'AMP_', 'ADC_AIN', 'CLAMP_GND'))
            or name in {'ADC_LDO_SET', 'ADC_LDO_SET_RETURN', 'ADC_RAIL_SENSE', 'PWR_REF_4V096'}):
        return 'Analog'
    if any(token in name for token in ('SCLK', 'MOSI', 'MISO', 'DRDY', 'CS_')):
        return 'SPI'
    return 'Default'


def configure():
    data = json.loads(PROJECT.read_text(encoding='utf-8'))
    before = hashlib.sha256(PROJECT.read_bytes()).hexdigest()
    widths = {'Default': .25, 'Analog': .25, 'SPI': .20, 'Local supply': .40,
              'Quiet return': .30, 'Emitter return': 1.0, 'Battery and emitter power': 1.0}
    classes = []
    for index, (name, width) in enumerate(widths.items()):
        classes.append({'name': name, 'clearance': .20, 'track_width': width,
                        'via_diameter': .60, 'via_drill': .30,
                        'microvia_diameter': .30, 'microvia_drill': .10,
                        'diff_pair_width': .20, 'diff_pair_gap': .25,
                        'diff_pair_via_gap': .25, 'wire_width': 6, 'bus_width': 12,
                        'priority': 2147483647 if name == 'Default' else index})
    nets = sorted(net.get('name') for net in ET.parse(NETLIST).findall('./nets/net')
                  if not net.get('name').startswith('unconnected-'))
    assignments = [{'netclass': classify(name), 'pattern': name} for name in nets]
    data['net_settings'] = {'classes': classes, 'meta': {'version': 5},
                            'net_colors': None, 'netclass_assignments': None,
                            'netclass_patterns': assignments}
    settings = data.setdefault('board', {}).setdefault('design_settings', {})
    settings.setdefault('rules', {}).update({
        'min_clearance': .20, 'min_track_width': .20,
        'min_copper_edge_clearance': .50, 'min_hole_clearance': .25,
        'min_hole_to_hole': .25, 'min_through_hole_diameter': .30,
        'min_via_diameter': .60, 'min_via_annular_width': .15,
        'min_silk_clearance': .15, 'min_text_height': 1.0,
        'min_text_thickness': .15, 'min_resolved_spokes': 2,
        'solder_mask_to_copper_clearance': .09,
    })
    settings.setdefault('defaults', {}).update({'copper_line_width': .20,
                                               'silk_line_width': .15})
    settings['drc_exclusions'] = []
    # KiCad defaults these to ignore. They apply to this board and the placed
    # candidate was copy-probed clean before enabling them explicitly.
    settings.setdefault('rule_severities', {}).update({
        name: 'warning' for name in ('missing_courtyard',
            'track_not_centered_on_via', 'footprint_filters_mismatch',
            'footprint_type_mismatch')})
    PROJECT.write_text(json.dumps(data, indent=2)+'\n', encoding='utf-8')
    rules = '''(version 1)
# Native constraints supplement preferred netclass routing widths.
# Internal layers are planes; signal routes are restricted to the outer layers.
(rule "No signal tracks on internal plane layers"
  (condition "A.Layer == 'In1.Cu' || A.Layer == 'In2.Cu'")
  (constraint disallow track))
(rule "All vias are through vias"
  (constraint disallow blind_via buried_via micro_via))
(rule "PTH drill to other-net copper"
  (condition "A.Type == 'Pad' && A.Pad_Type == 'Through-hole'")
  (constraint hole_clearance (min 0.30mm)))
(rule "PTH to PTH drill clearance"
  (condition "A.Type == 'Pad' && B.Type == 'Pad' && A.Pad_Type == 'Through-hole' && B.Pad_Type == 'Through-hole'")
  (constraint hole_to_hole (min 0.45mm)))
(rule "Power route neckdown floor"
  (condition "A.hasNetclass('Battery and emitter power') || A.hasNetclass('Emitter return')")
  (constraint track_width (min 0.25mm) (opt 1.0mm)))
'''
    PROJECT.with_suffix('.kicad_dru').write_text(rules, encoding='utf-8')
    report = {'status': 'candidate_geometry_rules_not_routed_or_thermal_qualification',
              'project_sha256_before': before,
              'project_sha256_after': hashlib.sha256(PROJECT.read_bytes()).hexdigest(),
              'netlist_sha256': hashlib.sha256(NETLIST.read_bytes()).hexdigest(),
              'classes': classes, 'net_assignments': assignments,
              'net_count_by_class': dict(Counter(row['netclass'] for row in assignments)),
              'basis': ['https://jlcpcb.com/capabilities/pcb-capabilities',
                        'https://docs.kicad.org/10.0/en/pcbnew/pcbnew.html'],
              'remaining_reviews': ['actual DRC rule parser/effective classes',
                                    'power path resistance, vias and thermals',
                                    'Kelvin sense, decoupling, ground returns and isolation',
                                    'selected JLC stackup and assembly process']}
    (ROOT/'reports/routing_rules.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report['net_count_by_class']))


if __name__ == '__main__':
    configure()
