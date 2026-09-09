# Schematic candidate visual review

Reviewed 2026-09-09. This is a read-only visual review of the candidate exported before the next power-architecture revision. It is not an electrical, mechanical, assembly, ERC, or DRC approval.

## Evidence and scope

- Source: `exports/review/schematic_candidate.pdf`, 621,768 bytes, SHA-256 `C2D919E69F77CFD062490204C28764CEEEB5D36ABE25D26647EE27A9DC275160`.
- All 15 pages rendered with Poppler `pdftoppm -r 105 -png` to `reports/schematic_visual/page-01.png` through `page-15.png` and inspected individually.
- Root page is A2 landscape; child pages are A3 landscape. Detail crops were rendered directly from the PDF at 170–225 dpi; they are not regenerated drawings.
- The native schematic text and current manifest were read to identify exact symbol centers, note coordinates, and font sizes. Coordinates below are schematic millimetres from the top-left sheet origin.
- No source design, generator, native schematic, library, or PCB was changed. The existing preliminary 33-footprint board was left untouched; it is stale relative to the 173-symbol schematic and cannot establish current PCB/schematic parity.

## Actionable findings

### V1 — Fix overlapping emitter pin names before the next review export

On page 15, `emitter_connections.kicad_sch`, all three `Emitter_Wire_Interface` symbols print `HEATER+` and `SWITCHED_RETURN` on the same horizontal row inside a body only 15.24 mm wide. The two strings visibly overprint. This directly obscures the labels technicians need to distinguish positive and switched return.

| Reference | Symbol center (mm) |
| --- | --- |
| JEM1 | (101.60, 71.12) |
| JEM2 | (101.60, 121.92) |
| JEM3 | (101.60, 172.72) |

Widen this symbol, shorten the visible pin names while retaining an explanatory note, or place its two named pins on different rows. Keep physical pin numbers and net assignments unchanged. The native definition has opposing pins at local X = ±10.16 mm and Y = 0, with body X = ±7.62 mm. There is ample free sheet area to widen it.

Evidence: `schematic_visual/emitter_pin_overlap-15.png`.

### V2 — Make customer-installed module pin names readable in the PDF

On page 3, `adc_interface.kicad_sch`, U2 at (88.90, 116.84) mm is correctly identified as the customer-installed ADS1256 module, but the large red DNP cross and gray rendering reduce the visibility of several right-side AIN/AGND pin names. The pin numbers and external net labels remain separately visible. This is a visual presentation issue, not evidence of missing electrical connections.

U1 at (86.36, 106.68) mm on page 2 uses the same crossed-out gray presentation. Consider suppressing the PDF DNP cross/gray appearance for a reader-facing export, while preserving native assembly exclusion and adding explicit `CUSTOMER INSTALLED MODULE — EXCLUDED FROM PCBA` text. Do not remove the native exclusion merely to improve the picture. If the cross is retained, a clear uncrossed pin table beside each module would also resolve the readability issue.

Evidence: `schematic_visual/adc_module_detail-03.png`. No circle-outline collision was present in this candidate; the module body is rectangular.

### V3 — Replace stale SPI integration note

On page 2, `esp32_interface.kicad_sch`, the note at (25.40, 162.56) mm says the powered-off-protected SPI buffers are “to be integrated on the interface-protection sheet.” Page 8 already contains that circuit. Replace that phrase with a direct reference to `spi_protection.kicad_sch` and its page. Native generator location at review time: `tools/build_design.py:58`.

### V4 — Improve small print in the power notes and root index

No bottom notes were clipped or overlapped. However, the four dense power notes use 1.016 mm text, whereas most other explanatory notes use 1.27 mm. They are readable in the detail render but unusually small at printed sheet scale. Increase them to at least the other notes' 1.27 mm size, wrap them as needed, and use the available white space above the title block.

| Page | Sheet | Note origin (mm) | Native text size |
| --- | --- | --- | --- |
| 4 | power_inputs | (25.40, 261.62) | 1.016 mm |
| 5 | adc_supply | (25.40, 254.00) | 1.016 mm |
| 6 | power_supervision | (25.40, 251.46) | 1.016 mm |
| 7 | power_permission | (25.40, 256.54) | 1.016 mm |

Evidence: `schematic_visual/power_notes-04.png`. This crop confirms the note is intact; it does not establish that the numeric or architectural statements are correct.

On page 1, the 14 root sheet names use 1.27 mm and filenames 1.016 mm on an A2 page with large empty 149.86 × 30.48 mm sheet boxes. All names fit, but the scale makes the index hard to read in a full-page view. Increase sheet-name text to about 2.0–2.54 mm, and increase filename text if needed. Sheet box origins are X = 25.40, 203.20, or 381.00 mm; Y = 63.50 + 58.42 × row. Sheetname centers are (box X + 74.93, box Y − 2.54). Keep the longest titles within their column.

Evidence: `schematic_visual/root_names-01.png`.

## Per-page coverage

| Page | Sheet | Visual result |
| --- | --- | --- |
| 1 | single_detector_usb_master | All 14 hierarchy boxes and titles inside the A2 frame; root annotation intact. Index text is small relative to available space (V4). |
| 2 | esp32_interface | Socket labels and note blocks do not overlap; U1 cross/gray readability (V2), stale note (V3). |
| 3 | adc_interface | J5/J6 labels, analog numbering, and bottom notes fit; U2 cross overlays portions of pin names (V2). |
| 4 | power_inputs | J1/J2, F1/F2, switches, optocoupler, and global labels separated. Long footer title fits. Small bottom note (V4). |
| 5 | adc_supply | Supply symbols, bulk capacitors, and notes separated. No clipping. Small bottom note (V4). |
| 6 | power_supervision | Supervisor/MOSFET labels separated. Footer and bottom note do not collide. Small bottom note (V4). |
| 7 | power_permission | U14/U15 and all Q/R/C groups clear. Footer title fits. Small bottom note (V4). |
| 8 | spi_protection | U50/U51/U52 pin names fit. U51 output label and R58 input label are close at overview scale but do not touch in the detail crop. No clipping. |
| 9 | spi_defaults | Resistor/capacitor labels and both note blocks fit. No clipping or overprinting observed. |
| 10 | analog_pair_1 | JDET1/JDET2, U30, U33 and passive labels fit. Top/bottom notes and long footer title remain inside borders. |
| 11 | analog_pair_2 | JDET3/JDET4, U31, U34 and passive labels fit. No clipping or overprinting observed. |
| 12 | analog_pair_3 | JDET5/JDET6, U32, U35 and passive labels fit. No clipping or overprinting observed. |
| 13 | emitter_regulator | U100, RV100 and passives separated. Adjustment and thermal notes intact. No clipping observed. |
| 14 | emitter_pwm | U101, Q101–Q103 and resistor labels fit. NC markers and top/bottom annotations remain distinct. |
| 15 | emitter_connections | Repeated pin-name overprint inside JEM1–JEM3 (V1). Net names, numbers, references and notes otherwise fit. |

Detail evidence for page 8: `schematic_visual/spi_cs_spacing-08.png`.

## Next review

Rerender the revised export and recheck at least pages 1–8 and 15 after these changes. A fresh full-page inspection is appropriate if the power-stage revision changes sheet count or placement. Visual review does not replace the independent electrical review or final routing/parity checks.
