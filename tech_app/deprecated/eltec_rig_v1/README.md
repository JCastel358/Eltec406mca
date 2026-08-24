# Eltec Test Rig — unified sensor tester

One desktop application for every sensor model the ESP32/ADS1256 rig can
test. On start it shows a **sensor version dropdown**; picking a version and
pressing **Start tester** runs that model's qualified tester application
unchanged — same test flow, thresholds, per-batch filter-setup dropdowns, CSV
format, and results folders as before. The selection is remembered between
sessions.

```
tech_app/eltec_rig/
├── eltec_rig_tester.py          # the selector GUI (this app's entry point)
├── sensor_versions.py           # registry: one entry per testable model
├── m405m22/                     # Model 405 M22 (1 Hz, TP412) tester + its 165-test suite
├── m406mca/                     # Model 406 MCA (10 Hz, v6.1 policy) tester + its 99-test suite
├── v1_single_sensor/            # shared 406MCA analysis/pass-fail engine (vendored)
├── assets/                      # desktop icon (.png for Linux, .ico for Windows)
├── run_eltec_rig_tester.sh      # Xubuntu launcher        ┐ double-click either
├── run_eltec_rig_tester.cmd     # Windows launcher        ┘ on its platform
├── install_xubuntu_launcher.sh  # opt-in Desktop + menu entry (`--uninstall` removes)
├── install_windows_launcher.ps1 # opt-in Desktop + Start Menu (`-Uninstall` removes)
└── tests/                       # selector/registry/launcher glue tests
```

## Sensor versions

| Dropdown entry | App | Emitter drive | ADS1256 front end | Results |
| --- | --- | --- | --- | --- |
| Model 405 M22 (1 Hz, TP412) | `m405m22/` | DUT 1 Hz, reference phases 10 Hz | firmware boot default (gain 1, buffer off) | `Documents/Eltec_405M22_Test_Results/405m22_esp32` |
| Model 406 MCA (10 Hz) | `m406mca/` | 10 Hz (boot default) | app sends `FE,V19` after connect → gain 2, buffer on (the qualified v1.9 front end) | `Documents/Eltec_406MCA_Test_Results/v6_1_esp32` |

Model-specific dropdowns (filter cap / filter setup, simulator cases, etc.)
live **inside** each model's app, exactly where they were before — choose the
sensor version first, then the batch's filter setup on the Batch information
screen.

## Firmware

The bench board runs the unified **`Arduino/Eltec/Eltec.ino` v3.0** baseline
(single-channel streaming, `PWM,FREQ`, runtime `FE,...` front-end switch; the
IR telescope's dual-channel code is *not* in it — that lives in the separate
`Eltec_IR_Telescope` workspace on firmware v2.2, which is also a drop-in for
this app). Model differences are selected at runtime over serial:

- **405 M22**: never sends `FE`; every port open resets the board to the
  boot-default gain-1 unbuffered front end, and the app programs `PWM,FREQ,1`
  for the DUT phases.
- **406 MCA**: sends `FE,V19` right after the `IDN?` handshake on any
  firmware ≥ v2.1 and hard-verifies the `FE?` read-back — measuring a 406MCA
  on the wrong front end would invalidate every qualified threshold, so a
  mismatch refuses the connection. On a legacy standalone rig still flashed
  with v1.9, no `FE` command is sent (that firmware is natively the
  qualified front end).

Flash/verify:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32doit-devkit-v1 Arduino/Eltec
arduino-cli upload -p COM3 --fqbn esp32:esp32:esp32doit-devkit-v1 Arduino/Eltec
# then confirm: IDN? -> ELTEC-ESP32-ADS1256,v3.0
```

## Fixture notes (2026-08-18)

- **Battery monitoring is disabled in both models** (`BATTERY_MONITORING_ENABLED
  = False` in each tester): since the 2026-08-12 rewiring the 6.5 V battery
  drives the emitters only, the 9 V battery drives the sensors, and neither is
  measurable on the legacy AIN7 divider. The header shows "Battery: not
  monitored". Re-enable per model once the sensor battery is measurable (the
  plan is AIN6 through a ≥4:1 divider).
- The 405 M22 model's reference gate remains disabled
  (`REFERENCE_GATE_ENABLED = False`) until the channel-isolated buffer board
  is installed; the 406 MCA flow still uses its reference gate.

## Running it

| | Xubuntu | Windows |
| --- | --- | --- |
| run | `./tech_app/eltec_rig/run_eltec_rig_tester.sh` | double-click `tech_app\eltec_rig\run_eltec_rig_tester.cmd` |
| desktop icon | `./tech_app/eltec_rig/install_xubuntu_launcher.sh` | `powershell -ExecutionPolicy Bypass -File tech_app\eltec_rig\install_windows_launcher.ps1` |
| log | `~/.local/state/eltec-rig/launcher.log` | `%LOCALAPPDATA%\eltec-rig\launcher.log` |

Both installers are opt-in and per-user (`--uninstall` / `-Uninstall` to
remove). `ELTEC_PYTHON` overrides the interpreter on both platforms;
`ELTEC_LAUNCHER_NO_DIALOG=1` suppresses the Windows error popup for headless
runs. Dependencies are the same as the individual apps: Python 3 with
`tkinter`, `numpy`, `pyserial`, and `matplotlib` (see each model's README).

The selector remembers the last-used sensor version in
`~/.local/state/eltec-rig/state.json` (Xubuntu) /
`%LOCALAPPDATA%\eltec-rig\state.json` (Windows). Only one tester can run at a
time — both models share the same board and serial port.

## Tests

```bash
python3 -m unittest discover -s tech_app/eltec_rig/tests            # selector glue
python3 -m unittest discover -s tech_app/eltec_rig/m405m22/tests    # run from the repo root
python3 -m unittest discover -s tech_app/eltec_rig/m406mca/tests    # run from the repo root
```

On Windows the m406mca suite reports the two long-standing environment-only
failures (POSIX bash-installer test and the POSIX-exclusive tty flag
assertion); both pass on Xubuntu.

## Adding the next sensor version

See the docstring at the top of `sensor_versions.py`. Short version: add an
app directory (copy the closest model), append one `SensorVersion` entry, and
extend the firmware with runtime commands only if the model needs behavior
the v3.0 baseline cannot already select over serial.

## History

Created 2026-08-18 by unifying `tech_app/405m22_esp32` and
`tech_app/v6_1_esp32` behind one selector. Those original directories remain
in the repository untouched as the qualified standalone builds; new work on
either model happens **here**. The changes made to the bundled copies relative
to the originals:

- `m406mca/esp32_backend.py`: selects the qualified front end (`FE,V19` +
  `FE?` verification) on firmware ≥ v2.1.
- `m406mca/eltec_406mca_esp32_tester.py` and `m406mca/stability_calibration.py`:
  battery gate behind `BATTERY_MONITORING_ENABLED = False` (mirroring the
  405 M22 build), because the unified fixture has no battery on AIN7.
- `m406mca` tests updated for both changes (99 tests); `m405m22` is
  byte-identical to `405m22_esp32` apart from the removed `prompt.txt`.
- `v1_single_sensor/eltec_406mca_tester.py` vendored so the package is
  self-contained.
