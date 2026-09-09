# Current schematic PDF visual review

Reviewed on 2026-09-09 by an independent schematic visual QA agent.

Source: `exports/review/schematic_candidate.pdf`

SHA-256: `0bcee143453598c67100fae68172a60e036c274a5193b948fb6fc26bc12ff1ec`

The source hash was checked before rendering and again after inspection. The file did not change during this review.

## Method and scope

Read the PDF skill and used the bundled Poppler `pdftoppm` executable to render all 15 pages at 135 dpi. Visually inspected each individual full-page PNG, including frame margins and title blocks. Rendered an additional 300 dpi detail of page 13 around C102 to verify the polarized capacitor's plus sign. Intermediate renders are in `reports/schematic_visual_current/`.

This is a visual readability and formatting review of the exported PDF. It does not establish electrical correctness, part suitability, PCB manufacturability, mechanical fit, ERC/DRC status, or hardware qualification. No schematic or circuit edits were made.

## Result

No unresolved visual defects were found in the inspected PDF: no clipped net labels, overlapping symbols or component values, note collisions, unreadable drawn connections, or schematic content intruding into the title blocks.

The reported earlier issues are resolved:

- Page 7: `#FLG05` and its `PWR_REF_4V096` label are fully inside the left frame.
- Page 7: the shortened title, `Power readiness and isolation`, fits the title block.
- Pages 10-12: `C30` through `C35`, their labels, and values are above the title blocks with visible clearance.
- Page 15: all three emitter land symbols have enough width for `HEATER+` and `SWITCHED_RETURN`.

## Pages inspected

| PDF page | Content | Visual outcome |
| --- | --- | --- |
| 1 | Root sheet and 14 child sheet entries | All entries, notes, and title block contained in the frame. |
| 2 | ESP32 USB power and firmware interface | Socket pin mappings and notes clear; no clipping. |
| 3 | ADS1256 removable module and socket mapping | Digital and analog socket mappings and notes clear; no clipping. |
| 4 | Battery inputs, polarity protection and master switches | Both power domains, fuses, switches, flags, and notes clear. |
| 5 | Quiet held ADC supply and local digital supply | Regulator pin labels, capacitor additions, set resistors, and notes clear. |
| 6 | USB and ESP32 supervision with battery-referenced enable | Supervisor and control symbols, labels, diode polarity, and notes clear. |
| 7 | Power readiness and isolation | Dense sheet remains separated; flag, labels, notes, and title block clear. |
| 8 | SPI interface across independently switched supplies | Buffers, output resistors, enable circuits, and notes clear. |
| 9 | SPI defaults, decoupling and power-off leakage bounds | Pull resistors, capacitors, and notes clear. |
| 10 | Detector/reference pair 1 - buffered ADC isolation | U30/U33 mappings and passives clear; C30/C33 clear of title block. |
| 11 | Detector/reference pair 2 - buffered ADC isolation | U31/U34 mappings and passives clear; C31/C34 clear of title block. |
| 12 | Detector/reference pair 3 - buffered ADC isolation | U32/U35 mappings and passives clear; C32/C35 clear of title block. |
| 13 | Isolated adjustable emitter supply | Regulator, potentiometer, capacitor polarities, flags, and notes clear. |
| 14 | Three optically isolated emitter PWM channels | Optocoupler pin labels, three channel circuits, and notes clear. |
| 15 | Existing emitter wire connections | All three wire-land symbols, net labels, and harness notes clear. |

## Intentional rendering conventions and limits

The DNP crosses and faded rendering on customer-installed `U1`, `U2`, `J1`, and `J2` are standard deliberate assembly annotations. The crosses traverse the module bodies; unobscured socket mappings on pages 2 and 3 provide the module connection detail, and the jack external net labels remain readable. This is not an accidental overlap or a request to change their assembly status.

The circuit uses labeled functional symbols and named nets extensively. The labels are visibly separated and readable at zoom; this review did not infer physical net connectivity from proximity. Small engineering notes require normal PDF zoom for detailed reading, particularly when the A2 overview is fitted to a screen. The source PDF is the reading artifact; reduced PNG previews are inspection intermediates.

Any later PDF regeneration invalidates this exact-hash review until the changed pages have been inspected again.
