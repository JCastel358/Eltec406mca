# Reproducible preliminary silkscreen cleanup

`tools/clean_silkscreen.py` operates on a project copy and preserves electrical identity and every native non-silk item. It does not change pad geometry, nets, footprints' placement/UUID/schematic paths, tracks, zones, board outline, or existing fabrication geometry. It retains every reference on front silkscreen; it does not suppress DRC rules or hide references.

The final run picked up the newly rebuilt **206-footprint** candidate automatically. Native KiCad 10.0.6 DRC with all severities and schematic parity reported:

| Category | Before | After |
| --- | ---: | ---: |
| Text thickness | 199 | 0 |
| Text height | 199 | 0 |
| Silk overlap | 139 | 0 |
| Silk over copper/mask | 132 | 0 |
| Library footprint mismatch | 0 | 0 |
| Schematic parity issues | 0 | 0 |
| Expected unrouted connections | 462 | 462 |

This is **zero silk/geometry/parity violations on an unrouted candidate**, not a routed-board DRC pass or fabrication release. Full native outputs are `reports/silkscreen_cleanup/before_drc.json` and `after_drc.json`; `cleanup_report.json` records source/output hashes, all moved labels, every silk graphic transferred to Fab, candidate library hashes, and preservation checks.

## What the helper does

- Uses at least 1.0 mm text, 0.15 mm strokes, and a conservative 0.19 mm geometric clearance target around pads/silk. The project retains its native 0.15 mm silk clearance rule. The dimensions follow the project's verified [JLCPCB capability minimums](https://jlcpcb.com/capabilities/pcb-capabilities).
- Checks real native pad polygons, including rounded corners. It raises silk-line widths to at least 0.15 mm and transfers conflicting outline/polarity detail to Fab. Original Fab geometry remains unchanged.
- Shares graphic changes by footprint type and writes matching **candidate** project libraries. Pin legends use a shared local placement per footprint type. This resolves native library mismatches without suppressing the check.
- Reserves connector and pin labels first, then places references in nearby free space. Every reference remains visible at a horizontal or vertical readable orientation. Actual component-body geometry and solder-mask clearance remain obstacles; bare wire-land access courtyards are not treated as plastic components.
- Serializes and compares native non-silk structure before/after cleanup and after save/reload. Reference text, footprint UUIDs, and schematic paths are separately checked. The source PCB is never overwritten.

The emitter wire-land legend was explicitly corrected from **GND** to **RETURN** in the project-owned `Emitter_Wire_Lands.kicad_mod` and its generator `tools/build_interface_footprints.py`. This marks the `EMITn_LOW` MOSFET drain/PWM return; `V+` remains the emitter regulated supply. Detector GND/case labels and `source_reference` remain unchanged. The helper also corrects old board instances that still carry the old GND text.

## Running and integrating

From this new project's root:

```powershell
& 'C:/Program Files/KiCad/10.0/bin/python.exe' tools/clean_silkscreen.py
```

The result is `reports/silkscreen_cleanup/candidate/single_detector_usb_master.kicad_pcb` with matching libraries under `reports/silkscreen_cleanup/libraries/master.pretty/`. The copied project's table points at those candidate libraries for honest native DRC comparison.

After the final electrical/placement build, rerun the helper and inspect its fresh reports. To integrate, first verify the source PCB still matches `source_sha256` in the report and preserve a backup. Copy **only the cleaned PCB and the candidate `.kicad_mod` library files** into the native project and its existing `libraries/master.pretty/`. Keep the actual project's existing `fp-lib-table`, `sym-lib-table`, project settings, and schematic hierarchy. The copy's table is deliberately relative to its review folder and must not replace the actual project's table.

Rerun native DRC after integration. The helper is generic across the changed footprint count, and it was also rerun on an already cleaned 201-footprint copy: all references stayed visible and native violations/parity remained zero with unrouted count unchanged. Such a rerun may refine nearby labels because transferred Fab detail becomes an existing body-boundary obstacle; it is safe rather than guaranteed byte-identical. The source and output directories must be distinct, and the helper refuses an output path that would overwrite its own input.

## Visual review and practical limit

Native PDF/SVG previews were inspected to check clipping and label visibility. Use PDF export with `--scale 0` for the full board: its negative-Y power bay otherwise falls outside a default fixed-scale page. The final full-board view is `reports/silkscreen_cleanup/overview.pdf`; its raster companion is `overview.png`.

The emitter corner and central protection circuitry remain dense. Some reference labels move several millimetres from their component to keep all labels printable and clear. The full displacement list is in the report. Assembly should also use the native Fab drawing, where each component's reference is centered on its footprint, rather than guessing association from a crowded silk label alone. The cleanup establishes readable, rule-compliant text placement; it does not certify mechanical accessibility, RF behavior, routing quality, or finished assembly suitability.
