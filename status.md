# Eltec 406MCA ESP32/Xubuntu status

Last updated: 2026-07-16

## Executive summary

The active application is the isolated v6 ESP32 tester in
`tech_app/v6_esp32/`. It uses the ESP32/ADS1256 rig, a mandatory permanently
mounted reference sensor on ADS1256 AIN1, the DUT sensor on AIN0, the 6 V SLA
on AIN7, and fixed 10 Hz / 50 percent emitter PWM on GPIO25.

V6 now checks and stabilizes the reference unit before it reads the DUT. There
is no fixed reference warm-up delay. The reference check begins streaming as
soon as PWM turns on, uses the same robust-peak stability rule as the DUT, and
then averages five fresh cycles. An invalid reference blocks AIN0 entirely and
invalidates the calibration until the emitter/reference unit is recalibrated.

The latest real hardware calibration is complete and valid. The v6 automated
suite passes all 79 tests. V5 remains available as historical context and has
not been overwritten.

## Current production sequence

### Reference-unit calibration

The Batch information and Load sensor screens contain a visible reference-unit
card. With a known-good/new emitter installed, press **Calibrate reference
unit**. The application:

1. Checks the 6 V battery and forces PWM off before starting.
2. Turns on the fixed 10 Hz / 50 percent GPIO25 PWM.
3. Immediately streams the reference sensor from AIN1; it does not sleep for a
   fixed warm-up period.
4. Computes one robust high peak per rising-edge PWM cycle. The robust peak is
   the median of the highest 10 percent of samples, using at least five.
5. Requires five consecutive absolute cycle-to-cycle robust-peak deltas at or
   below `0.100 mV`. A larger delta resets the confirmation run.
6. After stability, averages the raw peak-to-peak values of the next five
   complete cycles to obtain one reference reading.
7. Repeats that adaptive reading five times and averages the five readings to
   create the baseline.
8. Requires all five calibration readings to be repeatable within 10 percent
   of their average, saves the baseline, and always turns PWM off.

The calibration is schema v2 and is stored at:

```text
~/Documents/Eltec_406MCA_Test_Results/v6_esp32/reference_sensor_calibration.json
```

Schema v1 used a timed capture/median metric and is intentionally rejected so
old and new reference metrics cannot be mixed.

### Each sensor test

After loading a DUT and pressing Enter, the enforced order is:

```text
battery check
-> PWM on
-> immediate AIN1 adaptive peak-stability stream
-> average five fresh reference cycles
-> compare with the saved +/-10% reference window
-> PWM off
-> only after reference PASS: read AIN0 DUT offset
-> PWM on
-> continuous AIN0 adaptive-stability capture
-> measure 10 fresh DUT cycles
-> PWM off
-> PASS/FAIL verdict
```

Important safety behavior:

- AIN0 is not read before the reference unit passes.
- A reference reading outside the calibrated +/-10 percent window immediately
  blocks the DUT test, invalidates the saved baseline, and returns the operator
  to the Load sensor screen.
- A reference unit that cannot stabilize within 20 seconds also blocks the DUT
  and invalidates calibration.
- Further DUT testing stays locked until **Calibrate reference unit** succeeds.
- PWM is disabled on success, timeout, cancellation, serial errors, calibration
  errors, and application shutdown.
- The 1,000-line/second serial stream is consumed with buffered bulk reads, and
  Xubuntu opens the ESP32 tty exclusively. This prevents a full 20-second DUT
  timeout from losing records because of per-line read overhead or another
  serial program sharing the port.

## Current user interface

- The home/setup card intentionally says only **Reference unit calibrated**.
  It does not call the unit AIN1 or show its baseline/range on the home screen.
- Calibration progress visibly reports the five adaptive reference runs.
- The completed sensor screen emphasizes a large green **PASS** or red **FAIL**
  verdict.
- Every FAIL shows the standard production failure-mode selector. A DUT
  stability timeout preselects **Unstable - Unstable** and records `Unstable`
  as both the failure-mode tag and reason when the sensor is saved.
- **Show test details** reveals offset, sensitivity, polarity, polarity
  confidence, SNR, stability telemetry, reference drift, and failure reasons.
- **Show waveform** remains an independent control and can be used without
  opening the details.
- Comment, waveform snapshot, re-measure, save/next, and save/exit behavior is
  preserved.
- Simulator mode remains clearly badged, uses a synthetic passing reference,
  and never overwrites the hardware reference calibration.

## DUT adaptive analysis

The AIN0 production rule remains:

```text
PWM on -> uninterrupted AIN0/sync stream -> robust-peak stability
       -> 10 fresh measurement cycles -> production analysis
```

- Settings are mandatory and loaded from
  `tech_app/v6_esp32/stability_settings.json`.
- Five consecutive robust-peak deltas must be `<= 0.100 mV`.
- Stability must occur within 20 seconds of PWM activation. The post-stability
  measurement cycles may finish after the deadline.
- The first three complete PWM cycles must each be `10.0 +/- 0.1 Hz`.
  Missing sync, isolated edges, or wrong cadence are rig errors, not part
  verdicts.
- Official DUT sensitivity is the median of the 10 per-cycle raw `max - min`
  values. Sensitivity, polarity, confidence, noise, and SNR all use those same
  10 post-stability cycles.
- The signal-quality gate requires SNR >= `1.5` (about 3.5 dB).
- A DUT stability timeout creates a recoverable pending FAIL with
  sensitivity/polarity left unmeasured and **Unstable - Unstable** preselected.
  Saving it records the official batch row and automatically preserves a PNG
  plus full-sample and per-cycle CSV diagnostics.

The `0.100 mV` stability threshold, inherited sensitivity limits, and SNR limit
still require broader production qualification with representative known-good
and known-bad parts.

## Latest real hardware results

Connected rig during the latest work:

- Firmware identity: `ELTEC-ESP32-ADS1256,v1.7`.
- Serial port detected: `/dev/ttyUSB1`.
- Battery during calibration: `6.261 V` (`ok`).
- Reference sensor is connected on AIN1 and working.

Adaptive schema-v2 calibration readings:

```text
1: 5.3290 mV, stable at 3.151 s
2: 5.3422 mV, stable at 0.629 s
3: 5.3484 mV, stable at 0.616 s
4: 5.3434 mV, stable at 1.002 s
5: 5.3530 mV, stable at 0.689 s
```

Saved calibration:

- baseline average: `5.3432 mV`;
- allowed lower limit: `4.8089 mV`;
- allowed upper limit: `5.8775 mV`;
- tolerance: `+/-10%`;
- valid: `true`.

An independent cold-start adaptive reference check then stabilized at
`3.151 s`, averaged `5.3344 mV` across its five fresh cycles, drifted only
`-0.165%` from baseline, and passed the gate. No AIN0 read was performed during
that verification.

## Results and diagnostics

V6 output is isolated under:

```text
~/Documents/Eltec_406MCA_Test_Results/v6_esp32/
```

Each new batch CSV includes the reference audit trail:

- `reference_calibrated_at`;
- `reference_calibration_mv`;
- `reference_lower_mv`;
- `reference_upper_mv`;
- `reference_check_mv`;
- `reference_drift_pct`.

Older batch CSVs retain their existing headers; appending to an old batch does
not rewrite prior rows. Re-entering an exact batch number resumes at the next
sensor. Snapshot filenames remain collision-safe.

The separate `stability_calibration.py` tool is only for collecting/reviewing
AIN0 peak-delta evidence from known-good DUTs. It does not change production
settings or issue production verdicts. Do not confuse it with the GUI's
reference-unit calibration.

## Firmware and wiring

Required firmware is `Arduino/Eltec/Eltec.ino` v1.7 or newer. No firmware
change was required for the latest GUI/reference changes because v1.7 already
supports:

- `STREAM,START` for AIN0 DUT samples;
- `STREAM,START,REF` for AIN1 reference samples;
- digital PWM sync with every sample;
- AIN7 battery reads;
- fixed GPIO25 PWM control;
- strict sample counts and ADC-overrun reporting.

Current wiring:

- GPIO25 -> MOSFET module emitter gate, fixed 10 Hz / 50 percent PWM;
- ADS1256 AIN0 -> buffered DUT sensor;
- ADS1256 AIN1 -> permanently mounted reference sensor;
- ADS1256 AIN7 -> 6 V SLA through the battery divider.

Use `Arduino/Eltec/ESP32_ADS1256_Wiring_v1_7.md`. The older
`ESP32_ADS1256_Wiring.docx` describes the historical 9 V/AIN1 arrangement and
must not be used for this fixture.

## Verification status

Latest verification:

```text
python3 -m unittest discover -s tech_app/v6_esp32/tests -v
```

Result: all **79 v6 tests pass**, including:

- AIN1 firmware-channel selection;
- reference adaptive stability and exactly five fresh averaged cycles;
- proof that the production path has no fixed reference `sleep`/warm-up;
- reference calibration persistence, schema, repeatability, and invalidation;
- proof that failed/missing reference calibration prevents AIN0 access;
- continuous AIN0 timing, timeouts, cancellation, and serial integrity;
- buffered high-throughput serial reads and exclusive-port ownership;
- standardized failure-mode selection and automatic **Unstable - Unstable**
  classification for DUT stability timeouts;
- verdict-focused result UI and optional detail/waveform controls;
- simplified **Reference unit calibrated** home wording;
- GUI smoke coverage, CSV compatibility, and PWM cleanup.

Python compilation also passes. The last known unchanged v5 suite contains 31
passing tests, but v5 was not modified as part of the latest reference/UI work.

## How to run v6

From the repository root:

```bash
./tech_app/v6_esp32/run_eltec_406mca_esp32_tester.sh
```

Or from the v6 directory:

```bash
./run_eltec_406mca_esp32_tester.sh
```

The optional v6 desktop/menu launcher is installed only when explicitly run:

```bash
./tech_app/v6_esp32/install_xubuntu_launcher.sh
```

V6 launcher identities are isolated from v5:

- display name: `Eltec 406MCA ESP32 Tester v6`;
- menu ID: `com.eltec.406mca-esp32-tester-v6.desktop`;
- desktop entry: `~/Desktop/Eltec 406MCA ESP32 Tester v6.desktop`;
- launcher log: `~/.local/state/eltec-406mca-esp32-v6/launcher.log`.

## Important files

- `tech_app/v6_esp32/eltec_406mca_esp32_tester.py` — production GUI,
  reference calibration/gate, DUT workflow, CSV, snapshots, and simulator.
- `tech_app/v6_esp32/esp32_backend.py` — discovery, serial protocol, AIN0/AIN1
  streaming, diagnostics, scalar reads, and PWM control.
- `tech_app/v6_esp32/stability_analysis.py` — shared robust-peak cycle and
  stability analysis used by the reference unit and DUT.
- `tech_app/v6_esp32/stability_settings.json` — mandatory production peak-delta
  settings.
- `tech_app/v6_esp32/stability_calibration.py` — engineering evidence CLI for
  AIN0 stability tuning.
- `tech_app/v6_esp32/README.md` — current behavior, setup, dependencies, and
  operator instructions.
- `tech_app/v6_esp32/tests/` — backend, stability, calibration, workflow, CSV,
  and GUI tests.
- `Arduino/Eltec/Eltec.ino` — v1.7 firmware.
- `Arduino/Eltec/ESP32_ADS1256_Wiring_v1_7.md` — current fixture wiring.
- `Arduino/Eltec/ESP32_memory.md` — detailed firmware/rig notes.
- `tech_app/v5_esp32/` — historical v5 application; keep separate.

## Remaining work / known caveats

1. Qualify the inherited sensitivity thresholds on the current 6 V fixture
   using representative known-good and known-bad sensors.
2. Tune/confirm the SNR threshold using the same production dataset.
3. Collect several known-good DUT captures with `stability_calibration.py`,
   review peak-delta percentiles, and decide whether `0.100 mV` should remain
   the production stability threshold.
4. Run repeatability studies across battery state, emitter replacement,
   ambient temperature, and multiple reference/DUT sensors.
5. The reference check is deliberately strict: an out-of-window or unstable
   reference invalidates calibration and requires emitter inspection plus a
   fresh five-reading calibration.

## Working-tree state

The work is local and uncommitted. Current expected status includes:

- modified `Arduino/Eltec/Eltec.ino`;
- modified `Arduino/Eltec/esp32_rig_readout.py`;
- modified `Arduino/Eltec/ESP32_memory.md`;
- new `Arduino/Eltec/ESP32_ADS1256_Wiring_v1_7.md`;
- new/untracked `tech_app/v5_esp32/`;
- new/untracked `tech_app/v6_esp32/`;
- new/untracked `status.md`.

Preserve unrelated user changes. Do not replace or reset the historical v4/v5
applications. In particular, `tech_app/v6_esp32/` is currently untracked by
Git, so a fresh Codex session should not assume these files are committed.
