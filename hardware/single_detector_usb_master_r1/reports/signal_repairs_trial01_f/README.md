# Frozen signal repair candidate — trial01_f

This is the selected copy-only repair. Earlier trial directories are diagnostics, not the integration candidate. No main-board or project files were changed by this task.

- Source board SHA-256: `b33a97f84bf1f496620f39c85c54ef9b8a860b96ba0045ccac9a89994dcc416f`.
- Native-refilled candidate SHA-256: `93b2e6a36082550680599f7186cef9cffda7636118f845f8416b0d8a76a316f9`.
- KiCad native DRC with schematic parity and zone refill: **0 violations, 0 parity errors, 3 unrelated opens**. The remaining nets are ADC_5V_HELD, EM_MASTER_GATE, and EM_BAT_FUSED, already assigned to the separate power repair. Neither repaired signal nor either locally reshaped net has an open.
- Locked Kelvin seed retained exactly; no extra conductor in its protected corridor. Both checks were repeated after native refill.
- Every footprint, pad, rule area, zone outline, board outline, and non-fill board setting remains unchanged. Existing routing remains unchanged except the four explicitly recorded removed tracks. Native zone fill polygons were recalculated for new via antipads.

## Repairs

| Net | Action | Copper length | New vias |
|---|---|---:|---:|
| AMP_OUT4 | Replace one B.Cu straight segment with a small dogleg to make an off-pad via pocket | 2.3331 mm replacement | 0 |
| ESP_MOSI | Replace three F.Cu segments around J3.15; horizontal moves to y88.31 with a corner at x55.9 | 8.9514 mm replacement | 0 |
| ADC_SCLK | Join J5.3 to existing via at (61.9125,89.2584), around the left end of the ESP header | 37.9950 mm added | 1 |
| AMP_OUT5 | Join U35.7 to existing via at (31.2263,29.2357) | 14.1548 mm added | 3 |

Clock width is 0.20 mm; both amplifier nets are 0.25 mm. All four added vias are through vias, 0.6 mm diameter / 0.3 mm drill, tented on both sides. The clock runs at y88.75 along the top of the antenna keepout, leaving its copper edge 0.15 mm outside the y89.0 boundary. The via at (55.8,91.25) is outside the keepout's x55.5 boundary. No signal uses inner-layer tracks.

The original 33-ohm R56 between U50.6 (ADC_SCLK_DRV) and ADC_SCLK remains unchanged. Repository firmware `Arduino/Eltec/Eltec.ino:269` selects SPI mode 1 at 1.5 MHz. Neither the trace geometry nor that clock frequency proves rise-time/overshoot behavior; the existing SPI waveform commissioning check remains applicable.

## Deterministic integration

`paths.json` and `signal_repairs.json` contain all replacement/addition geometry and all 37 new routing UUIDs. The report's `removed` map contains all four original UUIDs and their exact old net/layer/start/end/width/lock state in native nanometres. Validate those old states before removal. Retain removed SWIG track objects in a Python list while operating on the board. Then apply every entry of `report['paths']` through `tools/add_signal_repairs.py:add(board, proposal)`; UUID5 generation reproduces the reported UUIDs deterministically.

Merge the route-only delta into the independently verified power/bypass candidate, then refill zones and repeat native DRC, parity, connectivity, and protected-Kelvin verification. Do not copy this board over a newer integrated board, because this source intentionally predates the separate power/bypass fixes.

The generator's current hash matches the `tool_sha256` in the report. It defaults to the frozen proposal JSON files under reports; these files are inputs, while the immutable path copies in this selected folder remain the authoritative handoff.
