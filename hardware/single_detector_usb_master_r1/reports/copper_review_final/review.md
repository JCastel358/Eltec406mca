# Final routed copper and return-path review

Audited board SHA-256: `690d80156d71a897e6ca834e49c7fc9674a03378a2d5fc5f3264eae576b5b15f`. This is the integrated board recorded by `reports/final_route_integration/integration.json`, with native refill DRC reporting 0 violations, 0 opens and 0 schematic parity issues. The read-only audit preserved it in `source_snapshot.kicad_pcb` and did not change the main CAD. Historical results under `reports/copper_review` are unchanged.

**Finding:** no new signal route crosses the emitter reference-plane split or enters the antenna keepout. The specific improvement identified is local GND stitching between the two reference planes at the new clock/analog layer transitions. A bounded three-via candidate now passes native validation; no signal reroute is required by this review.

## Actual filled reference copper

The audit checked 562 SPI/control and analog straight-track segments across 58 nets, sampling centre lines against actual filled In1.Cu for front signals and In2.Cu for back signals at intervals of at most 0.1 mm.

- No selected signal sample lay over EM_GND or inside the antenna track keepout.
- The longest individual centre-line reference void is approximately 1.50 mm. All 179 reported void-run midpoints match the clearance opening around an actual non-GND through pad or via.
- Opposite-outer-layer emitter-power projection crossings remain the same four groups identified in the earlier review: CLAMP_GND1/EM_REG near [44.5,77.412], AMP_SERIES1/EM_REG near [42.85,78.66], ADC_PDWN_N/EM_REG near x51.773,y69.6…71.3, and ADC_PDWN_N/EM_BAT_SW near [53,74]. The sampled crossings retain adjacent internal GND copper.
- There is no individual selected track running for more than 5 mm within 1 mm of opposite-layer emitter power copper under this proximity check.

The newly added or replaced signal tracks were also sampled at 0.025 mm maximum intervals at their centre and both copper edges. This stricter check exposes normal local edge overlap of header/via antipads that a centre-line check does not describe. It is not an impedance calculation.

## Clock, MOSI and antenna boundary

The ADC_SCLK repair adds 37.995 mm of track and one through via; the three replaced ESP_MOSI tracks total 8.951 mm.

| Region | Actual reference result |
| --- | --- |
| ADC_SCLK, [50.4,88.75] to [55.55,88.75] | All 5.15 mm has GND at the centre and both track edges. |
| ADC_SCLK, x55.8,y89.2…91.25 | GND for the first 1.50 mm; the remaining 0.55 mm enters its own signal-via antipad. |
| ESP_MOSI, y88.31,x51.83…55.9 | Centre-line GND throughout 4.07 mm; aggregate 1.273 mm has one track edge partly over nearby header antipads. |

The clock's horizontal trace has 0.15 mm copper-edge clearance to the conservative y89 antenna boundary; its vertical trace has 0.20 mm to the x55.5 boundary. Its existing via annulus at [55.8,91.25] touches that conservative boundary without entering it. The native keepout DRC passes. MOSI's horizontal copper-edge margin to y89 is 0.59 mm. These are geometry margins to the chosen keepout, not RF-performance margins or antenna qualification.

No long centre-line gap across the antenna void was found. The local partial edge coverage by connector antipads should not be represented as a controlled-impedance result. The physical module/antenna and real signal edges still need assembly qualification.

## Analog repair and local bypass access

The AMP_OUT5 repair adds 14.155 mm of track and three signal through vias. The AMP_OUT4 local replacement totals 2.333 mm, approximately 0.279 mm longer than the replaced straight segment. Their sampled centre-line voids are local through-feature antipads; neither route crosses an EM_GND region. No additional broad analog rerouting is indicated by this geometry check.

The integrated bypass changes are present. C11, C13, C39 and C36 each have their nearest local ground-plane access through a dedicated 1.2 mm surface GND stub. Both inner-layer filled regions at those via sites are GND. This resolves the earlier longer shared surface-access paths without claiming a measured mounted inductance or regulator-noise result.

## Reference-plane stitching improvement

Before the final return stitches, the nearest GND through connection to the new ADC_SCLK via [55.8,91.25] was 7.994 mm away. The three AMP_OUT5 transitions were approximately 18.12, 18.86 and 18.07 mm from one. Although both adjacent reference planes belong to GND, nearby through connections provide a shorter inter-plane return path at those layer changes.

The bounded addition is only three tented GND through vias, 0.6 mm diameter and 0.3 mm drill, with no new surface tracks:

| GND stitch | Signal-transition distance after addition |
| --- | --- |
| [55.9,92.25] | 1.005 mm from the ADC_SCLK via |
| [28.65,19.55] | 2.789 mm from the first AMP_OUT5 via |
| [32.6,24.5] | 2.003 and 0.906 mm from the other two AMP_OUT5 vias |

The helper verifies pad-to-annulus clearance, both outer-layer routes, via drill spacing, antenna keepouts, and the actual GND fills on both inner layers. A complete review copy passes **0 native DRC violations, 0 opens, 0 parity issues**, and the protected-Kelvin check. All pre-existing native geometry is preserved. The candidate SHA-256 is `0cc6fffd001c48b4229ba58f00e337093ceeb621585f29773e6a010e9d88d40e`.

Use `tools/add_signal_return_stitches.py` / `add_return_stitches(board)` for integration. The three sites are independent of the separate C12/C16 power-ground additions. Root must refill and validate the combined board once; this report does not substitute for that combined check.

## Limits and evidence

This review establishes sampled copper coverage, geometry, native connectivity and clearances. It does not establish impedance, propagation delay, crosstalk, return impedance, analog noise, mounted ESL, RF performance, regulator stability or detector performance. Midpoint antipad matching is not exact union coverage of each complete interval. Full-width sampling excludes rounded endcaps; native DRC remains the clearance authority.

Evidence: `copper_review.json`, `focused_route_checks.json`, `reference_void_classification.json`, `return_stitch_proposals.json`, and `reports/signal_return_stitches/validation.json`. The general audit supports `--source` and `--output-dir`; this run used `--output-dir reports/copper_review_final`.
