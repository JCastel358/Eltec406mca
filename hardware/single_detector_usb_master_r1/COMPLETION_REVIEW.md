# R1 engineering candidate handoff

The separate editable prototype design is fully routed and passes native KiCad
10.0.6 electrical-rule, board-rule and schematic-parity checks. Its purpose is
single-switch operation using downstream USB presence, with hardware power
qualification and shutdown protection. Actual single-switch operation has not
yet been demonstrated on hardware. No board was ordered, uploaded or released
for production.

The checked PCB SHA-256 is
`2ab4937f2ec558227736722549489c8d60b382db2a86436887f104acd336f4e2`.
It contains 206 physical footprints, 2160 track/via objects and ten filled copper
zones on four layers. The preliminary outline is 86 × 145 mm. Native checks
report **zero DRC violations, zero unconnected items and zero schematic parity
issues**, with no configured exclusions. The only ignored DRC check concerns
unused tuning profiles; the only ignored ERC category concerns absent SPICE
simulation models. Neither hides a physical electrical-rule failure.

## Requirement evidence

| Requested result | Delivered evidence / qualification |
|---|---|
| Separate project; preserve original | `single_detector_usb_master/` and adjacent `libraries/`. `reports/original_preservation_final.json` checks 23 original archive/photo records and original/R1 firmware hashes. |
| Choose parts for the application | Exact manufacturer parts independent of IRLB8748 inventory; decisions and manufacturer-backed analysis in `DECISIONS.md` and `research/`. |
| USB switch controls both battery domains | Integrated positive-lead battery switching, isolated emitter control, held ADC/analog rail, hardware readiness and interface isolation. Circuit and conditional bounds are implemented; switching waveforms require the tests in `BRINGUP.md`. |
| Retain detector battery voltage | Switched detector supply remains battery-derived; no 8 V boost was added. |
| Growth only perpendicular to ESP32 | Fixed original 86 mm X span; extension in Y produces 145 mm candidate length. Original interfaces retained, no invented mounting holes. Actual fit remains unconfirmed. |
| Complete editable schematic and PCB | 213-symbol, 14-child-sheet schematic; full native copper routing and local libraries. Root project is `single_detector_usb_master/single_detector_usb_master.kicad_pro`. |
| ERC/connectivity | `reports/schematic_validation.json`, `interface_erc.json`: zero ERC violations; 272 explicit exported pin-connection checks pass. |
| DRC/parity/routing | `reports/board_validation.json`, `board_drc.json`: native refill/reopen, zero violations/opens/mismatches, through vias and outer-layer tracks verified. Separate regulator Kelvin guard passes. |
| Critical return-path review | `reports/copper_review_complete/review.md` and `reports/power_review_final/final_review.md`. Local reservoir, bypass and signal-plane return connections added without changing existing signal routes. |
| Populated assembly except specified items | 195 fitted parts / 68 BOM groups in `exports/assembly_draft/`; sockets and trimmer included. Only U1/U2/J1/J2 are customer installed; wire lands are copper features. Native fitted reference sets and placement coordinates match. |
| Review drawings | All 15 schematic PDF pages reviewed; all seven final board PDF pages reviewed. See `exports/review/schematic_candidate.pdf` and `exports/board_review/final_candidate/`. |
| Draft fabrication outputs | `exports/fabrication_draft/20260909T154345_189586Z_2ab4937f2ec5_DRAFT/`: 11 Gerber layers, Excellon holes/slots, drill maps/report, checksums and ZIP. Independent native snapshot checks and coordinate witnesses pass. |
| App/startup integration | Separate R1 firmware compiles for generic ESP32 with installed core 3.3.11 and passes 24 host-protocol checks. Supply faults stop emission and invalidate interrupted captures. It was not flashed. |
| Reproducible checks and portable handoff | `REPRODUCE.md`, `tools/`, reports and package manifest. The review ZIP reopens from its portable directory tree and undergoes a fresh native parity/DRC check. |

## Items requiring evidence before assembly approval or accepted measurements

Seven exact MPNs across nine references still need JLC catalog/global-sourcing
confirmation: D12, R39, R100, R101, U12, U13 and U30–U32. C12's identified public
catalog entry was out of stock. `research/global_sourcing_prepared.csv` contains
prepared requests; no requests or orders were submitted. Manufacturer identities
are fixed; substitutions require electrical/package review. Confirm vendor
rotations and THT process acceptance in the assembly preview. A selected part
number is not allocated assembly stock.

Confirm the actual module pin/USB power path, socket underside clearance,
enclosure fit and battery/charger voltage endpoints. Confirm the RIITOP switch
removes downstream VBUS and retains serial data when on. The supplied seller
ADC circuit is design evidence; it is not a measured certification of the
installed module.

The held ADC supply has a nominal 5.1798 V setpoint. Stacked static assumptions
leave only **4.64 mV below the 5.25 V operating ceiling**. This does not establish
overshoot or load-release performance through the module filter. Measure actual
module AVDD, input protection timing, startup/shutdown, rapid cycling, battery
removal/depletion, thermal behavior, noise and gain/calibration using `BRINGUP.md`.
The readiness logic detects undervoltage conditions, not overvoltage. Do not
accept detector test results from the new hardware until these bounds pass.

The pack remains a clearly labeled engineering prototype candidate. CAD checks,
protocol tests and drawing reviews do not constitute production qualification.
