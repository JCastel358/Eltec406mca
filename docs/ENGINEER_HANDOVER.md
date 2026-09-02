# Engineer handover — Eltec sensor test rig

For the engineer who inherits this rig: what it is, how it is built, the rules
that keep it trustworthy, and what is still open. Read this once end to end;
afterwards the [root README](../README.md) is the index.

Written 2026-08-28.

---

## 1. What you are inheriting (30 seconds)

A bench rig that qualifies Eltec pyroelectric IR sensors. An **ESP32** chops
an IR emitter with a PWM gate and streams an **ADS1256** 24-bit ADC at
1000 SPS over USB serial; a **Python/Tkinter** application on the laptop runs
the test procedure for the selected sensor model, judges the part, and writes
a CSV row per sensor. Three models are supported:

| Model | Procedure | State (2026-08-28) |
| --- | --- | --- |
| 405 M22 (1 Hz) | TP412: offset, emitter-off noise, sensitivity/polarity | production, calibrated on lot 500 |
| 406 MCA (10 Hz) | offset, sensitivity/polarity with the v6.1 stability policy | production |
| 449 M18 (5 + 18 Hz) | TP443 frequency tracking | app complete, **calibration pending**, firmware v3.2 not yet flashed |

The rig replaced a LabJack T7-Pro fixture in July 2026; every LabJack-era app
was retired on 2026-08-28 (git tag `archive/pre-cleanup-2026-08-28`).

## 2. Day-1 reading order

1. This file.
2. [`docs/CALIBRATION_RECORD.md`](CALIBRATION_RECORD.md) — every limit, factor and gate state, with provenance. **Read before touching any threshold.**
3. [`single_detector_rig/README.md`](../single_detector_rig/README.md) — the selector app, launchers, v2.0 skip/attempt machinery.
4. The README of the model you will work on: [405](../single_detector_rig/m405m22/README.md) (the most detailed — noise pipeline, serial-reliability post-mortems), [406](../single_detector_rig/m406mca/README.md), [449](../single_detector_rig/m449m18/README.md).
5. [`Arduino/Eltec/README.md`](../Arduino/Eltec/README.md) — firmware, serial protocol, flashing; then [`versions/README.md`](../Arduino/Eltec/versions/README.md) — which firmware belongs on which rig.
6. [`docs/DATA_MAP.md`](DATA_MAP.md) — where results and evidence live, and the backup routine.
7. [`CHANGELOG.md`](../CHANGELOG.md) — the dated history; skim the top ten entries.

## 3. Architecture

### Hardware chain

```
laptop (Windows 11 or Xubuntu) ──USB/CP210x, 500000 baud── ESP32 DevKit V1 ──SPI── ADS1256
                                                              │ GPIO33 gate                 │ AIN0 ← DUT sensor (op-amp buffered)
                                                              ▼                             │ AIN1 ← fixed reference 406MCA sensor
                                                    dual-MOSFET module ← 6.5 V battery      │ AIN7 ← (legacy divider, unused)
                                                              ▼
                                                           IR emitter                9 V battery → sensor buffer board
```

Wiring reference: [`Arduino/Eltec/ESP32_ADS1256_Wiring_v2_0.md`](../Arduino/Eltec/ESP32_ADS1256_Wiring_v2_0.md).
Two hardware facts shape everything else:

- **The buffer board is a dual op-amp with no channel isolation**, so the DUT
  couples into the AIN1 reference channel. The reference (emitter-health) gate
  is therefore **disabled on every model** until a channel-isolated buffer
  board exists (CALIBRATION_RECORD §2.4).
- **Opening or closing the serial port resets the ESP32** (DTR). Every
  connect therefore re-programs the board (`PIN,33`, `PWM,FREQ`, `PWM,DUTY`,
  `FE,V19` for the 406), and nothing survives a port close.

### Software layout

```
single_detector_rig/
  eltec_rig_tester.py      selector GUI: dropdown → launches the model app as a SUBPROCESS
  sensor_versions.py       registry (one SensorVersion per model) + REQUIRED_FIRMWARE
  attempt_history.py       shared: <lot>_attempts.csv writer + skipped-parts queue
  v1_single_sensor/        vendored 406MCA signal-math / pass-fail engine (all models import it)
  m405m22/  m406mca/  m449m18/
      eltec_<model>_esp32_tester.py   the GUI + test flow + limits (5–8k lines)
      esp32_backend.py                serial discovery, protocol, streaming, integrity checks
      stability_analysis.py           robust-peak stability, cycle segmentation, noise decimation
      stability_calibration.py        engineering evidence CLI (never issues verdicts)
      stability_settings.json         peak-delta threshold (tracked file)
      tests/                          unittest suite
      run_… / install_…               per-model launchers (normally launched via the selector)
```

Each model app runs standalone (cwd = its directory) and is launched by the
selector as an independent process so one model can never corrupt another.
Results go **outside** the repository (DATA_MAP §1).

### Measurement principles shared by the models

- Every capture is one uninterrupted stream with the firmware's PWM state as a
  digital sync bit per sample; the stream is validated (rate, gaps, duplicates,
  ADC overruns) before anything is judged, and bounded USB micro-gaps are
  refilled from firmware timestamps.
- Sensitivity = median per-cycle peak-to-peak over a measurement window that
  only opens after the **robust-peak stability** rule (N consecutive
  cycle-to-cycle deltas under the threshold in `stability_settings.json`) is
  met; up to three attempts; polarity and SNR from the same cycles.
- Raw readings are always recorded; a legacy-equivalent value = raw × the
  model's fixture factor is what the TP limits apply to.
- Verdict rows are append-only CSV; older files keep their header when columns
  are added.

## 4. The copy-per-model policy (read this twice)

The three model directories are **deliberate near-copies** of one skeleton
(`stability_calibration.py` ~98 % identical, backends ~75–97 %, testers
80–93 %). This is intentional: each production model is a *qualified build*.
A change to a shared module would silently alter a qualified model's
behaviour and force re-verification of everything.

**Rules**

1. Never extract a shared core, package or base class across the model
   directories. The duplication is the isolation.
2. A fix or feature is made in one model, verified there, then **ported by
   hand** to the siblings — or deliberately not, if it does not apply.
3. Per-model differences are legitimate and documented in each README
   (drive frequency, front end, limits, stability policy, noise test).

**Porting a fix across models**

1. Fix and test it in the model where it was found; commit.
2. `git show <that commit> -- single_detector_rig/<model>/` and apply the same
   hunks to each sibling that has the same code (`stability_analysis.py`,
   `stability_calibration.py`, `esp32_backend.py` and the tester share most
   structure; check the 406's older `stability_analysis.py` first — it is the
   shorter base the other two extend).
3. Adapt constants and model-specific strings; never copy a threshold across
   models.
4. Run that sibling's suite and, if the change touches capture or verdicts,
   drive its app once in simulator mode.
5. One CHANGELOG entry naming which models received the change.

The vendored engine `eltec_rig/v1_single_sensor/eltec_406mca_tester.py` is
shared by design (pure numpy signal math, ports untouched); treat it as frozen.

## 5. Firmware lifecycle

The live sketch is `Arduino/Eltec/Eltec.ino`; every released build is frozen
in `Arduino/Eltec/versions/Eltec_vX_Y/` and `Eltec.ino` is byte-identical to
the newest snapshot. One firmware serves every model — per-model needs are
selected at runtime over serial (`PWM,FREQ`, `PWM,DUTY`, `FE,...`, `PIN,<n>`).
**Do not fork the firmware per model.**

To change it:

1. Edit `Eltec.ino`. Bump the `IDN?` version string on **every**
   flash-relevant change (the whole point is that stale firmware is
   detectable) and add the version note to the header comment.
2. Compile: `python Arduino/Eltec/flash_firmware.py --check` first to see
   what the board runs, then `python Arduino/Eltec/flash_firmware.py` to
   compile, upload and verify `IDN?` / `GATE?` (finds the Arduino IDE's bundled
   `arduino-cli`, auto-detects the CP210x port).
3. Snapshot: `mkdir Arduino/Eltec/versions/Eltec_vX_Y && cp Arduino/Eltec/Eltec.ino Arduino/Eltec/versions/Eltec_vX_Y/Eltec_vX_Y.ino`, add its row to `versions/README.md`, update `REQUIRED_FIRMWARE` in `sensor_versions.py` and the `MINIMUM_FIRMWARE_VERSION` of any model that needs the new command.
4. Bench-verify (the model READMEs list the probe commands), CHANGELOG entry,
   **commit and push the same day**.

**Why step 4 is not optional:** firmware v1.9 was never committed and had to
be *reconstructed* from v2.0 by reverting four edits (`versions/README.md`
tells the story). The archive is a safety net, not a substitute for commits.

**Which firmware where:** the unified bench rig runs **v3.2** (currently
**v3.1** — v3.2 is compiled but not flashed; the 449 M18 mode refuses < v3.2,
the other two models run on v2.1–v3.2). A legacy standalone 406MCA rig running
the retired v6/v6.1 app must stay on **v1.9**. The IR telescope (separate
workspace) uses v2.2; the same board is reflashed between uses. Full table:
`versions/README.md`.

## 6. Adding a sensor model

The recipe is the docstring at the top of `sensor_versions.py`; in short:
copy the closest model directory, replace the spec constants, register a
`SensorVersion`, extend the firmware with a runtime command only if a new
behaviour is needed. Additionally: add the suite to `run_all_tests.py`, add a
section to `CALIBRATION_RECORD.md` (gates default OFF with a
`CALIBRATION PENDING` stamp until a paired-fixture calibration exists — the
449 M18 shows the pattern), add the results root to `DATA_MAP.md`.

## 7. Tests

```
python run_all_tests.py            # all four suites, summary table, exit 1 on failure
python -m unittest discover -s single_detector_rig/tests            # selector glue + attempt history
python -m unittest discover -s single_detector_rig/m405m22/tests
python -m unittest discover -s single_detector_rig/m406mca/tests
python -m unittest discover -s single_detector_rig/m449m18/tests
```

Stdlib `unittest` only (no pytest on the bench laptop). Baseline on 2026-08-28:
glue 38, 405 M22 174 (4 skipped), 406 MCA 109, 449 M18 110 — **431 tests**.
Known and accepted on Windows: the 406 suite reports one error
(`test_launcher_installation_uses_only_v6_1_identities`, runs the bash
installer) and one failure
(`test_auto_connect_validates_candidates_and_is_idempotent`, asserts the
POSIX-only exclusive-tty flag); both pass on Xubuntu. Display-only GUI tests
skip headless. Every app has a **Simulator** mode (opt-in, amber badge) that
exercises the full flow without hardware — use it after any UI or flow change.

## 8. Calibration procedures

**Paired-fixture sensitivity factor (the template — lot 500, 405 M22):**
measure 30–50 representative parts on the legacy fixture and on this rig in
the same order; per part compute legacy/raw; take the median and the
regression-through-origin slope (they should agree within ~1 %; the spread
should be a few % sd); replay the lot with the candidate factor and require
identical pass/fail decisions to the legacy fixture; set the factor, bump
`SENSITIVITY_CALIBRATION_ID`, enable the gate — in the same commit — and file
the pair data under `analysis/<model>_Data/`. Details and the actual numbers:
CALIBRATION_RECORD §2.3.

**449 M18 (open):** the same recipe **once per frequency** (`K_5`, `K_18`),
all parts at 5 Hz first then all at 18 Hz (TP443 note 2), plus the corrected
ratio and identical decisions around 1.2 V / 0.72 V / 0.70 / 1.30 on withheld
parts. Watch for non-random ratio residuals — that would mean the emitter's
pulse shape interacts with detector response and one factor per frequency is
not enough (`m449m18/README.md`).

**Noise limit / window rule (405 M22):** anchored on a single part (500-44).
When more known-noisy parts exist, replay their raw captures with
`engineer_tools/replot_noise_capture.py` before changing
`NOISE_MAX_OVER_FRACTION` or the band; any band change means re-deriving the
limit.

**Reference-unit calibration (when the isolated buffer board arrives):** set
`REFERENCE_GATE_ENABLED = True` in the model, run **Calibrate reference unit**
with a known-good emitter (five adaptive readings, repeatable within 10 %),
expect ~5 mV. Schema versions are enforced so stale baselines are rejected
(CALIBRATION_RECORD §5).

**Stability threshold evidence:** `python single_detector_rig/<model>/stability_calibration.py capture --sensor-id KNOWN_GOOD_01` then `summarize` on the `calibration/*_cycles.csv` files — review peak-delta percentiles before changing `stability_settings.json`.

## 9. Bench tools

| Tool | Use |
| --- | --- |
| `Arduino/Eltec/flash_firmware.py` (`--list`, `--check`, `--port`, `--sketch versions/Eltec_vX_Y`) | flash / identify the board |
| `Arduino/Eltec/esp32_rig_readout.py ports\|offset\|ref\|pwm on\|gate on\|stream\|test\|noisecmp` (`--freq`, `--fe v19\|v20`) | serial-level checks without the GUI; `gate on` holds the port open so the drive survives while you measure |
| `Arduino/Eltec/live_waveform.py --pwm --freq 1` | rolling scope view, SPACE toggles the emitter, `lag` readout must stay ~0 |
| `engineer_tools/replot_noise_capture.py` | replay saved raw noise captures under any band; verdict comparison |
| `engineer_tools/filter_response_analysis.py` | passband / aliasing characterisation of the noise pipeline |

## 10. Known hardware issues and open work

1. **Flash and bench-verify firmware v3.2** (`IDN? -> v3.2`, `PWM,DUTY,20 -> OK,PWM,DUTY,20.0`); until then the 449 M18 mode cannot connect.
2. **449 M18 calibration**: derive `K_5`/`K_18`, fill the TP443 offset band (`OFFSET_GATE_ENABLED`), confirm polarity on real parts, revisit the 0.100 mV peak-delta threshold once real amplitudes are known.
3. **Channel-isolated buffer board** → re-enable the reference gates on all models and recalibrate fresh.
4. **Sensor-battery monitoring on AIN6** (≥ 4:1 divider + firmware mux entry + host thresholds) → re-enable the battery gates. Plan: step the sensor supply to ~8 V to match TP412's bench supply.
5. **Legacy amplifier question** (405 noise): confirm the ~700× effective chain factor by checking the legacy scope's CH2 probe (1×/10×) and the amplifier's range switch, or obtain its true gain and passband corners; the factor rests on one part.
6. **405 noise anchor**: the 15 % window rule and the 60 s soak rest on part 500-44 — refine with more failing parts.
7. **406 MCA**: the 1.582 factor's lot-520 pair data is not in the repository — locate and file it. The 0.100 mV threshold and the SNR ≥ 1.5 gate still lack broad qualification with known-good/bad parts.
8. **405 DUT threshold 0.500 mV / 0.250 mV reference / 60 s deadline** are provisional (2026-08-12).
9. Low priority: `500-27_noise_raw.npz` and its `_2` twin are byte-identical — check whether Re-measure can save a stale buffer; adaptive noise-capture length; repeatability across temperature, emitter replacement and battery state.
10. Housekeeping: the GitHub repository is still named `Eltec406mca` — rename it to `Eltec_TestRig` in the repository settings (old URLs redirect).

## 11. Working conventions

- Work on `main`, push at the end of every session (`git push origin main --tags`).
- **Before deleting or restructuring anything, tag first**: `git tag -a archive/<what>-<date> -m "..." && git push origin <tag>`.
- Every behavioural change, threshold change, firmware bump or wiring change gets a dated entry at the top of `CHANGELOG.md` **in the same commit**; constants also update `docs/CALIBRATION_RECORD.md`.
- Never write generated data into the repository — results stay in `Documents/Eltec_*_Test_Results` (and are `.gitignore`d if copied in by accident).
- Keep `Eltec.ino` identical to the newest `versions/` snapshot; never edit a snapshot.
- Recovering a retired file: `git show archive/pre-cleanup-2026-08-28:tech_app/deprecated/v6_1_esp32/README.md > recovered.md` (any path under that tag).
- Environments: Windows 11 bench laptop (board on **COM3**; Python 3 with tkinter, numpy, pyserial, matplotlib; `arduino-cli` lives inside the Arduino IDE 2.x install, `flash_firmware.py` finds it) and Xubuntu (`/dev/ttyUSB0`, user in `dialout`, `sudo apt install python3 python3-tk python3-numpy python3-serial python3-matplotlib`). Line endings are enforced by `.gitattributes`.
