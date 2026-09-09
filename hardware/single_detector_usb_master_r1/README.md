# Single detector rig — USB master power, revision 1

**Routed engineering prototype candidate. Not a fabrication release.**

This separate project replaces the supplied ESP32 carrier while preserving the original files in `source_reference/`. Its required behavior is that the upstream USB switch starts and stops the complete rig, including both HeyFuture LiFePO4 6.4 V, 6 Ah battery domains, with the ADC protected throughout startup and shutdown. The emitter maximum stated current is 200 mA. Component selection is independent of the IRLB8748s available for the earlier perfboard idea.

The deliverable is an editable, electrically connected KiCad design with routed PCB, project libraries, assembly BOM/CPL, assembly views, draft fabrication exports, and reproducible ERC/DRC/parity checks. All physical board components are to be populated except the ESP32 module, ADS1256 module, and two barrel jacks. Module sockets are populated components. Original wire solder lands are copper features, not fictitious purchased parts.

Use `PLAN.md` for milestone status, `DECISIONS.md` for choices and assumptions, and `research/` for source-backed engineering evidence. Reports must identify the design revision/hash they validate. Absence of an error report is not a pass.

The original board is 86 × 103 mm. Until mating-part and enclosure evidence is confirmed, trial layouts using those coordinates remain preliminary. No enclosure fit, prototype timing, actual module internals, production sourcing, or manufacturing release is implied by CAD checks.

The trial outline is 86 × 145 mm, growing only perpendicular to the ESP32's
long axis. The 213-symbol schematic passes native ERC and 272 exported
connectivity checks. All 15 schematic PDF pages have been visually reviewed.
The 206-footprint, four-layer board is fully routed: native KiCad 10.0.6 reports
zero DRC violations, zero unrouted connections and zero schematic mismatches
after filling its ten ground/thermal zones. Exact report hashes identify the
checked revision. These are CAD results; physical sequencing is not yet tested.

Start with `single_detector_usb_master/single_detector_usb_master.kicad_pro`.
Keep the adjacent `libraries/` directory with the project. `REPRODUCE.md`
describes validation; `BRINGUP.md` records the unperformed hardware checks.
Use the separate `firmware/Eltec_USB_Master/Eltec_USB_Master.ino` for the R1
carrier after confirming the ESP32 configuration. It compiled and passed 24
host-protocol checks; nothing was flashed.

The draft assembly contains 195 fitted components in 68 BOM groups. Seven exact
manufacturer part numbers (nine references) need JLC catalog/sourcing
confirmation, and C12's identified catalog entry was out of stock at the public
check. Prepared sourcing requests are in `research/global_sourcing_prepared.csv`;
none was submitted. All vendor placement rotations still need assembly-preview
confirmation. These files are not an immediately approved populated-board order.

Mechanical fit, actual battery endpoints, module internals, USB switch behavior,
thermal performance and measurement calibration remain unverified. In particular,
the stacked static ADC supply corner leaves only 4.64 mV below its 5.25 V
operating ceiling; actual module AVDD transients must be measured before using
the prototype for accepted detector measurements.
