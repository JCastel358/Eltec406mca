# Eltec 405 M22 ESP32 Tester (1 Hz TP412 evaluation build)

This directory is the **405 M22 model of the unified rig app**
(`tech_app/eltec_rig`, normally launched from its selector): the ESP32/ADS1256
tester (derived from the 406MCA v6.1 application) for the **Model 405 M22**
high-gain thermally compensated pyroelectric IR detector, following document
**TP412** (offset, sensitivity/polarity at 1 Hz, and noise). The sensitivity
gate is **live** with the lot-500 pairwise calibration (factor 4.30, see
below); the authoritative list of every limit and its provenance is
[`docs/CALIBRATION_RECORD.md`](../../../docs/CALIBRATION_RECORD.md).

The 406MCA v6/v6.1 standalone applications this build was derived from were
retired on 2026-08-28 (git tag `archive/pre-cleanup-2026-08-28`); the live
406 MCA model is [`../m406mca/`](../m406mca/README.md).

## What is different from the 406MCA builds

| Aspect | 406MCA v6/v6.1 | 405 M22 build |
| --- | --- | --- |
| Emitter chop | fixed 10 Hz / 50% | **1 Hz / 50%** (the 405's responsivity spec frequency) |
| Firmware | v1.7+ (keep on **v1.9**) | **v2.0+** (ADS1256 gain 1, input buffer off, `PWM,FREQ`) |
| Sync validation | 10.0 ± 0.1 Hz | 1.0 ± 0.1 Hz |
| Stability deadline | 20 s | **60 s** (each cycle now takes a full second) |
| DUT qualification | 5 (v6) / 10 (v6.1) deltas ≤ 0.100 mV | 5 consecutive deltas ≤ **0.500 mV** (relaxed for these high-gain parts; the AIN1 reference keeps its own 0.250 mV limit) |
| Measurement window | 10 (v6) / 20 (v6.1) cycles | 10 cycles, 3 attempts (v6.1-style kick/retry) |
| Healthy DC offset | 0.3–1.2 V | **0.8–3.0 V** (full TP412 band; needs firmware v2.0) |
| Filter setups | 406MCA filters | **TP412: -625 / -628 / -629** (see ranges below) |
| Sensitivity gate | calibrated, ±0.10 mV raw near-limit band | calibrated (lot 500, live since 2026-08-17), same ±0.10 mV raw near-limit band |
| Sensitivity factor | 1.582 legacy-equivalent | **4.30** legacy-equivalent (`405m22_tp412_lot500_pairwise_v1`) |
| Noise test | none | **TP412 emitter-off noise test** after the offset gate, BEFORE the sensitivity capture |
| Test order / UI | offset → sensitivity | offset (fail-fast) → noise → sensitivity, with a **step progress bar** |
| Battery gate | 6 V SLA on AIN7 | **disabled — not monitored** (batteries isolated, see below) |
| Reference baseline | own calibration per build | own calibration on firmware v2.0, **reference driven at 10 Hz** (the AIN1 unit is a 406MCA sensor; schema v4) |
| Results root | `~/Documents/Eltec_406MCA_Test_Results/...` | `~/Documents/Eltec_405M22_Test_Results/405m22_esp32/` |

Everything else — the guided flow, mandatory AIN1 reference-unit gate,
serial-integrity checks, robust-peak stability rule, failure modes,
CSV/audit trail, snapshots, and simulator — is carried over intact.

## TP412 sensitivity ranges (operator selects the filter per batch)

| Filter setup | min–max (legacy scope mV) |
| --- | --- |
| -625 filter | 5.99 – 11.98 |
| -628 filter | 4.22 – 8.44 |
| -629 filter | 4.92 – 9.84 |

All three assume the blackened tube + extra -25B filter optics at 10 cm from
the 500 K blackbody. **These ranges gate live since 2026-08-17**, through
the lot-500 pairwise fixture calibration
(`SENSITIVITY_CALIBRATION_ID = 405m22_tp412_lot500_pairwise_v1`):

* 46 sensors were measured on the legacy fixture and on this rig; the
  per-part legacy/raw ratio has median **4.2973** and
  regression-through-origin slope 4.2853 (agreement within 0.3%, spread
  4.4% sd), so `SENSITIVITY_LEGACY_EQUIVALENT_FACTOR = 4.30`.
* Raw near-limit band for -625: FAIL below **1.29 mV**, clean PASS above
  **1.49 mV**, and **PASS · NEAR LIMIT** (passes, with a re-measure
  suggestion; no quarantine since 2026-08-25) between (5.99 / 4.30 = 1.393
  center ± 0.10 raw).
* Validation on the same lot: the old fixture's one low-sensitivity failure
  (500-10) computes to 4.03 mV legacy-equivalent vs its old reading of
  4.08 and fails; the closest passer (500-15) computes to 6.52 — the exact
  value the old fixture recorded for it; no part lands in the guard band;
  the hottest part (500-41) computes to 10.88 vs 11.30 old, inside the
  11.98 maximum. Legacy-equivalents above the TP412 max fail high.

## Test order and step progress bar (offset first since 2026-08-13)

Each sensor now runs its per-part checks in TP412 order, with a step
progress bar on the measuring screen ("STEP 1/4 — OFFSET", bar filling as
the sequence proceeds, tick marks at the step boundaries):

1. **Offset (quick check)** — a plain DC read with the emitter off, before
   anything else. Since 2026-08-17 the early fail-fast is **high-side
   only**: an offset above 3.0 V fails immediately (a high-offset part
   typically **rails AIN0 at the ~5 V ADC full scale**, and highs never
   settle back into band), but a LOW first read is *not* a verdict —
   lot 500 showed these parts' offsets **rise for tens of seconds after
   insertion** (35 of 48 parts read 0.15–1.1 V below their settled
   legacy-fixture value on the first read; the two parts that got an
   immediate re-measure matched it almost exactly). A low part therefore
   continues, and the offset verdict comes from a **settled re-read taken
   after the sensitivity capture** (~40 s in the fixture); the CSV records
   both (`offset_initial_v` = insertion read, `offset_v` = settled verdict
   value). A near-0 V float still reads as "no sensor", but only after a
   5 s wake-up poll window (a just-inserted part can sit below the floor
   for a few seconds — lot 500's 500-44 was wrongly rejected as an empty
   slot before this). Because a bad high part never reaches the reference
   gate, its AIN1 interference can no longer invalidate the reference
   calibration;
2. **Reference check** (fixture gate, emitter at the reference unit's
   10 Hz). If the reference fails anyway, AIN0 is re-read: a part that has
   drifted/railed above 3.0 V mid-test explains the failure, so the part
   records the HO failure and the calibration is spared — only an in-band
   AIN0 lets a reference failure invalidate the calibration;
3. **Noise (emitter off)** — see below; a noise FAIL also stops the test
   (preselecting **N - Noisy**) without spending up to three 60 s
   stabilization attempts on a part that is already bad;
4. **Sensitivity** — 1 Hz drive, adaptive peak stability, 10 measurement
   cycles.

## TP412 noise test (emitter off)

The noise step runs on the same stream plumbing with the emitter off (it has
been off since the reference gate). The old settle detection is gone — with
the emitter off there is no signal to stabilize, only the noise being
measured — so the app now:

1. streams an **adaptive quiet wait** (discarded; the live scope keeps
   updating): the capture starts once 2 consecutive per-second mean deltas
   are ≤ `NOISE_BASELINE_SETTLE_DELTA_MV` (~107 µV/s), earliest at 3 s; if
   the DC level is still settling at `NOISE_WAIT_MAX_S` (20 s) the capture
   starts anyway and the report notes it — a slow baseline can **never**
   fail the part from here;
2. keeps the next **20 s** as the noise capture (fixed for now; making the
   length adapt to how noisy the part is remains future work);
3. **band-limits** the capture 20:1 (1000 → 50 SPS, ~22 Hz passband; since
   2026-08-20 an anti-alias FIR — Kaiser windowed-sinc, ≥ 60 dB stopband —
   replaced the original boxcar average, whose −13 dB sidelobes let 60 Hz
   mains fold to 10 Hz at only −16 dB and read as part noise) and gives
   each 1 s window its **own least-squares baseline** (mean and slope — "a
   fresh offset every second"), so residual settling cannot inflate the
   windowed pk-pk;
4. cuts the detrended trace into twenty 1 s windows and computes pk-pk
   per window: **PASS iff at most 15% of the windows (3 of 20) exceed
   the pin-level limit.** Windows whose RAW samples touch the ADC clip
   level count as over-limit (averaging must not hide a railed input).
   The 15% allowance is the 2026-08-17 lot-500 calibration: the one part
   the legacy fixture failed for noise (500-44, 496 mV on the old scope)
   measured 4/20 windows over here and was slipping through the previous
   20% allowance, while every other part measured 0/20 except one isolated
   2-window environmental spike (500-3, mid-pack on the old fixture) that
   must stay tolerated. 4 windows over now fails, 2 still passes.

**Extended noise soak (2026-08-18, per-part opt-in).** The 08-18 re-run of
lot 500 proved 500-44's burst noise is INTERMITTENT: it measured 4/20
windows over on 08-17 but 0/20 (worst 38% *below* the limit) on 08-18,
while the same re-run threw environmental transients at clean parts
(500-1 reached 3/20 over at 1.03 mV). No threshold separates those two —
tightening would false-fail clean parts *and* still miss 44's quiet
stretches. The discriminator is observation time: a load-step toggle runs
that part's noise capture at **60 s with the allowed over-window count held
at the same absolute 3** (3 of 60, deliberately NOT 15% of 60 = 9). One
environmental bang still spans only 1–3 windows regardless of length, while
genuine recurring bursts accumulate windows across the longer watch
(~50% catch per 20 s on 44's history → ~90% per soak). The toggle resets
after every sensor; use it on suspect or historically noisy parts. The CSV
records the actual capture length in `noise_capture_s` and the window count
in `noise_windows_total`.

Because noise now precedes the driven capture, an Unstable part still
records its real measured noise report (not a SKIPPED placeholder), and the
CSV's historical `noise_settle_s` column records the (now adaptive) quiet
wait; `noise_analysis_rate_hz` documents the band-limited rate the verdict
used and `noise_baseline_settled` records whether the level settled before
the capture started.

**Keeping the raw capture for analysis (2026-08-17).** The RAW 1000 SPS
noise record of the current part stays in memory until the next sensor is
loaded, and the result screen's **"Save noise capture"** button writes it to
`noise_captures/lot_<batch>/<sensor>_noise_raw.csv` (one `#` metadata line,
then `sample,t_s,volts`) plus a matching `.npz` — opt-in per part, for
offline spike-morphology work (rise times, widths, spike-to-spike
comparison) that the 50 SPS band-limited verdict trace cannot support.
Since 2026-08-18 the raw capture is also saved **automatically the moment
ANY window goes over the limit — PASS or FAIL** (each over-window event is
either a burst episode or an environmental transient, and both are too rare
to lose; the 08-18 re-run would have banked six such captures). The file
paths land in the batch CSV's `waveform_snapshot_paths` column either way,
and the metadata line records whether the save was operator-requested or
automatic. Note when
analyzing: at 1000 SPS the ADS1256's digital filter settles in ~1 ms, so
measured spike rise times shorter than ~1–2 ms are the ADC's step response,
not the spike's.

**On screen the noise result is a verdict, not a number:** the details tile
shows `PASS` / `FAIL` / `Skipped`, and a failure reason gives window counts
without voltages. This rig measures at the sensor pin in µV while the
legacy station reads mV behind its amplifier chain, so an on-screen
magnitude invites a false comparison. All levels remain in the `noise_*`
CSV columns and in the auto-saved failure snapshot; the waveform view still
plots the trace against its red ±limit/2 cutoff lines with a numeric
readout.

### The pin-level limit and the ADS1256's resolution (recalibrated 2026-08-13)

TP412's **300 mV pk-pk** scope limit is read **behind the legacy bench
amplifier chain** (TL084-based; nominal ×4000). A same-part
cross-measurement showed the nominal gain is NOT the effective one: the
part read ~240–270 µV at the pin over the scope-equivalent 50 s / 20 Sa/s
view while the legacy scope displayed only ~150–200 mV (a true ×4000
would show ~1 V). The **measured effective factor is ~620–830×**, adopted
as `NOISE_EFFECTIVE_CHAIN_FACTOR = 700`, so the pin-level limit is
300 mV / 700 ≈ **0.429 mV = 429 µV pk-pk** (single-part derivation;
refine with more parts and by checking the legacy scope's CH2 probe
setting and the amp's range switches). Whether the ADS1256 can see
signals at this scale:

- **Quantization is fine.** At PGA 1 (±5 V full scale) one LSB is
  10 V / 2²⁴ ≈ **0.6 µV**. (Higher PGA gain is not available for headroom:
  the DUT rides on a 0.8–3.0 V DC offset, which clips any gain above ~1.6
  single-ended.)
- **The ADC's own noise sets the floor.** At 1000 SPS / gain 1 the
  ADS1256's input-referred noise is ~4–8 µV RMS, i.e. ~30–50 µV pk-pk per
  1 s window raw, and the raw ~500 Hz bandwidth also admits mains/EMI the
  band-limited legacy chain never saw.
- **Band-limiting fixes it.** The 20:1 decimation (`NOISE_DECIMATION_FACTOR`;
  an anti-alias FIR since 2026-08-20 — same ~22 Hz passband as the original
  boxcar but ≥ 60 dB of stopband so out-of-band mains/EMI can no longer
  fold into the judged band) cuts the floor to a bench-measured **4.2 µV
  median window pk-pk (~1% of the 429 µV limit)**, and the resulting 50 SPS
  view closely matches the legacy scope's own roll-mode 20 Sa/s
  acquisition. A pk-pk noise spec is only meaningful in the bandwidth it
  was written for.

Bench-verified 2026-08-13 (captures + scripts in the results folder under
`noise_experiments/`): fixture floor 4.2 µV @ 20:1 (electronics clean); a
desk fan near the rig adds ~19 Hz / ~200 Hz microphonic peaks plus mV
transients through the part (pyros are piezoelectric — no fans during
noise tests); the resident good part reads ~120–140 µV median per window,
~60% of the legacy band on both fixtures with the ×700 factor.

## Scope views (live + result)

Both waveform panels overlay the PWM sync square wave ON the signal trace
(orange, scaled to the same band, "HIGH = EMITTER ON") so polarity can be
read directly: a POSITIVE part peaks while the overlay is high. X (seconds)
and Y (volts) axes carry numeric tick labels on a 1/2/5-step grid, and
traces draw as per-pixel min/max envelopes so narrow spikes survive
downsampling.

**Noise displays are relative (2026-08-12 rework):** during the noise step
the live scope — and the result screen's dedicated noise scope — plot the
**band-limited** trace as its **range around the mean, in µV** (not
absolute volts), with **solid red cutoff lines at ± half the pk-pk limit**
(±214 µV for the 429 µV limit) and the numeric pk-pk range in the corner, so
"is it crossing the red lines" is the whole reading. The y-axis stays
symmetric around 0 and never spans less than 2x the pk-pk limit so ordinary
noise cannot be auto-zoomed into looking large, and the time axis follows
the decimated 50 SPS rate.

TP412 itself allows **no** excursion over the limit (largest excursion
rule); the 15% window allowance is a deliberate engineering relaxation because
these sensors are extremely sensitive and a few excursions are expected.
All noise metrics go to the batch CSV (`noise_*` columns, mV units), and a
failing noise capture is preserved as a PNG snapshot automatically.

## Power / battery monitoring (changed 2026-08-12)

The emitter-induced spike was fixed by isolating the supplies:

- **6.5 V battery → emitters ONLY** (via the MOSFET module);
- **9 V battery → sensors** (DUT and AIN1 reference buffer supplies).

Neither battery is measurable on the legacy AIN7 divider, so the battery
gate is **disabled** (`BATTERY_MONITORING_ENABLED = False`); the header pill
shows "Battery: not monitored" and no `BAT?` reads are issued.
**TODO (hardware):** measure the sensor battery on **AIN6** through a ≥4:1
divider (e.g. 300k/100k) and re-enable the gate with thresholds for the
actual supply — the plan is to step the sensor supply down to ~8 V (or use
an 8 V battery) so the noise readings stay comparable to TP412's +8 V bench
supply. All the battery machinery is kept in the code for that re-enable.

## Firmware requirement

Flash `Arduino/Eltec/Eltec.ino` **v2.0** or newer. v2.0 changes the ADS1256
front end for the TP412 offset band:

```text
sensor channels AIN0/AIN1: PGA gain 2 -> 1  (full scale +/-5 V, LSB 596 nV)
input buffer: ON -> OFF                     (removes the AVDD-2 V = 3.0 V ceiling)
IDN? -> ELTEC-ESP32-ADS1256,v2.0
```

so DC offsets up to and past 3.0 V read linearly (v1.9 clipped at 2.5 V).
This build refuses to connect to pre-v2.0 firmware, and flashing v2.0
invalidates any reference baseline recorded before it (calibration schema
v3) — run **Calibrate reference unit** once after the flash.

**Do not flash v2.0 on rigs running the 406MCA v6/v6.1 apps** without
re-verifying their thresholds: they were qualified on v1.9's gain-2 buffered
front end (halved LSB, different noise floor, and `BAT?` loses accuracy with
the buffer off).

## Quick signal checks with the Arduino/Eltec tools

The standalone rig tools accept a `--freq` option:

```bash
# Live rolling waveform with the 1 Hz drive on (window auto-widens to ~6 s)
python3 Arduino/Eltec/live_waveform.py --pwm --freq 1

# Guided battery -> offset -> reference -> capture sequence at 1 Hz
python3 Arduino/Eltec/esp32_rig_readout.py test --freq 1 -s 20

# Hold the emitter PWM on at 1 Hz while watching the module LED
python3 Arduino/Eltec/esp32_rig_readout.py pwm on --freq 1
```

Note: the `bat` command and the battery step of `test` read the AIN7 divider,
which no longer sees a battery on this fixture — ignore those readings.

## How to run

The application runs on **both Xubuntu and Windows** from the same checkout —
same code, same CSV format, same results folder layout. Only the launcher
differs. Batch CSVs written on one host are readable on the other.

Normally: start the unified selector (`tech_app/eltec_rig/run_eltec_rig_tester.cmd`
/ `.sh`) and pick **Model 405 M22**. The model app can also be started
standalone:

### Xubuntu

From the repository root:

```bash
./tech_app/eltec_rig/m405m22/run_eltec_405m22_esp32_tester.sh
```

The optional Xubuntu launcher installer creates only 405 M22 identities:

```bash
./tech_app/eltec_rig/m405m22/install_xubuntu_launcher.sh
```

- display name: `Eltec 405 M22 ESP32 Tester`;
- menu ID: `com.eltec.405m22-esp32-tester.desktop`;
- launcher log: `~/.local/state/eltec-405m22-esp32/launcher.log`;
- results: `~/Documents/Eltec_405M22_Test_Results/405m22_esp32/`;
- serial port: `/dev/ttyUSB*` (opened exclusively).

### Windows

Double-click `run_eltec_405m22_esp32_tester.cmd`, or from a terminal:

```bat
tech_app\eltec_rig\m405m22\run_eltec_405m22_esp32_tester.cmd
```

It runs the GUI under `pythonw.exe` (no console window), logs to the file
below, and shows an error dialog if the app cannot start — the equivalent of
the Xubuntu launcher's `notify-send`/`zenity` path.

The optional shortcut installer adds a Desktop and a Start Menu entry
(per-user, no admin rights, `-Uninstall` removes both):

```bat
powershell -ExecutionPolicy Bypass -File tech_app\eltec_rig\m405m22\install_windows_launcher.ps1
```

- display name: `Eltec 405 M22 ESP32 Tester`;
- Start Menu: `Programs\Eltec\Eltec 405 M22 ESP32 Tester`;
- launcher log: `%LOCALAPPDATA%\eltec-405m22-esp32\launcher.log`;
- results: `%USERPROFILE%\Documents\Eltec_405M22_Test_Results\405m22_esp32\`;
- serial port: `COM*` (Windows already opens COM ports exclusively, so the
  POSIX-only `exclusive=True` flag is skipped there).

Port discovery is by USB VID/PID on both hosts, so the CP210x bridge on the
rig is found automatically either way.

### Serial-stream reliability (Windows fix, 2026-08-12)

The Windows CP210x driver's default receive queue holds only ~100 ms of the
1,000-line/s stream, and once it overflows it drops — and around the ring
wrap even re-delivers — receive data, which the integrity validator reports
as timestamp gaps plus duplicate timestamps with a host/firmware count
mismatch. The app therefore (a) requests a 1 MiB OS receive buffer at
connect, (b) runs a dedicated drain thread while streaming so the OS queue
is emptied even when the GUI thread stalls, and (c) throttles the capture
loops' re-analysis/preview work to once per half PWM period (driven capture)
or once per noise window, because every full-array pass holds the GIL and
starves that drain thread.

Rare residual micro-gaps (a few samples at the USB scheduling level) still
occur even with all of that, and they were failing real 17–23 s captures
("1 timestamp gaps (~6 missing samples); host/firmware sample counts differ
(16533/16539)"). Since 2026-08-12 (evening) the validator therefore
**tolerates bounded micro-gap loss**: at most `STREAM_MAX_MICRO_GAPS` (3)
gaps and `STREAM_MAX_MISSING_SAMPLES` (20 ≈ 20 ms) lost samples per capture,
with the firmware/host counters agreeing within the same budget; a tolerated
gap is noted on the rig object (`last_stream_tolerance_note`), and losing a
few milliseconds cannot change a 1 s-window noise verdict or a robust
per-cycle peak. Duplicate/reordered/torn records, ADC overruns, a >2% rate
error, or anything beyond the budget still reject the capture with nothing
recorded, and every production capture that fails the integrity check — a
reference-calibration reading, the per-test reference gate, the driven DUT
capture, and the noise capture — is retried up to
`REFERENCE_READING_STREAM_RETRIES` (2) times. Nothing from a rejected
capture is ever recorded, so a retry is always a fresh capture; any other
error still aborts immediately.

Tolerated gaps are also **refilled** (2026-08-13): every capture path runs
the incoming samples through a `StreamGapFiller` that uses the firmware's
per-sample `timestamp_us` to rebuild missing slots (linear-interpolated
volts; a sync transition swallowed by a gap lands at the gap midpoint).
Without this, index-based math saw a shortened timeline — a 2-sample gap in
one 100-sample 10 Hz reference cycle read as 1000/98 = 10.204 Hz and failed
the ±0.1 Hz PWM sync validation as a fake "check firmware and GPIO33" rig
error (seen especially with a laptop charger's EMI raising the USB drop
rate). If sync validation still fails while any gap was seen (edge
swallowed, or a gap beyond the fill budget), it is reclassified as a
retryable `StreamIntegrityError`; with a clean stream it remains a hard
rig fault.

### Serial-stream reliability, part 2: battery power throttling (2026-08-17)

Every capture began failing ("13 timestamp gaps … 103 duplicate timestamps;
host/firmware sample counts differ") while the same board, cable, and
backend were **provably clean when driven headlessly** — CLI captures, a
capture with the 1 Hz emitter chopping (EMI ruled out), and this backend
run without the GUI all delivered 23,000+ samples with zero defects. Two
measured facts explain it:

1. **The 1 MiB buffer request never worked.** `GetCommProperties` shows the
   Windows CP210x driver grants a **512-byte** receive queue regardless of
   the `SetupComm`/`set_buffer_size` request — only **~16 ms** of stream.
2. **Windows 11 on battery throttles the backgrounded GUI.** An occluded
   window gets its sleep timers coarsened to ~15.6 ms and the process is
   demoted to EcoQoS (efficiency cores, minimum clock); the event log shows
   USB power management actively targeting the CP210x (hundreds of
   USBHUB3 196 events/day) starting exactly 2026-08-14. A drain thread
   paced by a 2 ms sleep then wakes too late, the 512-byte queue wraps, and
   the driver drops + re-delivers data — the exact gap/duplicate signature.

Fixes shipped in `esp32_backend.py`:

- The drain thread now **blocks inside the driver read** (wakes on byte
  arrival, no sleep pacing) and runs at raised thread priority.
- The process opts out of Windows power throttling AND timer coarsening
  (`SetProcessInformation(ProcessPowerThrottling)`), both at module import
  in the tester and again at rig connect.
- The drain loop reads the driver's own `CE_RXOVER`/`CE_OVERRUN` flags
  (`ClearCommError`); if the driver reported overflow, the failure dialog
  now says the computer stalled — keep the window visible / plug into AC —
  instead of blaming the USB cable.
- `rig.granted_rx_queue_bytes` records what the driver actually granted.

USB selective suspend was also set to Disabled (AC and DC) in the Balanced
power plan on the Windows laptop, 2026-08-17. Operator guidance: keep the
tester window visible during captures when on battery.

### Skipping a sensor that cannot be measured (NOT MEASURED rows)

When an attempt ends with nothing recorded — the stream integrity check
rejecting every retry, a stalled or busy serial port, a rig pre-flight
failure — the result step now shows the error on a red card with **two**
options: *Measure again* and *Skip sensor (not measured)*. Before this, a
retry was the only way forward and one unreadable sensor could hold up the
whole batch.

Skipping opens a small dialog that asks for a reason
(`NOT_MEASURED_REASON_CHOICES`: ESP32 stream/rig fault, sensor could not be
loaded, or skipped by technician) plus an optional note, and then saves the
sensor and moves to the next one (or ends the batch). The row it writes is
deliberately **not** a verdict:

* `pass_fail` = `NOT MEASURED`, not `FAIL`.
* `offset_v`, `sensitivity_*`, and `polarity` stay **empty** — a skipped
  sensor must never look like a 0 V offset or a 0 mV sensitivity in the
  analysis.
* `failure_mode_tag` = `NM`, `failure_mode_reason` = the chosen reason.
* `fail_reasons` starts with `Not measured:` and carries the rig error text
  verbatim, so the CSV records *why* the sensor was skipped.
* The optional note goes to `operator_comments`.

The `NM` reasons are kept out of `FAILURE_MODE_CHOICES`, so the failure-mode
picker on a sensor that *was* measured never offers them. In the batch
summary, NOT MEASURED rows get their own chip and a grey row, and are
excluded from both the tested count and the yield (a rig fault must not read
as a batch of bad sensors).

### Reference gate DISABLED — op-amp channel crosstalk (2026-08-17)

`REFERENCE_GATE_ENABLED = False`: the fixture's buffer/voltage-follower stage
is a dual op-amp with no channel isolation, so the sensor under test couples
into the AIN1 reference signal. Observed as the reference reading collapsing
from ~4.94 mV to ~0.30 mV with a DUT loaded (lot_500, 2026-08-17 10:50). The
consequence is fundamental: the reference reading tracks whichever sensor is
loaded, so a baseline captured with one part is invalid the moment another is
inserted — no recalibration can make the gate trustworthy on this hardware.
It would randomly lock out good parts or pass a weak emitter.

While disabled:

* Sensor verdicts are unaffected — offset, noise, sensitivity, and polarity
  are all measured on AIN0. Only automatic emitter-health monitoring is lost.
* The setup screen shows a "Reference gate disabled (op-amp crosstalk)" card
  instead of the calibrate button; no calibration is required to test.
* The test ladder is 3 steps (offset → noise → sensitivity); the CSV
  reference columns stay empty instead of recording contaminated readings.
* **Operator mitigation:** if several sensors in a row fail low sensitivity,
  suspect the emitter before condemning the parts.

The reworked buffer board uses per-channel isolated op-amps. When it is
installed: set `REFERENCE_GATE_ENABLED = True`, run "Calibrate reference
unit" fresh on the new hardware (the pre-crosstalk baseline was archived as
`reference_sensor_calibration_crosstalk_contaminated_20260817.json.bak`),
and expect the baseline back near ~5 mV. All gate machinery and its tests
(`HardwareWorkflowTests`, run with the flag forced on) are kept working for
that day; `ReferenceGateDisabledTests` covers today's shipping default.

### Reference unit drive frequency (10 Hz)

The permanently mounted AIN1 reference unit is a **406MCA** sensor, so every
reference phase — the five calibration readings and the per-test reference
gate — drives the emitter at that model's qualified **10 Hz**
(`PWM,FREQ,10`), while DUT phases run at the 405 M22's TP412 **1 Hz**. The
baseline is therefore directly comparable to the sensor's historical 10 Hz
characterization, and reference captures are ~10x shorter. The calibration
schema is v4: it stores `reference_pwm_hz` and rejects the earlier v3
baselines whose readings were driven at 1 Hz (a pyroelectric response at
1 Hz is several times larger and not comparable).

### Dependencies

Same on both hosts: Python 3, Tk (`python3-tk` on Xubuntu; bundled with the
python.org/Store Windows builds), numpy, pyserial, and matplotlib (optional,
for nicer snapshots).

## Tests

```bash
python3 -m unittest discover -s tech_app/eltec_rig/m405m22/tests -v
```

The suite mirrors the v6.1 coverage retimed for 1 Hz and adds the TP412
changes: 1000-sample cycles, the 60 s deadline (including a stability run
closing exactly at 60.0 s), the 5/10 kick/retry policy, the calibrated
sensitivity gate with the TP412 filter table (lot-500 factor 4.30, guard
band, over-max), the 0.8–3.0 V offset band (high-side fail-fast, low-side
deferral to the settled re-read, the no-sensor wake-up poll), firmware
v2.0 rejection of v1.8/v1.9 boards, battery-never-read behavior, the
emitter-off noise test (noise-before-sensitivity order, fixed 3 s quiet
wait, windowed 15% rule with the 3-pass/4-fail boundary, noise fail-fast
without a driven capture, the measured-noise-on-unstable behavior, CSV
round trip, clipped-window handling), the bounded micro-gap tolerance
(tolerated single-gap capture with a note; rejection of too many gaps,
duplicates, and count mismatches beyond the budget), the progress ladder,
the no-406MCA-fallback reference rule, and the isolated results and
launcher identities. Tests skipped off the Xubuntu host: the bash launcher
installer test and (headless) the display-only GUI tests.

## Known caveats

1. Stability/SNR thresholds (0.500 mV DUT peak-delta, 0.250 mV reference
   peak-delta, SNR ≥ 1.5, reference ±25%) and the noise limit's effective
   chain factor (~700x → 429 µV pin-level) remain provisional; the
   2026-08-17 lot-500 comparison calibrated the sensitivity factor (4.30)
   and the noise window allowance (15%), but the noise anchor is a single
   part (500-44) — refine both when more failing parts are available.
2. The sensitivity gate is ON with the lot-500 pairwise calibration
   (`405m22_tp412_lot500_pairwise_v1`); it fails over-max as well as
   under-min, and a reading inside the ±0.10 mV raw near-limit band passes
   with a re-measure warning (no quarantine since 2026-08-25).
3. With the ±5 V unbuffered front end, a floating AIN0 no longer reads as an
   obvious ~2.5 V rail; the 0.05 V plausibility floor (with the 5 s wake-up
   poll) is the only no-sensor guard — a railed ~5 V input is a genuine
   high-offset failure, not a missing part.
4. No battery is monitored until the AIN6 hardware exists (divider + firmware
   channel + thresholds).
