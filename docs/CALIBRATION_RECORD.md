# Calibration record — every production constant and where it came from

This file is the **single authoritative home** for every number that decides a
PASS or FAIL on the Eltec test rig: the value, the line of code that holds it,
how it was derived, and the state of each gate per sensor model. The model
READMEs explain the *mechanics*; this file records the *evidence*.

**Rule for changing any constant here:** update the code, update this file,
and add a dated entry to [`CHANGELOG.md`](../CHANGELOG.md) — in the **same
commit**. For sensitivity factors also bump the model's
`SENSITIVITY_CALIBRATION_ID` so every CSV row says which calibration produced
it.

Last reconciled against the code: **2026-09-02**.

---

## 1. Gate status at a glance

| Gate | 405 M22 | 406 MCA | 449 M18 |
| --- | --- | --- | --- |
| Offset band | **ON** 0.80–3.00 V | **ON** 0.3–1.2 V | OFF (placeholder 0.3–3.0 V, TP443 page not available) |
| Emitter-off noise | **ON** ≈429 µV pk-pk, 15 % window rule | not part of the 406 test | not part of the TP443 test |
| Sensitivity | **ON** factor 4.30 (lot 500) | **ON** factor 1.582 (lot 520) | **OFF — CALIBRATION PENDING** (K_5, K_18 not derived) |
| Near-limit band | ±0.10 mV raw → PASS + warning | ±0.10 mV raw → PASS + warning | n/a until the gate is on |
| Polarity | POSITIVE required | POSITIVE required | POSITIVE required at both frequencies (assumed — verify on the bench) |
| SNR | ≥ 1.5 | ≥ 1.5 | ≥ 1.5 at both frequencies |
| Stability (peak delta) | 0.500 mV, 5 deltas, 10 cycles, 3 attempts, 60 s | 0.100 mV, 10 deltas, 20 cycles, 3 attempts, 20 s | 0.100 mV, 5 deltas, 20 cycles @5 Hz / 4 blocks of 9 @18 Hz, 3 attempts, 30 s |
| Reference unit (AIN1 emitter health) | **OFF** since 2026-08-17 (op-amp crosstalk) | **OFF** since 2026-08-24 (same) | **OFF** (same) |
| Battery | **OFF** (no battery on AIN7 since 2026-08-12) | OFF in the unified copy | OFF |
| Emitter drive | 1 Hz / 50 % (reference phases 10 Hz) | 10 Hz / 50 % | 5 Hz then 18 Hz, **20 %** duty (reference 10 Hz / 50 %) |
| ADS1256 front end | boot default: gain 1, buffer OFF (±5 V) | app sends `FE,V19`: gain 2, buffer ON (±2.5 V) | boot default (±5 V) |
| Minimum firmware | v2.0 | v1.7 (legacy standalone rigs stay on **v1.9**) | **v3.2** |

Code locations: `single_detector_rig/<model>/eltec_<model>_esp32_tester.py`
(gates, limits, factors), `<model>/esp32_backend.py` (drive frequency, duty,
gate pin, minimum firmware), `<model>/stability_settings.json` (peak-delta
threshold). The three model directories are deliberate near-copies — see the
copy-per-model policy in [`ENGINEER_HANDOVER.md`](ENGINEER_HANDOVER.md).

**Array rig (50 positions, ACCES USB-AIO16-64MA DAQ) — model 40623, TP120** — see §4b:

| Gate | 40623 array |
| --- | --- |
| Offset band | **ON, PROVISIONAL** 0.3–1.2 V (TP120); < 0.05 V on a loaded socket = D; ≥ 4.9 V = HO (railed). Only HO/railed fail fast at insertion; LO/D on the settled reading. |
| Noise | **PENDING** — pin-level limits `None` (measured and recorded only; TP120's 10.0–37.9 mV are DMM readings behind amplifier 9000232 + rectifier-hold 9000272). Structural rules when limits exist: 15 % window rule (high), median rule (low). |
| Sensitivity / polarity | **not implemented** (no emitter board) |
| Front end | unity-gain buffer per position → DAQ 0–5 V single-ended, 1000 scans/s per channel, oversample 3 with the first conversion dropped |

Code: `array_rig/m40623/array_analysis.py` (limits, verdict model),
`array_rig/m40623/eltec_40623_array_tester.py` (DAQ and timing constants).

---

## 2. Model 405 M22 (TP412, 1 Hz)

Tester: `single_detector_rig/m405m22/eltec_405m22_esp32_tester.py`.
Test procedure document: **TP412** (offset, sensitivity/polarity at 1 Hz, noise).
Detailed mechanics: [`m405m22/README.md`](../single_detector_rig/m405m22/README.md).

### 2.1 Constants

| Constant | Value | Where | Provenance |
| --- | --- | --- | --- |
| `OFFSET_MIN_V` / `OFFSET_MAX_V` | 0.80 / 3.00 V | tester ≈ l.124 | TP412 offset band. Needs firmware ≥ v2.0 (unbuffered gain-1 front end reads linearly to ~5 V). |
| `SENSOR_OFFSET_MIN_PLAUSIBLE_V` | 0.05 V | tester ≈ l.315 | Near-0 V float = empty slot / dead buffer. Polled for `OFFSET_WAKE_TIMEOUT_S` = 5 s before deciding (2026-08-17: lot-500 part 500-44 was wrongly rejected as an empty slot before the poll existed). The old 3.5 V high-side "no sensor" rail was removed 2026-08-13: a railed ~5 V AIN0 **is** the high-offset failure signature. |
| Offset policy | high-side fail-fast; low side judged on a **settled re-read** after the sensitivity capture | tester | 2026-08-17, lot 500: offsets rise for tens of seconds after insertion — 35 of 48 parts read 0.15–1.1 V *below* their settled legacy value on the insertion read. CSV keeps both: `offset_initial_v` and `offset_v`. Settled offsets match the old fixture to +0.018 ± 0.032 V (2026-08-18 re-run). |
| `NOISE_LEGACY_PP_LIMIT_MV` | 300 mV | tester ≈ l.387 | TP412's scope limit, read **behind the legacy bench amplifier**. |
| `NOISE_LEGACY_AMPLIFIER_GAIN` | 4000 (nominal, **not** used for the verdict) | tester ≈ l.386 | Sticker gain of the TL084-based legacy amplifier. |
| `NOISE_EFFECTIVE_CHAIN_FACTOR` | **700** | tester ≈ l.390 | See §2.2. |
| `NOISE_PP_LIMIT_MV` | 300 / 700 ≈ **0.429 mV (429 µV) pk-pk at the sensor pin** | tester ≈ l.391 | Derived. Displayed as red cutoff lines at ±214 µV. |
| `NOISE_DECIMATION_FACTOR` | 20 (1000 → 50 SPS, passband flat to ~22 Hz) | tester ≈ l.392 | Chosen 2026-08-13 (fixture floor 5.6 % of limit at 20:1; ≥10:1 suffices; raw is unusable at 36 %). Since **2026-08-20** the decimator is a Kaiser windowed-sinc anti-alias FIR (`stability_analysis.decimate_antialiased`, ≥ 60 dB stopband from 28 Hz) — the original boxcar's −13 dB sidelobes folded 60 Hz mains to 10 Hz at only −16 dB (41 % phantom energy on the interference-heavy `test-22` capture). Same passband and timeline; all nine archived raw captures replay to identical verdicts. **2026-08-31:** precision — the Kaiser's passband edge is 22.15 Hz but its −3 dB corner is **24.4 Hz** (22.17 Hz was the boxcar's; with the detrend the verdict's −3 dB band is 0.852–24.4 Hz), and the FIR now seats on **real edge context** (0.31 s per side: quiet-wait tail + extra streamed samples, archived in the capture NPZ) instead of reflection padding, which had let out-of-band interference into the first/last judged window at only ~11–21 dB. No-context replays (all pre-existing captures) are bit-identical. |
| Judged band | ≈ **0.85–22 Hz** | analysis | Per-window detrend = high-pass −3 dB at 0.85 Hz; decimation = low-pass at 22.17 Hz (`engineer_tools/filter_response_analysis.py`, 2026-08-20). |
| `NOISE_WINDOW_S` | 1 s windows | tester ≈ l.385 | |
| `NOISE_MAX_OVER_FRACTION` | **0.15 → PASS iff ≤ 3 of 20 windows exceed the limit** | tester ≈ l.410 | Tightened from 20 % on **2026-08-17** (lot 500): the one part the legacy fixture failed for noise (500-44, 496 mV on the old scope) measured 4/20 windows over and was slipping through at 20 %; every other part was 0/20 except 500-3's isolated 2-window environmental spike, which must stay tolerated. 4 over fails, 2 passes. **Single-part anchor — refine with more known-noisy parts.** |
| `NOISE_CAPTURE_SECONDS` | 20 s | tester ≈ l.411 | Fixed for now; adaptive length is future work. |
| `NOISE_SOAK_CAPTURE_SECONDS` / `NOISE_SOAK_MAX_OVER_FRACTION` | 60 s / allowed count held at the **same absolute 3 (3 of 60)** | tester ≈ l.425 | **2026-08-18**: 500-44's burst noise is intermittent (4/20 over on 08-17, 0/20 on 08-18) while clean parts take environmental transients (500-1 hit 3/20). No threshold separates them; observation time does. Operator-selectable per part on the load step. |
| Quiet wait | adaptive: `NOISE_WAIT_BEFORE_CAPTURE_S` 3 s min, `NOISE_WAIT_MAX_S` 20 s, start when 2 consecutive 1 s mean deltas ≤ limit/4 (≈107 µV/s) | tester ≈ l.441 | 2026-08-13. Can only delay the capture, never fail the part (`noise_baseline_settled` column records a deadline start). |
| Per-window detrend | each 1 s window judged against its own least-squares baseline (mean + slope) | `stability_analysis.detrend_window_segments` | 2026-08-13 — residual DC settling cannot inflate the pk-pk (mirrors the legacy AC-coupled amplifier). |
| `NOISE_CLIP_LIMIT_V` | 98 % of the ±5 V input range | tester ≈ l.445 | A window whose RAW samples touch the clip counts as over-limit; the FIR's ~0.3 s ring-through from a railed window can also push its neighbour over (emergent, not a coded rule). |
| Raw-capture auto-save | whenever any window goes over, PASS or FAIL | tester | 2026-08-18. Files under `noise_captures/lot_<lot>/`; see [`DATA_MAP.md`](DATA_MAP.md). |
| `SENSITIVITY_LEGACY_EQUIVALENT_FACTOR` | **4.30** | tester ≈ l.452 | See §2.3. |
| `SENSITIVITY_CALIBRATION_ID` | `405m22_tp412_lot500_pairwise_v1` | tester ≈ l.459 | Stamped on every CSV row. |
| TP412 filter limits (legacy-scope mV) | -625: 5.99–11.98 · -628: 4.22–8.44 · -629: 4.92–9.84 | tester ≈ l.141–147 | TP412; blackened tube + extra -25B optics, 10 cm from the 500 K blackbody. Raw limit = legacy limit / 4.30. Over-max also fails. |
| `SENSITIVITY_RAW_NEAR_LIMIT_HALF_WIDTH_MV` | ±0.10 mV raw (≈ ±0.43 mV legacy) | tester ≈ l.458 | Within the factor's margin of error → the part **PASSES** with a re-measure suggestion (`PASS · NEAR LIMIT`, `sensitivity_gate_outcome` = NEAR LIMIT in the CSV). Was a `RETEST / QUARANTINE` verdict until **2026-08-25**. For -625 the band is 1.29–1.49 mV raw around the 1.393 mV centre; no lot-500 part fell inside it. |
| `MIN_SIGNAL_TO_NOISE_RATIO` | 1.5 (≈3.5 dB) | tester | Inherited from the 406 build; guards against an undriven emitter. Not yet tuned on real data. |
| Polarity | POSITIVE | tester | Every Eltec model tested so far peaks while the emitter is on. |
| `peak_delta_threshold_mv` | **0.500 mV** | `m405m22/stability_settings.json` | Relaxed from 0.100 mV on **2026-08-12**: the inherited 406 limit kept these high-gain parts timing out as Unstable. Provisional — review percentiles with `stability_calibration.py`. |
| `DUT_STABILITY_CONFIRMATION_DELTAS` / `SENSITIVITY_MEASUREMENT_CYCLES` / attempts / `STABILITY_TIMEOUT_S` | 5 / 10 / 3 / 60 s | tester ≈ l.466 | 1 s cycles: qualification + 10 cycles needs ≥ ~16 s, so 60 s instead of the 406's 20 s. |
| `REFERENCE_PEAK_DELTA_THRESHOLD_MV` / `REFERENCE_TOLERANCE_PERCENT` | 0.250 mV / ±25 % | tester ≈ l.486 | Reference unit's own gate; deliberately independent of the DUT threshold. Inert while the gate is off. |
| `REFERENCE_CALIBRATION_SCHEMA_VERSION` | 4 | tester ≈ l.512 | See §5. |
| `PWM_FREQUENCY_HZ` / `REFERENCE_PWM_FREQUENCY_HZ` / duty | 1 Hz / 10 Hz / 50 % | `esp32_backend.py` l.66–68 | TP412 specifies responsivity at 1 Hz. The AIN1 reference unit is a **406MCA sensor**, so reference phases drive its qualified 10 Hz (told by the user 2026-08-12). Only 1 and 10 Hz are accepted. |
| `PWM_GPIO` | 33 | `esp32_backend.py` | Emitter gate moved D25 → D33 on 2026-08-25 (firmware v3.1). The app sends `PIN,33` after every connect. |
| `MINIMUM_FIRMWARE_VERSION` | (2, 0, 0) | `esp32_backend.py` l.74 | Needs the gain-1 unbuffered front end. |
| `STREAM_MAX_MICRO_GAPS` / `STREAM_MAX_MISSING_SAMPLES` / `REFERENCE_READING_STREAM_RETRIES` | 3 / 20 samples / 2 | tester ≈ l.504 | Bounded USB micro-gap tolerance (2026-08-12); gaps are refilled from firmware timestamps (2026-08-13). Anything else (duplicates, overruns, > 2 % rate error) rejects the capture. |
| `BATTERY_MONITORING_ENABLED` | False | tester ≈ l.282 | No battery on AIN7 since the 2026-08-12 rewiring (6.5 V → emitters only, 9 V → sensors). Re-enable when the sensor battery is measurable on AIN6 (≥ 4:1 divider). |
| `REFERENCE_GATE_ENABLED` | **False** | tester ≈ l.297 | See §2.4. |

### 2.2 Why the noise limit is 300 mV ÷ 700, not ÷ 4000

TP412's 300 mV pk-pk is read on a scope behind the legacy bench amplifier
whose paperwork says ×4000 — that would put the pin-level limit at 75 µV, the
figure the app used for a few hours on 2026-08-13. A **same-part
cross-measurement** the same evening disproved it: the resident good part
showed ~150–200 mV on the legacy station (GW Instek GDS-1054B, CH2 at
100 mV/div, ±150 mV cursors, roll mode 5 s/div at 20 Sa/s — itself a ~20 SPS
low-frequency view, closely matching this rig's decimation) while this rig
measured ~240–270 µV at the pin over the same 50 s / 20 SPS view. A true
×4000 chain would have painted ~1 V (ten divisions); it demonstrably did not.
The **effective** end-to-end factor is therefore ~620–830×; **700** was
adopted. Two further scope photos (50 and 100 mV/div, spans ~150–250 mV) are
consistent with 600–800.

The 2026-08-20 passband analysis (`engineer_tools/filter_response_analysis.py`)
showed no physically sensible 1 Hz-centred band-pass can turn 4000 into 700
(best achievable ratio 0.235 vs the required 0.175, order-1 models), so the
gap was attributed to a **real gain difference** (10:1 probe setting, an
amplifier range switch, or a wrong nameplate) — not a bandwidth effect.
Partially superseded by the 2026-08-31 addendum below: the response has now
been measured, "wrong nameplate" and "10:1 probe" are effectively cleared,
and the gain-scale conclusion itself stands.

**2026-08-31 addendum — the amplifier's frequency response was measured
directly** (11-point sine sweep, ~1 mV injected; output amplitudes 0.1 Hz →
340 mV, 0.2 → 1.16 V, 0.5 → 3.06 V, 1 → 4.14 V, 2 → 4.10 V, 5 → 2.56 V,
10 → 1.14 V, 20 → 336 mV, 50 → 63 mV, 100 → 17 mV, 200 → 5 mV):

- The amp is a **band-pass peaking ×4140 at ~1.4 Hz** (−3 dB 0.46–4.1 Hz,
  ≈2 real poles per side — two cascaded AC-coupled stages; gain ≥ 4000 only
  over 0.86–2.2 Hz; |H| = 700 only at 0.15 and 13.2 Hz). The **nameplate
  ×4000 is its real midband gain** (+0.5 dB) *if* the ~1 mV injection level
  was accurate; a 10:1 probe is disfavoured on arithmetic (it over-explains).
- **Bandwidth still cannot produce 700**: weighting the archived pin spectra
  by the measured response gives effective factors 3400–6000 (clean lot-500
  parts ≈ 4200), not 620–830 — the 08-20 conclusion survives its order-1
  modelling limitation.
- The measured **shape** validates against the archive: scaled by a flat
  ≈3.2× it reproduces all 14 archived capture verdicts, 500-44's 496 mV
  legacy reading (predicts 492 mV) and the 150–200 mV good-part reading.
  The residual is a **frequency-flat ~3–6× in-service scale**; prime
  suspect: **source-impedance loading** (the high-impedance pyro element
  into the amp's input network — the sweep drove it from a stiff
  generator), which would make nameplate, sweep and 700 simultaneously
  consistent.
- **Open action (updated):** record where the sweep was injected (amp input
  terminals vs the fixture's sensor socket) and how the 1 mV was verified
  (measured at the node vs generator dial; amplitude vs pk-pk). Then repeat
  1 Hz and 0.2 Hz through a ~10–50 pF series capacitor or ~1 GΩ series
  resistor emulating the pyro source (expect ≈14 dB drop at 1 Hz under the
  loading hypothesis). Most informative extra sweep points: 0.03, 15,
  500 Hz. The 08-13 cross-measurement's probe ratio and amp range-switch
  position are unrecorded — capture them next time the legacy station is up.
- **Decision (2026-08-31): the rig is not required to replicate the amp's
  spectral response.** The acceptance criterion is verdict-level agreement
  with the legacy fixture (parts that pass there pass here, parts that fail
  for noise there fail here), which the flat-band ÷ 700 architecture meets
  on the lot-500 comparison and the archived captures. The scalar 700 and
  the judged band stay; the per-tone divergence (more lenient than the amp
  below ~13 Hz, stricter above, sub-0.85 Hz discounted) is accepted and
  documented here.

Bench facts behind the choice of band (2026-08-13, raw data in
`noise_experiments/`, see [`DATA_MAP.md`](DATA_MAP.md)): fixture floor with
the part removed = **4.2 µV** median window pk-pk at 20:1 (≈1 % of the limit —
the electronics are not the problem); a desk fan near the rig adds ~19 Hz and
~200 Hz microphonic peaks plus mV transients through the part (pyroelectric
elements are piezoelectric — **no fans near the fixture during noise tests**);
covering the sensor lowered the reading only ~13 %, so the 0.5–5 Hz noise is
intrinsic to the part or its bias chain.

### 2.3 Sensitivity factor 4.30 — the lot-500 pairwise calibration (2026-08-17)

50 sensors of lot 500 were measured on the legacy fixture and on this rig in
the same order; raw data in [`analysis/405M22_Data/`](../analysis/405M22_Data/)
(`405M22_Data_old_fixture.xlsx`, `405M22_Data_new_fixture.csv.xlsx`).

- 46 usable pairs; per-part legacy/raw ratio: **median 4.2973**,
  regression-through-origin slope 4.2853 (agreement within 0.3 %), sd 4.4 %,
  range 3.67–4.66 → factor set to **4.30**.
- Replaying the lot reproduces every old-fixture verdict: 500-10 (the only
  low-sensitivity part) computes to 4.03 mV legacy-equivalent vs 4.08 old and
  fails; the closest passer 500-15 computes to 6.52 — the exact old reading;
  500-41 computes to 10.88 vs 11.30 old, inside the 11.98 mV maximum; parts
  27/33/37 fail high offset and 44 fails noise on both fixtures. The only extra
  failure was 500-19's SNR gate on 08-17, which the 08-18 re-run showed was a
  bad capture (it passes cleanly).
- **Independent re-run 2026-08-18:** median 4.324, repeatability sd 2.5 %.
- The near-limit band (§2.1) is the factor's margin of error expressed in raw
  mV.

### 2.4 Reference gate disabled — op-amp channel crosstalk (2026-08-17)

The fixture's buffer/voltage-follower stage is a **dual op-amp with no channel
isolation**, so the sensor under test couples into the AIN1 reference channel:
the reference reading collapsed from ~4.94 mV to ~0.30 mV with a DUT loaded
(lot 500, 2026-08-17 10:50) and tracked whichever part was inserted. No
recalibration can fix that, so `REFERENCE_GATE_ENABLED = False` skips the
reference phase entirely (3-step test: offset → noise → sensitivity). Verdicts
are unaffected — everything is measured on AIN0; only automatic emitter-health
monitoring is lost. **Operator rule meanwhile: several low-sensitivity
failures in a row → suspect the emitter before condemning parts.**

To re-enable: install the per-channel-isolated buffer board, set the flag to
`True`, run **Calibrate reference unit** fresh (expect a baseline near ~5 mV).
The contaminated baseline was archived as
`reference_sensor_calibration_crosstalk_contaminated_20260817.json.bak` in the
405 results folder. The gate code is kept and unit-tested with the flag forced
on. The same crosstalk disabled the 406 MCA (2026-08-24) and 449 M18 gates.

---

## 3. Model 406 MCA (10 Hz)

Tester: `single_detector_rig/m406mca/eltec_406mca_esp32_tester.py` (the v6.1
policy build). Detailed mechanics:
[`m406mca/README.md`](../single_detector_rig/m406mca/README.md).

| Constant | Value | Where | Provenance |
| --- | --- | --- | --- |
| Healthy offset | 0.3–1.2 V (`OFFSET_MIN_V` / `OFFSET_MAX_V`) | vendored engine `eltec_rig/v1_single_sensor/eltec_406mca_tester.py` l.46 | 406MCA production limit (carried from the LabJack era). |
| `SENSOR_OFFSET_MIN_PLAUSIBLE_V` / `_MAX_PLAUSIBLE_V` | 0.05 / 2.5 V | tester ≈ l.208 | Outside = no sensor / no buffer. |
| Frequency setup | 10.0 ± 0.1 Hz (first three cycles validated) | tester / backend | 406MCA production limit. |
| Filter sensitivity minimums (legacy-scope mV) | -3: 25 · -27: 25 · -266: 30.9 · -273 + blackened tube: 2.3 · **-284 + extra -6 + blackened tube: 4.0 (default)** | tester | 406MCA production limits. |
| `SENSITIVITY_LEGACY_EQUIVALENT_FACTOR` | **1.582** | tester ≈ l.227 | Provisional paired-fixture factor from **lot 520** (`SENSITIVITY_CALIBRATION_ID = lot_520_paired_v1`), derived during the v6.1 build (July 2026). **Documentation gap:** the lot-520 pair data and statistics are not in this repository — locate and file them under `analysis/` when found. |
| Near-limit band | ±0.10 mV raw → PASS + warning | tester ≈ l.232 | For the default -284 setup: raw centre 4.0 / 1.582 = 2.53 mV → `< 2.43` FAIL, `2.43–2.63` PASS · NEAR LIMIT, `> 2.63` clean PASS. RETEST/QUARANTINE verdict removed 2026-08-25. |
| `MIN_SIGNAL_TO_NOISE_RATIO` | 1.5 | tester ≈ l.222 | |
| Polarity | POSITIVE | | |
| `peak_delta_threshold_mv` | **0.100 mV** | `m406mca/stability_settings.json` | v6 era; still flagged for broader qualification with known-good/bad parts. |
| Stability policy | 10 consecutive deltas, 20 measurement cycles, 3 attempts (a kick discards the window), 20 s deadline | tester ≈ l.239 | v6.1 policy (July 2026), adopted into the unified app. |
| `REFERENCE_PEAK_DELTA_THRESHOLD_MV` / `REFERENCE_TOLERANCE_PERCENT` | 0.250 mV / ±25 % | tester ≈ l.254 | Tolerance was ±10 % in v6; widened to ±25 % in v6.1 (older ±10 % files load under the wider window). Inert while the gate is off. |
| Historical reference baseline | 5.3432 mV (limits 4.8089–5.8775 mV at ±10 %) | results folder JSON | Recorded on firmware v1.7 with the 6 V SLA fixture; **historical only** — the crosstalk finding makes any AIN1 baseline on the current buffer board untrustworthy. |
| `REFERENCE_CALIBRATION_SCHEMA_VERSION` | 2 | tester ≈ l.256 | See §5. |
| `REFERENCE_GATE_ENABLED` | **False** since 2026-08-24 | tester ≈ l.207 | Same crosstalk as §2.4. |
| ADS1256 front end | gain 2, buffer ON, ±2.5 V (`WAVEFORM_INPUT_RANGE_V` 2.5) | backend sends `FE,V19` after `IDN?` on firmware ≥ v2.1 and hard-verifies `FE?` | Every 406 threshold was qualified on this front end (firmware v1.9). A port open resets the board to the v2.0 front end, which is why the app re-applies it on every connect. **Legacy standalone 406MCA rigs must stay on firmware v1.9** — v2.0+ halves the ADC resolution (LSB 298 → 596 nV) and changes the noise floor. |
| `PWM_FREQUENCY_HZ` / duty / `PWM_GPIO` | 10 Hz / 50 % / 33 | `esp32_backend.py` l.40 | |
| `MINIMUM_FIRMWARE_VERSION` | (1, 7, 0) | `esp32_backend.py` l.45 | v1.7 fixed the ADS1256 configuration read-back; older streams could run at the 30 kSPS reset default. |
| `BATTERY_MONITORING_ENABLED` | False (unified copy, 2026-08-18) | tester ≈ l.185 | Constants kept for a legacy rig with the 6 V SLA on AIN7: block ≤ 5.8 V, warn ≤ 6.0 V, fault outside 3.0–7.5 V. |

---

## 4. Model 449 M18 (TP443 frequency tracking, 5 Hz + 18 Hz) — CALIBRATION PENDING

Tester: `single_detector_rig/m449m18/eltec_449m18_esp32_tester.py` (added
2026-08-26). Detailed mechanics and the derivation recipe:
[`m449m18/README.md`](../single_detector_rig/m449m18/README.md).

| Constant | Value | Where | State |
| --- | --- | --- | --- |
| `SENSITIVITY_GATE_ENABLED` | **False** | tester ≈ l.387 | Every verdict is stamped `CALIBRATION PENDING`; readings and the raw ratio are recorded, TP443 limits are not enforced. |
| `SENSITIVITY_LEGACY_EQUIVALENT_FACTORS` | K_5 = 1.0, K_18 = 1.0 (placeholders) | tester ≈ l.388 | An electrically pulsed emitter modulates less at 18 Hz than at 5 Hz, so the raw 18/5 ratio is **not** the TP443 ratio; one factor per frequency is needed, derived like §2.3 (30–50 parts on both fixtures, all at 5 Hz then all at 18 Hz per TP443 note 2). K_18 will be larger than K_5. |
| `SENSITIVITY_CALIBRATION_ID` | `449m18_tp443_uncalibrated_v0` | tester ≈ l.392 | Bump when the factors land, in the same change as the flag. |
| TP443 limits (to be applied on legacy-equivalent values) | spec 1: ≥ 1.2 V at 5 Hz · spec 2: ≥ 0.72 V at 18 Hz · spec 3: ratio 18/5 in 0.70–1.30 · spec 4: ratio ≤ 0.72 or a sampled failure ⇒ measure the tray 100 % | tester | TP443 page 6. |
| `OFFSET_GATE_ENABLED` / band | False / 0.3–3.0 V placeholder | tester ≈ l.138 | TP443's offset page was not available; fill in and enable (high-side fail-fast + settled low-side verdict as on the 405). |
| `POLARITY_GATE_ENABLED` / `EXPECTED_POLARITY` | True / POSITIVE at both drives | tester ≈ l.399 | Assumption from the other models — **confirm on the bench**. |
| `MIN_SIGNAL_TO_NOISE_RATIO` | 1.5 at both frequencies | tester ≈ l.359 | |
| `peak_delta_threshold_mv` | 0.100 mV | `m449m18/stability_settings.json` | Inherited; revisit once real raw amplitudes are known. |
| Stability policy | 5 deltas, 3 attempts, 30 s; 20 measurement cycles at 5 Hz (4 s); 36 cycles at 18 Hz judged as **4 blocks of 9 cycles** | tester ≈ l.406 | 18 Hz = 55.56 samples per cycle at 1000 SPS, so single-cycle peaks jitter; 9 cycles = exactly 500 samples, after which the phase pattern repeats. Sync validation judges the mean cadence (±0.05 Hz at 5 Hz, ±0.18 Hz at 18 Hz) with a one-sample allowance per cycle. |
| Drive | 5 Hz then 18 Hz at **20 % duty** (`PWM_DUTY_CYCLE_PERCENT` 20); reference 10 Hz / 50 % | `esp32_backend.py` l.72–74 | The legacy fixture's 20/80 blade. Requires firmware **v3.2** (`PWM,DUTY`); `MINIMUM_FIRMWARE_VERSION` = (3, 2, 0). **v3.2 is compiled but not yet flashed or bench-verified** (2026-08-28). |
| Reference gate / battery gate | both off | tester ≈ l.322 / l.307 | As on the other models. Schema v4. |

**Open work before production:** flash and bench-verify firmware v3.2; derive
K_5 / K_18; fill the TP443 offset band; confirm polarity on real parts; revisit
the 0.100 mV threshold.

---

## 4b. Model 40623 (TP120, 50-position DAQ array) — CALIBRATION PENDING

Tester: `array_rig/m40623/eltec_40623_array_tester.py`; limits and verdict
model: `array_rig/m40623/array_analysis.py`. Test procedure document:
**TP120 rev W** ([`TP120(40623).pdf`](TP120(40623).pdf)) — sensitivity &
polarity (3 Hz chopper, not implemented: no emitter board), noise, offset
check. Mechanics: [`m40623/README.md`](../array_rig/m40623/README.md).
Added 2026-09-02.

### 4b.1 Constants

| Constant | Value | Where | Provenance / state |
| --- | --- | --- | --- |
| `OFFSET_MIN_V` / `OFFSET_MAX_V` | 0.3 / 1.2 V | `array_analysis.py` | TP120 rev W "40623 Offset Check": test box 9000054, **+8 V supply, 100 kΩ source resistor**, DMM 20 V scale, "let detectors stand for ten to fifteen minutes, if needed". **PROVISIONAL**: the array PCB's supply/loading must be confirmed to match 9000054 before these transfer without a correction. |
| `OFFSET_SETTLE_DELTA_V` | 0.05 V | `array_analysis.py` | TP120 sensitivity pages: "wait until reading does not shift more than ± 0.05 V". Applied as a recorded warning (early vs settled reading), never a verdict. |
| `OFFSET_DEAD_V` | 0.05 V | `array_analysis.py` | A loaded socket under this = D (dead / no output). Same value as the 405's wake-up floor; bench-tunable. Empty vs loaded at ~0 V is the technician's call at lock time (the rig cannot tell them apart). |
| `OFFSET_RAIL_V` | 4.9 V (0.98 × 5 V range) | `array_analysis.py` | A railed part = HO (405 lesson: never a wiring error). |
| Offset policy | HO / railed fail fast at insertion; LO / D judged on the **settled** reading (mean of the last 2 s of the capture); insertion read recorded as `offset_initial_v` | tester | Carried from the 405 M22 lot-500 observation (offsets settle upward for tens of seconds). |
| `NOISE_LEGACY_PP_LIMIT_LOW_MV` / `_HIGH_MV` | 10.0 / 37.9 mV | `array_analysis.py` | TP120 rev W "40623 Noise": fixture 9000233 (50 sockets, 5×10 switch box), **±5 V** supply, **under vacuum**, 5 min stabilisation, 15–20 s settle per position, amplifier box **9000232** → rectifier-hold **9000272** (Reset until < 1.0 mV, release, **≥ 60 s** hold) → DMM 200 mV DC. "Noise level must be between 10.0 mV and 37.9 mV." A LOW limit exists (dead crystal/FET). **These are DMM readings behind an amplifier of unknown gain/passband — never pin-level.** |
| `NOISE_LEGACY_CHAIN_FACTOR` | **None** | `array_analysis.py` | Not derived. Derivation = the 405's §2.2/§2.3 recipe: the same parts on the legacy 9000233 fixture (DMM readings per position) and on this rig; `engineer_tools/array_noise_parity.py` pairs them and proposes the factor. |
| `NOISE_PP_LIMIT_LOW_MV` / `_HIGH_MV` | **None** | `array_analysis.py` | = legacy limit ÷ chain factor once derived. With `None` every noise verdict is `NO_LIMIT` (measured, recorded, never a failure); tiles show the value and "no limit yet". |
| `NOISE_MAX_OVER_FRACTION` | 0.15 (structural default) | `array_analysis.py` | Copied from the 405 M22's lot-500 rule. **Re-decide with the paired lot.** |
| Low-side rule | MEDIAN window pk-pk below the low limit → NOISE_LOW | `array_analysis.py` | Structural choice: a dead crystal is quiet in every window; the median ignores one environmental bang. Re-decide with the paired lot. |
| `NOISE_DECIMATION_FACTOR` / `NOISE_WINDOW_S` | 20 / 1.0 s | `array_analysis.py` | The single rig's pipeline unchanged (1000 → 50 SPS Kaiser FIR, 621 taps, 310-sample edge context, per-window detrend): judged band ≈ 0.85–22 Hz. Frozen oracle: `tests/golden_noise_reference.py` (from `single_detector_rig/m405m22/stability_analysis.py` at d7526b5). |
| `NOISE_CAPTURE_SECONDS` | 60 s (20 s engineering option) | tester | TP120's ≥ 60 s hold → 60 one-second windows. |
| `NOISE_STABILISATION_S` | 300 s (skippable, actual wait recorded per row) | tester | TP120: "Let detectors stand for five minutes". |
| Adaptive quiet wait | 3–20 s, 2 blocks within 0.1 mV | tester | Carried from the 405; `NOISE_BASELINE_SETTLE_DELTA_MV = 0.1` is the 405's derived value rounded — only affects wait time, never a verdict. |
| `CALIBRATION_STATUS` / `CALIBRATION_ID` / `VERDICT_STATUS` | PENDING / `40623_array50_daq_PENDING` / PROVISIONAL | `array_analysis.py` | Stamped on every CSV row and every raw capture. Bump the id when the limits are derived. |
| DAQ range / rate / oversample | 0–5 V (code 2, 76.3 µV/LSB) / 1000 scans/s / 3 (first conversion dropped) | tester | See §6. Recorded per row (`daq_range_code`, `daq_oversample`, `daq_drop_conversions`, `daq_scan_rate_hz`, `daq_actual_timer_hz`). |

### 4b.2 Derivation plan for the noise limits

1. Bench spike (`daq_bench_probe.py`): instrument floor per channel in the
   judged band (cal-mode GROUND), the -HG check with a known voltage, which
   conversion slot is unsettled, 60 s stream integrity, crosstalk — numbers
   into §6.
2. Paired lot: 30–50 parts (include known-noisy and known-dead ones)
   measured on the legacy 9000233 fixture per TP120 (DMM reading per
   position, under vacuum) and on this rig (60 s capture, same day); type
   the legacy readings into `legacy_readings.csv` (`sensor_id, position,
   legacy_noise_mv`) and run `engineer_tools/array_noise_parity.py`. It
   reports the median ratio and regression-through-origin slope for the
   worst-window and median-window metrics, replays alternative bands from
   the saved `.npz`, and proposes `NOISE_PP_LIMIT_LOW/HIGH = 10.0/37.9 ÷
   factor`.
3. Require identical pass/fail decisions to the legacy fixture on the lot,
   set the constants, bump `CALIBRATION_ID`, update this section and the
   §1 mini-table, CHANGELOG entry — one commit.

### 4b.3 Open hardware questions (flagged, not blocking)

1. PCB supply and loading: TP120 measures offset at +8 V with a 100 kΩ source
   resistor but noise at ±5 V. Which does the PCB implement? The offset
   limits transfer directly only if the loading matches 9000054.
2. Vacuum: the legacy noise spec is under vacuum; the paired derivation must
   be done in the conditions this rig actually uses.
3. Amplifier 9000232 gain and passband are unknown; a nominal gain, if found,
   could seed a provisional limit (flagged nominal-derived — remember §2.2:
   the 405's sticker gain was not its effective gain).
4. Whether the DAQ is the `-HG` high-gain factory variant (changes the volts
   scaling; the probe's known-voltage check detects it).
5. Multiplexer crosstalk at 1000 scans/s is unspecified by the datasheet
   (−60 dB at 500 kS/s) — measured by the probe.

---

## 5. Reference-calibration schema history (`reference_sensor_calibration.json`)

| Schema | Introduced | Meaning | Accepted by |
| --- | --- | --- | --- |
| v1 | v6 (early) | timed capture / median metric | nobody — rejected so old and new metrics never mix |
| v2 | v6 (2026-07) | adaptive five-reading baseline (robust-peak stability, five fresh cycles averaged, five readings averaged, repeatable within 10 %) | 406 MCA (`REFERENCE_CALIBRATION_SCHEMA_VERSION = 2`) |
| v3 | 2026-08-12 | baseline taken on the firmware v2.0 front end (gain 1, buffer off) | superseded the same day |
| v4 | 2026-08-12 (evening) | additionally stores `reference_pwm_hz: 10` — reference readings driven at the 406MCA reference unit's 10 Hz (a pyroelectric response at 1 Hz is several times larger and not comparable) | 405 M22, 449 M18 |

Older schemas are deliberately rejected so a fresh **Calibrate reference
unit** run is forced after each change. All of this is inert while the
reference gates are off (§2.4).

---

## 6. Fixture and firmware constants that the limits depend on

| Item | Value | Notes |
| --- | --- | --- |
| ADC | ADS1256, 24-bit, **1000 SPS** single channel, 2.5 V reference | `volts = code × (2·VREF/PGA) / 8388607` |
| Front end (boot default, v2.0+) | PGA 1, input buffer OFF, ±5 V, LSB ≈ 0.6 µV | 405 M22 and 449 M18. Higher PGA is not available for headroom — the 405's 0.8–3.0 V offset clips any gain above ~1.6. |
| Front end (`FE,V19`) | PGA 2, buffer ON, ±2.5 V, linear only to AVDD − 2 V = 3.0 V | 406 MCA; offsets above ~2.4 V clip. |
| ADC input noise | ~4–8 µV rms at 1000 SPS / gain 1 (≈30–50 µV pk-pk per 1 s window raw; ≈5–10 µV after 20:1) | Why the noise verdict is band-limited. |
| Emitter gate | GPIO33 (D33) → dual-MOSFET trigger module, direct wire | D25 until 2026-08-25. `PIN,<n>` retargets at runtime; the apps send `PIN,33`. |
| Power | 6.5 V battery → emitters only; 9 V battery → sensor buffers; grounds common | Isolated 2026-08-12 — this fixed the emitter-induced spike. Neither battery is monitored (AIN7 divider unused; plan: AIN6 with ≥ 4:1 divider). |
| Serial | 500000 baud ASCII over CP210x USB | Windows driver grants only a 512-byte receive queue; see the stream-reliability notes in `m405m22/README.md`. |
| Bench board (2026-08-28) | DOIT ESP32 DEVKIT V1, COM3 on the Windows laptop, running firmware **v3.1**; v3.2 compiled, not yet flashed | `python Arduino/Eltec/flash_firmware.py --check` reports what a board runs. |
| **Array rig DAQ** (2026-09-02) | ACCES USB-AIO16-64MA DAQ-PACK, VID 0x1605 PID 0x8145, serial 40E68DEE0D501728, `AIOUSB.dll` 2.4.0.0 (64-bit, System32); 16-bit SAR, one ADC behind two multiplexer stages, 500 kS/s aggregate, **no anti-alias filter**, range per group of 4 channels, contiguous scan only, firmware loaded from the host at plug-in | `array_rig/m40623/daq_bench_probe.py info`. Self-calibration (`ADC_SetCal :AUTO:`) supported and run at every connect. |
| Array rig acquisition | 0–5 V range (76.3 µV/LSB), CH0–CH49 single-ended, 1000 scans/s per channel, oversample 3 with the first conversion dropped → 200 kS/s aggregate, 400 KB/s USB; callback buffers 64 000 B × 32 | Bench spike numbers (instrument floor per channel in the judged band, actual timer Hz, 60 s rate error, pool events, crosstalk, -HG check, unsettled slot) **pending** — recorded here when the spike is run. |
| Array PCB | 5 × 10 positions, one unity-gain buffer per position, row order onto CH0–CH49 (row 1 = CH0–9) | Supply / source-resistor loading vs TP120's fixtures 9000054 (+8 V, 100 kΩ) and 9000233 (±5 V, vacuum): **to be confirmed** (§4b.3). |

---

## 7. Retired values (do not resurrect by accident)

| Was | Now | When / why |
| --- | --- | --- |
| 405 noise limit 75 µV (300 mV ÷ 4000) | 429 µV (÷ 700) | 2026-08-13 evening — same-part cross-measurement (§2.2) |
| 405 noise window allowance 20 % (4 of 20) | 15 % (3 of 20) | 2026-08-17 — lot 500 anchor 500-44 |
| 405 noise decimation: 20:1 boxcar | 20:1 Kaiser anti-alias FIR, same passband | 2026-08-20 — aliasing (41 % phantom on `test-22`) |
| 405 fixed 3 s quiet wait | adaptive 3–20 s quiet wait + per-window detrend | 2026-08-13 — test-10 passed with settling counted as noise |
| 405 DUT peak-delta 0.100 mV | 0.500 mV | 2026-08-12 — high-gain parts timed out Unstable |
| 405 high-side "no sensor" rail at 3.5 V | removed; railed ~5 V = HO failure | 2026-08-13 — real high-offset parts were blocked as "no sensor" |
| 405 offset verdict on the insertion read | settled re-read after sensitivity | 2026-08-17 — offsets rise for tens of seconds after insertion |
| `RETEST / QUARANTINE` verdict for the ±0.10 mV band (both models) | `PASS · NEAR LIMIT` + re-measure suggestion | 2026-08-25 — "if it passes it passes"; old CSVs with RETEST rows are shown as failures in the summary (they were quarantine records) |
| 406 reference tolerance ±10 % | ±25 % | v6.1 (July 2026) |
| Reference / battery gates ON | OFF | 2026-08-17 / 08-24 (crosstalk) and 2026-08-12 (no battery on AIN7) |
| Emitter gate GPIO25 | GPIO33 | 2026-08-25, firmware v3.1 |
| Reference readings at 1 Hz (405) | 10 Hz, schema v4 | 2026-08-12 — the reference unit is a 406MCA sensor |
