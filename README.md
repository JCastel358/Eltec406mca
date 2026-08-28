# Eltec sensor test rig (ESP32 + ADS1256)

Bench rig and software that qualify Eltec pyroelectric IR sensors. An ESP32
chops an IR emitter and streams a 24-bit ADS1256 ADC to a laptop; **one
Python desktop application** (`tech_app/eltec_rig`) runs the test procedure
for the sensor model chosen in a dropdown, judges each part, and writes a CSV
row per sensor. Windows 11 and Xubuntu are both supported.

| Sensor model | Procedure | Status (2026-08-28) | Results folder (`Documents\…`) |
| --- | --- | --- | --- |
| **405 M22** (1 Hz) | TP412 — offset, emitter-off noise, sensitivity/polarity | production; sensitivity factor 4.30 from lot 500 | `Eltec_405M22_Test_Results\405m22_esp32` |
| **406 MCA** (10 Hz) | offset, sensitivity/polarity, v6.1 stability policy | production; factor 1.582 from lot 520 | `Eltec_406MCA_Test_Results\v6_1_esp32` |
| **449 M18** (5 + 18 Hz) | TP443 frequency tracking | **calibration pending** (K_5 / K_18 not derived; needs firmware v3.2, not yet flashed) | `Eltec_449M18_Test_Results\449m18_esp32` |

---

## I'm a technician — run a batch

1. Laptop on AC power, ESP32 USB cable in, both batteries connected, no fans near the fixture.
2. Double-click the **Eltec Test Rig** desktop icon (or `tech_app\eltec_rig\run_eltec_rig_tester.cmd` / `.sh`).
3. Pick the sensor model → **Start tester** → batch number, your name, filter setup → load a part → **Enter**.
4. Read the banner (PASS / PASS · NEAR LIMIT / FAIL) → **Save + Next Sensor (Enter)**.

Everything else — verdicts, the Skip / Re-measure buttons, where files go,
what to do when something fails — is in
**[docs/TECHNICIAN_RUNBOOK.md](docs/TECHNICIAN_RUNBOOK.md)**.

## I'm the engineer taking over

Start with **[docs/ENGINEER_HANDOVER.md](docs/ENGINEER_HANDOVER.md)** (what
you are inheriting, architecture, the copy-per-model rule, firmware lifecycle,
open work). Then **[docs/CALIBRATION_RECORD.md](docs/CALIBRATION_RECORD.md)**
before touching any limit.

## Repository map

```
README.md                    this file — the index
CHANGELOG.md                 dated history, newest first (one entry per behavioural change)
CLAUDE.md                    ground rules for AI-assisted sessions
run_all_tests.py|.cmd|.sh    runs all four test suites, prints a summary
docs/
  TECHNICIAN_RUNBOOK.md      operator instructions
  ENGINEER_HANDOVER.md       engineering manual + open work
  CALIBRATION_RECORD.md      every limit/factor/gate state with provenance  ← source of truth for numbers
  DATA_MAP.md                where results & evidence live (outside the repo) + backup routine
tech_app/eltec_rig/          THE application — selector + one directory per sensor model
  eltec_rig_tester.py        selector GUI (dropdown → launches the model app)
  sensor_versions.py         model registry + required firmware
  attempt_history.py         per-batch attempt log + skipped-parts queue (shared)
  v1_single_sensor/          vendored signal-math engine (shared, frozen)
  m405m22/ m406mca/ m449m18/ one qualified tester each: GUI, backend, analysis, tests, launchers, README
  run_eltec_rig_tester.*     launchers; install_*_launcher.* = optional desktop icon
Arduino/Eltec/               firmware
  Eltec.ino                  live sketch (v3.2)        README.md = protocol, flashing, troubleshooting
  versions/                  frozen copy of every build + which firmware belongs on which rig
  flash_firmware.py          one-command compile/upload/verify (+ run_flash_firmware.cmd/.sh)
  esp32_rig_readout.py, live_waveform.py   bench tools
  ESP32_ADS1256_Wiring_v2_0.md             current wiring (…_legacy_v1_9.md = retired standalone 406 rigs)
engineer_tools/              replot_noise_capture.py (replay raw noise captures), filter_response_analysis.py
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

The bench board must run `Arduino/Eltec/Eltec.ino` **v3.2** for all three
models (it currently runs v3.1; the 405 M22 and 406 MCA modes work on
v2.1–v3.2, the 449 M18 mode needs v3.2). Flash and verify with one command:

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

Four `unittest` suites, ~431 tests, no hardware needed. On Windows the 406 MCA
suite reports two known environment-only cases (they pass on Xubuntu) — see
the handover doc §7.

## Where the data is

Results, attempt logs, waveform snapshots, raw noise captures and calibration
files are written **outside this repository** under
`Documents\Eltec_<model>_Test_Results\`. They are the only copy of the evidence
behind the production constants — the backup routine is in
[docs/DATA_MAP.md](docs/DATA_MAP.md).

## Documentation index

| Document | Audience | Contents |
| --- | --- | --- |
| [docs/TECHNICIAN_RUNBOOK.md](docs/TECHNICIAN_RUNBOOK.md) | technician | start, run a batch, read verdicts, troubleshoot |
| [docs/ENGINEER_HANDOVER.md](docs/ENGINEER_HANDOVER.md) | engineer | architecture, policies, procedures, open work, conventions |
| [docs/CALIBRATION_RECORD.md](docs/CALIBRATION_RECORD.md) | engineer | constants + provenance + gate states per model |
| [docs/DATA_MAP.md](docs/DATA_MAP.md) | engineer | results layout, evidence, backup |
| [tech_app/eltec_rig/README.md](tech_app/eltec_rig/README.md) | engineer | the selector app, launchers, v2.0 skip/attempt features |
| [m405m22/README.md](tech_app/eltec_rig/m405m22/README.md) · [m406mca/README.md](tech_app/eltec_rig/m406mca/README.md) · [m449m18/README.md](tech_app/eltec_rig/m449m18/README.md) | engineer | per-model test mechanics |
| [Arduino/Eltec/README.md](Arduino/Eltec/README.md) · [versions/README.md](Arduino/Eltec/versions/README.md) | engineer | firmware, protocol, flashing, version archive |
| [CHANGELOG.md](CHANGELOG.md) | both | what changed, when, why |

## Dependencies

Python 3 with `tkinter`, `numpy`, `pyserial`, `matplotlib` (optional, nicer
snapshots). Xubuntu: `sudo apt install python3 python3-tk python3-numpy
python3-serial python3-matplotlib`, user in `dialout`. Flashing needs the
Arduino IDE 2.x installed (its bundled `arduino-cli` is used).
