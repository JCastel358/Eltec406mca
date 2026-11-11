# Eltec sensor test rigs

Bench rigs and software that qualify Eltec pyroelectric IR sensors. Two rigs
live in this repository, one top-level folder each:

- **`single_detector_rig/`** — an ESP32 chops an IR emitter and streams a
  24-bit ADS1256 ADC to a laptop; one Python desktop application runs the
  test procedure for the sensor model chosen in a dropdown, judges each part,
  and writes a CSV row per sensor.
- **`array_rig/`** — fifty detectors at once: a 50-position buffer PCB read
  by an ACCES USB-AIO16-64MA DAQ; the same selector-plus-model-directory
  structure, testing offset and noise per tray (sensitivity waits for the
  emitter board).

Windows 11 and Xubuntu are both supported.

The current **M25/HUB array** workflow is available from the normal Array Rig
launcher: automatic DC/noise switching, two 90-second settling periods and a
saved 50-position report. See [the current array instructions](array_rig/README.md).
Real M25/HUB acquisition uses Windows and the ACCES driver; simulation also
runs without hardware. No Arduino/ESP32 flash is needed for this offset/noise
check. Earlier contiguous-channel instructions are the explicit legacy profile.

| Sensor model | Procedure | Status (2026-09-02) | Results folder (`Documents\…`) |
| --- | --- | --- | --- |
| **405 M22** (1 Hz) | TP412 — offset, emitter-off noise, sensitivity/polarity | production; sensitivity factor 4.30 from lot 500 | `Eltec_405M22_Test_Results\405m22_esp32` |
| **406 MCA** (10 Hz) | offset, sensitivity/polarity, v6.1 stability policy | production; factor 1.582 from lot 520 | `Eltec_406MCA_Test_Results\v6_1_esp32` |
| **449 M18** (5 + 18 Hz) | TP443 frequency tracking | **calibration pending** (K_5 / K_18 not derived; needs firmware v3.2, not yet flashed) | `Eltec_449M18_Test_Results\449m18_esp32` |
| **40623 array** (50 positions, DAQ) | TP120 offset check + noise, fifty at a time | **calibration pending** (noise limits not derived, offset limits provisional; raw captures saved for the paired lot) | `Eltec_40623_Test_Results\40623_array_daq` |

---

## Set up another Windows computer

1. Install **64-bit Python with Tcl/Tk and pip**, and enable its PATH option.
   The software check for this update used Python **3.14.5**. Install Git if
   you want to clone/pull updates; GitHub's Download ZIP also works.
2. Clone the repository, or run `git pull --ff-only` in an existing clean
   checkout. In PowerShell, from the repository root:

   ```powershell
   git clone -c core.longpaths=true https://github.com/JCastel358/Eltec406mca.git
   cd Eltec406mca
   python -m pip install -r requirements.txt
   python -c "import struct, tkinter, numpy, serial, matplotlib; print('Python bits:', struct.calcsize('P') * 8)"
   ```

   The clone option enables Git's Windows long-path support for the archived
   hardware exports. Keep the checkout path short; for an existing checkout,
   run `git config core.longpaths true` before pulling if Git reports
   "Filename too long". The bitness check must print **64** for real DAQ operation. Install into
   the same Python used to launch the app. If multiple Pythons are installed,
   set `ELTEC_PYTHON` to that interpreter's full executable path before using
   a `.cmd` launcher (the launchers otherwise prefer `pythonw.exe` on PATH).
3. For the **M25/HUB array**, install the ACCES **USB-AIO16-64MA driver
   package**, including the 64-bit `AIOUSB.dll`. Drivers are installed per PC
   and are not included in Git. This workflow runs on Windows and does not
   require Arduino, an ESP32 flash, or the separate `50AmpBoard` checkout.
   For the **single-detector rig**, install the board's USB-serial driver if
   needed and select its actual COM port; it may differ on the new computer.
4. Check the array dashboard without hardware first:

   ```powershell
   python array_rig/eltec_array_tester.py --simulate
   ```

   Choose **Generated demo** and complete a batch. To run normally, use
   `array_rig/run_eltec_array_tester.cmd`; the single-detector launcher is
   `single_detector_rig/run_eltec_rig_tester.cmd`. Optional desktop shortcuts
   are created by each rig's `install_windows_launcher.ps1` on the new PC.
   Before real acquisition, follow the current [array power/setup procedure](array_rig/README.md).
5. **Transfer saved data separately if needed.** Git does not contain the
   `Documents/Eltec_*_Test_Results/` folders, including results, raw captures,
   and reference calibration files. Preserve their directory structure and
   originals; see the [data and backup guide](array_rig/docs/DATA_MAP.md).
   A new physical setup still needs its applicable reference checks; copying
   files does not establish calibration for changed hardware.

The MOD50 browser dashboard and its assets are included in the repository
and run locally. Node.js and Playwright are only for optional browser
regression tests, not for operating the app. Python packages and device
drivers must be installed before offline use. Source files, firmware snapshots,
hardware packages and documentation are included; local `tmp/` runs, caches
and generated measurement data are not new Git content.

## I'm a technician — run a batch

1. Laptop on AC power, ESP32 USB cable in, both batteries connected, no fans near the fixture.
2. Double-click the **Eltec Test Rig** desktop icon (or `single_detector_rig\run_eltec_rig_tester.cmd` / `.sh`). For a tray of fifty on the array rig use the **Eltec Array Rig** icon (`array_rig\run_eltec_array_tester.cmd` / `.sh`) — runbook §3b.
3. Pick the sensor model → **Start tester** → batch number, your name, filter setup → load a part → **Enter**.
4. Read the banner (PASS / PASS · NEAR LIMIT / FAIL) → load the next sensor →
   **Next (Enter)**. The number only advances when a part passes.

Everything else — verdicts, the Stop / Next buttons, where files go,
what to do when something fails — is in
**[array_rig/docs/TECHNICIAN_RUNBOOK.md](array_rig/docs/TECHNICIAN_RUNBOOK.md)**.

## I'm the engineer taking over

Start with **[array_rig/docs/ENGINEER_HANDOVER.md](array_rig/docs/ENGINEER_HANDOVER.md)** (what
you are inheriting, architecture, the copy-per-model rule, firmware lifecycle,
open work). Then **[array_rig/docs/CALIBRATION_RECORD.md](array_rig/docs/CALIBRATION_RECORD.md)**
before touching any limit.

## Repository map

```
README.md                    this file — the index
CHANGELOG.md                 dated history, newest first (one entry per behavioural change)
CLAUDE.md                    ground rules for AI-assisted sessions
run_all_tests.py|.cmd|.sh    runs every test suite of both rigs, prints a summary
array_rig/docs/
  TECHNICIAN_RUNBOOK.md      operator instructions
  ENGINEER_HANDOVER.md       engineering manual + open work
  CALIBRATION_RECORD.md      every limit/factor/gate state with provenance  ← source of truth for numbers
  DATA_MAP.md                where results & evidence live (outside the repo) + backup routine
  TP120(40623).pdf           test procedure of the 40623 (array rig); daq_usb_aio16_64ma/ = DAQ vendor docs
single_detector_rig/         THE single-detector application — selector + one directory per sensor model
  eltec_rig_tester.py        selector GUI (dropdown → launches the model app)
  sensor_versions.py         model registry + required firmware
  attempt_history.py         per-batch attempt log (shared)
  v1_single_sensor/          vendored signal-math engine (shared, frozen)
  m405m22/ m406mca/ m449m18/ one qualified tester each: GUI, backend, analysis, tests, launchers, README
  run_eltec_rig_tester.*     launchers; install_*_launcher.* = optional desktop icon
array_rig/                   THE array application — selector + one directory per detector model (same pattern)
  eltec_array_tester.py      selector GUI (model dropdown → launches the model app)
  sensor_versions.py         model registry + required hardware (DAQ)
  tray_history.py            per-lot tray event log (shared)
  m40623/                    the 40623 build: GUI, DAQ backend (ctypes AIOUSB.dll), numpy noise port, probe, bench readout + live viewer, tests, launchers
Arduino/Eltec/               firmware (single rig today; the array rig's emitter board will use it later)
  Eltec.ino                  live sketch (v3.4)        README.md = protocol, flashing, troubleshooting
  versions/                  frozen copy of every build + which firmware belongs on which rig
  flash_firmware.py          one-command compile/upload/verify (+ run_flash_firmware.cmd/.sh)
  esp32_rig_readout.py, live_waveform.py   bench tools
  ESP32_ADS1256_Wiring_v2_0.md             current wiring (…_legacy_v1_9.md = retired standalone 406 rigs)
engineer_tools/              engineering-only tools, one folder per topic (README.md inside lists them)
  noise_band/                replot_noise_capture.py (replay raw noise captures under any band), filter_response_analysis.py
  array_parity/              array_noise_parity.py (derive the array rig's pin-level noise limits from a paired lot)
  emitter/                   emitter_waveform_comparison.py (legacy chopper vs rig emitter waveform shape)
  reference_unit/            reference_candidate_qualifier.py (pick the detector to mount as the AIN1 reference unit)
analysis/
  405M22_Data/               lot-500 paired-fixture data behind the 4.30 factor
  reports/                   noise-filtering explainer, historical buffer/SNR write-ups
assets/eltec_logo.png        logo loaded by the apps
```

Retired applications (LabJack-era v1–v4, ESP32 v5/v6/v6.1, the standalone
405m22 build, the pre-v2.0 unified app) were removed from the tree on
2026-08-28 and are preserved at git tag **`archive/pre-cleanup-2026-08-28`**:
`git show archive/pre-cleanup-2026-08-28:<path>`.

## Firmware

The current single-detector sketch is `Arduino/Eltec/Eltec.ino` **v3.4**;
the 449 M18 mode requires v3.2 or newer. Check the connected board before
deciding whether to flash. The array DAQ loads its firmware from the ACCES
driver package and needs no ESP32 update. Check the single-detector firmware:

```
python Arduino/Eltec/flash_firmware.py --check
```

For a unified single-detector board that needs updating (legacy standalone
406 rigs stay on v1.9), compile, flash and verify with:

```
python Arduino/Eltec/flash_firmware.py          # --check to only ask what the board runs
```

Firmware guide: [Arduino/Eltec/README.md](Arduino/Eltec/README.md). Which
build belongs on which rig (legacy 406 rigs stay on v1.9):
[Arduino/Eltec/versions/README.md](Arduino/Eltec/versions/README.md).

## Tests

```
python run_all_tests.py
```

Seven `unittest` suites cover the live viewer, single-detector rigs and array
rig, without hardware. On Windows the 406 MCA suite has one known
environment-only installer case (it passes on Xubuntu); the runner reports
it separately. Any other failure makes the runner fail.

## Where the data is

Results, attempt logs, waveform snapshots, raw noise captures and calibration
files are written **outside this repository** under
`Documents\Eltec_<model>_Test_Results\`. They are the only copy of the evidence
behind the production constants — the backup routine is in
[array_rig/docs/DATA_MAP.md](array_rig/docs/DATA_MAP.md).

## Documentation index

| Document | Audience | Contents |
| --- | --- | --- |
| [array_rig/docs/TECHNICIAN_RUNBOOK.md](array_rig/docs/TECHNICIAN_RUNBOOK.md) | technician | start, run a batch, read verdicts, troubleshoot |
| [array_rig/docs/ENGINEER_HANDOVER.md](array_rig/docs/ENGINEER_HANDOVER.md) | engineer | architecture, policies, procedures, open work, conventions |
| [array_rig/docs/CALIBRATION_RECORD.md](array_rig/docs/CALIBRATION_RECORD.md) | engineer | constants + provenance + gate states per model |
| [array_rig/docs/DATA_MAP.md](array_rig/docs/DATA_MAP.md) | engineer | results layout, evidence, backup |
| [single_detector_rig/README.md](single_detector_rig/README.md) | engineer | the selector app, launchers, v2.0 skip/attempt features |
| [m405m22/README.md](single_detector_rig/m405m22/README.md) · [m406mca/README.md](single_detector_rig/m406mca/README.md) · [m449m18/README.md](single_detector_rig/m449m18/README.md) | engineer | per-model test mechanics |
| [array_rig/README.md](array_rig/README.md) · [m40623/README.md](array_rig/m40623/README.md) | engineer | the array rig: selector, DAQ chain, the 40623 flow, colours, files, PENDING status, the engineering tools (bench probe, readout, live viewer) |
| [array_rig/docs/TP120(40623).pdf](array_rig/docs/TP120(40623).pdf) · [array_rig/docs/daq_usb_aio16_64ma/](array_rig/docs/daq_usb_aio16_64ma/) | engineer | the 40623 test procedure; DAQ datasheet, DAQ-PACK guide, letter of volatility |
| [Arduino/Eltec/README.md](Arduino/Eltec/README.md) · [versions/README.md](Arduino/Eltec/versions/README.md) | engineer | firmware, protocol, flashing, version archive |
| [CHANGELOG.md](CHANGELOG.md) | both | what changed, when, why |

## Dependencies

Python 3 with `tkinter`, `numpy`, `pyserial`, `matplotlib` (optional, nicer
snapshots). The array rig additionally needs the ACCES **USB-AIO16-64MA
Install** driver package (provides `AIOUSB.dll`) and a **64-bit** Python; no
extra Python packages. Xubuntu: `sudo apt install python3 python3-tk python3-numpy
python3-serial python3-matplotlib`, user in `dialout`. Flashing needs the
Arduino IDE 2.x installed (its bundled `arduino-cli` is used).
