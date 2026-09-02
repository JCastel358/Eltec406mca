# Eltec 449 M18 ESP32 Tester (TP443 frequency tracking, 5 Hz + 18 Hz)

The **Model 449 M18** entry of the unified rig app
([`single_detector_rig`](../README.md)). It implements document **TP443
page 6, "449M18 Frequency Tracking"**: the part's sensitivity is read at
**5 Hz** and then at **18 Hz**, and the **18 Hz / 5 Hz ratio** is checked.
The build is derived from the 405 M22 tester (same guided flow, adaptive
peak-stability capture, skip/attempt machinery, CSV audit trail, snapshots,
simulator) with the model layer replaced.

> **Status: CALIBRATION PENDING.** The rig can run the full two-frequency
> test and records every reading, but the TP443 limits are **not enforced**
> until the per-frequency fixture factors have been derived from a paired
> comparison against the legacy fixture (see
> [Calibration](#calibration-the-two-fixture-factors)). Every verdict says so
> on screen and in the CSV (`sensitivity_gate_enabled = NO`).

## What TP443 asks for

| | Legacy fixture (9000142, optical bench) | This rig |
| --- | --- | --- |
| Source | 500 K blackbody, 0.873" aperture, 10 cm, -25 filter | pulsed IR emitter, -25 filter (same optics as the other models) |
| Modulation | **mechanical 20/80 blade** at 5 Hz (±0.05) then 18 Hz (±0.18) | ESP32 PWM at **5 Hz / 20 % duty** then **18 Hz / 20 % duty** (firmware v3.2 `PWM,DUTY`) |
| Readout | scope, 0.5 V/div, blade sync on EXT + DC 504 counter | ADS1256 AIN0 at 1000 SPS, PWM state as sync, peak-stability capture |
| Spec 1 | sensitivity ≥ **1.2 V** at 5 Hz | `legacy_5 = raw_5 × K_5` ≥ 1200 mV |
| Spec 2 | sensitivity ≥ **0.72 V** at 18 Hz | `legacy_18 = raw_18 × K_18` ≥ 720 mV |
| Spec 3 | ratio 18 Hz / 5 Hz **0.70 – 1.30** | `(K_18·raw_18) / (K_5·raw_5)` in 0.70 – 1.30 |
| Spec 4 | one sample failure, or a ratio **≤ 0.72**, ⇒ measure the tray 100 % | `ratio_tray_100_percent_flag = YES` on the row; amber card on screen; listed in the batch summary |
| Order | all parts at 5 Hz first, then all at 18 Hz (blade speed is changed by hand) | both drives back to back per part (electronic switch); each reading is recorded per frequency anyway |

Notes carried from TP443: a failing detector is removed and retested
(note 1) — that is the **Re-measure** button; only parts failing specs 1–3
are failures, the tray flag is a sampling instruction, not a verdict.

### Why two calibration factors

The legacy fixture keeps the source hot and only opens/closes the optical
path, so the source modulation is identical at 5 Hz and 18 Hz and the scope
ratio isolates the detector's frequency response. An electrically pulsed
emitter heats and cools: its optical swing per pulse is smaller at 18 Hz than
at 5 Hz, so

```
V18/V5 (this rig) = detector(18)/detector(5) × emitter_mod(18)/emitter_mod(5)
```

and the raw ratio is **not** the TP443 ratio. A factor per frequency
absorbs that (`K_18` will be larger than `K_5`), exactly like the 405 M22's
lot-500 factor absorbed that fixture's gain difference. The 20/80 duty is
kept so the pulse energy per cycle and the harmonic content stay as close to
the blade as the emitter allows; the factors are only valid for this drive.

## Test flow (per part)

1. **Offset** — DC read with the emitter off. A near-0 V input is treated as
   "no sensor" (with the usual 5 s wake-up poll and the "Is a sensor
   loaded?" question). The offset is **recorded, not gated**:
   `OFFSET_GATE_ENABLED = False` with a placeholder 0.3–3.0 V band, because
   TP443's offset page was not available. Fill in the real band and enable
   the gate (high-side fail-fast + settled low-side verdict, as on the 405).
2. **5 Hz / 20 %** — `PIN,33` · `PWM,FREQ,5` · `PWM,DUTY,20` · `PWM,ON`;
   sync validated (5 cycles, mean within ±0.05 Hz); adaptive peak
   stability (5 consecutive cycle-to-cycle robust-peak deltas ≤ 0.100 mV,
   three attempts, 30 s deadline); **20 measurement cycles** (4 s); pk-pk
   median, polarity, SNR. `PWM,OFF`.
3. **18 Hz / 20 %** — same, `PWM,FREQ,18`; sync validated with the mean
   within ±0.18 Hz; **36 measurement cycles** (2 s) judged as **4 blocks of
   9 cycles** (below). Skipped when the 5 Hz capture never stabilized (the
   part is already a FAIL — TP443 note 1 says retest).
4. **Settled offset** re-read (the value the row records; the insertion-time
   read is `offset_initial_v`).
5. **Verdict** — always: stable window at both drives, POSITIVE polarity at
   both, SNR ≥ 1.5 at both. With the gate on: specs 1–3 and the spec-4 tray
   flag on the legacy-equivalent values.

### 18 Hz and the sample grid

At 1000 SPS an 18 Hz period is 55.56 samples, so consecutive cycles sample
the pulse at different phases and the per-cycle robust peak (median of the
top 10 % samples) jitters by far more than the 0.1 mV stability threshold —
the first simulator run never "stabilized" at 18 Hz. Two consequences:

- **Sync validation** judges the **mean** cadence of the validation cycles
  against TP443's tolerance and gives every single cycle a one-sample
  allowance (`f²/rate` = 0.32 Hz at 18 Hz), because a single period can only
  read 55 or 56 samples (18.18 / 17.86 Hz).
- **Stability** is judged on **blocks of 9 cycles** (`STABILITY_CYCLES_PER_BLOCK`):
  9 × 55.56 = exactly 500 samples, after which the phase pattern repeats, so
  the per-block robust peak is repeatable. `stability_analysis.block_sync_cycles`
  keeps every 9th rising edge of the real sync and the unchanged peak-delta
  rule runs on that; the final pk-pk/polarity/SNR math re-finds the real 36
  cycles inside the 4 selected blocks. The 18 Hz telemetry columns
  (`stab18_capture_cycles`, `stab18_measurement_cycles`, stabilization cycle)
  therefore count **blocks**. 5 Hz (200 samples per cycle, phase-tolerant
  estimator) and the 10 Hz reference stay at one cycle per block.

## Calibration: the two fixture factors

Same recipe as the 405 M22 lot-500 calibration, once per frequency:

1. Measure 30–50 representative parts on the legacy fixture (all at 5 Hz,
   then all at 18 Hz — TP443 note 2) and the same parts here (the batch CSV
   already holds `sensitivity_5hz_raw_mv`, `sensitivity_18hz_raw_mv`, and
   `ratio_18_over_5_raw` per part).
2. Per part and frequency, `legacy / raw`; take the median and the
   regression-through-origin slope; they should agree within a percent or
   two, with a tight spread — that is `K_5` and `K_18`.
3. On withheld parts check the corrected 5 Hz value, the corrected 18 Hz
   value, the corrected ratio, **and** identical pass/fail decisions around
   1.2 V, 0.72 V, 0.70 and 1.30. If the ratio residuals are not random the
   emitter's pulse shape is interacting with detector-specific response and
   one factor per frequency is not enough — that is the deciding empirical
   question of the electrically pulsed approach.
4. In `eltec_449m18_esp32_tester.py` set `SENSITIVITY_LEGACY_EQUIVALENT_FACTORS`,
   bump `SENSITIVITY_CALIBRATION_ID`, set `SENSITIVITY_GATE_ENABLED = True`
   in the **same** change, and replay the lot (the CSV keeps raw and
   corrected columns side by side).

`stability_calibration.py capture --sensor-id X --frequency 5|18` collects
known-good peak-delta evidence per drive if the 0.100 mV threshold in
`stability_settings.json` needs revisiting for this model.

## Result screen and CSV

- Banner: `PASS`, `PASS · CALIBRATION PENDING` (amber explanation card), or
  `FAIL`; an amber **TP443 SPEC 4 — MEASURE THE WHOLE TRAY** card when the
  ratio is ≤ 0.72 or a sampled part failed.
- Details tiles: Offset · 5 Hz (equiv.) · 18 Hz (equiv.) · Ratio 18/5 ·
  Polarity, with raw values, factors, limits, SNR and measured sync
  frequency underneath; both captures on their own scope.
- Failure modes: the standard taxonomy plus **`FT - Frequency tracking
  (18/5 Hz ratio)`** for spec 3.
- Batch CSV (`Documents/Eltec_449M18_Test_Results/449m18_esp32/449m18_esp32_lot_<lot>.csv`):
  `sensitivity_mv` = raw 5 Hz (shared name), then
  `sensitivity_5hz_*` / `sensitivity_18hz_*` (raw, legacy-equivalent,
  factor, minimum, outcome), `ratio_18_over_5_raw`, `ratio_18_over_5_corrected`,
  `ratio_min/max`, `ratio_outcome`, `ratio_tray_100_percent_flag`,
  `sensitivity_calibration_id`, `sensitivity_gate_enabled`, polarity and
  SNR per frequency, measured sync per frequency, reference audit columns,
  `stab5_*` and `stab18_*` stability telemetry, `measure_attempts`,
  `skip_count`. Sibling `*_attempts.csv` as in the other models.
- Snapshots (`Capture waveforms`, and automatically for an unstable drive):
  one PNG (+ cycle CSV sidecars) per drive, suffixed `_5hz` / `_18hz`.

## Firmware

**v3.2 or newer is required** — the backend refuses older builds at
connect. v3.2 adds `PWM,DUTY,<pct>` (1–99 %, boot default 50 %, not
persisted) and `pwm_duty` in `STATUS?`; the backend programs `PWM,FREQ` and
`PWM,DUTY` before every `PWM,ON`. Nothing is sent for the other models, and
a port open resets the board to 10 Hz / 50 %, so the 405 M22 and 406 MCA
paths are unchanged. Flash with `python3 Arduino/Eltec/flash_firmware.py`.

The AIN1 reference gate is **disabled** (`REFERENCE_GATE_ENABLED = False`,
op-amp crosstalk, same as the other models); its machinery runs at 10 Hz /
50 % and is unit-tested with the flag forced on. Battery monitoring is
disabled as on the whole fixture.

## Simulator cases

`Random good sensor`, `Known good`, `Low sensitivity`, `Wrong polarity`,
`Low offset`, `High offset`, plus the TP443 cases `Borderline sensitivity`
(1.224 V / 0.881 V → ratio ≈ 0.72: passes spec 3, flags the tray),
`Never stabilizes`, `Low 18 Hz sensitivity`, `Low 18 Hz ratio` (0.55),
`High 18 Hz ratio` (1.45). Each case is generated per frequency with a 20 %
ON pulse response; with the gate off they all PASS except polarity and
stability, with the gate on they exercise every spec.

## Tests

```bash
python3 -m unittest discover -s single_detector_rig/m449m18/tests    # from the repo root
```

`test_449m18_integration.py` (policy, evaluation gate off/on, hardware
sequence, simulator, CSV, snapshots, launchers, Tk smoke test),
`test_esp32_backend.py` (5/18 Hz + duty programming, v3.2 requirement),
`test_stability_analysis.py` (5 Hz cadence, 18 Hz quantization allowance,
block sync), `test_stability_calibration.py`.

## Running it standalone

Normally started from the unified selector. Standalone:
`run_eltec_449m18_esp32_tester.cmd` (Windows) /
`run_eltec_449m18_esp32_tester.sh` (Xubuntu); optional desktop entries via
`install_windows_launcher.ps1` / `install_xubuntu_launcher.sh`. Logs go to
`%LOCALAPPDATA%\eltec-449m18-esp32\launcher.log` /
`~/.local/state/eltec-449m18-esp32/launcher.log`.
