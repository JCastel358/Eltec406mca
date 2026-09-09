"""Deterministic native KiCad writer; connectivity remains editable in KiCad.

This does not route a PCB. Build scripts supply pin maps, real nets, placement
and manufacturer evidence. KiCad netlist/ERC/DRC are independent consumers.
"""
from pathlib import Path
import json
import uuid
import shutil

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "single_detector_usb_master"
LIB = ROOT / "libraries"
NAMESPACE = uuid.UUID("eb393e4c-71a1-4354-b09a-ef5b8b1c23bc")
LIBNAME = "Eltec_Master"
PROJECTNAME = "single_detector_usb_master"


def uid(key):
    return str(uuid.uuid5(NAMESPACE, str(key)))


def q(value):
    return json.dumps(str(value), ensure_ascii=False)


def effects(size=1.27, hide=False, justify=""):
    return (f'(effects (font (size {size} {size}))'
            + (' (hide yes)' if hide else '')
            + (f' (justify {justify})' if justify else '') + ')')


def prop(name, value, x, y, hide=False, size=1.27):
    return f'(property {q(name)} {q(value)} (at {x} {y} 0) {effects(size, hide)})'


class Design:
    def __init__(self):
        self.symbols = {}
        self.parts = []
        self.sheets = {}
        self.notes = {}

    def define_passive(self, name, kind='R'):
        """Conventional IEC resistor/capacitor/diode, pin1 left, pin2 right.

        For diode symbols pin1 is cathode. Polarized capacitors use a separate
        symbol and explicit pin1 positive designation when added by a stage.
        """
        self.define(name, [('1', '', 'passive', 'L'), ('2', '', 'passive', 'R')], width=2.54)
        line = lambda points: ('(polyline (pts ' + ' '.join(f'(xy {x} {y})' for x, y in points)
                               + ') (stroke (width 0.254) (type default)) (fill (type none)))')
        if kind == 'C':
            body = ''.join(line(points) for points in [
                [(-0.508, -1.778), (-0.508, 1.778)],
                [(0.508, -1.778), (0.508, 1.778)],
                [(-2.54, 0), (-0.508, 0)], [(0.508, 0), (2.54, 0)]])
        elif kind == 'D':
            body = ''.join(line(points) for points in [
                [(-1.27, 0), (1.27, 1.27), (1.27, -1.27), (-1.27, 0)],
                [(-1.27, -1.27), (-1.27, 1.27)],
                [(-2.54, 0), (-1.27, 0)], [(1.27, 0), (2.54, 0)]])
        else:
            body = ('(rectangle (start -2.54 1.016) (end 2.54 -1.016) '
                    '(stroke (width 0.254) (type default)) (fill (type none)))')
            if kind == 'F':
                body += line([(-2.54, 0), (2.54, 0)])
        raw = (f'(symbol {q(name)} (pin_names (offset 0)) (in_bom yes) (on_board yes) '
               f'{prop("Reference", kind, 0, 3.81)} {prop("Value", name, 0, -3.81)} '
               f'(symbol {q(name+"_0_1")} {body}) '
               f'(symbol {q(name+"_1_1")} '
               f'(pin passive line (at -5.08 0 0) (length 2.54) (name "" {effects(1.016)}) (number "1" {effects(1.016)})) '
               f'(pin passive line (at 5.08 0 180) (length 2.54) (name "" {effects(1.016)}) (number "2" {effects(1.016)}))))')
        self.symbols[name].update(raw=raw, height=1.27)

    def define(self, name, pins, width=12.7, description=""):
        """pins: (number, visible name, KiCad electrical type, L/R side).

        Power and signal pins retain their actual electrical types. A block
        symbol is appropriate for a plug-in module, socket, or multi-gate IC.
        """
        sides = {side: [p for p in pins if p[3] == side] for side in ('L', 'R')}
        height = max(2.54, (max(map(len, sides.values())) + 1) * 1.27)
        locations, pin_text = {}, []
        for side, entries in sides.items():
            for index, (number, label, kind, _) in enumerate(entries):
                # Keep all pin coordinates on the 1.27 mm schematic grid.
                y = (len(entries) - 1) * 1.27 - index * 2.54
                x = -width - 2.54 if side == 'L' else width + 2.54
                number = str(number)
                if number in locations:
                    raise ValueError(f'Duplicate pin {name}.{number}')
                locations[number] = (x, -y, side)
                pin_text.append(
                    f'(pin {kind} line (at {x} {y} {0 if side == "L" else 180}) '
                    f'(length 2.54) (name {q(label)} {effects(1.016)}) '
                    f'(number {q(number)} {effects(1.016)}))')
        raw = (f'(symbol {q(name)} (pin_names (offset 1.016)) '
               f'(in_bom yes) (on_board yes) '
               f'{prop("Reference", "U", 0, height+3.81)} '
               f'{prop("Value", name, 0, -height-3.81)} '
               f'{prop("Description", description, 0, 0, True)} '
               f'(symbol {q(name+"_0_1")} (rectangle '
               f'(start {-width} {height}) (end {width} {-height}) '
               f'(stroke (width 0.254) (type default)) (fill (type background)))) '
               f'(symbol {q(name+"_1_1")} {" ".join(pin_text)}))')
        self.symbols[name] = {'raw': raw, 'pins': locations, 'height': height}

    def add(self, ref, symbol, value, nets, sheet, position, footprint='',
            pcb=None, mpn='', manufacturer='', datasheet='', populated=True,
            physical=True, description=''):
        if ref in {p['ref'] for p in self.parts}:
            raise ValueError(f'Duplicate reference {ref}')
        if symbol not in self.symbols or sheet not in self.sheets:
            raise ValueError(f'Undefined symbol or sheet for {ref}')
        nets = {str(k): v for k, v in nets.items()}
        unknown = set(nets) - set(self.symbols[symbol]['pins'])
        if unknown:
            raise ValueError(f'Unknown pins {ref}: {unknown}')
        part = dict(ref=ref, symbol=symbol, value=value, nets=nets, sheet=sheet,
                    position=position, footprint=footprint, pcb=pcb, mpn=mpn,
                    manufacturer=manufacturer, datasheet=datasheet,
                    populated=populated, physical=physical,
                    description=description, uuid=uid(ref))
        self.parts.append(part)
        return part

    def note(self, sheet, text, x, y, size=1.27):
        self.notes.setdefault(sheet, []).append(
            f'(text {q(text)} (at {x} {y} 0) {effects(size, justify="left top")} '
            f'(uuid {q(uid(sheet+text))}))')

    def write(self):
        for folder in (PROJECT, LIB, ROOT / 'reports'):
            folder.mkdir(parents=True, exist_ok=True)
        fpdir = LIB / 'master.pretty'
        fpdir.mkdir(exist_ok=True)
        copied_sources = {}
        for part in self.parts:
            source_id = part['footprint']
            if not source_id or source_id.startswith(LIBNAME+':'):
                continue
            library, name = source_id.split(':', 1)
            source = Path('C:/Program Files/KiCad/10.0/share/kicad/footprints') / (library+'.pretty') / (name+'.kicad_mod')
            if not source.is_file():
                raise FileNotFoundError(f'{part["ref"]}: footprint not found: {source}')
            destination = fpdir / source.name
            if name in copied_sources and copied_sources[name] != source_id:
                raise ValueError(f'Footprint basename collision: {source_id}')
            copied_sources[name] = source_id
            # These exact stock basenames are generator-owned copies. Native
            # FootprintSave may reserialize them, so byte inequality alone does
            # not mean the source library names collide.
            if not destination.exists() or destination.read_bytes() != source.read_bytes():
                shutil.copyfile(source, destination)
            part['source_footprint'] = source_id
            part['footprint'] = LIBNAME+':'+name
        (LIB / 'master.kicad_sym').write_text(
            '(kicad_symbol_lib (version 20250114) (generator "eltec_master")\n'
            + '\n'.join(s['raw'] for s in self.symbols.values()) + '\n)', encoding='utf-8')
        root_uuid = uid('root')
        for sheet, title in self.sheets.items():
            parts = [p for p in self.parts if p['sheet'] == sheet]
            lines = [f'(kicad_sch (version 20250114) (generator "eltec_master") '
                     f'(uuid {q(uid(sheet))}) (paper "A3") (title_block '
                     f'(title {q(title)}) (date "2026-09-09") (rev "R1-DRAFT") '
                     '(company "Eltec Instruments") '
                     '(comment 1 "ENGINEERING WORK IN PROGRESS - NOT FOR FABRICATION"))']
            used = sorted({p['symbol'] for p in parts})
            lines.append('(lib_symbols ' + '\n'.join(
                self.symbols[name]['raw'].replace(f'(symbol {q(name)}',
                f'(symbol {q(LIBNAME+":"+name)}', 1) for name in used) + ')')
            lines.extend(self.notes.get(sheet, []))
            for part in parts:
                x, y = part['position']
                sym = self.symbols[part['symbol']]
                h = sym['height']
                ident = part['uuid']
                fields = [prop('Reference', part['ref'], x, y-h-3.81),
                          prop('Value', part['value'], x, y+h+3.81, size=1.016)]
                for key, value in [('Footprint', part['footprint']),
                                   ('Datasheet', part['datasheet']),
                                   ('MPN', part['mpn']),
                                   ('LCSC', part.get('lcsc', '')),
                                   ('Manufacturer', part['manufacturer']),
                                   ('Description', part['description']),
                                   ('Assembly', 'Populate' if part['populated'] else 'Customer installed')]:
                    fields.append(prop(key, value, x, y, True))
                lines.append(
                    f'(symbol (lib_id {q(LIBNAME+":"+part["symbol"])}) '
                    f'(at {x} {y} 0) (unit 1) (in_bom {"yes" if part["physical"] else "no"}) '
                    f'(on_board {"yes" if part["footprint"] else "no"}) '
                    f'(dnp {"no" if part["populated"] else "yes"}) '
                    f'(uuid {q(ident)}) {" ".join(fields)} '
                    + ' '.join(f'(pin {q(n)} (uuid {q(uid(ident+n))}))' for n in sym['pins'])
                    + f' (instances (project {q(PROJECTNAME)} (path '
                    f'{q("/"+root_uuid+"/"+uid(sheet))} (reference {q(part["ref"])}) (unit 1)))))')
                for number, (dx, dy, side) in sym['pins'].items():
                    px, py = round(x+dx, 6), round(y+dy, 6)
                    net = part['nets'].get(number)
                    if net is None:
                        lines.append(f'(no_connect (at {px} {py}) (uuid {q(uid(ident+"nc"+number))}))')
                        continue
                    ex = round(px + (-5.08 if side == 'L' else 5.08), 6)
                    lines.append(f'(wire (pts (xy {px} {py}) (xy {ex} {py})) '
                                 f'(stroke (width 0) (type default)) (uuid {q(uid(ident+"wire"+number))}))')
                    lines.append(f'(global_label {q(net)} (shape input) (at {ex} {py} '
                                 f'{180 if side == "L" else 0}) '
                                 f'{effects(1.016, justify="right" if side == "L" else "left")} '
                                 f'(uuid {q(uid(ident+"label"+number))}) '
                                 f'{prop("Intersheetrefs", "${INTERSHEET_REFS}", ex, py, True)})')
            lines.append(')')
            (PROJECT / f'{sheet}.kicad_sch').write_text('\n'.join(lines), encoding='utf-8')
        lines = [f'(kicad_sch (version 20250114) (generator "eltec_master") '
                 f'(uuid {q(root_uuid)}) (paper "A2") (title_block '
                 '(title "Single detector rig - USB master power") (rev "R1-DRAFT") '
                 '(date "2026-09-09")) (lib_symbols)']
        lines.extend(self.notes.get('root', []))
        for index, (sheet, title) in enumerate(self.sheets.items()):
            x, y = 25.4 + (index % 3)*177.8, 63.5 + (index // 3)*58.42
            lines.append(f'(sheet (at {x} {y}) (size 149.86 30.48) '
                         f'(stroke (width 0.254) (type default)) (fill (color 0 0 0 0)) '
                         f'(uuid {q(uid(sheet))}) {prop("Sheetname", title, x+74.93, y-2.54)} '
                         f'{prop("Sheetfile", sheet+".kicad_sch", x+74.93, y+33.02, size=1.016)} '
                         f'(instances (project {q(PROJECTNAME)} (path {q("/"+root_uuid)} '
                         f'(page {q(index+2)})))))')
        lines.append(')')
        (PROJECT / f'{PROJECTNAME}.kicad_sch').write_text('\n'.join(lines), encoding='utf-8')
        (PROJECT / 'sym-lib-table').write_text(
            f'(sym_lib_table (version 7) (lib (name "{LIBNAME}") (type "KiCad") '
            '(uri "${KIPRJMOD}/../libraries/master.kicad_sym") (options "") '
            '(descr "Project-owned, explicit pin maps; see research evidence")))', encoding='utf-8')
        (PROJECT / 'fp-lib-table').write_text(
            f'(fp_lib_table (version 7) (lib (name "{LIBNAME}") (type "KiCad") '
            '(uri "${KIPRJMOD}/../libraries/master.pretty") (options "") '
            '(descr "Project-owned physical footprints")))', encoding='utf-8')
        project_file = PROJECT / f'{PROJECTNAME}.kicad_pro'
        if not project_file.exists():
            project_file.write_text('{}\n', encoding='utf-8')
        (ROOT / 'reports/design_manifest.json').write_text(
            json.dumps(self.parts, indent=2) + '\n', encoding='utf-8')
