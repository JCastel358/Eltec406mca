# Routed reference-plane and ground handoff

**Candidate ready for integration; five non-ground connections remain.** Source main PCB SHA-256 `fad6bd088b9a705895c90298aca01d0d376371b9805369eec672771b68cf23af` remains unchanged. The copy at `candidate/single_detector_usb_master.kicad_pcb` has SHA-256 `b33a97f84bf1f496620f39c85c54ef9b8a860b96ba0045ccac9a89994dcc416f`.

Native KiCad 10.0.6 DRC with all severities and schematic parity reports:

| Check | Before planes/ground links | Final copy |
| --- | --- | --- |
| DRC violations | 0 | 0 |
| Schematic parity issues | 0 | 0 |
| Unconnected items | 12 | 5 |
| Remaining GND/EM_GND opens | 7 | 0 |

All prior footprints, native UUIDs/paths, pads, tracks, outline, antenna rule area, and other existing geometry are retained. Serialization was compared before/after and after save/reload. The additions are 10 filled zones, 20 tented 0.6/0.3 mm through vias, and 13 front-layer ground connections. There are no internal signal tracks or blind/buried vias. `protected_kelvin.verify` independently passes against the exact trial01 locked-seed manifest; see `kelvin_validation.json`.

## Ground completion

The two U10 EP15 thermal vias and the separate C11/C15/C37/C38 return vias remain as recorded in `plane_plan.json`. U10 GND pins 10 and 11 also connect left to the exposed pad via short 0.25 mm front-layer GND links:

- Pin 10: [35.45,−6.50] to [34.00,−6.50].
- Pin 11: [35.45,−7.00] to [34.00,−7.00].

These 0.5 mm pitch lead escapes use the project's 0.20 mm track-clearance rule. The plane/via proposals elsewhere retain a 0.25 mm clearance margin. Their native DRC passes and they do not enter the protected U10.12-to-C11.1 Kelvin rectangle.

After the first routed fill, four surface ground groups still lacked access to the correct inner plane. Native connectivity identified the GND group as R111/R116/R121 and the three emitter groups at C101, R29 and C100. The following off-pad stitches complete them:

| Anchor | Via centre, mm | Net / front-ground width |
| --- | --- | --- |
| R121.2 | [41.35,103.0875] | GND, 0.30 mm |
| C101.2 | [81.5,33.85] | EM_GND, 1.00 mm |
| R29.2 | [83.825,−25.2] | EM_GND, 1.00 mm |
| C100.2 | [90.07,35.5] | EM_GND, 1.00 mm |

The R121 link serves the existing shared R111/R116/R121 surface group. Its 1.35 mm horizontal path is clear of the antenna rule area; coarser candidate positions near R111/R116 were blocked by pads, existing routes, or the antenna boundary. The antenna keepout was preserved, not reduced. The local U16 emitter pocket also receives its previously planned via at [82.81,7.77]. Existing PWM EM_GND vias provide that plane's access.

Nine of the original 18 U100 thermal-via candidate sites survive the routed clearance checks. The front and back EM_GND thermal copper fills to 157.391 mm² and 140.030 mm² respectively. Native DRC has no isolated-copper violation. This does not establish a regulator temperature or noise specification; final assembly and worst-case dissipation remain bench qualifications.

## Remaining work and integration

The five remaining opens are ADC_5V_HELD at U10.13, EM_MASTER_GATE at Q20.1, EM_BAT_FUSED between Q19/F2, ADC_SCLK at J5.3, and AMP_OUT5 at U35.7. They are explicitly left for the parallel power/signal routing work.

Before integrating, confirm the main source still matches the source hash and back it up. Copy only this native PCB into the real project. Retain the real project's existing schematic, settings, library tables and libraries. The candidate's review-only library tables point back to the source libraries and should not replace the project tables. After merging the remaining route additions, refill and rerun native DRC/parity, protected Kelvin validation, and visual review of the actual final board.

Evidence: `plane_plan.json`, `copy_validation.json`, `before_drc.json`, `after_drc.json`, and `kelvin_validation.json`. The copy was produced by `tools/add_reference_planes.py --build-copy --output-dir reports/reference_planes_routed`.
