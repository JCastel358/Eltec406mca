# Routed reference-plane precheck

Plan only, using `reports/routing/trial01/position_restore_after_agent_COPY.kicad_pcb`, SHA-256 `8b520389e2ef9eb5893cb059224ffa1bd636c99375ede9544eca515e2828c495`. No board was modified, filled or DRC-tested by this precheck. This copy does not have the adjacent project files needed for an authoritative native DRC/parity run. Rerun `add_reference_planes.py --build-copy` against the imported, width-corrected main project before integrating copper.

No required U10/C11/SET or local emitter stitch is blocked. Nine U100 thermal-via sites remain from the original 18-site search. No signal route needs to be changed for this geometric proposal; filled-plane connectivity and DRC may still reveal issues.

| Connection | Routed proposal |
| --- | --- |
| U10 EP15 | Keep vias [34,−9.45] and [34,−4.55], with 1.0 mm front GND stubs. |
| C11 ground | Keep via [38.5,−11.675], with 0.6 mm front GND stub. |
| C15 ground | Move via to [34,2.325], with a 2.1 mm long, 0.3 mm wide front GND stub. |
| C37 ground | Move via to [37.3,2.325], same length/width as C15. |
| C38 ground | Move via to [40.6,2.325], same length/width as C15. |
| U16.3 emitter ground | Keep via [82.81,7.77] and 1.0 mm front EM_GND stub. |
| PWM emitter plane | No new Q101 stitch: routed EM_GND through vias at [24.8184,113.1054] and [35.256,101.7031] are already inside the region. |

The prior SET-via positions at y−0.975 and closer alternative sites conflict with routed foreign copper. Their replacements preserve separate local plane access and do not put a hole into a capacitor pad. The original 1 mm domain moats, 2 mm U101 optical-boundary gap, central ADC/SPI GND corridor, and existing all-layer antenna keepout remain in the proposal. The additional ground connections lie clear of the protected U10.12/C11.1 Kelvin rectangle; the independent native Kelvin validator still needs to run after integration.

Accepted U100 off-pad thermal-via centres (mm):

- [88.750,38.000], [91.018,38.000], [93.286,38.000]
- [93.286,49.000]
- [82.550,44.628]
- [94.950,40.210], [94.950,42.372], [94.950,44.628], [94.950,46.790]

Rejected U100 sites:

- EM_BAT_SW routes block [84.214,38], [86.482,38], [82.55,40.210], and [82.55,42.372].
- EM_ADJ routes block [84.214,49], [86.482,49], [88.750,49], and [91.018,49].
- R100.2 pad clearance blocks [82.55,46.790].

The resulting proposal adds 10 zones, 16 through vias (9 U100 thermal plus 7 local ground connections), and 7 front-layer ground stubs. It adds no internal signal track or blind/buried via. Nine thermal vias passing geometry checks is not a thermal-performance qualification.

The helper now calls the KiCad10 existing-via width API with an explicit copper layer, avoiding a native assertion when routed vias are present. Its collision search also uses conservative bounding boxes before native polygon checks; the final clearance decision still uses the complete relevant copper shapes.
