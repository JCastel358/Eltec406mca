# Decision and assumption log

## D001 — Separate native project (confirmed user requirement)
Use this directory for the new design. Source copies remain evidence and are never edited in place. The original supplied schematic and PCB hashes are in `source_reference/source_provenance.json`.

## D002 — Supply and load statements (confirmed user facts, incomplete electrical envelope)
Both batteries are HeyFuture LiFePO4 nominal6.4V6Ah,38.4Wh, superseding the older9V detector notes and rounded6.5V description. Maximum emitter current is200mA. Fully charged voltage, undervoltage endpoint and protection behavior remain unverified; do not infer exact endpoints from nominal voltage.

## D003 — Single USB control (confirmed user requirement)
The RIITOP switch is upstream of the ESP32's USB connector. Derive power-control presence from downstream USB/VIN, subject to confirming the actual ESP32 board's power path. Do not connect battery power to a host-facing USB rail. Do not depend on firmware execution for shutdown.

## D004 — Ground domains (native-netlist evidence)
The emitter return is explicitly isolated by the LTV847. Detector analog grounds and ESP32 digital ground meet through plug-in module wiring, which the original carrier schematic does not describe. Preserve emitter isolation unless a reviewed electrical reason justifies changing it. Make required analog/digital ground connections explicit in the replacement instead of relying on hidden module wiring.

## D005 — Original geometry is a preliminary placement basis
Native CAD confirms an 86 × 103 mm rectangle, no carrier mounting holes, 48 mm jack separation, and module header pin centers. Preserve these as the trial layout baseline. The photos are not dimensioned enclosure CAD. New fit and vertical clearances remain unconfirmed; no invented mounting holes or optical component orientation.

## D006 — Correct module representation
The original ESP32 footprint duplicates pad 28 on pad 27 and incorrectly marks a through-hole module pattern as SMD. Rebuild clean module/socket representations. Individual physical female socket strips are populated assembly items; modules themselves are customer-inserted. Ground connections and unused pins must be explicit.

## D007 — Preserve wire interfaces unless a change is justified
Original detector/emitter patterns are solder lands for wires, not directly mounted sensor packages. They require no purchased connector to populate. A new detachable connector would be a real scope/mechanical choice and must include a selected part and mating harness. Do not infer sensor lead orientation from these wire lands.

## D008 — Simplification is subject to shutdown safety
The two available IRLB8748s were for the earlier perfboard concept; they do not constrain this new board. N-channel positive-lead switches require gate drive above battery voltage. A USB-only negative-lead scheme does not adequately manage detector shared-ground paths. The earlier external-adapter proposal is unvalidated research, not a circuit to copy automatically.

The trial design uses a battery-derived regulated ADC/analog supply, protected
SPI interface and hardware power qualification. The recovered seller circuit
supports the calculation model; its as-built tolerances still require physical
verification. The electrical candidate is frozen for trial routing, with the
conditional acceptance limits recorded in `BRINGUP.md` and `power_corners.json`.

## D009 — Validation and sourcing boundaries
KiCad ERC/DRC cannot prove analog timing, module internals, physical fit or battery range. Record these independently. JLC supports populated THT sockets; do not silently omit them. LCSC identity does not prove a currently allocated JLC assembly part. No source has been ordered or uploaded.

## Open inputs

- Verify battery endpoint specifications from the actual pack/charger documentation.
- Verify installed ADS1256 module agrees with the recovered seller circuit; measure input-filter DCR and actual AVDD margin.
- Recover/verify ESP32 USB/VIN path and off-state behavior of the selected switch.
- Confirm the selected 8.5 mm socket heights against actual module underside clearance.
- Confirm remaining JLC sourcing, stock allocation, placement rotations and process acceptance.

## D010 — Updated user answers, 2026-09-09

- Battery label: **HeyFuture LiFePO4, 6.4 V, 6 Ah, 38.4 Wh**. The nominal label is confirmed; charge cutoff/BMS endpoints still need manufacturer evidence.
- Preserve the detector's existing battery-voltage supply. Do not introduce the 8 V TP412 test condition or a 5 V detector supply in this revision. This preserves the current rig's condition; it does not certify TP412 compliance.
- Board growth is permitted only perpendicular to the ESP32's long axis. In the recovered native coordinates this means keeping the 86 mm X span and extending Y. Preserve the existing socket and jack coordinates; a power bay beyond the original upper edge is permitted for preliminary placement. No new mounting holes are authorized by this geometry statement.
- Exact ADC purchase identification: Amazon ASIN **B0DBSWRQZS**, provided by the user. The seller's schematic image shows a 10 uH input filter between header5V and AVDD, an internal 3.3 V regulator, and ADC input RC filters. Review this supplied circuit evidence before freezing shutdown calculations; it does not establish every as-built component tolerance.
- The user explicitly reiterated that the new populated board should use the best applicable components without being constrained by the IRLB8748s on hand.

## D011 — SPI interface component qualification

Use receiver-powered TI SN74LVC2G125 dual buffers with specified partial-power-down Ioff, rather than assuming the similarly named SN74LVC125A quad has the same protection. Three dual packages cover SCLK/MOSI/CS and MISO/DRDY. Each supply domain has its own default-disabled OE pull-up and driver; OE pull-ups must not connect the two 3.3 V rails together. The receiver defaults and current limits are recorded on the schematic and in the SPI research note. This is a circuit-stage decision; the complete sequence still requires the power stage.

## D012 — Analog isolation after the buffer

ADG4613 selectors now connect each ADC header input either to a low-impedance buffered detector signal or to ground through220ohms. This prevents normal switch leakage from acting through the detector's100kohm source load. Each ADC header has a permanent33kohm0.1% bleed for final power-off leakage. A noninverting gain of1+499/33000 compensates its nominal load; switch resistance and the existing module/ADC input impedance still cause measurable attenuation. Calculations and qualification limits are recorded separately; do not call it exact unity gain.

OPA2325 replaces OPA2196 on the held ADC rail. Its zero-crossover input architecture covers the valid405 offset range without moving a crossover region into that range. Keeping its output supply on the held rail bounds a faulty detector's amplified output. Very high rejected offsets may clip and are diagnostic values, not precise measurements. Retain the original100kohm detector loads and10kohm input-current limiting. This is independent of the IRLB8748 inventory.

## D013 — Regulator and startup review before routing

Reject the initial TPS7A49/SS14 reverse bypass because it does not establish the regulator's+0.3V differential absolute limit. Reject the4.6V ADC supervisor threshold because it falls below the ADC's4.75V operating floor. A reverse-protected LT3041 and explicit comparator/reference qualification have been implemented and independently checked against manufacturer data. Separate revision firmware waits and retries for hardware readiness, stops emission on a power fault, and invalidates the affected capture. It has compiled and passed 24 app protocol checks; it has not been flashed or bench tested.

## D014 — Trial routing does not resolve transient qualification

The nominal held rail is 5.1798 V. The stacked static upper corner leaves only
4.64 mV below the ADS1256 5.25 V operating ceiling. This is insufficient to
certify load-release behavior through the module's input filter from arithmetic
alone. The candidate is suitable for a screened engineering prototype, subject
to actual AVDD startup/load-release/shutdown measurements and the other listed
acceptance bounds. A passed ERC/DRC or ready signal cannot certify overshoot.
Battery-depletion restart/chatter also needs measurement; firmware invalidates
faulted captures and does not automatically resume emission.

## D015 — Routing and assembly data have independent checks

The LT3041 OUTS pin must reach the output capacitor positive terminal as a
separate sensing branch. Same-net connectivity is insufficient evidence of that
layout requirement. The routing importer must preserve the reviewed locked
branch and reject foreign branches joining its protected corridor. Inner layers
are reserved for ground copper; emitter and detector/USB returns remain distinct.
All actual routes and ground-plane junctions require native DRC and physical
path review before draft fabrication exports.

Assembly BOM/CPL exports must agree with the native PCB's fitted reference set.
Only ESP32, ADS1256 and the two barrel jacks are customer-installed physical
components. THT sockets and the trimmer are included in the 195 fitted parts.
Vendor package rotation, stock allocation and process acceptance remain separate
from a verified catalog identity.

## D016 — Complete routed prototype candidate

Trial01's final integration closed all connections. The signal repair replaced
four explicitly identified existing segments to escape the amplifier and route
the clock around the left side of the ESP32 header. All other existing routes
were preserved. The shorter clock path avoids the antenna keepout. Native DRC,
schematic parity, connectivity and the protected Kelvin check pass after refill.
The main board remains marked PRELIMINARY R1 - REVIEW ONLY. Physical acceptance
and JLC purchasing decisions remain outside these CAD results.
