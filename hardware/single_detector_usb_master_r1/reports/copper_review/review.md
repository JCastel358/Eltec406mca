# Routed signal/reference and bypass review

Review source: main board SHA-256 `b33a97f84bf1f496620f39c85c54ef9b8a860b96ba0045ccac9a89994dcc416f`, preserved as `source_snapshot.kicad_pcb`. The review did not change the main PCB. This version still has the five non-ground opens being repaired separately; their final added routes need the same review.

## Signal routing relative to the filled reference copper

The read-only audit examined 533 existing straight SPI/control and analog track segments across 58 nets. It sampled their centre lines at intervals no greater than 0.1 mm against the **actual filled** adjacent inner-layer copper: In1.Cu for front signals and In2.Cu for back signals.

- No sample lay over an EM_GND region, and no selected signal entered the antenna track keepout.
- No individual unreferenced run exceeded 1.50 mm. The longest runs occur at detector output wire-land and module-header pad exits: for example SENSOR3 from [16.0,39.1775] toward [17.0581,40.2356] on B.Cu, SENSOR2 at [16.0,51.5975] on B.Cu, and ADC_AIN4 ending at [63.37,23.825] on B.Cu.
- All 168 reported void-run midpoints match the actual copper-clearance opening around a non-GND through pad or via. This is consistent with local antipads at connection/layer-transition points, rather than a long route crossing a split plane. The classification checks midpoints; it does not claim exact polygon-union coverage of every interval.
- No individual selected track ran for more than 5 mm within 1 mm of opposite-layer emitter power copper under the implemented proximity check.

The following opposite-layer power/signal projection crossings do exist. Their sampled crossing points retain adjacent internal GND copper:

| Signal | Emitter power net | Approximate crossing location, mm |
| --- | --- | --- |
| CLAMP_GND1 | EM_REG | x43.99…44.95, y77.412 |
| AMP_SERIES1 | EM_REG | x42.81…42.88, y78.63…78.70 |
| ADC_PDWN_N | EM_REG | x51.773, y69.61…71.30 |
| ADC_PDWN_N | EM_BAT_SW | x52.66…53.43, y73.63…74.41 |

These are geometrical projection crossings on opposite outer layers with intervening reference copper. They are not a DRC short or evidence of an exposed signal crossing the GND/EM_GND split. No broad rerouting is indicated by this check alone. Preserve the reference copper if these routes are later moved.

## U10 local bypass ground access

C13/C39 and input bypass C36 are connected to GND, but their original local ground access uses longer surface paths than necessary. Native connected-track traversal deliberately excludes zone traversal, so a remote via belonging to the same plane cannot be mistaken for a nearby surface connection. The listed lengths sum the touched track segments, including portions inside pad copper, and are conservative path estimates.

| Capacitor ground | Existing nearest routed plane access | Surface path estimate | Existing trace widths |
| --- | --- | --- | --- |
| C11.2, [38.5,−10.475] | Via [38.5,−11.675] | 1.20 mm | 0.60 mm |
| C13.2, [39.975,−13.5] | C11's via [38.5,−11.675] | 4.84 mm | 0.30 mm, then C11's 0.60 mm stub |
| C39.2, [44.975,−9] | Via [47.2076,−6.7675] | 3.16 mm | 0.30 mm |
| C36.2, [29.5,−9.475] | U10 EP top via [34,−9.45] | 4.70 mm | 0.30 mm |

The bounded improvement is to give C13, C39 and C36 their own off-pad through vias and 1.2 mm-long, 0.6 mm-wide front GND stubs. This reduces their shared surface-return path and loop extent while retaining every original route and pad:

| Anchor | Additional GND via centre, mm |
| --- | --- |
| C13.2 | [38.775,−13.5] |
| C39.2 | [43.775,−9.0] |
| C36.2 | [29.5,−10.675] |

These additions are implemented in `tools/add_bypass_stitches.py` with callable `add_stitches(board)`. The review copy passes native zone refill, **0 DRC violations**, **0 schematic parity issues**, and unchanged **5 non-ground opens**. The independent protected-Kelvin check passes. Existing geometry is retained and the three vias are 0.6 mm diameter / 0.3 mm drill, tented, with no via-in-pad. Details and six fixed UUIDs are in `reports/bypass_stitches/validation.json`. This validated copy has SHA-256 `c3b187f29d3a4d4d83b87f2b8779bdfd03c98771166516fd4cec3f67be56e0a9`.

## Limits and final integration

This is a native-copper geometry/connectivity review, not an impedance model, electromagnetic simulation, regulator stability proof or measured noise qualification. It cannot establish mounted ESL, transient amplitude, signal edge quality or detector sensitivity. The sampling check should be rerun after the remaining five non-ground repairs, and the final combined board must be refilled and pass native DRC/parity and Kelvin validation.

A separate read-only check near the remaining U35.7/AMP_OUT5 escape found no GND surface trace in the proposed east/northeast via pocket around [31.4,20.15]. Nearby GND routes are west at x27.1915 and north near y18.184; removing them would not clear the identified B.Cu AMP_OUT4 obstruction. That local signal adjustment remains with the routing agent.

Evidence: `copper_review.json`, `reference_void_classification.json`, `bypass_improvement_proposals.json`, and the native source snapshot. `tools/review_copper.py` reproduces the signal/reference and local connected-path audit for the current main board without changing it.
