# Eltec Test Rig (ESP32) status

Last updated: 2026-08-18

## Unified test rig: one app for every sensor version (2026-08-18)

The rig now has ONE production application, **`tech_app/eltec_rig/`** ("Eltec
Test Rig"): a selector GUI with a **sensor version dropdown** (Model 405 M22
and Model 406 MCA today; the registry in `sensor_versions.py` is built to
grow) that launches the chosen model's qualified tester unchanged. Each
model keeps its own test flow, thresholds, per-batch filter-setup dropdowns,
CSV format, and results folders. The last-used version is remembered. Runs
on Windows and Xubuntu with launchers and opt-in desktop-icon installers for
both (`run_eltec_rig_tester.cmd|.sh`, `install_windows_launcher.ps1`,
`install_xubuntu_launcher.sh`; identity `Eltec Test Rig` /
`com.eltec.test-rig.desktop`; logs in `%LOCALAPPDATA%\eltec-rig` /
`~/.local/state/eltec-rig`). See `tech_app/eltec_rig/README.md`.

- The bundled models are COPIES: `m405m22/` (= `405m22_esp32`, byte-identical
  apart from the removed prompt.txt) and `m406mca/` (= `v6_1_esp32` + the
  changes below). The original `tech_app/405m22_esp32/` and
  `tech_app/v6_1_esp32/` directories stay untouched as the qualified
  standalone builds; new work happens in `eltec_rig/`.
  `v1_single_sensor/eltec_406mca_tester.py` is vendored inside `eltec_rig/`
  so the package is self-contained.
- **Firmware v3.0** (`Arduino/Eltec/Eltec.ino`, snapshot in
  `versions/Eltec_v3_0/`) is the unified baseline: functionally identical to
  v2.1 (single-channel streaming, `PWM,FREQ`, runtime `FE,...` front-end
  switch) with the IR-telescope dual-channel code REMOVED. Compiled clean
  (290,208 bytes, 22% flash); **not yet flashed** — the bench board still
  runs v2.1, which the unified app fully supports.
- **406MCA on v2.1+/v3.0 firmware**: `m406mca/esp32_backend.py` now sends
  `FE,V19` right after the `IDN?` handshake and hard-verifies the `FE?`
  read-back, restoring the gain-2 buffered front end (±2.5 V) every 406MCA
  threshold was qualified on (each port open resets the board to the v2.0
  front end, which is why it re-applies on every connect). A mismatch
  refuses the connection. On v1.x boards (legacy standalone 406MCA rigs on
  v1.9) no `FE` command is sent. Legacy `v6`/`v6.1` apps must still NOT be
  used on v2.x/v3.x boards — only the unified app selects the front end.
- **406MCA battery gate disabled in the unified copy**
  (`BATTERY_MONITORING_ENABLED = False` in `m406mca`, mirroring the 405 M22
  build): the unified fixture has no battery on the AIN7 divider (6.5 V →
  emitters only, 9 V → sensors). Header shows "Battery: not monitored"; all
  battery machinery is kept intact for re-enable (planned: AIN6 divider).
  `m406mca/stability_calibration.py` got the same flag.
- **Tests**: unified glue 17 (1 display-only skip); `m405m22` 165 (5
  display/Windows-only skips); `m406mca` 100 (3 display-only skips), including
  the added v2.0 wrong-front-end rejection. POSIX installer/exclusive-tty
  checks pass on Xubuntu; Windows-only launcher checks skip there.
  Original `405m22_esp32` (165 OK) and `v6_1_esp32` (95, same two
  environment failures) re-verified untouched.

## IR telescope split into its own workspace (2026-08-18)

Everything IR-telescope-related moved to
**`C:\Users\JoseCastelblanco\Documents\Eltec_IR_Telescope`** (its own git
repo, initial commit `859fcb6`): the `ir_telescope` app (49 tests passing),
firmware **v2.2** as `Arduino/Eltec_v2_2/` (the only build with
`STREAM,START,BOTH`), dual-capable copies of `live_waveform.py` /
`esp32_rig_readout.py` under `tools/`, the wiring doc, and the telescope
sections of this file as its `STATUS.md`. That directory was already the
telescope app's session-data root; existing session folders were preserved
and gitignored. In THIS repo, `tech_app/ir_telescope/` was deleted and the
dual-channel features were stripped from `Arduino/Eltec/live_waveform.py`
(no more `--channel both`/`--overlay`) and `esp32_rig_readout.py` (no more
`dual`; `noisecmp`, `--fe`, and `--freq` kept). The board must be flashed
with the telescope workspace's v2.2 for telescope sessions and back to the
rig firmware afterwards (v2.2 is also a drop-in for this rig's apps).

Workspace note: this repository is being renamed from `Eltec406mca` to
**`Eltec_TestRig`** (it tests more than the 406 MCA now). Only the folder
name changes; all in-repo paths are relative.

## Stream-corruption root cause + fix, and the ACTUAL board firmware (2026-08-17, afternoon)

Every 405 M22 capture began failing stream integrity ("timestamp gaps +
duplicate timestamps + host/firmware count mismatch") after the morning's
telescope session. Root-caused and FIXED the same day — it was neither the
firmware nor the telescope work:

- **The bench board on COM3 actually runs v2.1** (live `IDN?` check), not the
  v2.2 this file previously claimed. That is fine: the 405 M22 app needs only
  >= v2.0, and v2.1 contains no telescope code at all. Re-flash v2.2 only when
  the telescope app (which requires `STREAM,START,BOTH`) is next used — it is
  a proven drop-in for 405 M22 testing (single-channel path byte-identical).
- **Root cause:** the Windows CP210x driver grants only a **512-byte receive
  queue** (~16 ms of stream; the app's 1 MiB `SetupComm` request is silently
  ignored — measured via `GetCommProperties`), and Windows 11 **on battery**
  throttles the backgrounded tester GUI (EcoQoS + ~15.6 ms timer coarsening;
  USBHUB3 event 196 began actively power-targeting the CP210x on 2026-08-14).
  The sleep-paced drain thread then wakes too late and the driver queue wraps:
  drops + re-delivered duplicates. Headless captures (CLI, and the app's own
  backend without the GUI, with the 1 Hz emitter chopping) were 100% clean,
  exonerating board, cable, firmware, and EMI.
- **Fix (tech_app/405m22_esp32/esp32_backend.py):** drain thread now blocks in
  the driver read (no timers) at raised priority; the process opts out of
  Windows power throttling and timer coarsening; driver `CE_RXOVER` overflow
  flags are captured so the failure dialog attributes host stalls instead of
  blaming the cable. USB selective suspend disabled (AC+DC) in the power plan.
  Full detail: tech_app/405m22_esp32/README.md "part 2".
- **Reference anomaly explained + gate disabled (same day, later):** the
  4.94 → 0.30 mV reference collapse is CHANNEL CROSSTALK — the fixture's
  buffer/voltage-follower is a dual op-amp with no channel isolation, so the
  DUT couples into AIN1 and the reference reading tracks whichever sensor is
  loaded. No recalibration can fix that, so `REFERENCE_GATE_ENABLED = False`
  now skips the reference phase entirely (3-step test: offset → noise →
  sensitivity; verdicts unaffected — all measured on AIN0). Re-enable the
  flag and recalibrate when the per-channel-isolated buffer board is
  installed. The crosstalk-contaminated calibration JSON and the partial
  lot_500 CSV were archived with dated suffixes; batch 500 restarts at
  500-1. Operator rule meanwhile: several LS failures in a row → suspect
  the emitter first.

## IR telescope firmware verified (2026-08-17)

**Firmware v2.2 was flashed and verified on the bench rig** (Windows host,
COM3) for the telescope session; the board has since been returned to /
verified running **v2.1** (see the section above). **One firmware family runs
everything**: v2.2 satisfies the 405 M22 app's `>= v2.0` gate, and
single-channel streaming is byte-identical to v2.1, so no re-flash is needed
for 405 M22 testing whenever v2.2 is on the board for the telescope.

Hardware testing immediately caught a **real bug in the first v2.2 build**. The
mux-cycling order was the throughput-optimised one from the datasheet — change
the mux and `SYNC` first so the next channel's settling overlaps the SPI read,
then `RDATA`. On this board that is wrong: `SYNC` restarts the converter and the
output register is not safe to read across it. Proof, same physical input: AIN0
is pristine single-channel (sample-to-sample step max 6 mV, no full-scale reads
in 8000 samples) but in dual mode showed **4.8 V single-sample jumps and hit
full scale 7 times in 8474 pairs**. Reading the conversion *before* touching the
mux costs ~50 µs of overlap per pair (397 → 379 SPS) and removes it entirely:
step max 8 mV, zero full-scale reads, zero glitches on both channels. The
`ESP32_memory.md` entry says explicitly not to optimise it back.

Measured on the rig: **379 pairs/s per channel, pair skew 1.32 ms**, 0 timing
gaps over 20 s. The host now measures the delivered rate at session start and
derives every filter coefficient from it instead of the nominal 424.

**Open fixture issue, not a code problem.** Both channels carry very large slow
drift — 135 mV (AIN0) and 170 mV (AIN1) rms below 0.3 Hz — while the crossing
band (1–8 Hz) is quiet at 5.8 and 7.7 mV and above 20 Hz is ~1.2 mV. That drift
leaks past the trigger's highpass and pushes the threshold to ~400 mV, which
costs sensitivity to everything. The `BAT?` reading also wandered 1.04 → 3.71 V
across runs and both sensor DC levels climbed run to run, so the **sensor supply
is the first thing to check**; thermal settling is the second. `check` now
reports this split explicitly so drift is not mistaken for a noisy site.

Note: the telescope's detectors are **not** the 406MCA/405 M22 parts the rest of
this repo tests. The app therefore has no absolute noise expectation and never
compares against those specs — it measures the floor on site and expresses
thresholds relative to it. Whether the threshold is low enough for real targets
is settled by a walk test.

## IR telescope (new, 2026-08-14)

A separate application in `tech_app/ir_telescope/` reads **both** detectors of a
two-element IR telescope at once and reports which way a target crossed, how
fast, and how much to trust the answer. AIN0 is the right-hand detector; AIN1
sits 15 mm to its left and 3 mm below it.

This needed a firmware change: **v2.2** adds `STREAM,START,BOTH`, which
interleaves AIN0 and AIN1 by cycling the ADS1256 mux and emits one `P,` line
per pair at ~424 SPS per channel. It is purely additive — single-channel
streaming is byte-for-byte unchanged, so v2.2 is a drop-in replacement for v2.1
on the 405 M22 rig. Compiled clean; **not yet flashed or run against hardware.**

`Arduino/Eltec/live_waveform.py` gained `--channel both` (and `--overlay`) so
the two detectors can be watched on one scope view, and
`esp32_rig_readout.py` gained a `dual` command that characterises the link
(delivered rate, pair skew, per-channel level).

The app covers the commissioning-kit workflow: `check` (rig health + measured
noise floor), `calibrate` (learns the direction sign from three walks),
`live --walk-test` (beeps plus L/R ground-truth keys, scored in the report),
`record` (headless logging), `replay` (re-analyse a session with new settings)
and `report`. Everything runs with `--simulate` and no hardware.

Three findings from building it, all caught by tests rather than in the field:

- A one-pole highpass in the trigger does not reject slow thermal drift, so the
  detector latches onto the background and misses everything real. A trailing
  boxcar fixes the drift but grows a phantom copy of every target one window
  later. The build uses a delay-matched two-pole exponential baseline.
- Releasing an event on an instantaneous 2 sigma test can essentially never
  succeed — Gaussian noise crosses 2 sigma on 4.6% of samples, so half a second
  of unbroken quiet never accumulates and events run to the 30 s limit. The
  release runs on a smoothed envelope.
- "How long the trigger stayed up" is dominated by filter decay and grows with
  the log of amplitude, so it cannot distinguish a 10 ms glitch from a 250 ms
  crossing. The half-peak width of the band-passed waveform can (0.08 s vs
  0.96 s), and that is what the app reports and gates on.

**Direction sign is a per-installation calibration, not a constant.** It
depends on whether the optics invert the image and on which side of the
telescope you stand. Defaults are documented guesses; `calibrate` turns it into
a measurement, and every report says in bold when it has not been run.

49 tests (`python -m unittest discover -s tests`), no hardware needed.

## Executive summary

The active application is the unified selector in `tech_app/eltec_rig/`.
It launches the qualified Model 405 M22 or Model 406 MCA workflow while
preserving each model's thresholds, result namespace, and test sequence. The
unified fixture uses the ESP32/ADS1256 rig and runtime-selectable firmware
front end documented above. The older standalone v6 and v6.1 directories
remain historical/qualification references rather than the fleet launcher.

V6 now checks and stabilizes the reference unit before it reads the DUT. There
is no fixed reference warm-up delay. The reference check begins streaming as
soon as PWM turns on, uses the same robust-peak stability rule as the DUT, and
then averages five fresh cycles. An invalid reference blocks AIN0 entirely and
invalidates the calibration until the emitter/reference unit is recalibrated.

The active production launcher is now the unified rig described above. Its
Model 406 MCA copy is based on the v6.1 policy, with the runtime-qualified
front-end selection and unified-fixture battery behavior documented above.
The old standalone v6/v6.1 builds remain available for historical comparison.
In the earlier headless run, the v6 suite
discovered 80 tests (77 passed and 3 display-only tests skipped), while v6.1
discovered 93 tests (90 passed and 3 display-only tests skipped). V5 remains
available as historical context and has not been overwritten.

The reference calibration values documented later in this file are historical.
On the latest filesystem check, neither the v6 nor v6.1 reference calibration
JSON existed, so the production workflow correctly requires calibration before
it will record another DUT.

The isolated **Model 405 M22** evaluation build in `tech_app/405m22_esp32/`
was upgraded on 2026-08-12 to follow document **TP412** and the rewired
fixture. The emitter-induced spike is FIXED in hardware: the 6.5 V battery
now powers the emitters only and a separate 9 V battery powers the sensors
(battery monitoring is disabled until the sensor battery is measurable on
AIN6). The build adds the TP412 emitter-off NOISE test, the full 0.8-3.0 V
offset band (requires new firmware v2.0), and the TP412 -625/-628/-629
filter set. A late-evening rework the same day reordered the per-part steps
(offset fail-fast -> noise -> sensitivity), replaced the noise settle
detection with a fixed 3 s quiet wait, made the noise displays relative
(range around mean with red cutoff lines), added a step progress bar,
and taught the stream validator to tolerate bounded USB micro-gaps (the
recurring "1 timestamp gaps (~6 missing samples)" abort). On 2026-08-13
the micro-gaps got timestamp-based refilling (they had also been faking
10.2 Hz PWM-sync failures), and the noise limit was RECALIBRATED: TP412's
300 mV sits behind the legacy amp chain, whose EFFECTIVE factor a same-part
cross-measurement pinned at ~700x (not the nominal 4000x), so the app now
gates ~429 uV pk-pk at the sensor pin on a band-limited (1000 -> 50 SPS)
trace. Also on 2026-08-13 (after real high-offset parts were wrongly
blocked): the OFFSET gate now runs FIRST — before the reference gate —
and the high-side "no sensor" plausibility rail is GONE. A railed AIN0
(~5 V ADC full scale) is the real high-offset signature and records an
immediate HO FAIL; a high-offset part can no longer trip or invalidate
the reference calibration, and any reference-gate failure re-checks AIN0
and condemns the part instead of the fixture when it reads high. See the
"Model 405 M22 (1 Hz) TP412 build" section below. The 406MCA v6/v6.1 applications
are unchanged, and **406MCA rigs must stay on firmware v1.9** (v2.0 changes
the ADC front end).

**Firmware v2.0 is now FLASHED and verified on the bench rig** (2026-08-12,
Windows host, COM3) — see "Firmware version archive and the v2.0 flash"
below. Every known firmware build is now frozen under
`Arduino/Eltec/versions/`, including a reconstructed **v1.9** for reverting
406MCA rigs. The 405 M22 application now runs on **both Xubuntu and
Windows**; only the launcher differs.

## Model 405 M22 (1 Hz) TP412 build

`tech_app/405m22_esp32/` adapts the v6.1 application to the Model 405 M22
high-gain pyroelectric detector, whose responsivity is specified at 1 Hz,
and now follows TP412 (offset, sensitivity/polarity, noise). Sensitivity
PASS/FAIL remains disabled until a comparison batch yields the fixture
calibration factor, so it is still not a qualified production gate.

### Fixture power (changed 2026-08-12 — this fixed the emitter spike)

- **6.5 V battery -> emitters ONLY** (through the MOSFET module).
- **9 V battery -> sensors** (DUT buffer and the AIN1 reference supply).
- Neither battery is measurable on the legacy AIN7 divider (a 9 V pack
  through the ~2:1 divider sits at ~4.5 V on the pin, and the v2.0
  unbuffered input loads the divider anyway). The app's battery gate is
  **disabled** (`BATTERY_MONITORING_ENABLED = False`); the header shows
  "Battery: not monitored" and no `BAT?` reads are issued.
- **TODO (hardware):** measure the sensor battery on **AIN6** with a >=4:1
  divider (e.g. 300k/100k), add the firmware channel, and re-enable the
  gate. The user plans to step the sensor supply down to ~8 V (or use an
  8 V battery) so noise levels stay comparable to TP412's +8 V bench supply.

### Test flow per sensor (TP412) — REORDERED AGAIN 2026-08-13 (offset first)

```text
STEP 1/4 DUT offset (emitter off, plain DC read) -> out-of-band =
         IMMEDIATE FAIL (reference gate, noise AND sensitivity are all
         skipped; HO/LO/D preselected). A railed ~5 V AIN0 IS the
         high-offset failure signature — only a near-0 V float still
         blocks as "no sensor" (the high-side 3.5 V rail is gone).
STEP 2/4 reference gate (PWM on @ 10 Hz, AIN1) -> PWM off. On ANY
         reference failure (out-of-window OR unstable) AIN0 is re-read:
         above 3.0 V the PART records the HO failure and the reference
         calibration is spared; only an in-band re-read lets the failure
         block testing and invalidate the calibration.
STEP 3/4 NOISE TEST (emitter off): adaptive quiet wait (3-20 s,
         streamed, discarded) -> 20 s capture -> windowed pk-pk verdict
         -> noise FAIL = IMMEDIATE FAIL (sensitivity skipped, N - Noisy)
STEP 4/4 PWM on @ 1 Hz -> stability + 10-cycle sensitivity/polarity
         capture -> PWM off -> PASS/FAIL
```

Why offset moved first (2026-08-13, user report with screenshots): real
high-offset parts read 5.000 V on AIN0 and were being blocked by the old
3.5 V plausibility rail as "No sensor detected", or — when the reference
gate ran first — produced "+913%" reference spikes whose suppression
pattern (which required AIN0 <= 3.5 V) did not match, so the app kept
demanding a reference recalibration for a condition the bad part itself
caused. Swapping in a good part always restored the reference, proving
the part was the culprit. Now the offset gate fail-fasts before the
reference unit is ever streamed, and the interference suppression is
direction-agnostic (high, low, or unstable reference) gated only on the
re-read offset being above the 3.0 V TP412 limit.

The measuring screen now shows this ladder as a **step progress bar**
("STEP 3/4 — NOISE (EMITTER OFF)", bar filling continuously with tick marks
at step boundaries; within a step it only ever moves forward, and the status
line still narrates the details/retries). Noise moved BEFORE sensitivity so
a noisy part is rejected in ~23 s instead of first spending up to three 60 s
stabilization attempts; an Unstable part consequently records its real
measured noise report now (not a SKIPPED placeholder).

- **Offset**: TP412 band `0.80-3.00 V` gated in full, and since 2026-08-13
  it is the FIRST per-part step. Firmware v2.0 (PGA gain 1, ADS1256 input
  buffer off) reads AIN0 linearly up to the ~5 V full scale; the waveform
  range is +/-5 V. The high-side plausibility rail (3.5 V) was REMOVED —
  real high-offset parts rail at ~5.000 V and must record an HO failure,
  not a "no sensor" block. Only `SENSOR_OFFSET_MIN_PLAUSIBLE_V = 0.05 V`
  remains: a near-0 V float (missing/unseated part or broken buffer) is
  still a hardware error, so an empty rig can never record a phantom FAIL
  row.
- **Sensitivity/polarity**: same adaptive rule (5 consecutive robust-peak
  deltas within the DUT threshold, 10 measurement cycles, 3 attempts, 60 s
  deadline); polarity must be POSITIVE; SNR >= 1.5 gate still active.
  **DUT stability threshold relaxed 0.100 -> 0.500 mV (2026-08-12):** the
  inherited 406MCA 0.100 mV limit was too strict for these high-gain parts
  and kept timing out as Unstable. The value lives in
  `tech_app/405m22_esp32/stability_settings.json`
  (`peak_delta_threshold_mv`) and applies to the DUT on AIN0 only. **The
  AIN1 reference unit is unaffected** — it has always used its own
  `REFERENCE_PEAK_DELTA_THRESHOLD_MV = 0.250` constant, which deliberately
  ignores the tracked DUT setting so relaxing the part-under-test rule can
  never relax the fixture's own gate. Still provisional pending real
  captures.
- **Filter setups** (operator picks per batch): TP412 `-625` (5.99-11.98
  mV), `-628` (4.22-8.44 mV), `-629` (4.92-9.84 mV), all legacy-scope mV
  with blackened tube + extra -25B optics. The gate is DISABLED until the
  **comparison batch** (~50 sensors measured on the legacy fixture and on
  this rig in the same order) yields the calibration factor; raw
  sensitivities are recorded unscaled (factor 1.0). The over-max branch is
  already implemented and inert.
- **Noise test** (reworked 2026-08-12 late evening; LIMIT CORRECTED
  2026-08-13): runs BEFORE the sensitivity capture, right after the offset
  gate, with the emitter off. The old settle detection (robust-peak deltas
  + mean-near-offset + 60 s deadline, restart on breach) was REMOVED — with
  the emitter off there is no signal to stabilize, only the noise being
  measured — and replaced by a fixed **3 s quiet wait**
  (`NOISE_WAIT_BEFORE_CAPTURE_S`; streamed so the live scope stays alive,
  but discarded). Then **20 s** are captured, **band-limited by a 20:1
  boxcar average** (1000 -> 50 SPS, ~22 Hz passband, 60 Hz mains ~-16 dB),
  and cut into 1 s windows: **PASS iff <= 20% of windows (4 of 20) exceed
  75 uV pk-pk at the sensor pin** — the user clarified on 2026-08-13 that
  TP412's 300 mV was read behind the legacy bench amplifier's x4000 gain,
  so the pin-level limit is 300/4000 = 0.075 mV. Windows whose RAW samples
  touch the ADC clip level count as over-limit (averaging must not hide a
  railed input). TP412 itself allows no excursion over the limit; the 20%
  allowance is a deliberate relaxation for these very sensitive parts. All
  noise thresholds are provisional pending the comparison batch — the
  decimation factor stands in for the legacy amplifier's unknown passband.
  Noise metrics are recorded in the `noise_*` CSV columns (`noise_settle_s`
  records the fixed wait; new `noise_analysis_rate_hz` documents the
  band-limited rate); a failing noise capture auto-saves a PNG snapshot and
  FAILS THE PART IMMEDIATELY — the sensitivity capture is skipped
  (`N - Noisy` preselected). The "skipped when Unstable" rule is gone
  (noise is measured before stability is ever attempted). The 20 s duration
  is a constant for now — adapting it to the part's noise level is listed
  future work.
- **Can the ADS1256 read 75 uV? (2026-08-13 analysis)** Quantization: yes —
  at PGA 1 / +/-5 V one LSB is 10 V / 2^24 ~= 0.6 uV (~125 codes across the
  band). Higher PGA gain is NOT available for headroom because the DUT's
  0.8-3.0 V DC offset clips any gain above ~1.6 single-ended. The real
  limiter is the ADC's own input noise: ~4-8 uVrms at 1000 SPS / gain 1,
  i.e. ~30-50 uV pk-pk per 1 s window — half the 75 uV budget before the
  part contributes, plus raw-bandwidth (~500 Hz) mains/EMI the legacy
  band-limited amplifier never saw (the bench part read 1.17 mV pk-pk
  raw). After the 20:1 boxcar the white ADC floor drops by sqrt(20) to
  ~1-2 uVrms => ~5-10 uV pk-pk per window (<= ~13% of the limit), which
  makes the 75 uV gate feasible ADC-wise.
- **Bench A/B noise experiment (2026-08-13, three 60 s emitter-off
  captures; raw data + tooling under
  `~/Documents/Eltec_405M22_Test_Results/405m22_esp32/noise_experiments/`):**
  - **Part OUT (fixture floor):** median 1 s-window pk-pk 27 uV raw,
    **4.2 uV at 20:1** (5.6% of the 75 uV limit), 4.2 uVrms raw — matches
    the datasheet prediction almost exactly. 60 Hz peak only ~1 uV/rtHz.
    The electronics are NOT the problem; every large signal seen in
    part-in runs enters through the sensor itself.
  - **Part IN + desk fan ON:** median window pk-pk 543 uV raw / 275 uV at
    20:1, worst window 40 mV(!), distinct spectral peaks at ~19 Hz and
    ~195-213 Hz plus mV-scale slow transients. The peaks vanish with the
    part removed AND with the fan off: pyro elements are piezoelectric, so
    fan vibration reads microphonically, and moving air/IR produces the
    slow excursions. Production guidance: no fans on/near the fixture
    during noise tests.
  - **Part IN, fan OFF, charger unplugged (quiet room):** median window
    pk-pk 240 uV raw -> 139 uV at 20:1 -> 84 uV at 100:1; spectrum
    dominated by 0.5-5 Hz (peak ~14 uV/rtHz at 2 Hz), i.e. the part's own
    low-frequency noise, ~33x above the fixture floor at 20:1.
  - **Part IN, SENSOR COVERED (same quiet conditions):** median 122 uV at
    20:1 — only ~13% below the uncovered run, spectrum still 2-4 Hz
    dominated. Ambient IR/air was therefore a minor contributor; the
    low-frequency noise is intrinsic to the part (or its bias/battery
    chain).
  - **SAME-PART CROSS-MEASUREMENT on the legacy fixture (user photo,
    2026-08-13 evening) — the x4000 figure is NOT the effective gain.**
    The user put this part on the legacy station: GW Instek GDS-1054B,
    CH2 at 100 mV/div, cursors at -152/+148 mV (the 300 mV band), roll
    mode 5 s/div / 1000 pts / 20 Sa/s (so the legacy view is itself a
    ~20 SPS low-frequency view — closely matching our 50:1 decimation),
    scope frequency counter "<2 Hz" (matching our 2-4 Hz dominance). The
    trace spans ~150-200 mV and PASSES. Our same-day pin capture of the
    same part shows ~240-270 uV over a 50 s / 20 SPS view: a true x4000
    chain would display ~1 V (10 divisions) — it demonstrably does not,
    so the EFFECTIVE chain factor is ~620-830x (10:1 probe setting, an
    amp range switch, or midband-vs-passband gain are the usual
    suspects; the amp is TL084-based, gain set by resistors, so the chip
    marking cannot confirm it). Adopted `NOISE_EFFECTIVE_CHAIN_FACTOR =
    700` => pin-level limit `NOISE_PP_LIMIT_MV = 300/700 ~= 0.429 mV`
    (~429 uV, red cutoffs at ~+/-214 uV). With that factor both fixtures
    agree: the part uses ~60% of the allowed band on each, and the part
    is GOOD. Single-part derivation — refine with 2-3 more parts and by
    checking the legacy scope's CH2 probe setting (1X vs 10X) and any
    gain switch on the amplifier box.
  - **Decimation choice CONFIRMED at 20:1** (~22 Hz): floor at 5.6% of the
    limit (>=10:1 already suffices; 1:1 raw is unusable at 36%), the
    part's dominant 0.5-5 Hz noise passes untouched, fast pops up to
    ~22 Hz stay visible, and narrower bands (50:1/100:1) only lower
    readings ~25-40% while hiding excursion detail. No code change needed.
- **Adaptive noise quiet-start + per-window baseline (2026-08-13, night,
  user request):** a real run (test-10) PASSED but showed "worst 1.0 mV /
  3 windows over" that was pure DC settling — the offset had not finished
  settling when the capture window started, and the within-window slope
  inflated the pk-pk. Two changes, per the user's proposal: (1) the quiet
  wait is now ADAPTIVE — the capture starts once 2 consecutive 1 s
  block-mean deltas are <= NOISE_BASELINE_SETTLE_DELTA_MV (~107 uV/s,
  limit/4), earliest at 3 s; if the level is still moving at
  NOISE_WAIT_MAX_S = 20 s the capture starts anyway and the report/CSV
  note it (`noise_baseline_settled` NO) — unlike the old settle rule this
  can NEVER fail the part, only delay the start. (2) each 1 s analysis
  window is judged against its OWN least-squares baseline (mean AND slope
  — the rigorous version of "take a new average offset every second"),
  implemented as `detrend_window_segments` inside
  `analyze_noise_capture_band_limited`; residual settling cannot inflate
  the windowed pk-pk, while noise excursions around the moving baseline
  are fully kept (mirrors the legacy AC-coupled amplifier, which never
  showed the scope any DC drift). The result/live scopes display the
  detrended trace, so the picture finally matches the verdict (test-10's
  trace visibly left the red band while passing). The noise status line
  shows the live level movement in uV/s while waiting.
- **Noise result display is VERDICT-ONLY (2026-08-13, night, user
  request):** with "Show test details" on, the noise tile now reads
  `PASS` / `FAIL` / `Skipped` instead of a worst-pk-pk number, the detail
  line says only `noise PASS|FAIL` (plus "level still moving at the wait
  deadline" when applicable), and the on-screen FAIL reason gives window
  counts without voltages ("5 of 20 one-second windows exceeded the noise
  limit (allowed 4)"). Reason: this rig measures at the sensor pin in µV
  while the legacy station reads mV behind its amplifier chain, so any
  magnitude on screen invited a false comparison and confused operators.
  Nothing is lost — every level (worst, median, limit, over percent,
  analysis rate, baseline-settled flag) is still written to the dedicated
  `noise_*` CSV columns and to the auto-saved failure snapshot PNG. The
  **waveform view is unchanged**: the noise scope keeps its µV range
  trace, red ±limit/2 cutoff lines, and numeric readout.
- **Two more legacy scope photos (same evening):** two further sensors at
  50 mV/div and 100 mV/div, all with the +/-150 mV-style cursor band,
  display ~150-250 mV spans and pass — consistent with the adopted ~700x
  effective chain factor (if they are test-9/test-10, implied factors
  bracket ~600-800). No constant change.
  - **Charger EMI confirmed as the micro-gap source:** 18 gaps/min with
    the laptop charger plugged in, 4/min mid-session, **0 gaps in 60 s
    with the charger unplugged**. The gap filler rode through all of them;
    unplugging the charger (or powering the laptop from battery) during
    calibrations remains good practice.
- **Offset fail-fast** (reworked 2026-08-13 — offset now BEFORE the
  reference gate): an offset outside the TP412 0.8-3.0 V band records the
  failure immediately — the reference gate, noise, and sensitivity never
  run, the CSV row carries blank capture and reference-check columns, and
  HO/LO/D is preselected from the offset value. Because a bad part never
  reaches the reference gate, its AIN1 interference can no longer
  invalidate the reference calibration at all. The interference
  suppression survives as a safety net for parts that drift/rail AFTER
  passing the offset gate: on ANY reference-gate failure
  (`high_offset_dut_explains_reference_failure`, direction-agnostic —
  out-of-window high, out-of-window low, or failed-to-stabilize) AIN0 is
  re-read, and above 3.0 V the part records the HO failure while the
  calibration stays valid; the reference gate re-runs on the next part,
  so a genuine emitter fault is still caught one part later. Only an
  in-band re-read lets a reference failure invalidate the calibration.
- **The AIN1 reference unit is a 406MCA sensor** (told by the user
  2026-08-12, evening): every reference phase — the five calibration
  readings and the per-test reference gate — now drives the emitter at that
  model's qualified **10 Hz** (`PWM,FREQ,10`), while DUT phases stay at the
  405 M22's TP412 1 Hz. Its baseline is then comparable to the 406MCA's
  historical 10 Hz characterization, and reference captures are ~10x
  shorter. The calibration schema is now **v4** (stores
  `reference_pwm_hz: 10`); v3 baselines (1 Hz drive, none ever successfully
  recorded) and older are rejected, so one fresh "Calibrate reference unit"
  run on firmware v2.0 is still required before any DUT can be recorded.
- **Windows serial-stream fix (2026-08-12, evening):** the first Windows
  calibration attempt failed with "17 timestamp gaps (~63 missing samples);
  80 duplicate timestamps; host/firmware sample counts differ
  (14648/14631)". This was NOT the v2.0 PGA/buffer change (firmware kept
  perfect pace — 0 ADC overruns; 14631 - 63 lost + 80 re-delivered = 14648
  exactly). Root cause: the Windows CP210x driver's small default receive
  queue overflows during GUI stalls and then drops AND re-delivers data
  (bench-reproduced: duplicated lines byte-identical, one merged mid-field).
  Fix in the 405 M22 backend: 1 MiB receive-buffer request at connect (same
  as live_waveform's lag fix) plus a dedicated drain thread that empties the
  OS queue while streaming regardless of GUI stalls. Bench-verified on COM3:
  the dup/multi-gap signature is gone under abusive 3 s + 0.5 s/s stall
  loads. Rare 3-4-sample micro-gaps (USB scheduling level, ~1 per minute
  under abuse) can still occur, so every production capture (calibration
  readings, the per-test reference gate, the driven DUT capture, and the
  noise capture) auto-retries a StreamIntegrityError up to 2 times (fresh
  capture, nothing recorded from a rejected stream; other errors still
  abort immediately). The capture loops also throttle their full-array
  re-analysis and preview work to once per half PWM period / noise window —
  the previous every-0.1 s full pass held the GIL long enough to starve the
  drain thread. NOTE: a second calibration attempt on the evening of
  2026-08-12 failed with the same corruption signature, but that app
  instance had been launched before the fix (a running process keeps its
  old code) and was still holding COM3 afterward — fully close the app and
  relaunch before judging the fix.
- **Bounded micro-gap tolerance (2026-08-12, late evening):** even with the
  buffer/drain fix, the residual USB-scheduling micro-gaps kept failing
  real captures — the user repeatedly hit "1 timestamp gaps (~6 missing
  samples); host/firmware sample counts differ (16533/16539)" (a ~16.5 s
  driven capture losing 6 ms once; zero tolerance plus 2 retries still
  loses statistically once gaps land in most captures).
  `_validate_stream_diagnostics` now tolerates at most
  `STREAM_MAX_MICRO_GAPS = 3` gaps and `STREAM_MAX_MISSING_SAMPLES = 20`
  (~20 ms at 1 kS/s) lost samples per capture, requiring the firmware/host
  count difference to stay within the same budget; a tolerated gap is noted
  on `rig.last_stream_tolerance_note`. Losing a few milliseconds cannot
  change a 1 s-window pk-pk verdict or a robust per-cycle peak (median of
  the top 10% of ~1000 samples/cycle). Duplicates, reordering, torn lines,
  ADC overruns, >2% rate error, or anything beyond the budget still reject
  the capture with nothing recorded (the historical 17-gap/80-duplicate
  overflow signature still fails), and the bounded retries remain.
- **Micro-gap follow-up (2026-08-13): timestamp gap-filling + retryable
  sync errors.** The same micro-gaps had a second symptom: "ESP32 PWM sync
  frequency is 10.067 Hz; individual validation cycles span 10.000-10.204
  Hz" during the 10 Hz reference gate — noticeably more often with the
  laptop charger plugged in (switching-charger EMI on the USB link raises
  the drop rate). Cause: `validate_rising_sync_cycles` measures each cycle
  as edge-index difference / sample rate, so a 2-sample gap in one
  100-sample reference cycle reads 1000/98 = 10.204 Hz (the dialog's
  10.067 = 3 cycles over 298 samples) and raised a NON-retried
  HardwareNotReadyError ("check firmware and GPIO25"). Fix: a
  `StreamGapFiller` in every capture path rebuilds missing sample slots
  from the firmware `timestamp_us` values (linear-interpolated volts, sync
  transition at the gap midpoint) for gaps within the tolerance budget, so
  all index-based math — sync cadence, cycle segmentation, noise windows —
  sees a contiguous 1 kS/s timeline again (the integrity validator's
  minimum-sample check subtracts the filled count). If sync validation
  still fails while ANY gap was seen (e.g. a gap swallowing an edge, or one
  beyond the fill budget), it is reclassified as StreamIntegrityError so
  the existing bounded retries take a fresh capture instead of aborting
  with a misleading wiring error; with a clean stream it remains the hard
  rig fault it always was. At 1 Hz DUT validation the ±0.1 Hz tolerance is
  ±10%, so only the 10 Hz reference phase was ever affected.
- **Scope views (2026-08-12, late evening):** the live and result waveform
  panels overlay the PWM sync square wave ON the signal trace (scaled to
  the same band, orange, "HIGH = EMITTER ON") so polarity is directly
  inspectable, and both axes carry numeric tick labels on a nice-step grid.
  Traces render as per-pixel min/max envelopes so a narrow spike survives
  downsampling. **Noise displays are now RELATIVE (same evening, per user
  request):** during the noise step the live scope, and on the result
  screen the dedicated noise scope, plot the band-limited trace as its
  range around the mean — symmetric about 0, with SOLID RED cutoff lines at
  +/-half the pk-pk limit and a numeric "range N pk-pk" readout — instead
  of absolute volts, so whether the part crosses the limit reads directly
  off the red lines. Units auto-select (uV under the corrected 75 uV limit:
  red lines at +/-37.5 uV, minimum span 150 uV; mV for any band >= 1 mV),
  the time axis follows the decimated 50 SPS rate, and the y-axis never
  spans less than 2x the limit so normal noise cannot be over-zoomed into
  looking large. The simulator generates a matching synthetic ~36 uVrms
  trace and runs it through the SAME band-limited analysis so the panel and
  CSV plumbing are exercised without hardware.
- Results stay isolated under
  `~/Documents/Eltec_405M22_Test_Results/405m22_esp32/`
  (`%USERPROFILE%\Documents\Eltec_405M22_Test_Results\405m22_esp32\` on
  Windows), batch CSVs are named `405m22_esp32_lot_*.csv`, and the launcher
  identities are `Eltec 405 M22 ESP32 Tester` /
  `com.eltec.405m22-esp32-tester.desktop`.

### Running it on either host (Xubuntu or Windows)

Same code, same CSV format, same results-folder layout on both; only the
launcher differs, so a batch started on one machine is readable on the other.
Port discovery is by USB VID/PID on both, so the CP210x bridge is found
automatically (`/dev/ttyUSB*` vs `COM*`). The app already carried its
Windows-specific paths (DPI awareness, private font loading, forced repaint,
and skipping the POSIX-only exclusive-tty flag); what was missing was a
Windows launcher.

| | Xubuntu | Windows (added 2026-08-12) |
| --- | --- | --- |
| run | `./tech_app/405m22_esp32/run_eltec_405m22_esp32_tester.sh` | `tech_app\405m22_esp32\run_eltec_405m22_esp32_tester.cmd` (double-clickable) |
| shortcuts | `./tech_app/405m22_esp32/install_xubuntu_launcher.sh` | `powershell -ExecutionPolicy Bypass -File tech_app\405m22_esp32\install_windows_launcher.ps1` |
| log | `~/.local/state/eltec-405m22-esp32/launcher.log` | `%LOCALAPPDATA%\eltec-405m22-esp32\launcher.log` |

Both shortcut installers are **opt-in** and per-user; the Windows one takes
`-Uninstall` and writes a Desktop plus a `Programs\Eltec` Start Menu entry
(icon `assets/eltec_desktop_icon.ico`, generated from the existing PNG). The
`.cmd` launcher runs the GUI under `pythonw.exe` (no console window), honors
the same `ELTEC_PYTHON` override as the shell script, rotates its log at
5 MB, and pops an error dialog on failure — the equivalent of the Xubuntu
`notify-send`/`zenity` path. Set `ELTEC_LAUNCHER_NO_DIALOG=1` to suppress
that (blocking) dialog in headless/test runs.

Verification: `python3 -m unittest discover -s tech_app/405m22_esp32/tests`
passed **145 discovered tests on 2026-08-13, late night** (4 skipped on
Windows: 3 display-only GUI tests plus the POSIX-only launcher-installer
test). The offset-first reorder updated/added coverage for: high offsets
(3.2/3.8/railed 5.0 V) failing immediately with only
`["pwm_off", "offset"]` device calls and the calibration untouched; the
in-band-then-railed drift scenario sparing the calibration on a HIGH,
LOW, or UNSTABLE reference failure while recording HO; an in-band re-read
still invalidating the calibration on out-of-window and unstable
reference failures; a 0.02 V float still blocking as "no sensor"; the
progress-ladder labels in the new Offset -> Reference -> Noise ->
Sensitivity order; and the removal of `SENSOR_OFFSET_MAX_PLAUSIBLE_V`.
The adaptive quiet-start/detrend work added coverage for: settled level starts
at the 3 s minimum, a settling ramp delays the start past the ramp, a
never-settling ramp caps at 20 s and measures anyway (flag NO, no
exception), pure-line detrend residual ~0, a 0.5 mV/s settling drift that
passes ONLY with per-window detrending, and the `noise_baseline_settled`
CSV column; the synthetic noise fixtures became window-center-symmetric so
the least-squares detrend leaves their pk-pk exact. The
2026-08-13 gap-filler work added coverage for interpolation/midpoint-sync
filling, oversize-gap counting without fabrication, uint32 timestamp
wraparound, untimestamped passthrough, a refilled 2-sample reference-cycle
gap passing sync validation, and an edge-swallowing gap surfacing as a
retryable StreamIntegrityError. The 75 uV noise-limit correction added
coverage for the boxcar decimation, high-frequency content removed vs
low-frequency kept, raw-clip windows the average would hide, the corrected
constants (300 mV / x4000 = 0.075 mV, 20:1 decimation), the uV-scale CSV
round trip with the new `noise_analysis_rate_hz` column, and rescaled
synthetic noise fixtures (20 uV quiet / 400 uV noisy squares). The tests added with the Windows serial-stream fix cover the 1 MiB
receive-buffer request (and tolerance of ports without/refusing it), the
streaming drain thread's lifecycle, a slow consumer losing no samples, a
drain-thread serial error invalidating the port, the StreamIntegrityError
classification, and the bounded stream-retry helper; the 10 Hz reference
change adds coverage for PWM,FREQ,10 programming, rejection of any
non-1/10 Hz drive, and a 10 Hz (100-sample-cycle) reference stream through
the dedicated-delta/five-fresh-cycles rule. The late-evening rework updated
and extended the coverage: the emitter-off noise workflow (noise BEFORE
sensitivity, fixed 3 s quiet wait, windowed 20% rule with the exact-20%
boundary, noise fail-fast skipping the driven capture, measured-noise
recorded on Unstable parts, CSV round trip with blank capture columns,
clipped-window handling), the offset fail-fast (including under the
high-AIN1 interference suppression), the micro-gap tolerance budget
(tolerated single-gap capture with note; rejection of too many gaps,
duplicate re-delivery, and count mismatches beyond the budget; the
low-level noise capture's discard/keep alignment), the four-step progress
ladder ordering, the 0.8-3.0 V offset band, the TP412 filter table,
firmware v2.0 rejection of v1.8/v1.9 boards, battery-never-read behavior,
and three Windows-launcher tests (both launcher files exist and carry no
406MCA identity — checked on every host; the PowerShell installer parses;
the `.cmd` reports a missing interpreter into its log). A Windows Tk GUI
smoke script additionally drove the real app in simulator mode end-to-end
(progress bar built and advancing, live scope flipping into/out of the
relative noise display, result screens for a passing part and an
offset-fail-fast part) since the display-only unit tests skip on Windows. The v6 (80 tests)
and v6.1 (95 tests) suites were re-run unchanged for isolation: their only
failures on Windows are the two long-standing environment-only tests (the
POSIX bash-installer test and the POSIX-exclusive tty flag assertion); both
suites pass on the Xubuntu host and `git status` confirms no v6/v6.1 files
were modified.

**Front-end A/B noise check (firmware v2.1, 2026-08-12):** to answer
whether the low noise readings are an artifact of the v2.0 gain/buffer
change, `esp32_rig_readout.py noisecmp` captures emitter-off noise
back-to-back on BOTH front ends (v2.0 gain-1/unbuffered, then v1.9
gain-2/buffered; `--v19-first` reverses the order) and reports worst/median
1 s-window pk-pk for each — same part, same wiring, minutes apart. `stream`,
`test`, `ref`, `offset`, and `live_waveform.py` also take `--fe v19|v20`.
The switch is session-only: any port open resets the board back to the v2.0
front end, so the 405 M22 app is unaffected.

For quick bench checks the rig tools accept `--freq`:
`python3 Arduino/Eltec/live_waveform.py --pwm --freq 1` shows the live 1 Hz
waveform (the rolling window auto-widens to ~6 s), and
`python3 Arduino/Eltec/esp32_rig_readout.py test --freq 1 -s 20` runs the
guided sequence at 1 Hz. Without `--freq` both tools keep the firmware's
10 Hz boot default. NOTE: their `bat` command reads the AIN7 divider, which
no longer sees a battery on this fixture — ignore it. `live_waveform.py`
also received a host-side fix on 2026-08-12: the reader now uses buffered
bulk serial reads instead of pyserial's byte-at-a-time `readline()`, which
was falling behind the 1000 S/s stream while matplotlib held the GIL and
made emitter toggles appear ~20 s late (the backlog sat in the enlarged OS
buffer). The stats box now shows a `lag` readout that should stay at 0.0 s.

## Experimental v6.1 stability policy

The isolated evaluation build is in `tech_app/v6_1_esp32/`. Its DUT policy is:

| Attempt | Qualification | Official measurement | Over-threshold delta during measurement |
| --- | ---: | ---: | --- |
| 1 | 10 consecutive deltas `<= 0.100 mV` | 20 stable cycles | Discard and start attempt 2 |
| 2 | 10 consecutive deltas `<= 0.100 mV` | 20 stable cycles | Discard and start attempt 3 |
| 3 | 10 consecutive deltas `<= 0.100 mV` | 20 stable cycles | Record `Unstable - Unstable` |

Every official measurement cycle must keep its robust-peak delta within the
threshold. A kick discards the entire partial measurement window. All three
attempts use the same 10/20 lengths. The existing 20-second deadline
governs each qualification/requalification decision; a measurement window that
starts after timely qualification may finish after the deadline.

V6.1 keeps reference-unit behavior unchanged. It uses its own results root,
CSV telemetry, launcher identity, and state log. If its own reference
calibration is absent, it can read the compatible v6 calibration as a read-only
fallback; any later calibration or invalidation writes only to v6.1.

The v6.1 result details and CSV report the final attempt, kicked-window count,
active qualification length, and active measurement length. A third kick or a
qualification deadline records a normal unstable sensor FAIL with the standard
failure mode rather than a rig error.

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
5. Current v6 code requires five consecutive absolute cycle-to-cycle
   robust-peak deltas at or below its dedicated `0.250 mV` reference threshold.
   A larger delta resets the confirmation run. The DUT threshold remains
   `0.100 mV`; see the open policy-review item below.
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

The `0.100 mV` stability threshold and SNR limit still require broader
production qualification with representative known-good and known-bad parts.
V6.1 now preserves the raw new-fixture sensitivity and also reports a
legacy-equivalent value using the provisional paired-fixture factor `1.582`.
For the default `-284 filter + extra -6 + blackened tube` setup, raw sensitivity
below `2.43 mV` fails, `2.43-2.63 mV` inclusive is recorded as
`RETEST / QUARANTINE`, and above `2.63 mV` passes the sensitivity gate. The
legacy filter-specific minimum remains the center of the same `+/-0.10 mV` raw
guard-band policy for the other selectable setups. Offset is not scaled, and
all other gates remain active and unchanged. The sensitivity policy remains
provisional pending repeated known-low and borderline sensor evidence.

## Historical calibration and latest live hardware results

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

Historically saved calibration (the JSON was not present on the latest disk
check):

- baseline average: `5.3432 mV`;
- allowed lower limit: `4.8089 mV`;
- allowed upper limit: `5.8775 mV`;
- tolerance: `+/-10%`;
- valid: `true`.

An independent cold-start adaptive reference check then stabilized at
`3.151 s`, averaged `5.3344 mV` across its five fresh cycles, drifted only
`-0.165%` from baseline, and passed the gate. No AIN0 read was performed during
that verification.

Latest non-recording v6.1 check on 2026-07-16:

- ESP32 port: `/dev/ttyUSB0`;
- battery: `6.262 V`;
- live AIN1 reference: `5.6704 mV`, inside the historical
  `4.8089-5.8775 mV` range;
- after the all-attempts 10/20 update, the DUT offset was `0.6439 V`;
- the live reader explicitly reported qualification `10` and measurement `20`;
- the DUT qualified and completed attempt 1 with 20/20 measurement cycles;
- PWM-on capture time was `7.423 s`, and the final observed delta was
  `0.0170 mV`;
- no batch row or calibration file was created or changed, and PWM was forced
  off after every capture.

The loaded part did not reproduce a measurement-window kick during that run.
Synthetic full-stream integration tests exercise attempt 2 with the same 10/20
lengths, and deterministic state-machine tests cover attempt 3 and the
third-kick unstable verdict.

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

The current firmware source is `Arduino/Eltec/Eltec.ino` **v3.0**
(2026-08-18), the unified test-rig baseline. It retains the v2.1 runtime
front-end switching used by both bundled sensor workflows:

- **v2.1**: the ADS1256 front end became runtime-switchable for A/B noise
  comparison: `FE,V19` (gain 2, buffer ON — the 406MCA-qualified front end),
  `FE,V20` (gain 1, buffer OFF — boot default), plus `FE,GAIN,<1|2>` /
  `FE,BUF,<0|1>` to isolate which of the two changes matters, and `FE?` to
  query. Setters are rejected while streaming; each switch SELFCALs and
  read-back-verifies before OK. NOT persisted: every reset — including the
  DTR toggle when any host opens the port — reverts to the v2.0 front end,
  so the 405 M22 app (which never sends `FE`) always measures on v2.0
  behavior. Purpose: verify whether the much lower noise readings seen
  after the v2.0 change are a front-end artifact or real (`noisecmp`
  below). In v19 mode full scale is ±2.5 V, so DC offsets above ~2.4 V
  clip — that comparison is only valid for parts with lower offsets.
- **v2.0**: ADS1256 sensor channels (AIN0 DUT + AIN1 reference) run at PGA
  gain 1 (+/-5 V full scale, LSB 596 nV instead of 298 nV) with the input
  buffer OFF, so DC offsets read linearly to 3.0 V+ for the TP412 offset
  band (the old gain-2 buffered front end hard-clipped at 2.5 V).
  **WARNING: do not flash v2.0 on rigs running the 406MCA v6/v6.1 apps** —
  they were qualified on v1.9's gain-2 buffered front end and their noise
  floors/thresholds would need re-verification; keep those rigs on v1.9.
  With the buffer off, `BAT?` also loses accuracy (the unbuffered input
  loads the resistive AIN7 divider) — irrelevant on the 405 M22 fixture,
  where the battery gate is disabled anyway.
- v1.9: runtime `PWM,FREQ,<hz>` command (0.1-20 Hz), `pwm_hz` in `STATUS?`;
  boots at the 10 Hz default.
- v1.7+: AIN0/AIN1 streams with digital PWM sync, AIN7 battery reads, fixed
  GPIO25 PWM control, strict sample counts and ADC-overrun reporting.

Current 405 M22 fixture wiring:

- GPIO25 -> MOSFET module emitter gate, 1 Hz / 50 percent PWM
  (programmed by the app via `PWM,FREQ,1`);
- ADS1256 AIN0 -> buffered DUT sensor;
- ADS1256 AIN1 -> permanently mounted reference sensor;
- ADS1256 AIN7 -> legacy divider, no longer connected to a monitored
  battery (sensor-battery monitoring planned on AIN6);
- 6.5 V battery -> emitters only; 9 V battery -> sensors.

Use `Arduino/Eltec/ESP32_ADS1256_Wiring_v2_0.md` for this fixture.
`ESP32_ADS1256_Wiring_v1_7.md` stays valid for 406MCA rigs on firmware
v1.9. The older `ESP32_ADS1256_Wiring.docx` describes the historical
9 V/AIN1 arrangement and must not be used.

## Firmware version archive and the v2.0 flash

### Version archive (new 2026-08-12)

Every known `Eltec.ino` build is frozen under `Arduino/Eltec/versions/`, one
folder per version named to match its `.ino` so the Arduino IDE and
`arduino-cli` open it with no renaming. `Arduino/Eltec/Eltec.ino` stays the
live, editable sketch; the archive is read-only snapshots. See
`Arduino/Eltec/versions/README.md` for the full table and flash commands.

| Folder | Provenance | Front end | Used by |
| --- | --- | --- | --- |
| `Eltec_v1_5` | commit `6d5e14a` | PGA 2, buffer on | historical |
| `Eltec_v1_7` | commit `1d2adf0` | PGA 2, buffer on | historical |
| `Eltec_v1_8` | commit `33faa76` (HEAD) | PGA 2, buffer on | historical |
| `Eltec_v1_9` | **reconstructed** | PGA 2, buffer on | **406MCA v6 / v6.1 rigs** |
| `Eltec_v2_0` | 2026-08-12 snapshot | PGA 1, buffer OFF | 405 M22 rig (superseded by v2.1) |
| `Eltec_v2_1` | working tree | PGA 1, buffer OFF at boot; runtime `FE,...` switch | **405 M22 rig** |

**v1.9 had to be reconstructed, not extracted.** It was never committed:
`HEAD` holds v1.8 and the working tree had already advanced to v2.0, and no
v1.9 blob exists in `git log`, `git stash`, the reflog, or `git fsck`'s
dangling objects (the clone dates from 2026-08-10, so there is no earlier
local history). It was rebuilt from v2.0 by reverting exactly the four
functional v2.0 edits — `PGA_SENSOR` 0 -> 1, `REG_STATUS` `0x00` -> `0x02`,
the matching `(status & 0x06) == 0x02` read-back, and the version string —
while keeping v1.9's `PWM,FREQ` feature.

The reconstruction is **verified**: `diff Eltec_v1_8.ino Eltec_v1_9.ino`
contains only the `PWM,FREQ` feature and nothing else, and both sketches
compile for `esp32:esp32:esp32doit-devkit-v1`. Only the comment wording of
the real v1.9 cannot be guaranteed; the code is v1.8 + `PWM,FREQ`.

Going forward, snapshot the outgoing build into `versions/` on every version
bump (or simply commit the live sketch each time).

### v2.0 flashed and verified on the bench rig (2026-08-12)

Flashed from the Windows host with the `arduino-cli` bundled inside Arduino
IDE 2.x
(`%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe`,
v1.5.1, esp32 core 3.3.11):

```bat
arduino-cli compile --fqbn esp32:esp32:esp32doit-devkit-v1 Arduino\Eltec
arduino-cli upload -p COM3 --fqbn esp32:esp32:esp32doit-devkit-v1 Arduino\Eltec
```

- board: DOIT ESP32 DEVKIT V1, chip ESP32-D0WD-V3 rev v3.1,
  MAC `30:76:f5:92:55:e8`;
- port: `COM3` (Silicon Labs CP210x) on Windows; the rig still enumerates as
  `/dev/ttyUSB*` on Xubuntu;
- sketch 289,420 bytes (22% of flash), 22,932 bytes of RAM; all four flash
  regions written and **hash-verified**.

Post-flash bench probe (reference unit still installed on AIN1, and it works
as before):

```text
IDN?       -> ELTEC-ESP32-ADS1256,v2.0
STATUS?    -> STATUS,pwm=0,streaming=0,vref=2.500,rate=1000,pwm_hz=10.000
GATE?      -> GATE,pin=25,drive=0,read=0
OFFSET?    -> OFFSET,1.33681      (AIN0 DUT, inside the TP412 0.8-3.0 V band)
REF?       -> REF,0.82869         (AIN1 reference unit)
PWM,FREQ,1 -> OK,PWM,FREQ,1.000
REF stream @ 1 Hz -> 0.8007-0.8604 V, 59.66 mV pk-pk, sync toggling correctly
```

The ADS1256 register read-back passed (`rate=1000` reported after
`adsInit()`'s verify), so the gain-1/unbuffered front end is confirmed live.
`BAT?` still answers (`6.3847 V`) but reads the AIN7 divider and must be
ignored on this fixture.

### v2.1 flashed and verified on the bench rig (2026-08-12)

Same board/toolchain as above (COM3, all regions hash-verified). Bench
probe of the new runtime front-end switch, DUT and reference still
installed:

```text
IDN?      -> ELTEC-ESP32-ADS1256,v2.1
FE?       -> FE,gain=1,buf=0,fs=5.000     (boot default = the v2.0 front end)
OFFSET?   -> 1.33745 V     REF? -> 0.82664 V     [v2.0 front end]
FE,V19    -> OK,FE,gain=2,buf=1
OFFSET?   -> 1.33800 V     REF? -> 0.82436 V     [v1.9 front end]
FE,BUF,0 / FE,GAIN,1 -> granular setters OK; FE,BOGUS rejected with ERR
FE,V20    -> OK; 2 s AIN0 stream = 2000 samples, mean 1.33745 V,
             1.170 mV pk-pk (emitter off)
```

**Key cross-check: the two front ends agree on the DUT offset to within
0.55 mV (0.04%) and on the reference to within 2.3 mV.** The gain/buffer
change is therefore not distorting DC readings at this fixture's ~1.34 V
offset, which is the expected result — the v2.0 change only moved the
clipping ceiling, not the scale. Whether the emitter-off *noise* figure
depends on the front end is the open question `noisecmp` answers.

**Still to do:** run **Calibrate reference unit** in the 405 M22 app to
record the 1 Hz AIN1 baseline on the v2.0 front end. The schema bump
(v2 -> v3) deliberately rejects any pre-v2.0 baseline, so this is required
once before the app will record a DUT. The 59.66 mV pk-pk above was a
smoke-test read through a deliberately slow host reader (it consumed only
~1.5 s of the 3 s stream) — it is not a calibration figure.

## Verification status

Latest verification:

```text
python3 -m unittest discover -s tech_app/v6_esp32/tests -v
```

Latest headless result: **80 v6 tests discovered; 77 passed and 3 display-only
GUI tests skipped**, including:

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

V6.1 verification:

```text
python3 -m unittest discover -s tech_app/v6_1_esp32/tests -v
```

Latest headless result: **93 v6.1 tests discovered; 90 passed and 3 display-only
GUI tests skipped**. The suite includes exact first-, second-, and third-attempt
cycle selection; retry deadline behavior; immediate unstable classification on
the third measurement kick; a direct streamed attempt-2 capture; calibrated
sensitivity factor/boundary/CSV behavior; results/launcher isolation; and the
unchanged v6 regression run described above.

## How to run the production unified rig

For a fresh Xubuntu computer, use the repeatable fleet setup rather than
installing these dependencies and permissions by hand:

```bash
./setup_xubuntu.sh
```

See `XUBUNTU_FLEET_SETUP.md` for first-install, online/offline update, health
check, rollback, and result-backup procedures. The setup installs the unified
selector by default and never copies a historical reference calibration to a
new fixture.

From the repository root:

```bash
./tech_app/eltec_rig/run_eltec_rig_tester.sh
```

Or from the unified app directory:

```bash
./run_eltec_rig_tester.sh
```

The production desktop/menu launcher is installed by setup, or directly with:

```bash
./tech_app/eltec_rig/install_xubuntu_launcher.sh
```

Unified launcher identities are isolated from every standalone model launcher:

- display name: `Eltec Test Rig`;
- menu ID: `com.eltec.test-rig.desktop`;
- desktop entry: `~/Desktop/Eltec Test Rig.desktop`;
- launcher log: `~/.local/state/eltec-rig/launcher.log`.

## How to run the legacy standalone v6.1 build

From the repository root:

```bash
./tech_app/v6_1_esp32/run_eltec_406mca_esp32_tester.sh
```

Its optional launcher installer creates only the v6.1 identities:

```bash
./tech_app/v6_1_esp32/install_xubuntu_launcher.sh
```

- display name: `Eltec 406MCA ESP32 Tester v6.1`;
- menu ID: `com.eltec.406mca-esp32-tester-v6-1.desktop`;
- desktop entry: `~/Desktop/Eltec 406MCA ESP32 Tester v6.1.desktop`;
- launcher log: `~/.local/state/eltec-406mca-esp32-v6-1/launcher.log`;
- results: `~/Documents/Eltec_406MCA_Test_Results/v6_1_esp32/`.

## Important files

- `tech_app/eltec_rig/` — **the active production application**: unified
  sensor-version selector (`eltec_rig_tester.py` + `sensor_versions.py`
  registry), bundled `m405m22/` and `m406mca/` testers, cross-platform
  launchers and desktop-icon installers, README, and tests. Everything
  below remains as historical/standalone reference.
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
- `tech_app/v6_1_esp32/` — isolated v6.1 evaluation build with the stricter
  three-attempt DUT policy, documentation, launchers, and tests.
- `tech_app/405m22_esp32/` — isolated Model 405 M22 (1 Hz) TP412 build:
  `eltec_405m22_esp32_tester.py` (offset-fail-fast -> reference gate ->
  noise -> sensitivity order with a step progress bar, railed-AIN0 = HO
  handling, adaptive-wait noise test, relative noise displays,
  micro-gap-tolerant stream validation, TP412 filters/offset band,
  battery gate disabled), retimed backend/stability modules (incl. the
  sync-free windowed noise analysis), its Xubuntu **and Windows**
  launchers, README, and a 145-test suite.
- `tech_app/405m22_esp32/run_eltec_405m22_esp32_tester.cmd` and
  `install_windows_launcher.ps1` — Windows launcher and opt-in Desktop/Start
  Menu shortcut installer (`-Uninstall` removes them).
- `Arduino/Eltec/Eltec.ino` — **v3.0** firmware, live/editable sketch: the
  unified test-rig baseline (= v2.1 functionality; boots on the gain-1
  unbuffered front end, `FE,V19` restores the 406MCA-qualified one at
  runtime; no telescope dual-channel code).
- `Arduino/Eltec/versions/` — frozen snapshot of every known firmware build
  (v1.5, v1.7, v1.8, reconstructed **v1.9**, v2.0, v2.1, v2.2, v3.0), one
  compilable folder each, with `README.md` covering provenance, which rig
  runs which build, and revert commands.
- `Arduino/Eltec/ESP32_ADS1256_Wiring_v2_0.md` — current 405 M22 fixture
  wiring; `ESP32_ADS1256_Wiring_v1_7.md` remains for 406MCA rigs.
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
6. Evaluate v6.1 on representative intermittent parts before deciding whether
   to promote its three identical 10/20 attempts into production v6.
7. Confirm the intended v6 reference-stability threshold before the first fleet
   release. Current code, its v6 README, and integration coverage use a dedicated
   `0.250 mV` reference threshold; earlier status text said `0.100 mV`, which is
   still the separate DUT threshold.

## Working-tree state

The unified rig/firmware work from the other computer and the Xubuntu fleet
provisioner are integrated on `codex/xubuntu-fleet-merge-20260818`. Local batch
CSVs, EWO data, calibration, and other operator evidence remain untracked user
data and must not be removed by setup, update, or release work.

Preserve unrelated user changes. Do not replace or reset the historical v4,
v5, active v6, or v6.1 applications.

## 405 M22 remaining work

1. ~~Flash firmware **v2.0** to the rig.~~ **DONE 2026-08-12** — flashed and
   verified on the bench rig (COM3, DOIT ESP32 DEVKIT V1). Superseded the
   same day by **v2.1** (runtime `FE,...` front-end switch), also flashed
   and verified; `IDN?` now reports `ELTEC-ESP32-ADS1256,v2.1`. See
   "Firmware version archive and the v2.0 flash". Do NOT flash v2.0/v2.1 on
   406MCA production rigs — put those back on
   `Arduino/Eltec/versions/Eltec_v1_9` if one was flashed by mistake.
2. **NEXT:** fully close any running copy of the app, relaunch it, and with a
   known-good emitter run **Calibrate reference unit** to record the AIN1
   baseline on the v2.0 front end at the reference unit's 10 Hz drive
   (schema v4; older baselines are rejected — this is deliberate). The two
   failed Windows attempts were serial-stream corruption (fixed; see the
   Windows serial-stream fix bullet) — the second attempt ran an app
   instance started before the fix.
3. Sanity-check the rig with `live_waveform.py --freq 1` (lag readout should
   stay ~0.0 s; SPACE toggles the emitter) and run one part end-to-end:
   watch the step progress bar walk offset -> reference -> noise ->
   sensitivity, the relative noise view against its red cutoff lines,
   the noise tile, and the `noise_*` CSV columns. Also re-run one of the
   known high-offset parts from 2026-08-13: it should now record an
   immediate `HO - High offset` FAIL (offset tile ~5 V) with no
   "No sensor detected" block and no reference-recalibration demand.
4. ~~Run the **comparison batch**~~ — **DONE 2026-08-17** (lot 500, 50
   sensors on both fixtures; data in `analysis/405M22_Data/`). Results
   applied the same day:
   - **Sensitivity factor = 4.30** (`SENSITIVITY_LEGACY_EQUIVALENT_FACTOR`;
     per-part legacy/raw ratio median 4.2973, regression 4.2853, sd 4.4%,
     46 pairs) and `LOW_SENSITIVITY_FAILURE_ENABLED = True` with
     calibration id `405m22_tp412_lot500_pairwise_v1`. Replaying the lot:
     every old-fixture verdict reproduces exactly (500-10 fails LS at
     4.03 mV eq vs 4.08 old; 27/33/37 HO; 44 noise), the only extra
     failure being 500-19's SNR gate (a check the old fixture lacked;
     user re-verifying that part physically).
   - **Noise window allowance 20% -> 15%** (3 of 20): anchored on 500-44,
     the old fixture's only noise failure, which measured 4/20 windows
     over and was slipping through at 20%; 500-3's isolated 2-window
     spike stays tolerated. Single-part anchor — refine with more bad
     parts.
   - **Offset policy reworked** (lot 500 showed offsets rising for tens of
     seconds after insertion; 35/48 parts read 0.15-1.1 V low on the
     insertion read): early fail-fast is high-side only, the verdict
     offset is a settled re-read after the sensitivity capture, the CSV
     gains `offset_initial_v` alongside the settled `offset_v`, and the
     no-sensor floor gets a 5 s wake-up poll (fixes the false "No sensor
     detected" / "low offset" first attempts logged on 500-44 and 500-5).
   - **2026-08-18 re-run verification:** factor confirmed independently
     (median 4.324, repeatability sd 2.5%); settled offsets now match the
     old fixture to +0.018 +/- 0.032 V; 500-19 passes cleanly (08-17 SNR
     fail was a bad capture); 500-27 now fails for the correct reason
     (noise, 18/20 windows). 500-44's burst noise proved INTERMITTENT
     (0/20 over this run vs 4/20 on 08-17) - thresholds cannot catch it,
     so a per-part **60 s extended noise soak** was added (allowed count
     held at an absolute 3 = 3/60), plus automatic raw-capture saving
     whenever any window goes over. See the README noise section.
5. Confirm the fixed 3 s pre-noise quiet wait and the 20 s capture length
   on real parts (the adaptive settle detection was removed 2026-08-12 per
   the user — there is no signal with the emitter off, only noise);
   adaptive capture length is planned future work. FIRST REAL CAPTURE
   CHECK: run one known-good part and look at the band-limited noise
   numbers — if it reads far over 75 uV, the excess is fixture noise
   (buffer/9 V rail/cabling) to hunt before trusting the gate.
6. Hardware: add sensor-battery monitoring on **AIN6** (>= 4:1 divider,
   firmware channel, host thresholds) and re-enable the battery gate; the
   sensor supply may step down to ~8 V to match TP412's bench supply.
7. Review captures with `stability_calibration.py` before trusting the
   provisional 0.500 mV DUT threshold (relaxed from 0.100 mV on 2026-08-12),
   the 0.250 mV reference threshold, or the 60 s deadline at 1 Hz.
