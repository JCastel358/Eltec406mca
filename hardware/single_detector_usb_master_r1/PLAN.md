# Milestone plan and requirement evidence

Updated 2026-09-09. Status is based on current artifacts, not intended work.

| Milestone | Status | Completion evidence required |
|---|---|---|
| M0: Source, toolchain and original-design audit | Complete for supplied files | Source hashes preserved; KiCad10.0.6 native/API/CLI probes, geometry and original netlist/ERC audits recorded in reports and research |
| M1: Electrical architecture and calculations | Complete conditional prototype design; bench qualification open | Manufacturer-backed power paths, sequencing bounds, input protection, supply ranges, current/thermal/capacitor calculations and exact pin maps. Unverified acceptance inputs are explicit in BRINGUP.md and power_corners.json |
| M2: Editable schematic and project libraries | Candidate passes | Integrated 213-symbol, 14-child-sheet candidate: zero ERC violations, 68 interface, 121 analog and 83 power-connection checks; all 15 PDF pages visually reviewed. Physical power/measurement qualification remains separate |
| M3: Placement and mechanics | Candidate placement passes; physical fit open | Reopened preliminary 206-footprint, 86 x 145 mm board preserves original 15 interfaces. Critical capacitor positions refined; all references visible. Physical fit remains unconfirmed |
| M4: Routed PCB and integrity | Complete prototype CAD checks and copper review | Fully routed four-layer candidate; zero native DRC violations, zero parity differences and zero unconnected edges after refill. Protected Kelvin branch, local ground returns and sampled signal-reference coverage verified |
| M5: Assembly and draft fabrication package | Draft outputs complete; procurement acceptance open | 195 fitted references verified against native KiCad placement export; final reviewed assembly/copper PDFs and guarded Gerber/drill ZIP generated. Seven MPNs/nine references need catalog/sourcing confirmation; vendor rotations and actual allocation remain open |
| M6: Completion audit | Candidate handoff documented | COMPLETION_REVIEW.md maps requested artifacts to evidence and records the unperformed physical/manufacturing checks. Portable package validation is recorded in its manifest. No production-release claim |

## Required invariants

- Work only in this separate project; retain the supplied original unchanged.
- USB is the single operational power control; do not require app firmware to shut supplies down safely.
- Both battery domains stop drawing their normal operating current when USB is off.
- No powered sensor/driver path may violate ADC input limits during startup, steady operation, or shutdown.
- Battery circuitry must not back-power the unpowered USB/ESP32 domain.
- Preserve the firmware's functional SPI, DRDY and emitter control mapping unless a required correction is documented.
- Resolve electrical design before routing affected circuits.
- Retain source-to-schematic-to-PCB correspondence, including pin UUID paths, values, footprints and nets.
- Populate sockets and retained trimmers; only user-specified modules and jacks are omitted physical components.
- Refill copper immediately before final DRC; run schematic parity on the same saved revision.
- Zero unresolved electrical errors, unintended shorts, unrouted connections, or unexplained exclusions. Warnings are reviewed individually.
- Geometry derived from native CAD is evidence of the old design, not approval of new enclosure fit.
- Prepare review/draft manufacturing outputs only. No ordering, publication or production-release claim.

## Hardware / purchasing follow-up

1. Confirm mechanical/module/USB and battery endpoint evidence in BRINGUP.md.
2. Confirm prepared sourcing requests, allocation, vendor placement rotations and assembly process.
3. Separately authorize any order/upload after review; none has been placed.
4. Measure the prototype's power, input-protection, thermal and analog behavior. The stacked-corner upper voltage margin is too small to certify transient conversion validity without measurements.

## Continuation log

- 2026-09-09: Verified current disk state: only source evidence and two research reports existed; no new schematic/PCB or passed validation existed. Continued source/netlist evidence is useful progress. Resumed engineering work without treating prior intent as completed design.
- 2026-09-09: Created the native integrated project and deterministic validators. The173-symbol revision passed zero ERC violations and67 interface checks. Independent review then identified real regulator reverse-current and ADC-validity threshold issues; this revision is superseded during correction and has not been routed or released.
- 2026-09-09: Recovered seller module schematic from the user's exact purchase listing. It shows a10uH AVDD input filter and100R/100nF input filters. Implemented post-buffer switching, 33k ADC bleeders and OPA2325 with nominal load compensation. New power regulator/control implementation is under independent review.
- 2026-09-09: Integrated the reverse-protected LT3041 power stage and ESP-powered GPIO35 readiness output. The 208-symbol candidate passed ERC and exported connectivity checks. Reopened the 201-footprint placement board with no parity mismatch; 453 connections remain unrouted. Those results do not constitute a routing or transient-performance pass.
- 2026-09-09: Separate R1 firmware compiles with ESP32 core 3.3.11 and passes 24 protocol checks against all three app backends. Startup waits/retries hardware readiness; supply faults stop emission and invalidate captures. No firmware was flashed or tested on hardware.
- 2026-09-09: Added explicit project netclasses/manufacturing rules and a draft assembly exporter. It checks BOM/CPL/native fitted populations and placement origins, keeps four customer-installed items separate, and records unverified sourcing/rotation instead of inventing approval.
- 2026-09-09: Full-board trial01 routed the two outer layers while retaining the locked 0.20 mm OUTS Kelvin branch. A bounded import adapter restored only R52's 33 nm coordinate rounding, preserving every routed track and all strict native identity checks. Five 0.2498 mm power neckdowns were widened to their 0.25 mm minimum; native DRC then reported zero violations and 12 opens.
- 2026-09-09: Integrated ten ground/thermal zones and reviewed ground stitches with exact pre-existing native item preservation. Native DRC and parity are clear; all ground opens are closed and five non-ground opens remain. This is progress toward a complete trial layout, not a release or measured power-performance pass.
- 2026-09-09: Integrated the five remaining connections, three local bypass-ground stitches and explicitly recorded small AMP_OUT4/ESP_MOSI reroutes. Native KiCad refill/reopen/DRC/parity now passes with zero violations and zero opens; the protected Kelvin branch passes. Schematic revalidation also passes. The obsolete UNROUTED annotation was replaced with PRELIMINARY R1 - REVIEW ONLY.
- 2026-09-09: Final review added five GND through vias and two local surface stubs for C12/C16 and signal-plane transitions. Every prior route and placement was preserved. Final board 2ab4937f2ec5 passes native checks; final PDFs, BOM/CPL and draft Gerber/drill package were regenerated. Original archive members, retained source copies and original firmware hashes still match.
