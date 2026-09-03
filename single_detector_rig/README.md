# Eltec Test Rig — unified sensor tester

**2026-09-02 — two buttons (Stop, Next) and a number only a PASS can spend.**
See [Test flow](#test-flow) below; the v2.0 skip queue it replaced is in
`CHANGELOG.md`. Every
older app (`405m22_esp32`, `v6_1_esp32`, `v6_esp32`, `v5_esp32`, the v1–v4
LabJack apps, `v6_1_failure_calibration`) and a frozen snapshot of this app
as it was before v2.0 (`eltec_rig_v1`) were removed from the working tree on
2026-08-28; they are preserved in full at git tag
`archive/pre-cleanup-2026-08-28`
(`git show archive/pre-cleanup-2026-08-28:tech_app/deprecated/<app>/<file>`).

One desktop application for every sensor model the ESP32/ADS1256 rig can
test. It opens **maximized** and shows a **sensor version dropdown**; picking a
version and pressing **Start tester** runs that model's qualified tester
application unchanged — same test flow, thresholds, per-batch filter-setup dropdowns, CSV
format, and results folders as before. The selection is remembered between
sessions.

```
single_detector_rig/
├── eltec_rig_tester.py          # the selector GUI (this app's entry point)
├── sensor_versions.py           # registry: one entry per testable model
├── attempt_history.py           # per-batch *_attempts.csv event log (shared by all models)
├── m405m22/                     # Model 405 M22 (1 Hz, TP412) tester + its 165-test suite
├── m406mca/                     # Model 406 MCA (10 Hz, v6.1 policy) tester + its 99-test suite
├── m449m18/                     # Model 449 M18 (5 Hz + 18 Hz, TP443 frequency tracking) tester + suite
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
| Model 449 M18 (5 Hz + 18 Hz, TP443) | `m449m18/` | DUT 5 Hz then 18 Hz, **20 % ON / 80 % OFF** (`PWM,DUTY`, firmware **v3.2**); reference phases 10 Hz / 50 % | firmware boot default (gain 1, buffer off) | `Documents/Eltec_449M18_Test_Results/449m18_esp32` |

The 449 M18 entry (added 2026-08-26) runs TP443 "449M18 Frequency Tracking":
sensitivity at 5 Hz, sensitivity at 18 Hz, and the 18/5 ratio, with the
spec-4 "measure the tray 100 %" flag. It is **calibration pending** — the
per-frequency fixture factors have not been derived yet, so it records raw
readings and the raw ratio and does not enforce the TP443 limits. See
[`m449m18/README.md`](m449m18/README.md).

Model-specific dropdowns (filter cap / filter setup, simulator cases, etc.)
live **inside** each model's app, exactly where they were before — choose the
sensor version first, then the batch's filter setup on the Batch information
screen.

## Firmware

The bench board runs the unified **`Arduino/Eltec/Eltec.ino` v3.2** baseline
(single-channel streaming, `PWM,FREQ`, `PWM,DUTY` (v3.2), runtime `FE,...`
front-end switch; the IR telescope's dual-channel code is *not* in it — that
lives in the separate `Eltec_IR_Telescope` workspace on firmware v2.2). The
405 M22 and 406 MCA modes also run on v2.1–v3.1; the 449 M18 mode refuses
anything older than v3.2 because it needs the duty-cycle command. Model
differences are selected at runtime over serial:

- **405 M22**: never sends `FE`; every port open resets the board to the
  boot-default gain-1 unbuffered front end, and the app programs `PWM,FREQ,1`
  for the DUT phases.
- **449 M18**: never sends `FE` either; programs `PWM,FREQ,5` + `PWM,DUTY,20`
  and then `PWM,FREQ,18` + `PWM,DUTY,20` for the two TP443 drives (the legacy
  fixture's 20/80 blade), `PWM,FREQ,10` + `PWM,DUTY,50` for the reference
  phases. A port open resets the board to 50 %, so the other models never
  see the changed duty.
- **406 MCA**: sends `FE,V19` right after the `IDN?` handshake on any
  firmware ≥ v2.1 and hard-verifies the `FE?` read-back — measuring a 406MCA
  on the wrong front end would invalidate every qualified threshold, so a
  mismatch refuses the connection. On a legacy standalone rig still flashed
  with v1.9, no `FE` command is sent (that firmware is natively the
  qualified front end).

Flash/verify - one command, no Arduino IDE needed (it finds the IDE's bundled
`arduino-cli`, auto-detects the board's port, compiles, uploads, then confirms
`IDN?`/`GATE?` over serial):

```bash
python3 Arduino/Eltec/flash_firmware.py
```

Windows: double-click `Arduino\Eltec\run_flash_firmware.cmd`. Xubuntu:
`Arduino/Eltec/run_flash_firmware.sh`. Useful flags: `--list` (show serial
ports), `--check` (report what the board runs, flash nothing), `--port COM7`,
`--sketch versions/Eltec_v2_2` (put the board on an archived build). The
equivalent by hand:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32doit-devkit-v1 Arduino/Eltec
arduino-cli upload -p COM3 --fqbn esp32:esp32:esp32doit-devkit-v1 Arduino/Eltec
# then confirm: IDN? -> ELTEC-ESP32-ADS1256,v3.1
```

## Fixture notes (2026-08-18)

- **Battery monitoring is disabled in both models** (`BATTERY_MONITORING_ENABLED
  = False` in each tester): since the 2026-08-12 rewiring the 6.5 V battery
  drives the emitters only, the 9 V battery drives the sensors, and neither is
  measurable on the legacy AIN7 divider. The header shows "Battery: not
  monitored". Re-enable per model once the sensor battery is measurable (the
  plan is AIN6 through a ≥4:1 divider).
- **Both models' reference gates are disabled** (`REFERENCE_GATE_ENABLED =
  False`; 405 M22 since 2026-08-17, 406 MCA since 2026-08-24) until the
  channel-isolated buffer board is installed.

## Running it

| | Xubuntu | Windows |
| --- | --- | --- |
| run | `./single_detector_rig/run_eltec_rig_tester.sh` | double-click `single_detector_rig\run_eltec_rig_tester.cmd` |
| desktop icon | `./single_detector_rig/install_xubuntu_launcher.sh` | `powershell -ExecutionPolicy Bypass -File single_detector_rig\install_windows_launcher.ps1` |
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

## Test flow

### Two screens and two buttons (2026-09-02)

There are two steps — **Batch information** and **Measure & result** — and
the action bar carries exactly two controls:

| Button | Colour | When shown | What it does |
| --- | --- | --- | --- |
| **Stop** / **Stop batch (Esc)** | **red** | every moment of a batch, never disabled | mid-capture: abandons the reading (nothing recorded, the number does not move). Idle: writes an unsaved verdict (no prompt since 2026-09-03), then ends the batch |
| **Start (Enter)** → **Next sensor (Enter)** | blue → **green** | setup → result | reads the sensor that is in the rig now; on a verdict it writes the row first |

The technician loads a part, presses Start (first) or Next (every one after)
and the read begins immediately — there is no "load the next sensor" screen
in between, because the part is loaded before the button is pressed. The old
load STEP's card (rig picture, and the 405's noise-soak toggle) moved onto
the setup screen; the 405 soak toggle also sits on the result card, where it
arms the read **Next** is about to take, and `run_measurement` clears it as
it starts so it never carries into a second part.

**Stop is the only control that is never disabled.** It works mid-capture
because it bumps `measure_token`, which the capture loops already poll
through their `cancelled=` callback: the loop raises at its next chunk
boundary, the worker's own `finally` blocks stop the stream and switch the
emitter off, and every callback that worker posts afterwards is ignored by
the token guard. A `stopped` row goes into the attempt log; no verdict row is
written.

**The bar always fits the window** (`_fit_footer`, all models). Two full-size
buttons fit any rig screen, but the blocked-measure labels ("Calibrate
reference unit to test") still need the ladder the six-button bar did: it
measures what the visible buttons need and takes the first
`FOOTER_VARIANTS` step that fits, preferring big buttons over spelled-out
labels:

1. full labels, one row;
2. drop the `(Enter)` / `(Esc)` hints (the shortcuts still work);
3. compact wording — *Next*, *Stop*, *Calibrate reference first*;
4. wrap the action buttons onto their own row, still full size;
5. only then step the buttons down a size.

It is bound to the footer's `<Configure>`, so it re-fits when the window is
maximized or resized — including growing back to full labels — and it works
the same on Windows and Xubuntu (the fit is measured from the actual font
metrics, not a hardcoded width). The "nothing was recorded" view keeps its
**Record as NOT MEASURED** option (writes the NOT MEASURED verdict row);
Next there simply reads the same sensor again.

There is no Re-measure button, which is a real trade-off: a verdict already
on screen cannot be discarded. It is saved as what it says — on a FAIL that
costs nothing, because the number stays open — or the reading is stopped
before it finishes so nothing is written at all.

### A sensor number is only spent by a PASS

`next_sensor_number_for_batch` returns one past the highest **PASSED** number
in the batch CSV. A FAIL, or a NOT MEASURED row, leaves the number open: the
next part loaded into the rig is tested as the same number, which is what the
bench already does physically — a bad part is set aside and another takes its
place. Whichever part finally passes is the one that ships under that number.

Consequences:

- A batch CSV is **one row per test**, not one row per number. `500-7` can
  appear several times, the last of them the PASS. Yield counts rows, so the
  failures are still all in it.
- **`number_attempt`** (1-based) says which part under that number a row is;
  **`measure_attempts`** counts the reads of one part and starts over for
  each replacement. Both are the last two CSV columns; `number_attempt`
  replaced `skip_count` in the same position on 2026-09-02.
- Deriving from the highest PASSED number (not from a count of passes) leaves
  batches written before the rule intact: their numbers were handed out per
  row, so a file whose last pass is `500-9` continues at `500-10`.

### Attempt history (what happened to this part?)

Each batch has a sibling **`<lot>_attempts.csv`** next to its results CSV
with one row per event: `measured` (every finished measurement with its
verdict, offset, sensitivity, polarity, noise worst pk-pk, fail reasons),
`measure_error` (nothing recorded + the rig error), `stopped` (the
technician pressed Stop during a capture), `saved`. Older batch CSVs keep
their header (rows stay aligned, newer columns are simply absent). Autosave
payloads carry `measure_attempts` and `number_attempt`.

### Shorted / dead sensor vs. empty slot (both models)

A shorted or dead part floats AIN0 exactly like an empty slot. Instead of a
hard "No sensor detected" wiring error, the app now asks **"Is a sensor
loaded?"** — *Yes* records the part as a FAIL with no offset (failure mode
preset to **SB - Sensor bad**, reason "No offset: AIN0 reads x V with a
sensor loaded"), ready to save; *No* keeps the old behaviour (nothing
recorded, seat the sensor and measure again).

### 406 MCA reference gate disabled (2026-08-24)

`REFERENCE_GATE_ENABLED = False` in `m406mca/eltec_406mca_esp32_tester.py`,
exactly like the 405 M22 build: the shared dual op-amp buffer has no channel
isolation, so the sensor under test couples into AIN1 and the reference could
not be calibrated. No calibration is required to test; the load-step card
says "Reference gate disabled (op-amp crosstalk)"; `reference_*` CSV columns
stay blank. All gate code is intact and unit-tested with the flag forced on —
set it back to `True` and run a fresh "Calibrate reference unit" once the
channel-isolated op-amp board is installed.

### Selector opens full screen (2026-08-25)

`eltec_rig_tester.py` starts maximized like each model's tester (Windows
`state("zoomed")`, X11 `-zoomed` applied once mapped, screen-sized geometry
as a last resort), and re-applies it when a tester closes and the selector
un-minimizes. The window is resizable now (a fixed-size window cannot be
maximized) with a 640×520 floor, and its one content block is centered by
weighted spacer rows/columns so a wide monitor grows the margins, not the
card. Type is a step larger to stay readable full screen.

### Tests

`tests/test_attempt_history.py` (23 tests: the attempt-log module, and the
earned-number rule plus the Stop / Next flow driven through ALL THREE
models' real `EmitterTesterApp` methods, plus the footer palette). Each
model suite carries the same numbering / Stop / Next checks against its own
copy of the app. Existing verdict logic, thresholds and calibrations are
untouched.

## Tests

```bash
python3 -m unittest discover -s single_detector_rig/tests            # selector glue
python3 -m unittest discover -s single_detector_rig/m405m22/tests    # run from the repo root
python3 -m unittest discover -s single_detector_rig/m406mca/tests    # run from the repo root
python3 -m unittest discover -s single_detector_rig/m449m18/tests    # run from the repo root
```

On Windows the m406mca suite reports one long-standing environment-only
error (the POSIX bash-installer test); it passes on Xubuntu.

## Adding the next sensor version

See the docstring at the top of `sensor_versions.py`. Short version: add an
app directory (copy the closest model), append one `SensorVersion` entry, and
extend the firmware with runtime commands only if the model needs behavior
the unified v3.x firmware cannot already select over serial. Also register the
suite in `run_all_tests.py` and add the model to `docs/CALIBRATION_RECORD.md`
and `docs/DATA_MAP.md`.

## History

Created 2026-08-18 by unifying `tech_app/405m22_esp32` and
`tech_app/v6_1_esp32` behind one selector. Those original directories were
kept untouched as the qualified standalone builds (moved to
`tech_app/deprecated/` with v2.0 on 2026-08-24) until 2026-08-28, when the
whole deprecated tree was removed from the working tree — it is preserved at
git tag `archive/pre-cleanup-2026-08-28`. New work on every model happens
**here**. The changes made to the bundled copies relative to the originals:

- `m406mca/esp32_backend.py`: selects the qualified front end (`FE,V19` +
  `FE?` verification) on firmware ≥ v2.1.
- `m406mca/eltec_406mca_esp32_tester.py` and `m406mca/stability_calibration.py`:
  battery gate behind `BATTERY_MONITORING_ENABLED = False` (mirroring the
  405 M22 build), because the unified fixture has no battery on AIN7.
- `m406mca` tests updated for both changes (99 tests); `m405m22` is
  byte-identical to `405m22_esp32` apart from the removed `prompt.txt`.
- `v1_single_sensor/eltec_406mca_tester.py` vendored so the package is
  self-contained.
