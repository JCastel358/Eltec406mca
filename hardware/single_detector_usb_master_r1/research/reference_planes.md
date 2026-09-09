# Proposed reference planes and regulator thermal ground

`tools/add_reference_planes.py` is prepared for use after trial routing. Its default command writes a geometric proposal only. `--build-copy` uses native KiCad zones, fills them, adds validated off-pad vias/short ground connections, and runs native DRC on a separate project copy. It never overwrites the main board.

**Latest routed validation:** `reports/reference_planes_routed/review.md` supersedes the pre-route counts and via coordinates below. The final routed ground candidate has 0 native DRC violations, 0 parity issues, no GND/EM_GND opens, and 5 remaining non-ground opens. Protected Kelvin validation passes. It adds 10 zones, 20 through vias and 13 ground connections while preserving all previous native geometry. Its source is `fad6bd08…` and its copy is `b33a97f8…`; full hashes and integration instructions are in that review.

The current review is an **unrouted geometry precheck**, based on source SHA-256 `d521b5ccd8612cdf9124a9c9deba861d1cd8b8a23226c328b7ebdb7d1327707e`. Native KiCad 10.0.6 validation on the copy reports **0 violations, 0 schematic parity issues, and 442 unconnected items**, compared with 462 unconnected items before adding copper. Those remaining connections must still be routed. This is not a finished-board DRC pass or thermal qualification. The review-copy SHA-256 is `b6c8333a3b9f599492e362b56542393bcdaba7cecaf2fd523a13194a25bea5a1`.

The exact polygons, pad-coverage checks, proposed/rejected via sites, and input hash are in `reports/reference_planes/plane_plan.json`. `plane_plan.png` shows the ground domains and corridor. `copy_validation.json` records native before/after counts and filled areas; the actual native reports are `before_drc.json` and `after_drc.json`.

## Plane geometry from the placed pads

Both internal layers carry the same regional arrangement. No internal signal tracks or blind/buried vias are added or allowed. Zone clearance is 0.25 mm; minimum copper width is 0.25 mm. Through-hole pads use 0.5 mm thermal spokes and 0.3 mm thermal gaps; SMD connections and the local U100 spreaders are direct copper. The source antenna rule area remains unchanged on every copper layer.

| Region | Proposed geometry in board millimetres | Reason |
| --- | --- | --- |
| Main GND | Board interior with edge notches for the emitter regions and a small U16 pocket cutout | Leaves the ADC/SPI route corridor within one GND region. |
| EM input/regulator | Top region x80…97.5, y−29.5…−14; narrow x92.5…97.5 bridge down to y33; regulator region x80…97.5, y33…78 | Connects the right-side emitter regions while going around the detector jack/power components. |
| EM PWM | x12.5…37.5, y95.8…114.5 | Covers actual emitter ground pads while leaving the GND-side LED-drive column at x40…40.4 in the main GND region. |
| U16 emitter pocket | x81.25…84.5, y2.25…8.5, with a 1 mm surrounding GND cutout | U16's emitter-side pads sit beside detector-domain parts; a broad emitter rectangle here would put detector circuitry over the wrong reference plane. |
| U100 front/back thermal copper | x82.1…95.4, y37.55…49.45, net EM_GND | Direct thermal connection to tab/pad3 with through-via coupling into the internal EM region and back spreader. |

The GND/EM_GND boundary has at least a 1 mm planned moat; the plane gap through the U101 optical boundary is 2 mm, from y93.8 to y95.8. This is a low-voltage domain separation arrangement, not a claimed safety-insulation rating. All placed GND pad centres remain over a planned GND region, and every EM_GND pad centre is over an emitter region. The rectangular check corridor x57…77, y30…108 stays within GND; no emitter split crosses it. The eventual routes must still be reviewed against the actual filled copper and antenna cutout.

## U100 vias outside exposed solder pads

The native rotated tab/pad3 copper box is **[83.35, 38.8] to [94.15, 48.2] mm**. Proposed through vias are 0.6 mm diameter with 0.3 mm drills. Their annuli stay at least 0.3 mm from every copper pad, including the tab. They are tented in the candidate and are not via-in-pad. The geometry check includes rounded pad shapes, other routes/vias, and the existing via keepouts.

| Via row/column | Proposed coordinates |
| --- | --- |
| y38.0 and y49.0 | x84.214, 86.482, 88.750, 91.018, 93.286 on each row |
| x82.55 | y40.210, 42.372, 44.628 |
| x94.95 | y40.210, 42.372, 44.628, 46.790 |

Seventeen sites survive. The candidate at **[82.55,46.79]** is rejected because it is too close to R100.2. The actual front and back thermal spreaders fill to approximately **157.8 mm² and 158.3 mm²** on the unrouted copy. These areas and the via count do not establish junction temperature; assess the finished assembly at worst-case regulator dissipation and airflow.

## Connecting the local planes

U16 is an **SMDIP-4** footprint with no plated ground hole, and the PWM emitter ground pads are SMD. Simply pouring internal copper leaves these local regions unattached. The helper looks for an existing EM_GND through-via/PTH connection within each local region. If none exists, it proposes an off-pad through via and a short **1.0 mm F.Cu EM_GND connection** to the existing ground pad:

| Ground anchor | Via centre | Short front-ground connection |
| --- | --- | --- |
| Q101.2 | [34.05,103.05] | From [34.05,104.05] to the via; 1.0 mm long |
| U16.3 | [82.81,7.77] | From [82.81,6.27] to the via; 1.5 mm long |

The initial copy without these connections had four isolated-copper warnings, one per local region per inner layer. With the two validated ground connections, native isolated-copper warnings are zero. The helper does not suppress the rule or call floating copper a completed plane. The eventual routed EM_GND path must connect these component grounds to the emitter battery return; this pre-route copy still has the explicitly reported unrouted items.

New stitching sites are checked against both outer layers, every copper pad, and keepouts. A short ground connection is added only when its complete front-layer shape passes the foreign-pad/track checks. If no clear site remains after routing, the helper refuses the copy and requests a geometry review; it does not move a component or alter an existing route.

## U10 exposed pad and separate quiet returns

U10's GND exposed pad 15 occupies **[33.15,−8.65] to [34.85,−5.35] mm**. Two additional 0.6/0.3 mm tented through vias sit beyond its short ends, clear of the copper and paste apertures. Each connects with a 1.0 mm front-layer GND stub. This couples the exposed pad to both broad internal GND planes without an unfilled hole in the exposed solder pad. The via annulus has 0.5 mm clearance from the exposed-pad end; clearance from all other pads is also checked.

The output capacitor and three SET capacitors each receive their own short front-layer connection and through via into that same broad GND region. They do not share a narrow surface trace carrying output-capacitor current. The proposed local connections are:

| Anchor | Via centre in mm | Front-layer GND width | Purpose |
| --- | --- | --- | --- |
| U10.15, top end | [34.0,−9.45] | 1.0 mm | Exposed-pad thermal ground |
| U10.15, bottom end | [34.0,−4.55] | 1.0 mm | Exposed-pad thermal ground |
| C11.2 | [38.5,−11.675] | 0.6 mm | Separate output-capacitor return |
| C15.2 | [34.0,−0.975] | 0.3 mm | Separate SET-capacitor quiet return |
| C37.2 | [37.3,−0.975] | 0.3 mm | Separate SET-capacitor quiet return |
| C38.2 | [40.6,−0.975] | 0.3 mm | Separate SET-capacitor quiet return |

These six GND vias are additional to the 17 U100 EM_GND thermal vias and two local EM_GND stitches. The current copy therefore adds 10 zones, 25 through vias and 8 outer-layer ground stubs. All existing geometry remains unchanged; no internal signal track is introduced. These are geometric candidates, not a claim that two EP vias establish a particular thermal rating or that the finished regulator noise has been qualified.

No additional front GND zone is introduced near U10. The exposed-pad stubs occupy x33.5…34.5 mm, clear of the protected U10.12-to-C11.1 Kelvin rectangle x35.0…37.4, y−7.725…−7.275 mm. The other new GND connections lie outside that y band. The final routed board must also pass the independent protected-Kelvin validator after plane integration.

After routing, the helper checks alternate EP sites within 0.2 mm and alternate short capacitor-ground sites. Every rejected site is recorded. If no acceptable site remains, it writes the proposal with `review_required` and refuses to build the copy. An unrelated existing GND via elsewhere in the large plane does not cause the local U10/capacitor return check to be skipped.

## Commands and integration

From this new project's root, after the latest placement/routing change:

```powershell
& 'C:/Program Files/KiCad/10.0/bin/python.exe' tools/add_reference_planes.py
& 'C:/Program Files/KiCad/10.0/bin/python.exe' tools/add_reference_planes.py --build-copy
```

Inspect the fresh plan and native reports before applying anything to the main board. The candidate is `reports/reference_planes/candidate/single_detector_usb_master.kicad_pcb`. Its copied library tables point back to the source project's libraries; retain the actual project's existing tables and settings if integrating only the PCB later. Verify that the main PCB still matches the plan's source hash and create a backup before replacement.

The helper rejects an already poured source rather than duplicating or silently replacing copper zones. It preserves every existing native item, including footprint UUIDs/schematic paths, pads/nets, tracks, outline and the antenna rule area; only the explicitly recorded zones, through vias and short outer ground connections are new. Native serialization is compared before/after and after save/reload. Rerun from the fresh routed source, refill, and inspect the final ground topology, clearances, thermal copper and remaining connectivity before release.
