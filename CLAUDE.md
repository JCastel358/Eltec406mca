# CLAUDE.md — ground rules for AI-assisted work in this repository

Two Eltec sensor test rigs, one top-level folder each: `single_detector_rig`
(ESP32 + ADS1256, one Tkinter selector with one qualified tester per sensor
model: 405 M22, 406 MCA, 449 M18; firmware in `Arduino/Eltec`) and `array_rig`
(50-position PCB read by an ACCES USB-AIO16-64MA DAQ, same selector + model
directory pattern; first model 40623 per TP120, offset + noise only,
CALIBRATION PENDING). Read `README.md` first; the numbers live in
`docs/CALIBRATION_RECORD.md`, the history in `CHANGELOG.md`.

## Hard rules

1. **The model directories of both rigs are deliberate near-copies. Never
   refactor them into a shared core, base class or package, and never share
   code between `single_detector_rig/` and `array_rig/`** (the only link is
   the frozen 405 noise oracle in `array_rig/m40623/tests/golden_noise_reference.py`).
   Port a fix by hand per `docs/ENGINEER_HANDOVER.md` §4, model by model, each
   with its own suite run.
2. **Never edit anything under `Arduino/Eltec/versions/`.** Snapshots are
   frozen. `Eltec.ino` must stay byte-identical to the newest snapshot; a
   firmware change means: bump the `IDN?` string, new snapshot folder, new
   row in `versions/README.md`, update `REQUIRED_FIRMWARE`.
3. **Legacy standalone 406MCA rigs stay on firmware v1.9.** Never suggest
   flashing v2.0+ on one.
4. **Every behavioural, threshold, firmware, wiring or doc-structure change
   gets a dated entry at the top of `CHANGELOG.md` in the same commit.** A
   constant change also updates `docs/CALIBRATION_RECORD.md` and bumps the
   model's `SENSITIVITY_CALIBRATION_ID` when a factor moves.
5. **Results and calibration data live outside the repo**
   (`Documents/Eltec_*_Test_Results`). Never write there from scripts you
   author, never commit `.npz`/result CSVs, never delete or rename files
   there — they are evidence.
6. **Run `python run_all_tests.py` before claiming anything is done.**
   Baseline: glue 38, 405 175 (4 skipped), 406 109 (on Windows exactly two
   known environment-only cases: `test_launcher_installation_uses_only_v6_1_identities`
   and `test_auto_connect_validates_candidates_and_is_idempotent`), 449 111,
   array glue 31, 40623 array 134 — 598 tests. Any other failure is yours.
7. **Commit and push before restructuring; tag before deleting.** Retired
   code is at `archive/pre-cleanup-2026-08-28` — recover with
   `git show <tag>:<path>`, do not re-create from memory.
8. Thresholds, factors and gate flags are **user decisions backed by bench
   data**. Do not "tune" them; if a number looks wrong, say so and cite the
   provenance row in the calibration record.

## Conventions

- Stdlib `unittest`, run from the repo root with `-s single_detector_rig/<dir>/tests` or `-s array_rig/<dir>/tests`; no pytest on the bench laptop. Tests never write under `Documents` (the array tester takes an explicit results root; `ELTEC_ARRAY_RESULTS_ROOT` redirects a GUI run).
- `.gitattributes` enforces line endings (LF in the repo; `.cmd`/`.bat`/`.ps1` CRLF on checkout). Do not hand-convert files.
- Windows bench laptop: board on COM3; `arduino-cli` is inside the Arduino IDE install (`flash_firmware.py` finds it). Xubuntu: `/dev/ttyUSB0`. The array rig's DAQ is `USB-AIO16-64MA` (VID 0x1605 PID 0x8145) driven through `AIOUSB.dll` (64-bit, System32) via `ctypes`; `array_rig/m40623/daq_bench_probe.py` is the bench check, `--simulate` runs everything without hardware.
- Match the existing code style (long explanatory comments above constants are the house style — they carry the *why*). Docs are Markdown with tables; dates are ISO.
- Prefer editing the existing doc that owns a topic over adding a new file: numbers → `CALIBRATION_RECORD.md`, operator steps → `TECHNICIAN_RUNBOOK.md`, engineering procedure → `ENGINEER_HANDOVER.md`, data locations → `DATA_MAP.md`, model mechanics → that model's README, firmware → `Arduino/Eltec/README.md`.

## Things that are easy to get wrong

- "GPIO25" in old text is the pre-2026-08-25 gate pin; the rig uses **GPIO33** and the apps send `PIN,33`.
- The 405 noise limit is 300 mV ÷ **700** (≈429 µV at the pin), not ÷ 4000.
- The ±0.10 mV near-limit band is a **PASS with a warning**, not a retest/quarantine.
- The reference (AIN1) and battery gates are **off** on every model — by decision, not by accident.
- `Documents/Eltec_406MCA_Test_Results/v6_1_esp32` is the correct, live 406 results path even though "v6_1_esp32" is a retired build name.
- TP120's 40623 noise limits (10.0–37.9 mV) are **DMM readings behind amplifier 9000232 + rectifier-hold 9000272** — never apply them at the pin. The array rig's pin-level noise limits are `None` until the paired lot derives the chain factor; with `None` every noise verdict is NO_LIMIT (measured, recorded, never a failure).
- The 40623 offset limits (0.3–1.2 V) are **PROVISIONAL** until the array PCB's loading is confirmed against fixture 9000054 (+8 V, 100 kΩ source resistor).
- Array position labels are TP120's `row-col` (`1-3` = row 1, part 3); DAQ channel = `(row-1)*10 + (col-1)`, CH0–CH49 single-ended.
- DAQ volts come from raw counts × our own range table, never from `ADC_GetScanV` (a `-HG` unit would mis-scale it); the range is per group of four channels, the scan is one contiguous range, there is no anti-alias filter (the rig captures wideband at 1000 scans/s and band-limits in software).
- The two rigs each have a `sensor_versions.py`; they must never be imported into one process (the runner uses one interpreter per suite).
- `Arduino/Eltec/Eltec.ino` and the frozen snapshots still say `tech_app/` in a comment — leave them (byte-frozen).
