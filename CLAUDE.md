# CLAUDE.md — ground rules for AI-assisted work in this repository

Eltec sensor test rig: ESP32 + ADS1256 hardware, one Tkinter application
(`single_detector_rig`) with one qualified tester per sensor model (405 M22,
406 MCA, 449 M18), firmware in `Arduino/Eltec`. Read `README.md` first; the
numbers live in `docs/CALIBRATION_RECORD.md`, the history in `CHANGELOG.md`.

## Hard rules

1. **The three model directories are deliberate near-copies. Never refactor
   them into a shared core, base class or package.** Port a fix by hand per
   `docs/ENGINEER_HANDOVER.md` §4, model by model, each with its own suite run.
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
   and `test_auto_connect_validates_candidates_and_is_idempotent`), 449 111.
   Any other failure is yours.
7. **Commit and push before restructuring; tag before deleting.** Retired
   code is at `archive/pre-cleanup-2026-08-28` — recover with
   `git show <tag>:<path>`, do not re-create from memory.
8. Thresholds, factors and gate flags are **user decisions backed by bench
   data**. Do not "tune" them; if a number looks wrong, say so and cite the
   provenance row in the calibration record.

## Conventions

- Stdlib `unittest`, run from the repo root with `-s single_detector_rig/<dir>/tests`; no pytest on the bench laptop.
- `.gitattributes` enforces line endings (LF in the repo; `.cmd`/`.bat`/`.ps1` CRLF on checkout). Do not hand-convert files.
- Windows bench laptop: board on COM3; `arduino-cli` is inside the Arduino IDE install (`flash_firmware.py` finds it). Xubuntu: `/dev/ttyUSB0`.
- Match the existing code style (long explanatory comments above constants are the house style — they carry the *why*). Docs are Markdown with tables; dates are ISO.
- Prefer editing the existing doc that owns a topic over adding a new file: numbers → `CALIBRATION_RECORD.md`, operator steps → `TECHNICIAN_RUNBOOK.md`, engineering procedure → `ENGINEER_HANDOVER.md`, data locations → `DATA_MAP.md`, model mechanics → that model's README, firmware → `Arduino/Eltec/README.md`.

## Things that are easy to get wrong

- "GPIO25" in old text is the pre-2026-08-25 gate pin; the rig uses **GPIO33** and the apps send `PIN,33`.
- The 405 noise limit is 300 mV ÷ **700** (≈429 µV at the pin), not ÷ 4000.
- The ±0.10 mV near-limit band is a **PASS with a warning**, not a retest/quarantine.
- The reference (AIN1) and battery gates are **off** on every model — by decision, not by accident.
- `Documents/Eltec_406MCA_Test_Results/v6_1_esp32` is the correct, live 406 results path even though "v6_1_esp32" is a retired build name.
