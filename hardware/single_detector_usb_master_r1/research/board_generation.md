# Candidate board generator

`tools/build_board.py` now consumes the actual KiCad XML netlist and current design manifest, resolves each used footprint into the project library, and calls `placement_plan.build_plan` for the actual on-board components. It does not freeze a reference count. Future footprints must have a genuine native courtyard; missing courtyards cause an explicit failure instead of an assumed package outline.

The generator preserves the 15 socket/jack/wire-land interfaces from `reports/interface_geometry.json`. The trial outline remains X = 12…98 mm and Y = −30…115 mm, or 86 × 145 mm. No mounting holes were inferred or added. Placement is still provisional and rerunning the placement generator can replace manual placement.

## Routing protection

By default, regeneration refuses to overwrite an existing board with any tracks/vias or any non-rule copper zones. An unfilled copper zone also counts as design work. Rule areas alone do not prevent placement-only regeneration.

The explicit `--replace-routed-board` flag permits discarding routing. Before replacement, it saves a byte-identical hash-named `.kicad_pcb` backup beneath `reports/board_backups` and verifies its hash. This flag is not used automatically. To continue routing, edit the existing PCB rather than rerunning the placement generator.

The generator also checks the existing board hash and all native schematic-sheet, project, manifest, and XML-netlist hashes immediately before saving. Concurrent source or board edits cause it to refuse the save. The original supplied design hashes remain independently checked.

## Four-layer candidate

The native board has four enabled copper layers and a nominal 1.6 mm carrier thickness:

| Layer | Intended purpose |
| --- | --- |
| F.Cu | Signal and power routing |
| In1.Cu | Detector/USB GND reference, with a separate isolated EM_GND region where needed |
| In2.Cu | Reference/return copper, with a separate isolated EM_GND region where needed |
| B.Cu | Signal and power routing |

These are candidate layer purposes. The generator does not create copper pours, route tracks, select approved dielectric thicknesses or materials, or establish impedance control. Never join EM_GND and GND through an inner layer or via. Any later ground split must preserve the return path of each signal.

## Antenna exclusion

The original ESP32 footprint includes an antenna-like silk graphic at X = 45.44…53.06 mm, Y = 93.35…106.05 mm. It supports approximate antenna location, not an authenticated module RF specification.

The new native rule area reserves X = 42.0…55.5 mm, Y = 89.0…110.0 mm on F.Cu, In1.Cu, In2.Cu, and B.Cu. Tracks, vias, pads, copper pours, and footprints are prohibited in this corridor. The rule is an actual KiCad rule area, not just a drawing. It leaves the inherited socket rows in place and agrees with the placement planner's no-new-component corridor.

This conservatively clears the carrier around the source graphic. It is not proof that the unknown plug-in module meets its manufacturer's antenna clearance recommendation. Verify the exact module revision, real antenna boundary, manufacturer keepout, mating socket/underside clearance, USB cable access, and enclosure effects before physical/RF qualification.

## Validation performed before the revised schematic rebuild

- Python compilation passed for `build_board.py` and `placement_plan.py`.
- A disposable native board was saved and reopened with all four copper layers and the antenna rule area. Every prohibited-item flag and every copper-layer assignment survived native round-trip.
- Disposable fixture tests confirmed that a rule-area-only board can regenerate, a routed board is refused by default, an unfilled copper zone is refused, and explicit replacement preserves a hash-matched backup.
- A synthetic capacity probe added six extra 0805 resistors to each analog pair region. All 185 physical footprints fit the unchanged trial outline with zero native courtyard-bounding-box intersections. This was a packing-capacity probe, not a changed schematic or a physical/electrical approval; no design artifacts were written by it.
- Fixture results are recorded in `reports/board_generator_probe.json`.

The real candidate PCB was deliberately not rebuilt during the ongoing power/analog schematic revisions. Its eventual native ERC/netlist/placement/parity/DRC results must be reported separately after both revised stages are ready. The existing old board is not evidence of current schematic parity.
