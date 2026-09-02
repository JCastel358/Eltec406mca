# Engineer handover — Eltec sensor test rigs

For the engineer who inherits this rig: what it is, how it is built, the rules
that keep it trustworthy, and what is still open. Read this once end to end;
afterwards the [root README](../README.md) is the index.

Written 2026-08-28; array rig added 2026-09-02.

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

**A second rig lives beside it since 2026-09-02** (`array_rig/`, tag
`archive/pre-array-rig-2026-09-02` marks the tree before the split): a
50-position PCB (5 × 10, one unity-gain buffer per position) read by an
**ACCES USB-AIO16-64MA** DAQ over USB, testing fifty detectors per tray. Same
selector + model-directory pattern, no firmware.

| Model | Procedure | State (2026-09-02) |
| --- | --- | --- |
| 40623 (array, 50 positions) | TP120: offset check + noise; sensitivity/polarity waits for the emitter board | app complete, **calibration pending** — noise limits `None`, offset limits provisional, raw captures saved for the paired lot |

## 2. Day-1 reading order

1. This file.
2. [`docs/CALIBRATION_RECORD.md`](CALIBRATION_RECORD.md) — every limit, factor and gate state, with provenance. **Read before touching any threshold.**
3. [`single_detector_rig/README.md`](../single_detector_rig/README.md) — the selector app, launchers, v2.0 skip/attempt machinery.
4. The README of the model you will work on: [405](../single_detector_rig/m405m22/README.md) (the most detailed — noise pipeline, serial-reliability post-mortems), [406](../single_detector_rig/m406mca/README.md), [449](../single_detector_rig/m449m18/README.md).
5. [`Arduino/Eltec/README.md`](../Arduino/Eltec/README.md) — firmware, serial protocol, flashing; then [`versions/README.md`](../Arduino/Eltec/versions/README.md) — which firmware belongs on which rig.
6. [`docs/DATA_MAP.md`](DATA_MAP.md) — where results and evidence live, and the backup routine.
6b. For the array rig: [`array_rig/README.md`](../array_rig/README.md) then [`m40623/README.md`](../array_rig/m40623/README.md) (DAQ chain, flow, colours, files) and CALIBRATION_RECORD §4b.
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

### Array rig hardware chain

```
laptop ──USB 2.0── ACCES USB-AIO16-64MA DAQ-PACK ──J2/J1 DB37── 50 unity-gain buffers ── 50 detector sockets (5 x 10)
            (AIOUSB.dll, ctypes)   16-bit SAR, 2 mux stages,        CH0-CH49 single-ended, row order:
                                   500 kS/s, no anti-alias filter    CH = (row-1)*10 + (col-1)
```

No ESP32 yet: the emitter board (sensitivity phase) will drive emitters with
the same firmware later. Facts that shape the array code: the range is per
group of four channels, the scan is one contiguous range, hardware
oversamples arrive raw in streamed mode, volts are computed from raw counts
with our own table (a `-HG` unit would mis-scale the driver's volts), the
device re-enumerates after loading its firmware at plug-in, and **the DLL's
immediate-read entry points (`ADC_GetScan` / `ADC_GetScanV`) rewrite the
device's configuration block (trigger byte 0x05 → 0x04) and never restore
it** — `AiousbDaq._reassert_config()` re-writes and read-back-verifies the
block after every immediate read and before every stream (found and fixed
2026-09-02: offsets polled, then a stream started, delivered 0 scans in
12 s). Bench proof: `array_rig/m40623/daq_bench_probe.py`; bench habits
(offsets, captures, a live scope): `daq_rig_readout.py` and
`daq_live_waveform.py` (§9).

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

```
array_rig/
  eltec_array_tester.py    selector GUI (model dropdown → SUBPROCESS), mirror of the single rig's
  sensor_versions.py       registry + REQUIRED_HARDWARE (the DAQ; no firmware)
  tray_history.py          shared: <lot>_attempts.csv tray-event writer + numbering helpers
  m40623/
      eltec_40623_array_tester.py   TrayController (Tk-free flow: poll → lock → noise → save) + the Tk GUI
      daq_backend.py                ctypes wrapper over AIOUSB.dll, config block, de-interleave, StreamDiagnostics, SimulatedDaq
      array_analysis.py             numpy port of the 405 noise DSP + TP120 offset classes + enum verdict model
      daq_bench_probe.py            engineering CLI (never issues verdicts)
      daq_rig_readout.py            engineering readout: ArrayRig API + CLI (offsets, captures to CSV / npz, watch) — mirror of esp32_rig_readout.py
      daq_live_waveform.py          engineering live viewer: rolling scope of any position + the 5 x 10 grid — mirror of live_waveform.py
      tests/                        golden_noise_reference.py = frozen copy of the 405 noise functions (the oracle)
```

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

The vendored engine `single_detector_rig/v1_single_sensor/eltec_406mca_tester.py` is
shared by design (pure numpy signal math, ports untouched); treat it as frozen.

**Across the two rigs the rule is stronger: nothing is imported from
`single_detector_rig/` into `array_rig/` or back.** The array rig's noise
DSP is a numpy re-expression of the 405's functions; the 405 originals are
frozen verbatim in `array_rig/m40623/tests/golden_noise_reference.py` and a
drift test fails if either side changes. Porting a DSP fix between the rigs
is therefore explicit: change the 405, re-freeze the oracle deliberately,
make the numpy port agree, run both suites.

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

**Array rig:** same recipe inside `array_rig/` (copy `m40623/`, replace the
spec constants in `array_analysis.py`, register in `array_rig/sensor_versions.py`,
extend the expected keys in `array_rig/tests/test_eltec_array_rig.py`, add the
suite, a CALIBRATION_RECORD section — the 40623's §4b shows the PENDING
pattern — and the DATA_MAP row).

## 7. Tests

```
python run_all_tests.py            # every suite of both rigs, summary table, exit 1 on failure
python -m unittest discover -s single_detector_rig/tests            # selector glue + attempt history
python -m unittest discover -s single_detector_rig/m405m22/tests
python -m unittest discover -s single_detector_rig/m406mca/tests
python -m unittest discover -s single_detector_rig/m449m18/tests
python -m unittest discover -s array_rig/tests                     # array selector glue + tray history
python -m unittest discover -s array_rig/m40623/tests              # DAQ backend (fake DLL), analysis (golden parity), tester flow, readout, live viewer (Agg), engineer tools
```

Stdlib `unittest` only (no pytest on the bench laptop). Baseline on 2026-09-02:
glue 38, 405 M22 175 (4 skipped), 406 MCA 109, 449 M18 111, array glue 31,
40623 array 241 — **705 tests**.
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

**Array rig noise limits (open — CALIBRATION_RECORD §4b.2):** run the bench
spike first (`daq_bench_probe.py info → selfcal → config → scan` with a known
voltage → `slots → floor 20 → stream 60 → crosstalk`; numbers into §6). Then
the paired lot: 30–50 parts on the legacy 9000233 fixture per TP120 (DMM
reading per position, under vacuum) and on the array rig the same day;
type the legacy readings into `legacy_readings.csv` and run
`engineer_tools/array_noise_parity.py --legacy legacy_readings.csv --array
<lot csv or npz dir>`; it proposes the chain factor and the pin-level
limits for the worst-window and median metrics and can replay other bands
from the saved captures. Require identical decisions to the legacy fixture,
then set the constants, bump `CALIBRATION_ID`, update §4b — one commit.

**Stability threshold evidence:** `python single_detector_rig/<model>/stability_calibration.py capture --sensor-id KNOWN_GOOD_01` then `summarize` on the `calibration/*_cycles.csv` files — review peak-delta percentiles before changing `stability_settings.json`.

## 9. Bench tools

| Tool | Use |
| --- | --- |
| `Arduino/Eltec/flash_firmware.py` (`--list`, `--check`, `--port`, `--sketch versions/Eltec_vX_Y`) | flash / identify the board |
| `Arduino/Eltec/esp32_rig_readout.py ports\|offset\|ref\|pwm on\|gate on\|stream\|test\|noisecmp` (`--freq`, `--fe v19\|v20`) | serial-level checks without the GUI; `gate on` holds the port open so the drive survives while you measure |
| `Arduino/Eltec/live_waveform.py --pwm --freq 1` | rolling scope view, SPACE toggles the emitter, `lag` readout must stay ~0 |
| `engineer_tools/replot_noise_capture.py` | replay saved raw noise captures under any band; verdict comparison |
| `engineer_tools/filter_response_analysis.py` | passband / aliasing characterisation of the noise pipeline |
| `array_rig/m40623/daq_bench_probe.py info\|selfcal\|config\|scan\|slots\|floor\|stream\|capture\|crosstalk` (`--simulate`, `--oneshot`) | the array rig's DAQ on the bench: identity, self-cal, config read-back, own-scale scan (the -HG question is settled only by a known, metered voltage on CH0 — the "driver V" column checks the arithmetic, not the gain), unsettled slot, instrument floor (onboard full-scale reference), 60 s integrity, crosstalk |
| `array_rig/m40623/daq_rig_readout.py info\|offset\|stream\|noise\|watch\|test` (`--simulate`, `-p 2-4`, `-o cap.csv`, `--npz cap.npz`) | the array rig's `esp32_rig_readout.py`: offsets of any or all positions, captures of all fifty channels to CSV / npz (replayable in the replot tool), text-mode live readout; no verdicts, files only at explicit paths |
| `array_rig/m40623/daq_live_waveform.py --position 2-4 -w 8` (`--simulate`, `--exit-after`, `--save-dir`, `--grid-metric noise`) | the array rig's `live_waveform.py`: rolling scope of any position switched live (arrow keys / `n` / `p` / a click on the 5 × 10 grid), tiles by offset band or judged-band pk-pk (`g`), judged-band trace; SPACE = Hold/Run, `s` saves the buffer as `daq_live_<stamp>.npz`; `lag` readout must stay ~0 |
| `engineer_tools/array_noise_parity.py` | derive the array rig's pin-level noise limits from a paired lot (CALIBRATION_RECORD §4b.2) |

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
11. **Array rig bench spike** — done 2026-09-02 for the DAQ alone (CALIBRATION_RECORD §6: stream integrity clean at 400 KB/s, floor 49 µV median / 79 µV worst pk-pk in the judged band, slot 0 unsettled → drop stays). **Found and fixed the same day:** the DLL's immediate reads clear the trigger byte, so the tester's order (offsets polled, then the stream) delivered 0 scans — the backend now re-asserts its block after every immediate read and before every stream, and the tester abandons a silent stream after 5 s into the retry / NOT MEASURED path (never remove either). **Still open with the PCB:** the -HG scaling question — only a known, metered ~1.5 V on CH0 decides it (`daq_bench_probe.py scan`; the probe's "driver V" column cannot, it uses the same formula) — and crosstalk (`crosstalk --source-channel N` with a 10 Hz source).
12. **Array rig hardware questions** (§4b.3): PCB supply/loading vs TP120's 9000054 (+8 V, 100 kΩ) and 9000233 (±5 V, vacuum); amplifier 9000232 gain/passband; vacuum for the paired lot.
13. **Array rig noise limits**: the paired lot and `array_noise_parity.py` (§8); until then every noise verdict is NO_LIMIT and every row says PENDING.
14. **Emitter board for the array**: TP120 sensitivity/polarity at 3 Hz — the tester has the disabled "Sensitivity" step and the `drive` slot; the ESP32 firmware's `PWM,FREQ` can drive it.

## 11. Working conventions

- Work on `main`, push at the end of every session (`git push origin main --tags`).
- **Before deleting or restructuring anything, tag first**: `git tag -a archive/<what>-<date> -m "..." && git push origin <tag>`.
- Every behavioural change, threshold change, firmware bump or wiring change gets a dated entry at the top of `CHANGELOG.md` **in the same commit**; constants also update `docs/CALIBRATION_RECORD.md`.
- Never write generated data into the repository — results stay in `Documents/Eltec_*_Test_Results` (and are `.gitignore`d if copied in by accident).
- Keep `Eltec.ino` identical to the newest `versions/` snapshot; never edit a snapshot.
- Recovering a retired file: `git show archive/pre-cleanup-2026-08-28:tech_app/deprecated/v6_1_esp32/README.md > recovered.md` (any path under that tag).
- The two rigs each have a `sensor_versions.py` (and other same-named modules); they are never imported into one process — `run_all_tests.py` runs one interpreter per suite. Tests never write under `Documents`: the array tester takes an explicit results root, and `ELTEC_ARRAY_RESULTS_ROOT` redirects a GUI / simulator run.
- **One process on the DAQ at a time** — the tester, `daq_bench_probe.py`, `daq_rig_readout.py` or `daq_live_waveform.py`, never two (the same rule as the serial port on the ESP32 rig, but with no error to warn you: on the bench a second program's stream silently took over — the first program's stream stopped delivering and failed its integrity check — and a self-calibration attempted while another program streamed failed with Win32 status 13). The three engineering tools issue no verdicts and write files only at explicit paths (`--save`, `-o`, `--npz`, `--save-dir`), never under `Documents`.
- Environments: Windows 11 bench laptop (board on **COM3**; Python 3 with tkinter, numpy, pyserial, matplotlib; `arduino-cli` lives inside the Arduino IDE 2.x install, `flash_firmware.py` finds it) and Xubuntu (`/dev/ttyUSB0`, user in `dialout`, `sudo apt install python3 python3-tk python3-numpy python3-serial python3-matplotlib`). Line endings are enforced by `.gitattributes`.
