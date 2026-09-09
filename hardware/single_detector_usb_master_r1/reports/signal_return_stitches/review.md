# Signal reference-plane stitch handoff

Callable helper: `tools/add_signal_return_stitches.py` → `add_return_stitches(board)`. It adds exactly three tented GND through vias, each 0.6 mm diameter / 0.3 mm drill. It does not alter existing tracks, pads, footprints, plane outlines, antenna keepouts or schematic data. No surface track is added. The caller fills and saves after combining authorized repairs.

| Via centre, mm | Purpose | Fixed native UUID |
| --- | --- | --- |
| [55.9,92.25] | ADC_SCLK layer-transition return | `59e6a54c-ef23-580a-8602-b5435bda8d4c` |
| [28.65,19.55] | AMP_OUT5 first layer-transition return | `7380bdc3-ebaa-5355-8e4a-34f59b74f065` |
| [32.6,24.5] | AMP_OUT5 remaining two transitions | `44ed5f42-fa44-5031-a26e-9767d610312f` |

Source main SHA-256: `690d80156d71a897e6ca834e49c7fc9674a03378a2d5fc5f3264eae576b5b15f`.

Validated copy: `candidate/single_detector_usb_master.kicad_pcb`, SHA-256 `0cc6fffd001c48b4229ba58f00e337093ceeb621585f29773e6a010e9d88d40e`.

Before/after native DRC both report **0 violations, 0 opens and 0 schematic parity issues**. Native zones were refilled, and all prior geometry was compared before/after and after reload while allowing only derived zone-fill changes. Protected-Kelvin validation passes before save and after reload. Main source hash remains unchanged. Complete commands, clearance checks, distances and identities are in `validation.json`.

Root and the power agent confirmed these locations are independent of their C12 via [26.475,−20.05] and C16 via [64.5,38.5]. Integrate the additive helpers, refill and validate the combined main PCB. Retain the actual project's tables/settings; copied review library tables are not deployment files. The purpose is shorter geometrical inter-plane return access, not a measured signal/noise qualification.
