# Three local bypass-ground stitches

`tools/add_bypass_stitches.py` exports callable `add_stitches(board)` for root integration. It adds only the following fixed-UUID objects and does not save or refill the caller's board. The standalone command operates on a complete review copy, refills its zones and runs native DRC/parity and protected-Kelvin validation.

| Anchor | Via centre, mm | Via UUID | Front-stub UUID |
| --- | --- | --- | --- |
| C13.2 | [38.775,−13.5] | `75e61fa8-d666-5e16-ab51-12eaddc773ba` | `3f0ae44b-40d8-5a51-8338-863e88055942` |
| C39.2 | [43.775,−9] | `6c94babd-6a85-5087-9c32-9f6cf7c21da6` | `577edaa8-d647-53e2-a919-009a1e56492d` |
| C36.2 | [29.5,−10.675] | `41c3a708-d17c-510e-b603-885a18919a03` | `403a6719-1c77-57ee-be22-3086192ea737` |

All vias are tented GND through vias, 0.6 mm diameter and 0.3 mm drill. Each connects to its existing capacitor pad 2 with a 1.2 mm-long, 0.6 mm-wide F.Cu GND stub. Native geometry checks confirm at least 0.3 mm pad-to-annulus clearance, clearance from routed foreign copper, and intact GND at each site on both inner layers.

Input main SHA-256: `b33a97f84bf1f496620f39c85c54ef9b8a860b96ba0045ccac9a89994dcc416f`.

Validated copy: `candidate/single_detector_usb_master.kicad_pcb`, SHA-256 `c3b187f29d3a4d4d83b87f2b8779bdfd03c98771166516fd4cec3f67be56e0a9`.

Before and after native DRC both report **0 violations, 0 parity issues and 5 non-ground opens**. Protected-Kelvin validation passes before save and after reload. Every existing native object is preserved; zone outlines/settings are preserved while derived filled polygons are allowed to regenerate. Repeating `add_stitches` against the completed copy validates the exact six UUIDs, returns `EXACT_STITCH_SET_ALREADY_PRESENT`, and leaves the track count unchanged. A partial set or altered geometry is refused.

Root should call this helper while merging the remaining signal/power repair additions, then refill and validate the combined board. The helper changes no schematic, project setting, library or footprint. Retain the actual project's library tables; the copied review tables are not deployment files. Shorter paths improve the geometric bypass loop but are not a claim of measured ESL or regulator noise performance.
