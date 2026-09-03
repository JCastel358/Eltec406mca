# Changelog — Eltec Test Rig

Newest first. One dated `##` entry per change that alters behaviour, a
threshold, the firmware, the wiring or the documentation structure — added in
the same commit as the change. This file is **history**: it records what
changed and why, at the time it changed. Durable facts (current constants and
their provenance, wiring, where data lives, how to run things) live in
`docs/` and the per-area READMEs and are kept current there; if an entry below
contradicts a `docs/` file, the `docs/` file wins.

Entries before 2026-08-28 were migrated verbatim from the former `status.md`.
Paths they mention may have moved since; the retired applications they refer
to are preserved at git tag `archive/pre-cleanup-2026-08-28`
(`git show archive/pre-cleanup-2026-08-28:<path>`).

## Single-detector rigs: Stop writes the verdict on screen without asking (2026-09-03)

Bench report: ending a batch with **Stop** (or Esc) put up a "save it before
ending the batch?" box for the part still on screen - a third question at
the end of every batch, on a verdict the technician had already judged, with
a wrong answer (or an Esc on the dialog itself) throwing the reading away.

- `stop()` on all three models (405 M22, 406 MCA, 449 M18) now calls
  `save_current_sensor()` directly when a verdict is unsaved, then ends the
  batch. The `askyesnocancel` prompt is gone; there is no way to leave a
  batch and discard a measured part any more. To end a batch *without*
  recording the part in the rig, press Stop **during** the capture - that
  path is unchanged (nothing recorded, the number does not move).
- A write that cannot complete still holds the batch open: `save_current_sensor`
  returns False for a cleared failure mode or a results-folder error and puts
  up its own dialog, so `stop()` returns and the verdict stays on screen.
  A FAIL always arrives with a failure mode pre-selected (`suggest_failure_mode`),
  so this is only reachable if the technician clears the box by hand.
- Ported by hand model by model per `docs/ENGINEER_HANDOVER.md` §4; the array
  rig's tray flow is untouched.
- Docs: `docs/TECHNICIAN_RUNBOOK.md` §3 and `single_detector_rig/README.md`
  action-bar table.
- Tests: `test_stop_offers_to_save_an_unsaved_verdict_first` replaced by
  `test_stop_saves_an_unsaved_verdict_without_asking` (asserts the prompt is
  never called) plus `test_stop_keeps_the_batch_running_when_the_write_fails`,
  in all three suites. Full run: 833 tests, one known Windows-only 406 case.

## 406 MCA: the offset verdict moves to a settled re-read after the capture (2026-09-03)

Bench report: parts failing `HO - High offset` that are inside the
0.3-1.2 V band if they are simply left in the rig a little longer. The 406
was reading the offset once, before the emitter even started, and judging
the part on that number 30-60 s later - while the 405 M22 has judged on a
settled re-read since 2026-08-17 (lot 500: 35 of 48 parts read 0.15-1.1 V
away from their settled value on the insertion read). The 406's parts drift
the other way (down), which is why this shows up as HO rather than LO.

What changed, model 406 MCA only (the 405 and 449 are untouched):

- **The verdict uses an `OFFSET?` re-read taken after the sensitivity
  capture.** No bench time is added: the part has already been powered in
  the rig through the whole stabilization plus the 20-cycle measurement, so
  this is the level TP120's 0.3-1.2 V band describes. The insertion read is
  kept as `offset_initial_v` in the batch CSV.
- **An out-of-band re-read is held, not failed.** The app keeps reading
  (1 s apart, `OFFSET_SETTLE_MAX_WAIT_S` = 20 s) and stops as soon as the
  level is back in band; as soon as it has stopped moving without improving
  (so a genuinely high part still fails in ~2 s); or at the deadline, where
  the last reading is used. "Stopped moving" is a **relative** delta,
  `OFFSET_SETTLE_DELTA_FRACTION` = 10 % of the current reading (62 mV at
  0.624 V) - about 1000x the read-to-read scatter of an `OFFSET?`
  median-of-24, so only real drift can breach it. **A reading still moving
  toward the band never ends the wait early**: "it changed for the better"
  is the case this exists to catch.
- **No noise estimation in this path** (considered and rejected): the
  firmware read is already a median of 24 samples over ~100 ms, so what
  remains is the sensor's own slow wander - the thing being measured - and
  the threshold has three orders of magnitude of margin over the read
  noise. If the wait ever ends early on wander, average three consecutive
  reads per sample before tightening the fraction.
- **In band but still moving = PASS with a warning**, never a failure
  (user decision): a drift limit would be an invented number until a lot's
  worth of `offset_settle_delta_v` exists to derive one from. The result
  card shows an amber "OFFSET WAS STILL SETTLING" note. Note this rig has
  no emitter-off noise test, so that warning is where a drifting part shows
  up.
- **The pre-flight "no sensor" check is low side only.** A high or railed
  AIN0 is a real part whose offset has not settled - it now runs the full
  test and records HO from the settled reading instead of raising the
  empty-slot dialog (the same lesson as the 405's 2026-08-13 change). Near
  0 V still blocks before anything is captured.
- New batch CSV columns: `offset_initial_v`, `offset_settle_s`,
  `offset_settle_delta_v`, `offset_settled`, `offset_settle_reads`. Batch
  files started before today keep their header and stay aligned.
- Simulator: new `Offset settles into band` case (starts at 1.35 V, decays
  into band) exercises the hold without hardware. Stop is honoured inside
  the wait like every capture loop.
- Also fixed in passing: the "Is a sensor loaded?" dialog was missing an
  `f` prefix and printed `{self.current_sensor_id}` literally.

Suite: 830 tests (406 MCA 160 -> 178), one known Windows-only case.

## Single-detector rig: a stalled stream now says which side stopped and restarts itself; the 406 MCA gets the stream-tolerance port and a front-end guard (2026-09-03)

Bench report (Xubuntu, 405 M22, 2026-09-02): "ESP32 noise stream stalled
after 37624 samples" on part after part, the technician pressing Re-measure
each time, and suspected to follow nights the rig stayed powered. The 406
MCA had separately failed a 12 s capture on "1 timestamp gaps (~2 missing
samples); host/firmware sample counts differ (12047/12049)" (2026-08-31).
What the code review found, and what changed:

- **The ESP32 holds no state that ages.** The firmware keeps one latched
  sample and a 48-byte command buffer, allocates nothing while streaming,
  and its 71-minute `micros()` wrap is handled on both sides; every sample
  lives on the laptop. Opening the port resets the board, so the app start
  each morning wipes its uptime anyway. If the overnight correlation is
  real it points at the laptop/USB side - and until today the error could
  not say which. No firmware change.
- **"Stalled" is attributed** (405 M22, 449 M18, 406 MCA). A stall is the
  host seeing no sample for `STREAM_TIMEOUT_S` (2 s). The capture now stops
  the stream *first* and reads the board's `STREAM,END` reply, whose
  numbers (firmware count vs host count, the backlog drained after the
  stop, any READY banner) pick a tag that ends the message: `[host-stall]`
  (the board kept sampling, the laptop stopped reading), `[board-reset]`
  (the counter restarted - the ESP32 rebooted), `[board-silent]` (counts
  match - the ADS1256 data-ready stopped) or `[no-reply]`. The message also
  states the seconds of silence and the advice for that case. The backends
  record `last_sample_monotonic`, the first non-protocol lines
  (`ignored_line_samples`) and `live_samples` for this. The reset *reason*
  is not readable: the ROM banner is emitted at 115200 baud (handover §10).
- **A stall is retried like a micro-gap failure.** `StreamStalledError` is a
  `StreamIntegrityError`, so `call_with_stream_retries` restarts the capture
  (fresh `STREAM,START`, PWM re-armed) up to `REFERENCE_READING_STREAM_RETRIES`
  (2) times - exactly what the technician did by hand - with nothing
  recorded from the silent capture. A dead port is still never retried.
  The status line says "Stream stalled during the noise capture; nothing
  was recorded - restarting the capture (1/2)".
- **Every automatic restart is evidence.** New attempts-log events
  (`attempt_history.py`): `stream_retry` (one row per restart, reason =
  phase + the attributed error) and `rig_note` (something the app put right
  on the rig). Neither moves the attempt count. `on_retry` callbacks now
  receive `(attempt, error)`.
- **406 MCA: the 405's stream work, ported by hand** (handover §4). Its
  backend was the pre-2026-08-12 build (no drain thread, no overflow
  attribution, no power-throttling opt-out) and its validator rejected any
  gap and any count mismatch - the 2026-08-31 failure was a 2 ms USB hiccup
  the 405 and 449 tolerate. The 406 now carries the same bounded budget
  (`STREAM_MAX_MICRO_GAPS` 3 / `STREAM_MAX_MISSING_SAMPLES` 20, refilled by
  `StreamGapFiller`), the same bounded retries around the calibration
  readings, the reference gate and the driven capture, and the
  405-generation backend. Policy decision, user-approved 2026-09-03: the
  same budget as the other two models; 20 ms cannot move a robust
  per-cycle peak. No 406 threshold changed.
- **406 MCA: front-end guard.** `FE,V19` is session state the board drops on
  a reset. `Esp32Rig.ensure_qualified_front_end()` re-reads `FE?` before
  every measurement and calibration and before every stream retry; a
  reverted board is re-programmed and verified as at connect, the status
  line says the board restarted, and a `rig_note` row records it.
- **Tests**: glue 45, 405 M22 200 (4 skipped), 406 MCA 160, 449 M18 135,
  array glue 31, 40623 array 241 - **812**. The 406 backend test
  `test_auto_connect_validates_candidates_and_is_idempotent` gained the
  405's POSIX guard, so on Windows only the bash-installer case remains
  environment-only (`run_all_tests.py` updated).
- Docs: 405/406/449 READMEs (serial-stream reliability), technician runbook
  §5 (the tags, what to tell the engineer), firmware README troubleshooting,
  handover §7/§10, CLAUDE.md. Not changed: thresholds, timeouts, the 2 s
  stall budget, the firmware.

## Single-detector rig: two buttons, and a number only a PASS can spend (2026-09-02)

Behaviour change in all three single-detector testers (405 M22, 406 MCA,
449 M18). The array rig is untouched.

**A sensor number is now earned, not issued.** The skip queue assumed the app
was tracking one physical part through the batch, but the bench does not work
that way: a part that fails is put aside and another one takes its place. So
the number no longer advances on a FAIL - `next_sensor_number_for_batch` is
one past the highest **PASSED** number, and a FAIL or a NOT MEASURED row
leaves its number open for the replacement. Fail on 500-7 and the next part
tested is 500-7 again, whether it is the same part re-seated or a new one out
of the tray; whichever finally passes is the part that ships as 500-7.

Consequences in the data:

- A batch CSV is **one row per test**, not one row per number. `500-7` can
  appear several times, the last of them the PASS. Yield still counts rows,
  so failures are all still in it.
- The `skip_count` column is replaced by **`number_attempt`** (1 = the first
  part tried under this number, 2 = the one loaded after it failed, ...), in
  the same column position. `measure_attempts` now counts the reads of the
  part in front of the technician and starts over for each replacement.
- Reading the highest PASSED number (rather than counting passes) leaves
  batches written before today intact: their numbers were handed out per row,
  so a file whose last pass is 500-9 continues at 500-10 as it always did.

**The action bar is two buttons: Stop and Next.**

- **Next** reads the sensor that is in the rig now. With a verdict on screen
  it writes the row first. There is no "load the next sensor" screen in
  between any more - the technician loads the replacement, then presses Next.
  Start does the same for the first part of a batch, so the load STEP is gone
  entirely and its card (rig picture, and the 405's noise-soak toggle) moved
  onto the setup screen; the 405 soak toggle is also on the result card, where
  it arms the read Next is about to take.
- **Stop** (red) is live at *every* moment, including mid-capture: it bumps
  the measurement token, which the capture loops already poll through
  `cancelled=`, so the reading is abandoned at the next chunk boundary with
  the stream stopped and the emitter switched off by the worker's own
  finally-blocks. Nothing is recorded and the number does not move. With no
  capture running it ends the batch, offering to save an unsaved verdict
  first. Esc is the same control.
- Gone with the skip queue: **Skip part**, **Re-measure**, **Measure
  skipped**, **Back**, **Save + Exit Batch**, and the `skipped` / `resumed` /
  `remeasure` attempt events. A new `stopped` event records an abandoned
  capture. Recording a sensor as NOT MEASURED after a rig fault stays (405 and
  449), renamed off the word "skip" in the UI and in the code
  (`open_not_measured_window`, `save_not_measured_sensor`).

Trade-off, stated because it is a real loss: with Re-measure gone there is no
way to discard a verdict that is already on screen. A suspect reading is
saved as what it says and the part re-tested - which under the new rule costs
nothing on a FAIL, since the number stays open. To abandon a reading without
recording it, press Stop while it is still measuring.

Tests: 753 (glue 44, 405 189, 406 123, 449 125, array glue 31, 40623 241).

## Array rig: DB37 connector split written down (2026-09-02)

Documentation only — no constant, threshold or behaviour changed.

The two DB37 cables between the array PCB and the DAQ carry the fifty
channels in four blocks, not two halves: CH0-CH15 and CH32-CH47 on the DAQ
connector that the guide calls J2, CH16-CH31 and CH48-CH49 on J1 (DAQ-PACK M
Series guide tables 5-1 and 5-2, pins 9/18/19/36 AGND). The breakout PCB was
built to that pinout; its KiCad netlist was re-traced end to end on this date
(sensor-box pin N -> buffer -> series resistor -> DAQ_CH(N-1) -> the DB37 pin
the datasheet assigns to that channel) and agrees with it for all fifty
positions, as does the pre-`norcomp_db37_merge` backup of the board.

Nothing in the software ever assumed a contiguous 0-24 / 25-49 split — the
scan is requested as the one contiguous range CH0-CH49 and the driver returns
the channels in channel order whichever cable each arrived on — but the model
README said only "J2/J1 DB37: CH0-CH49", which reads as though the two cables
divide the tray in half and misled a bench check. The README now carries the
per-connector pin table and the affected array positions, and
`daq_backend.py`'s fixture-geometry block says why no remap belongs there.

The connector split is a bench-metering fact: it matters when probing a DB37
pin by hand, not when reading a capture. `(row-1)*10 + (col-1)` is unchanged.

## Array rig: ESP32-style bench readout and live viewer for the DAQ (2026-09-02)

The bench habits of the single-detector rig (`Arduino/Eltec/esp32_rig_readout.py`'s
`Esp32Rig` API + CLI, `live_waveform.py`'s rolling scope) now exist for the
50-channel DAQ, inside `array_rig/m40623/` and built on `daq_backend.py`
only (nothing shared with the single rig). Both are engineering tools: no
verdicts, output files only at explicit paths, never under `Documents`, one
program on the DAQ at a time.

- `array_rig/m40623/daq_rig_readout.py` - `ArrayRig` (connect -> configure
  -> `ADC_SetCal(':AUTO:')` -> configure, the tester's order;
  `read_offset_voltage(position)` / `read_offsets()` = median of 24
  immediate scans, the ESP32 `OFFSET?` depth; `capture(seconds, positions)`
  -> `Capture` with own-scale volts `[channels, N]`, a synthesised `t_us`
  from the granted pacing-clock frequency (the DAQ has no per-sample
  timestamps), `band_limited_pp_mv()` through the tester's exact judged-band
  pipeline, `to_csv()` (`t_us,<position>,...`) and `to_npz()` (the bench
  probe's key layout + `drop_first`, replayable by
  `engineer_tools/replot_noise_capture.py` given the file path);
  `set_range()`; `LiveStream` = a thread that owns every device call and
  fills a ring buffer (`snapshot`, `latest`, `stats` with the delivery rate
  between the first and the newest chunk - so the driver's buffer latency
  does not read as a shortfall - and the lag behind real time). CLI
  `info | offset [POSITION ...] | stream -s 8 [-p ...] [-o cap.csv]
  [--npz cap.npz] | noise -s 20 [--quiet-wait S] | watch [-p ...] [-s S]
  [--interval 1] | test -s 20`; positions as `row-col`, `CHn` or a channel
  number; `--simulate`; the tester's acquisition constants as defaults
  (mirrored, with a drift test). A capture whose stream diagnostics report
  a problem raises (the ESP32 tool's overrun rule).
- `array_rig/m40623/daq_live_waveform.py` - matplotlib rolling scope of ANY
  position, switched live (arrow keys over the 5 x 10 grid, `n` / `p`, a
  click on a tile, a "Pos" button). Three panels: the wideband trace with
  the stats box (mean, raw pk-pk, judged-band worst/median, rate, lag,
  chunks, errors); the 5 x 10 grid of all positions coloured by the live
  offset in TP120's provisional bands or (`g`) by judged-band pk-pk with no
  limit; and the judged-band trace of the selected position (the tester's
  FIR-decimate-and-detrend pipeline on the displayed window, per-window
  pk-pk listed) in place of the ESP32 viewer's cycle-average panel (no
  emitter board, so no sync to fold on). SPACE = Hold/Run (the slot of the
  ESP32 viewer's emitter toggle), `]` / `[` window ladder 0.25-60 s as
  before, `s` saves the whole buffer as `daq_live_<stamp>.npz` (`--save-dir`,
  default the current directory; `--save-on-exit`), `--exit-after` for
  unattended runs (works headless with `MPLBACKEND=Agg`). Drawing is a
  canvas timer with blitting (a full redraw of the 50-tile figure costs
  ~100 ms); the y-scale holds with hysteresis instead of jumping every
  frame.
- Verified on the real unit 2026-09-02: `info`, `offset`, `test` (offsets,
  then a stream - the order that delivered nothing before the fix in the
  entry below), `stream` 0.5 s / 2 s with CSV and npz, `watch`, and the
  viewer for 6 s under Tk and 4 s headless - all integrity OK at 999-1000
  scans/s. Inputs read ~0 V because the array PCB was not connected.
- Tests: `tests/test_daq_rig_readout.py` (59) and
  `tests/test_daq_live_waveform.py` (40, Agg backend) - token forms,
  connect/config/self-cal, offsets, captures and their files, npz replay
  through the replot tool, `LiveStream` fill/wrap/stop/error paths and the
  rate rule, every CLI command under `--simulate`, the ladder / selection /
  judged-band helpers, viewer updates with hold, grid metric and save, the
  headless `--exit-after` path, constants drift, Documents guards. 40623
  array suite 142 -> 241; `run_all_tests.py` total **705**.
- Pre-commit review fixes (an independent read of the new code): the
  viewer holds its y-scale on a flat trace (a code-0 input used to fail the
  40 % fill rule and force a full figure redraw on every frame - 6 fps with
  the CPU pegged), `LiveStream.snapshot` copies only the wanted rows, the
  END-before-stop flag is decided at delivery on the DLL's thread (the
  consumer could dequeue an early END after the stop was requested), a
  range set between `live_stream()` and `start()` is honoured, duplicate
  `-p` tokens collapse to one, `to_npz('x.NPZ')` returns the file numpy
  writes, and a failing re-assert or stop never masks the read error that
  caused it.
- Docs: README map and test count, `array_rig/README.md` layout,
  `m40623/README.md` engineering + tests, ENGINEER_HANDOVER sections
  3/7/9/10/11, DATA_MAP section 4, CLAUDE.md rule 6 baseline and "easy to
  get wrong" items, runbook (one program on the DAQ at a time - on the bench
  a second program is not refused: it silently takes the stream away from
  the first, whose integrity check then fails).

## Array rig: the DLL's immediate reads clear the trigger byte - backend re-asserts its block; silent streams time out (2026-09-02)

A read-only verification of the DAQ installation and of every assumption in
`daq_backend.py` against the vendor documentation and the DLL's shipped
Delphi source (`C:\Users\Public\Documents\ACCES\USB-AIO16-64MA\Win32\Driver.SRC`)
found the installation clean (driver 3.0.150.9161 CyUSB3, `AIOUSB.dll` 2.4
x64 in System32 loaded by the 64-bit Python, the unit on a root-hub port at
USB 2.0 high-speed, selective suspend disabled on AC and DC; the not-present
"(D15 Lo)" Device Manager entries are the chip's pre-firmware identity and
harmless), every probe command clean again (60 s stream 60 000 scans in
60.00 s, 0 pool events, 0 leftover bytes; one-shot `ADC_BulkAcquire` path
3000/3000; instrument floor 53 uV rms, judged band 50.6 uV median / 77.5 uV
worst), all eleven code-vs-documentation items matching - and one blocker:

- **`ADC_GetScan` / `ADC_GetScanV` rewrite the device's configuration
  block** (`ADC_GetScan_Inner`: scan mode forced, timer and external
  trigger bits cleared, 0x05 -> 0x04, oversample forced to at least 1)
  and never restore any of it. The tester
  polls offsets with `ADC_GetScan` and then starts the noise stream without
  re-writing the block; reproduced on the unit in that exact order: pacing
  clock granted 1000.000 Hz, **0 scans in 12 s**, and the GUI would have
  sat at "quiet wait" until Cancel. The bench spike never saw it because
  every probe command reconfigures first. Fix in `daq_backend.py`:
  `AiousbDaq._reassert_config()` re-writes and read-back-verifies its block
  after every immediate read (`read_scan_counts`, `read_scan_driver_volts`)
  and before every `start_stream` / `bulk_acquire`; the fake DLL in
  `test_daq_backend.py` now models the side effect and three regression
  tests pin the re-assert. Confirmed on the unit: the same sequence now
  streams 3040 scans in 3.04 s with the block reading 0x05 throughout.
- `eltec_40623_array_tester.py`: `STREAM_NO_DATA_TIMEOUT_S = 5.0`
  (`CapturePlan.no_data_timeout_s`) - the quiet wait and the capture loop
  abandon a stream that delivers nothing for 5 s with a
  `StreamTimeoutError`, so the existing retry policy runs and the tray is
  recorded NOT MEASURED instead of hanging (two tests; the runbook's
  troubleshooting row now lists "no data from the stream").
- `StreamDiagnostics`: new `ended_early` - the driver's END flag arriving
  before `stop_stream` asked for it is now a problem ("the record is
  incomplete") and the only case the summary reports as "ended by device
  before stop" (the flag is delivered on every normal stop, so the old
  summary text was noise); pool events reworded to what the flag means (an
  extra buffer had to be inserted: the host fell behind) - still a retry;
  the comment on the END buffer timing corrected (`ADC_BulkContinuousEnd`
  joins the DLL's threads before it returns).
- The "-HG check" was overstated: the DLL's `ADC_GetScanV` uses the same
  counts x span / 65536 formula as our table (source checked), so the
  probe's "driver V" column can never reveal a high-gain variant; only a
  known, metered voltage on an input can. Probe footer, `m40623/README.md`,
  CALIBRATION_RECORD 4b.2 / 4b.3 reworded; `floor` wording corrected to the
  full-scale reference (ground clips at code 0 on a unipolar range);
  "full-speed" -> the link is USB 2.0 high-speed, the 1 MB/s figure is the
  driver's. CALIBRATION_RECORD section 6 array row extended with the
  finding, the driver version and the USB facts. No constant moved.
- Tests: 40623 array suite 134 -> 142 (backend 55 -> 61, tester +2).

## Array rig: bench spike numbers, parity tool, replot of tray captures (2026-09-02)

- **Bench spike on the real DAQ** (`daq_bench_probe.py info -> selfcal ->
  config -> scan -> slots -> floor -> stream 60`, DAQ alone, the PCB inputs
  not connected): the 21-byte configuration block round-trips, self-cal
  takes 0.3 s, the pacing clock grants exactly 1000.000 Hz, a 60 s callback
  stream delivered 60 000 scans in 60.00 s with 0 pool events, 0 leftover
  bytes and 0.4 % CPU in the consumer thread; the driver's end-of-stream
  flag arrives after `ADC_BulkContinuousEnd`. The conversion-slot check on
  floating inputs shows slot 0 off by up to 34 counts (typically 2-7), so
  dropping the first conversion after each multiplexer hop stays. Numbers
  in CALIBRATION_RECORD section 6.
- **Probe fix:** the cal-mode GROUND reference reads exactly code 0 on a
  unipolar range (it clips), so `floor` now uses the onboard full-scale
  reference through the same path (`--floor-source ground` kept for
  bipolar ranges). Instrument floor at the production settings: raw rms
  about 54 uV (0.7 LSB), judged-band window pk-pk median 49 uV, worst
  channel 79 uV.
- Still open on the bench: the -HG scaling check (known voltage on CH0
  with the PCB) and crosstalk (a source on one channel) - section 4b.3.
- `engineer_tools/array_noise_parity.py` - pairs a typed legacy data sheet
  (`sensor_id, legacy_noise_mv[, position, tray]`) with the array rig's lot
  CSV or tray `.npz` captures, fits the legacy chain factor (median ratio
  and regression through origin, on the worst-window and median-window
  metrics), counts how many parts the proposed window classifies like the
  legacy window, replays other bands from the captures (`--replay-band`),
  writes `calibration/parity_<date>.csv|.png` and prints the proposed
  `NOISE_PP_LIMIT_LOW/HIGH_MV`. Never edits the app.
- `engineer_tools/replot_noise_capture.py` understands the array rig's 2-D
  tray captures: each loaded position becomes a capture named
  `tray_<n>_<position>`, `--position` / `--channel` select channels,
  `--model 40623` scans the array root, and captures without a limit show
  "no limit" instead of a verdict. Replays reproduce the tester's numbers
  (the same math on both rigs).
- Tests: `array_rig/m40623/tests/test_engineer_tools_array.py` (parity on
  a synthetic lot with a planted factor, lot-CSV input, tray+position
  fallback, replay band; replot splitting and agreement with the tester's
  numpy port). 40623 array suite: 134; total 598.

## Array rig: 40623 tester, selector, launchers, docs (2026-09-02)

The array rig is complete for offset + noise per TP120 (sensitivity waits
for the emitter board). CALIBRATION PENDING throughout.

- `array_rig/m40623/eltec_40623_array_tester.py` - the model application.
  Tk-free core (`TrayController`, the tests drive it) + Tk GUI. Flow: Start
  lot (connect, configure, self-calibrate) -> Load & offset (offsets polled
  at 2 Hz; parts over 1.2 V or railed turn RED immediately so they are
  pulled before the capture; ~0 V tiles are yellow until the technician
  marks them EMPTY or LOADED; low reads are amber, not a verdict) -> Lock
  tray (occupancy frozen, sensor numbers assigned row-major over the loaded
  positions continuing the lot, the HO parts' FAIL rows written now) ->
  Noise (TP120 5-minute stabilisation countdown, skippable with the actual
  wait recorded; adaptive 3-20 s quiet wait; 60 s capture of all fifty
  channels at 1000 scans/s plus 310 samples of real context each side;
  stream integrity checked, retried up to twice, else NOT MEASURED rows) ->
  Judged (settled offset = mean of the last 2 s is the offset verdict;
  noise per channel in the single rig's band; tiles coloured) -> Save (one
  CSV row per position, raw `.npz` of the tray, grid `.png`, tray events) ->
  Re-measure / Next tray. Disabled "Sensitivity" step and a `drive` slot
  mark where the emitter board plugs in. `--simulate` /
  `ELTEC_ARRAY_SIMULATE=1` run on `SimulatedDaq`; `ELTEC_ARRAY_RESULTS_ROOT`
  redirects the results folder for engineering runs.
- `array_rig/eltec_array_tester.py` + `sensor_versions.py` (selector and
  registry, mirrors of the single rig's; `REQUIRED_HARDWARE` instead of
  firmware), `array_rig/tray_history.py` (tray-event log + numbering
  helpers), launchers/installers with the identities `eltec-array-rig` /
  `com.eltec.array-rig.desktop` / "Eltec Array Rig" and `eltec-40623-array`
  / `com.eltec.40623-array-tester.desktop` / "Eltec 40623 Array Tester"
  (derived from the single rig's templates), icons, `array_rig/README.md`
  and `array_rig/m40623/README.md`.
- Results: `Documents/Eltec_40623_Test_Results/40623_array_daq/`
  (`40623_array_lot_<lot>.csv`, `_attempts.csv`, `noise_captures/lot_<lot>/
  tray_<n>_raw.npz`, `grid_snapshots/lot_<lot>/tray_<n>.png`). CSV columns
  documented in the model README; older files keep their header. Every row
  and capture carries `calibration_status = PENDING`, `calibration_id =
  40623_array50_daq_PENDING`, `verdict_status = PROVISIONAL`, the DAQ
  settings and the stream diagnostics.
- Tests: `array_rig/tests` (selector glue, tray history: 31) and
  `array_rig/m40623/tests/test_array_tester.py` (paths, CSV header
  compatibility, numbering, lock, noise phase with retry and rig-fault
  paths, save, re-measure, limits-defined colouring, GUI smoke; results
  root redirected to a temporary directory with a guard that nothing lands
  under Documents). `run_all_tests.py` now runs six suites: 38 / 175 / 109 /
  111 / 31 / 128 = 592.
- Docs: README (two rigs), CLAUDE.md (rule 1 extended across rigs, six-suite
  baseline, array easy-to-get-wrong items), CALIBRATION_RECORD (§1 array
  mini-table, new **§4b** with every 40623 constant, the TP120 numbers
  verbatim, the derivation plan and the open hardware questions, §6 DAQ
  rows), DATA_MAP (root, layout, tools, backup), ENGINEER_HANDOVER (array
  chain and layout, cross-rig policy, tests, paired-lot procedure, probe,
  open work), TECHNICIAN_RUNBOOK (which rig, **§3b** run a tray,
  troubleshooting, do-nots).
- Bench spike on the real DAQ (probe `info -> selfcal -> config -> scan ->
  slots -> floor -> stream 60 -> crosstalk`) and
  `engineer_tools/array_noise_parity.py` follow in the next commits.

## Array rig: noise analysis port and provisional verdict model (2026-09-02)

`array_rig/m40623/array_analysis.py` - pure math, no I/O.

- The 405 M22 build's emitter-off noise pipeline (Kaiser anti-alias FIR,
  decimate 1000 -> 50 SPS, per-1-s-window least-squares detrend, windowed
  pk-pk with the clip rule re-checked on the raw window, `<=` with the
  1e-12 boundary tolerance) re-expressed in numpy for fifty channels at
  once. Tap values are computed with the same scalar arithmetic so they
  are identical; the rest agrees with the pure-Python original to ~1e-15
  relative. A full 50-channel x 60 s tray analyses in 0.3 s (the original
  would take tens of seconds per tray).
- The original functions are FROZEN verbatim in
  `array_rig/m40623/tests/golden_noise_reference.py` (copied from
  `single_detector_rig/m405m22/stability_analysis.py` at commit d7526b5)
  and used as the oracle by `test_array_analysis.py`; a drift test
  compares that copy with the live 405 module so a change on either side
  is noticed. No code is shared between the two rigs.
- Model 40623 (TP120 rev W) constants with provenance comments: offset
  0.3-1.2 V (PROVISIONAL: PCB loading vs fixture 9000054 unconfirmed),
  +/-0.05 V settle rule, 0.05 V dead floor, 4.9 V rail; legacy noise
  limits 10.0-37.9 mV recorded as what they are (DMM readings behind
  amplifier 9000232 + rectifier-hold 9000272) and the pin-level limits
  `None` until a paired lot derives the chain factor. With `None` limits
  every noise verdict is NO_LIMIT: measured and recorded, never a
  failure. The 405's 15 %-of-windows rule is carried as a structural
  default and marked re-decide.
- Verdict model with enums and structured `FailReason`s (no string-prefix
  sniffing): offset classes OK / HO / HO_RAILED / LO / DEAD / EMPTY (only
  HO and railed fail fast at insertion - offsets settle upward), noise
  verdicts PASS / HIGH (> max_over_fraction of windows over the high limit
  or clipped) / LOW (MEDIAN window pk-pk under the low limit, so one bang
  cannot revive a dead crystal) / NO_LIMIT, position verdicts with the
  precedence EMPTY -> NOT_MEASURED -> FAIL_OFFSET -> FAIL_NOISE_HIGH ->
  NOISE_LOW -> PASS, TP120 failure-mode tags (HO, LO, SH, D, N, NL, NM),
  tile states for the grid, the adaptive quiet-wait rule (never a
  verdict). Every result is stamped calibration_status PENDING,
  calibration_id `40623_array50_daq_PENDING`, verdict_status PROVISIONAL.
- `daq_bench_probe.py floor` and `crosstalk` now report judged-band
  worst/median pk-pk per channel through this module.

## Array rig: DAQ backend, simulator and bench probe (2026-09-02)

First code of the 50-position array rig (`array_rig/m40623/`, model 40623
per TP120). Nothing here issues a verdict yet; this commit is the hardware
boundary and the tool that proves it on the bench.

- `daq_backend.py` - `ctypes` wrapper over the ACCES `AIOUSB.dll` (cdecl,
  Win32 status returns; prototypes from the shipped `AIOUSB.cs`). Fixture
  geometry (`row-col` labels, CH = (row-1)*10 + (col-1)), the range-code
  table with OWN counts-to-volts scaling (never `ADC_GetScanV`, in case the
  unit is a `-HG` variant), the 21-byte configuration block
  (`AdcConfig`, one range on all sixteen 4-channel groups, timer+scan
  trigger 0x05, contiguous scan 0-49, hardware oversample), the bulk byte
  stream de-interleaver (partial-scan carry-over, drop the first conversion
  after each multiplexer hop, average the rest), `StreamDiagnostics` with
  the 1 % scan-rate rule and the driver's "buffer pool exhausted" flag as
  an integrity failure, connect retries while the device re-enumerates
  after its host-loaded firmware, the callback stream
  (`ADC_BulkContinuousCallbackStart` + the 8254 pacing clock; callback only
  copies and enqueues, a consumer thread de-interleaves) and a one-shot
  `ADC_BulkAcquire` fallback. `SimulatedDaq` mirrors the same surface with
  a profiled tray (HO, railed, LO, dead, empty and bursty positions, upward
  offset settling) and injectable stream faults.
- `daq_bench_probe.py` - engineering CLI: `info`, `selfcal`, `config`,
  `scan` (own volts next to the driver's: the -HG check), `slots` (per
  conversion-slot means: is slot 0 the unsettled one?), `floor` (cal-mode
  GROUND = instrument noise), `stream`/`capture` (rate, pool events,
  leftover bytes, CPU; optional raw `.npz`), `crosstalk`; `--simulate` and
  `--oneshot`.
- Production acquisition constants (defaults in both files, explained in
  the tester when it lands): range 0-5 V (76.3 uV/LSB), 1000 scans/s per
  channel, oversample 3 with the first conversion dropped, 64 000-byte
  buffers x 32 -> 200 kS/s aggregate and 400 KB/s over USB (40 % of each
  ceiling).
- Tests: `array_rig/m40623/tests/test_daq_backend.py` (55 cases against a
  scripted fake DLL and a fake clock: block encoding, carry-over at
  arbitrary split points, scaling for every range code, connect retry and
  timeout, read-back mismatch, callback flags and unaligned buffers,
  recorded callback errors, rate check, bulk one-shot, simulator profile).
  `run_all_tests.py` runs it as the fifth suite ("40623 array"); the four
  existing suites are unchanged.
- Bench spike still to do (next entry): `info -> selfcal -> config -> scan
  (known voltage) -> slots -> floor -> stream 60 -> crosstalk` on the real
  DAQ, numbers into `docs/CALIBRATION_RECORD.md`.

## Repository split: single_detector_rig/ + array_rig/ (2026-09-02)

A second test rig is being added: a 50-position detector array (5 rows x 10
columns, one unity-gain buffer per position) read by an ACCES I/O
USB-AIO16-64MA DAQ-PACK, testing offset and noise per TP120 (model 40623)
with an emitter board to follow. The repository now has one top-level
folder per rig; everything they share (firmware, docs, engineer tools,
analysis, assets, the test runner) stays at the top level.

- `tech_app/eltec_rig/` -> **`single_detector_rig/`** (`git mv`, history
  preserved); the now-empty `tech_app/` was deleted. Nothing inside the
  moved tree changed except path text. `array_rig/` arrives in the next
  commits.
- Path references updated: `run_all_tests.py` (the four suite paths; labels
  unchanged), `engineer_tools/replot_noise_capture.py` and
  `emitter_waveform_comparison.py` (`sys.path` inserts), the two 406 MCA
  tests that import the package by name (`single_detector_rig.m406mca`), a
  comment in `m406mca/esp32_backend.py`, and the selector's
  `install_xubuntu_launcher.sh` (its `REPO_ROOT` message climbed two levels
  for a package that is now one level below the root). Docs: `README.md`,
  `CLAUDE.md`, all four `docs/*.md`, the four app READMEs, the noise
  filtering report, `Arduino/Eltec/README.md`, both wiring notes and
  `Arduino/Eltec/versions/README.md` (its two path mentions only - the
  firmware snapshots themselves are untouched).
- Deliberately NOT edited: the `tech_app/v4_emitter` comment in
  `Arduino/Eltec/Eltec.ino` and in every frozen `versions/*/Eltec_v*.ino`
  (hard rule: snapshots are byte-frozen and `Eltec.ino` must stay identical
  to the newest one). Historical paths in this file and in the "History"
  paragraph of `single_detector_rig/README.md` are history and stay as
  written; `archive/pre-cleanup-2026-08-28:tech_app/...` tag paths remain
  valid.
- Evidence added to the repository: `docs/TP120(40623).pdf` (the 40623 test
  procedure, rev W) and `docs/daq_usb_aio16_64ma/` (the DAQ datasheet,
  DAQ-PACK M-series guide, letter of volatility and vendor links).
  `.gitattributes` now marks `*.pdf` binary.
- **Bench PCs: the installed desktop shortcuts point at the old path.**
  Re-run `single_detector_rig\install_windows_launcher.ps1` (Windows) or
  `single_detector_rig/install_xubuntu_launcher.sh` (Xubuntu) once; the
  remembered sensor-version selection is unaffected.
- No behaviour, threshold, CSV or firmware change. Tag
  `archive/pre-array-rig-2026-09-02` marks the tree before the move.
  `python run_all_tests.py` after the move: glue 38, 405 M22 175 (4
  skipped), 406 MCA 109 (the two known Windows-only cases), 449 M18 111 -
  identical to before.

## Live viewer: sync-locked cycle-average panel (2026-08-31)

`Arduino/Eltec/live_waveform.py` — the 449 M18 detector's ~20 mV response at
18 Hz / 20 % sits under a ~50 mV broadband white noise floor (measured by
spectrum on the day's captures: flat ~1 mV/√Hz to ~50 Hz, no mains lines),
so the rolling trace shows hash and the response is invisible live. For
white noise the optimal viewer is averaging, so the viewer now has an
oscilloscope-style average acquisition mode:

- A third panel folds the last N drive cycles (default 64, `--fold-cycles`,
  0 hides the panel) on the firmware sync bit's rising edges and draws the
  mean cycle ±1σ, with the mean-cycle pk-pk in the title. Noise on the
  average falls with √N (64 cycles ≈ 8×; ~3.5 s of history at 18 Hz).
  Cycles are resampled between their own edges, and timestamps are
  modularly unwrapped, so PWM phase restarts and the uint32 micros()
  rollover cannot smear the fold. Needs the ESP32 drive (sync edges); with
  an external drive the panel shows a hint instead.
- The fold uses the whole `--max-window` history buffer, not just the
  displayed window; the rolling trace and sync strip are unchanged (their
  x-axes are now linked explicitly rather than via sharex).
- Viewer-only change - no firmware, threshold or measurement behaviour
  touched. Verified offline (Agg): fold recovers a 12.8 mV synthetic
  18 Hz / 20 % response from 25 mV rms noise, is bit-identical across a
  uint32 timestamp wrap, returns None with no edges; a scripted-stream
  headless run exercises update() in both the 3-panel and `--fold-cycles 0`
  legacy layouts.

## Bench tools: `--duty` on the live viewer, `set_pwm_duty` on the wrapper (2026-08-31)

`Arduino/Eltec/live_waveform.py --duty <percent>` — the bench scope could set
the drive frequency but not the duty cycle, so the 449 M18's 20/80 drive
(TP443 blade equivalent) could not be watched live; only the production app
issued `PWM,DUTY`. Looking at an 18 Hz / 20 % waveform meant running a test.

- `Esp32Rig.set_pwm_duty(percent)` added to `Arduino/Eltec/esp32_rig_readout.py`
  (1–99 %, firmware v3.2+, refuses older builds with a re-flash instruction and
  reports the version found). It mirrors `set_pwm_frequency`: range-checked
  host-side, `PWM,DUTY,<pct>` on the wire, `pwm_duty_percent` tracked on the
  object. Duty survives a later `PWM,FREQ`, and the firmware restarts the PWM
  phase, so the change is clean even mid-drive.
- The viewer applies `--duty` whether or not it starts driving (like `--freq`),
  so a later SPACE toggle uses it, and shows the drive in the plot title and
  the stats box. Out-of-range values and pre-v3.2 boards exit with the message
  instead of a traceback, after the port is closed cleanly.
- The board boots at 50 % and nothing persists, so the 405 M22 and 406MCA
  drives are untouched unless `--duty` is passed. No firmware, threshold or
  measurement behaviour changed — bench viewing only.
- Verified offline against a scripted v3.2 board emulator: `PWM,FREQ,18` then
  `PWM,DUTY,20` then `PWM,ON`, title reads "18 Hz / 20% duty"; a v3.1 board
  exits with the flash instruction; `--duty 0` is rejected host-side before any
  serial IO. The bench board may still be on v3.1 — flash with
  `python Arduino/Eltec/flash_firmware.py` before using the flag.

## Live waveform viewer: the time window is now adjustable while watching (2026-08-31)

`Arduino/Eltec/live_waveform.py` — the rolling window was fixed at launch by
`-w`, so seeing more (or less) time meant closing the plot, restarting the
stream and losing the trace under inspection. It can now be changed live:

- `]` / `+` widens, `[` / `-` narrows, and the new **Window** button next to
  the Emitter button steps up and wraps. Rungs are 0.25, 0.5, 1, 2, 4, 6, 8,
  10, 15, 20, 30, 45, 60 s, plus whatever `-w` was launched with, clamped to
  `--max-window`. The current setting shows in the button and the plot title.
  The sub-second rungs are for the fast drives — 0.25 s is ~4 cycles of an
  18 Hz signal, where the 4 s default packed in 72 and showed only a band.
- The reader's history buffer is now sized from `--max-window` (default 60 s)
  instead of the starting window, so widening shows real samples. It cannot
  invent history: samples older than the window in force were never buffered,
  so a freshly widened plot fills in from the left over the next seconds.
- `-w` is unchanged as the STARTING window (still auto-widened to ~6 cycles
  for a drive below 2 Hz); `--max-window` raises the ceiling and the memory
  the buffer holds (60 s ≈ 75 k samples at 1 kS/s).

No firmware, threshold or measurement behaviour changed — this is a viewer
control only. Verified offline against a synthetic 1 kS/s stream (Agg
backend): each key press moves one rung, clamps at both ends, and the plotted
span and x-limits follow it.

## New engineer tool: legacy-chopper vs rig-emitter waveform comparison (2026-08-31)

`engineer_tools/emitter_waveform_comparison.py` — pre-qualification check
for the 449 M18 app's 18 Hz drive: can the rig's miniature blackbody
emitter (a resistor that must heat/cool every cycle) reproduce the detector
waveform shape the legacy fixture's blackbody + 20/80 chopper blade
produces, or does its thermal time constant round it off?

- `capture --setup legacy` streams AIN0 with the rig PWM forced OFF while
  the LEGACY fixture drives the detector (rig used purely as a recorder;
  detector signal -> AIN0, detector ground -> rig AGND is the one required
  common-ground connection). `capture --setup rig` drives the qualified
  18 Hz (or 5 Hz) / 20 % emitter PWM via the production m449m18 backend,
  waits a thermal warm-up, then streams; the per-sample sync bit gives
  exact cycle edges and the PWM-ON -> detector-peak thermal lag.
- `compare` folds each capture cycle-by-cycle on its own boundaries (sync
  edges when present, fundamental zero-crossings otherwise — a chopper
  motor drifts), normalises the mean cycle to unit pk-pk (amplitude is
  deliberately ignored) and reports 10-90 % rise / 90-10 % fall, width at
  50 %, H2..H5/H1 harmonic ratios, and shape correlation / RMS residual
  against the reference capture, plus an overlay plot.
- Captures land in `~/Documents/Eltec_EmitterComparison` — a new
  engineering-experiment folder, never the `Eltec_*_Test_Results`
  evidence folders. Analysis verified end-to-end on synthetic data (sharp
  25 % trapezoid vs 12 ms-tau first-order lag: rise 1.4 -> 8.5 ms,
  corr 0.886, lag 10 ms recovered).
- Needs firmware v3.2 on the board (backend refuses older at connect);
  the bench board may still be on v3.1 — flash with
  `python Arduino/Eltec/flash_firmware.py` first.
- Same-day usability fix: `compare` with no paths now auto-picks the
  newest `*_legacy_*` capture (reference) and the newest `*_rig_*` capture
  from the default folder and prints what it chose — the placeholder-style
  `compare <legacy.npz> <rig.npz>` example was a PowerShell parser error
  when pasted verbatim, and PowerShell does not expand globs either.

## 405 noise pipeline hygiene + the legacy amplifier measured (2026-08-31)

Follow-up to the 2026-08-31 audit of the noise verdict chain (implementation
verified correct end to end; these are the defects it surfaced). Archived
captures are unaffected — every no-context replay is bit-identical — but
LIVE captures now judge their first/last window with the anti-alias filter
seated on real samples, so a bench re-run on a known part is due before
trusting new edge-window counts near the limit.

- **Edge context for the anti-alias FIR** (the one filtering change):
  `decimate_antialiased` accepts real neighbour samples as filter history;
  reflection padding had let out-of-band interference into judged windows
  0/19 at only ~11–21 dB (vs ≥ 60 dB interior; ≤ 1.4 % effect on all 14
  archived captures). The tester streams 0.31 s extra and seats the FIR on
  the quiet-wait tail (left) + extra samples (right); both slices are
  archived in new `left_context_v`/`right_context_v` NPZ arrays so saved
  captures replay to the live verdict; `replot_noise_capture.py` honours
  them when present.
- **Soak FAIL message fixed**: `apply_noise_gate` computed "allowed" from
  the fixed 15 % and told a 60 s soak FAIL "allowed 9"; it now reports the
  allowance the capture was judged with, and
  `NoiseCaptureReport.max_over_percent` records it (was hard-wired to 15 %).
- `analyze_noise_capture_band_limited`'s `threshold_mv`/`max_over_fraction`
  are now REQUIRED — the old defaults were the withdrawn 75 µV / 20 %
  limits and a new caller that omitted them silently got a retired gate
  (production always passed the constants explicitly). Hand-ported with the
  edge-context change to the dormant m449m18 copy per the porting procedure
  (m406mca has no noise code).
- Stale text swept: five "75 µV" comments → ~429 µV; the live-preview
  comment no longer claims the boxcar preview is the verdict trace; in-app
  help no longer says noise runs after the driven capture nor that the AIN1
  gate is active; "same passband as the boxcar" corrected (same 22 Hz
  passband *edge*; the FIR's −3 dB corner is 24.4 Hz, so the verdict band
  is 0.852–24.4 Hz); the calibration record's noise-row line pointers
  re-reconciled; the clip row's "condemns its ringing neighbour" reworded
  as the emergent effect it is.
- `docs/CALIBRATION_RECORD.md` §2.2 addendum: the legacy amp's frequency
  response was measured (band-pass ×4140 peak at ~1.4 Hz, −3 dB
  0.46–4.1 Hz; nameplate = midband gain; bandwidth still cannot explain
  700 — the residual is a frequency-flat ~3–6×, prime suspect
  source-impedance loading; verification measurements listed). **User
  decision recorded: verdict-level agreement with the legacy fixture is the
  acceptance criterion; the scalar 700 and the judged band stay.**
- Tests: 38 / 175 / 109 / 111 (was 38 / 174 / 109 / 110) — new
  edge-context coverage in both models, soak-message and context-plumbing
  assertions in the 405 integration suite.

## Workspace reorganised for handover (2026-08-28)

- Committed the outstanding work that had only existed in the working tree:
  firmware v3.1/v3.2 snapshots, `flash_firmware.py` and its wrappers, the
  whole 449 M18 tester, the 405/406 GPIO33 backend updates and the
  noise-filtering report.
- Tagged `archive/pre-cleanup-2026-08-28`, then removed from the working tree:
  `tech_app/deprecated/` (v1–v4 LabJack apps, v5/v6/v6.1 ESP32 apps, the
  standalone `405m22_esp32` build, the `eltec_rig_v1` snapshot), the
  LabJack-era `analysis/v1_single_sensor`, `v2_scope_verification`,
  `v3_emitter`, `v4_emitter` scripts, `engineer_tools/eltec_406mca_signal_monitor*`,
  `Arduino/Eltec/siggen_rig_readout.py` and the historical
  `ESP32_ADS1256_Wiring.docx`. Nothing active imported from them
  (205 → 94 tracked files). All four test suites unchanged: 38 / 174 / 109 / 110.
- New documentation set for handover: the root `README.md` rewritten as the
  hub (it still described the LabJack-era 406MCA-only setup);
  `docs/CALIBRATION_RECORD.md` (every constant with provenance — extracted
  from the frozen reference block of the old `status.md`),
  `docs/TECHNICIAN_RUNBOOK.md`, `docs/ENGINEER_HANDOVER.md` (incl. the
  copy-per-model policy and the fix-porting procedure), `docs/DATA_MAP.md`
  (out-of-repo results + backup routine); `Arduino/Eltec/README.md` replaces
  the stale `ESP32_memory.md`; `ESP32_ADS1256_Wiring_v1_7.md` renamed
  `ESP32_ADS1256_Wiring_legacy_v1_9.md`; `status.md` became this file (its
  undated reference block, which contradicted the entries above it on
  firmware version, stability threshold and noise limit, was retired after
  extraction); `CLAUDE.md` added; stale pre-unification paths fixed in the
  model READMEs.
- Hygiene: `run_all_tests.py` (+ `.cmd`/`.sh`) runs all four suites;
  `.gitattributes` line-ending policy; `.gitignore` rules for results and
  captures; the 406 MCA model gained the Windows launchers the other models
  had; stale "v3.0 baseline" comments and the `EMITTER_PWM_CHANNEL` label
  (GPIO25 → GPIO33) fixed in the 405/406 testers.

## 449 M18 frequency-tracking tester + firmware v3.2 `PWM,DUTY` (2026-08-26)

New sensor version in the unified app: **Model 449 M18 (5 Hz + 18 Hz,
TP443)** — `tech_app/eltec_rig/m449m18/`, selectable from the dropdown.

- **Test**: TP443 "449M18 Frequency Tracking" — sensitivity at 5 Hz and at
  18 Hz, both with the legacy fixture's 20/80 blade duty, then the 18/5
  ratio (specs 1–3) and the spec-4 "measure the tray 100 %" flag (ratio
  ≤ 0.72 or a sampled failure). Both drives run back to back per part.
- **Calibration pending**: an electrically pulsed emitter does not modulate
  equally at 5 and 18 Hz, so each frequency needs its own fixture factor
  (`K_5`, `K_18`, paired comparison like the 405's lot-500). Until then
  `SENSITIVITY_GATE_ENABLED = False`: every reading + the raw ratio is
  recorded, the limits are not enforced, and every verdict is stamped
  CALIBRATION PENDING. The offset band is also not gated (TP443 offset page
  not available; placeholder 0.3–3.0 V behind `OFFSET_GATE_ENABLED`).
- **18 Hz sampling**: 55.56 samples per cycle at 1000 SPS — sync validation
  judges the mean cadence (single cycles get a one-sample allowance) and
  stability is judged on blocks of 9 cycles (= 500 samples exactly) so the
  robust peak is repeatable; the 36-cycle window is 4 blocks.
- **Firmware v3.2** (`Arduino/Eltec/Eltec.ino`, archived as
  `versions/Eltec_v3_2/`): `PWM,DUTY,<pct>` (1–99 %, boot default 50 %,
  not persisted) + `pwm_duty` in `STATUS?`. Compiles (290 KB / 22 %).
  **Not yet flashed or bench-verified** — the 449 backend refuses < v3.2,
  the other two models keep working on v3.1. Flash with
  `python Arduino/Eltec/flash_firmware.py`, then confirm `IDN? -> v3.2`
  and `PWM,DUTY,20` -> `OK,PWM,DUTY,20.0`.
- **Tests**: `m449m18/tests` (integration, backend, stability, calibration
  CLI) plus the rig glue/attempt-history suites now cover three models; the
  405/406 suites are untouched.
- **Open**: derive `K_5`/`K_18` (30–50 parts, both fixtures), fill the TP443
  offset band, confirm the 449's polarity convention on the bench (the
  POSITIVE gate is applied at both frequencies, `POLARITY_GATE_ENABLED`),
  and revisit the 0.100 mV peak-delta threshold once real raw amplitudes
  are known.

## Emitter gate moved to GPIO33, firmware v3.1 (2026-08-25)

The emitter PWM/gate output moved from **D25 (GPIO25)** to **D33 (GPIO33)**.
The perf-board wire to the dual-MOSFET module's PWM/TRIG input must move with
it - a board still wired to D25 drives nothing until the wire is moved (or the
host sends `PIN,25`, which the firmware still accepts).

- **Firmware** `Arduino/Eltec/Eltec.ino`: `pinGate = 33`, IDN bumped
  `v3.0 -> v3.1` per the sketch's own rule that every flash-relevant change is
  detectable over serial. Nothing else changed - `PIN,<n>` (2/12/13/14/25/26/
  27/32/33) still retargets at runtime and is not persisted, and GPIO33 gets
  the same RTC-hold release at attach that GPIO25 did. GPIO33 is RTC-capable
  but is NOT a DAC pin (only 25/26 are), so there is no DAC path to detach.
  Archived as `versions/Eltec_v3_1/`.
- **Both host backends**: `PWM_GPIO = 25 -> 33` in `m405m22/esp32_backend.py`
  and `m406mca/esp32_backend.py`. Because each backend already sends
  `PIN,{PWM_GPIO}` after connect, the app - not the boot default - is what
  actually selects the pin; the two now agree. Tests updated to expect
  `PIN,33`.
- **Docs**: `ESP32_ADS1256_Wiring_v2_0.md` (the current guide) and the live
  statements in `ESP32_memory.md` now say D33; the D25-era troubleshooting
  history in that file is left intact and labelled as history.
  `ESP32_ADS1256_Wiring_v1_7.md` is unchanged - it documents the v1.9 legacy
  406MCA rigs, which still gate on D25.
- **Verified on the bench**: flashed COM3, `IDN? -> ELTEC-ESP32-ADS1256,v3.1`,
  `GATE? -> pin=33,drive=0,read=0` at boot, `GATE,ON -> drive=1,read=1` (pad
  readback confirms the pin really drives), `PIN,33` + `PWM,FREQ,1` + `PWM,ON`
  -> `STATUS,pwm=1,pwm_hz=1.000`.
- **`flash_firmware.py`**: new one-command flasher so the sketch can be
  uploaded without the Arduino IDE - it finds the IDE's bundled `arduino-cli`,
  auto-detects the CP210x port, compiles, uploads, then confirms `IDN?` and
  `GATE?` over serial. `--sketch versions/Eltec_v2_2` puts the board back on
  any archived build.
- **Not changed**: the frozen apps under `tech_app/deprecated/` still send
  `PIN,25` and are pinned to older firmware, so a legacy rig keeps its own
  wiring.

## Near-limit sensitivity is a PASS with a warning, both models (2026-08-25)

The `+/-0.10 mV` raw band around the sensitivity limit (405 M22: `1.29-1.49
mV` raw on -625, `~5.99 mV` legacy; 406 MCA: `2.43-2.63 mV` raw, `~4.0 mV`
legacy) used to produce a `RETEST / QUARANTINE`
verdict with its own failure mode and *Save Quarantine* buttons. A reading in
that band is within the margin of error of the `x4.30` conversion factor, so
the app now treats it the way production wants: **the sensor passes**, the
result page shows the green `PASS · NEAR LIMIT` banner plus an amber card
("Passed - sensitivity near the limit ... Suggestion: Re-measure to confirm.
No quarantine is needed - if you move on, this sensor is saved as a PASS"),
the status line says the same, and the normal *Save + Next Sensor* / *Save +
Exit Batch* buttons record a plain `PASS` row. Re-measure stays one click
away in the footer. Code-wise the band is `SENSITIVITY_NEAR_LIMIT`
(`sensitivity_gate_outcome`, still written to the `sensitivity_gate_outcome`
CSV column so the record shows which passes were near the limit); the note
goes to `FinalResult.warnings` instead of `fail_reasons`; `OUTCOME_RETEST`,
the `RETEST - Sensitivity guard band` failure mode, the RETEST summary chip
and the quarantine footer labels are gone. Older batch CSVs that still carry
`RETEST` rows are shown as failures in the summary (they were quarantine
records, not passes). Same change in `m405m22/` and `m406mca/`. 405 M22: 174
tests OK; 406 MCA: 78 with the three long-standing Windows-only environment
errors; unified app: 33 OK.

## Eltec Test Rig v2.0: Skip part, footer Re-measure, attempt history (2026-08-24)

Technician-UI update to `tech_app/eltec_rig/` (both models, same code in
`m405m22/` and `m406mca/`), plus a repository tidy-up:

- **Footer is now the action bar**: Back · Measure skipped (N) │ Skip part
  (amber) · Re-measure (blue outline, moved out of the small tools row) ·
  Save + Exit Batch · **Save + Next Sensor (green)**. Buttons are one size
  larger (`xl`); `RoundButton` gained `success` / `warn` palettes.
- **Skip part** (load or result step): reason dropdown + optional note, then
  the next fresh number loads. The skipped id is NOT spent — the next fresh
  number is derived from the batch CSV AND the attempt log, so a part can
  never be counted twice. Skipped parts form a first-skipped-first-measured
  pile: **Measure skipped (N)** lists the ids in order and loads the first;
  after each save the next skipped id loads automatically until the pile is
  empty, then fresh numbering resumes. Re-skipping sends a part to the back.
  The batch summary and the batch-start status line show what is still
  skipped. The rig-fault view keeps "Record as NOT MEASURED".
- **App renamed "sensor tester" (2026-08-25)**: the header now reads
  `405 M22 SENSOR TESTER` / `406MCA SENSOR TESTER`, the 406 window title is
  "Eltec 406MCA ESP32 Sensor Tester v6.1", and both Xubuntu desktop entries
  say "Adaptive Sensor Tester" (the 406 `Comment=` also said it tests
  "emitters" - now "sensors", matching the 405 entry). Hardware references
  are untouched on purpose: `EMITTER_PWM_*`, the emitter-off noise test, the
  emitter-health/reference wording and the header's "EMITTER RIG" subtitle
  all still describe the fixture's chop source. The internal class name
  `EmitterTesterApp` is unchanged (code only, never displayed).
- **Footer never clips again (2026-08-25)**: full screen on Windows, the
  v2.0 action bar's rightmost button (*Save + Next Sensor*) ran past the edge
  of the content column - it needs ~1715 px where a maximized 1920 screen
  leaves 1490 (the step rail, divider and padding take the rest; a 1366
  screen or 150% scaling is worse). The bar is now two groups (nav left,
  actions right) and `_fit_footer` measures the visible buttons against the
  real width, then takes the first `FOOTER_VARIANTS` step that fits: full
  labels -> drop the `(Enter)`/`(Esc)` hints -> compact wording (*Save +
  Next*, *Save + Exit*, *Skipped (3)*) -> wrap the actions onto a
  second row at full size -> shrink the buttons. Bound to the footer's
  `<Configure>`, so it re-fits (and recovers) on maximize/resize and adapts
  to Xubuntu's font metrics without a hardcoded width. `RoundButton.restyle()`
  re-sizes a button in place. Both models; 6 new tests each (405 M22: 174 OK,
  406 MCA: 109 with the two long-standing Windows-only environment failures).
- **Selector opens maximized (2026-08-25)**: `eltec_rig_tester.py` now
  starts full screen the same way the model testers do (`zoomed` on Windows,
  `-zoomed` after mapping on X11, screen-sized geometry as a fallback) and
  re-applies it when a tester exits and the selector un-minimizes. It had to
  become resizable for that (a fixed-size window cannot be maximized), so
  the content block is centered by weighted spacer rows/columns and its type
  is one step larger; 640x520 minimum. Selector suite 33 tests OK.
- **Same-day follow-ups**: (a) the skip dialog is comment-only (no reason
  dropdown); (b) **406 MCA reference gate disabled** (`REFERENCE_GATE_ENABLED
  = False`, same mechanism/card text as the 405 M22 build) because the
  op-amp crosstalk made the reference uncalibratable — flip back to True and
  recalibrate once the channel-isolated op-amp board is in; the gate code is
  kept and tested with the flag forced on; (c) **shorted/dead sensor**: a
  floating AIN0 now raises `NoSensorDetectedError` and the app asks "Is a
  sensor loaded?" — Yes records a FAIL (no offset, failure mode preset to
  "SB - Sensor bad") that the technician saves normally; No keeps the old
  "nothing recorded" path. Both models.
- **Attempt history**: new shared `eltec_rig/attempt_history.py` writes
  `<lot>_attempts.csv` next to each batch CSV — one row per `measured` /
  `measure_error` / `remeasure` / `skipped` / `resumed` / `saved` event with
  the verdict, offset, sensitivity, polarity, noise worst pk-pk, fail
  reasons, reason and note. Verdict rows gain trailing `measure_attempts`
  and `skip_count` columns (older batch files keep their header).
- **Deprecated folder**: `tech_app/405m22_esp32`, `v6_1_esp32`, `v6_esp32`,
  `v5_esp32`, `v1_single_sensor`, `v2_scope_verification`, `v3_emitter`,
  `v4_emitter`, `v6_1_failure_calibration` moved (git mv, contents unchanged)
  to `tech_app/deprecated/`, plus `deprecated/eltec_rig_v1/` = snapshot of
  the unified app before this change. `engineer_tools/eltec_406mca_signal_monitor*.py`
  and `analysis/v1_single_sensor/test_406mca_analysis.py` repointed at the
  new v1 path; firmware `versions/README.md` paths updated.
- **Tests**: `eltec_rig/tests` 31 (17 glue + 14 new attempt-history / skip
  flow for both models); `m405m22` 166 OK (4 skipped); `m406mca` 99 with the
  same two Windows environment-only failures as before. Both real Tk apps
  were driven through skip → measure → re-measure → save → measure skipped
  in simulator mode without error.

## 405 M22 noise verdict: anti-alias FIR replaces the boxcar (2026-08-20)

The noise gate's band-limiting step had a real aliasing defect, found while
characterizing the pipeline's effective passband (the verdict is judged over
~0.85–22 Hz: the per-window detrend is a high-pass with −3 dB at 0.85 Hz,
the 20:1 decimation a low-pass at 22.17 Hz). After decimation to 50 SPS the
Nyquist is 25 Hz, and the boxcar's ~−13 dB sidelobes let everything above
fold into the judged band: 60 Hz mains landed at 10 Hz attenuated only
16 dB, and on the interference-heavy `test-22` bench capture the folded-in
content measured **62 µV RMS = 41% of the honest in-band signal** (quiet
lot-500 parts: ~2%, harmless). Phantom energy the legacy AC-coupled amp
chain never displayed was being counted as part noise.

- **Fix (unified app only, `eltec_rig/m405m22/stability_analysis.py`):**
  `analyze_noise_capture_band_limited` now decimates through a new
  `decimate_antialiased` — a Kaiser windowed-sinc FIR (621 taps, pure
  stdlib, cached; `math.sumprod` fast path ≈ 12 ms per 20 s capture) with
  the SAME passband (flat to 22 Hz, matching the boxcar's −3 dB corner) and
  the same output timeline (one sample per 20-raw-sample block, so all
  window/clip index math is unchanged), but ≥ 60 dB stopband from 28 Hz
  (60 Hz mains: −84 dB vs the boxcar's −16 dB). Edges use odd-reflection
  padding, which passes a linear settling ramp through EXACTLY (machine
  precision), so the adaptive quiet-start/per-window-detrend behavior for
  still-settling captures is preserved. `decimate_boxcar` stays for the
  live preview (display-only) and for A/B comparisons.
- **Calibration continuity PROVEN by replaying every saved raw capture**
  (`noise_captures/`, old vs new at the production 15% allowance): all nine
  verdicts identical, including the lot-500 anchors — 500-44 reads 502 µV
  worst / 1/20 over (was 501 µV) and stays PASS; 500-27 stays 18/20 FAIL;
  quiet parts move 1–7%. `test-22` (the 41% case) drops 729 → 689 µV worst
  and still fails 20/20 — honestly now: its ~9.7/19.4 Hz spike-train
  components are genuinely inside the declared 22 Hz band (the flat
  passband counts 19.4 Hz at 1.0 where the boxcar drooped it to 0.75).
  Whether 10–22 Hz interference SHOULD count against a part is the separate
  open legacy-amp-passband question below.
- **Squares read slightly higher through the flat passband** (in-band
  harmonics no longer drooped + band-edge Gibbs): the synthetic test
  fixtures' 2 mV square reads 2.356 mV (was exactly 2.000). Window-over
  counts — the thing the verdict uses — are unchanged on every fixture and
  every real capture. A raw-clip rail now also condemns its ringing
  neighbor window (windows_over 2 with 1 clipped window in the unit test);
  production-irrelevant since any clipped window already fails the capture.
- **Tests: 166 discovered (was 165; 4 skipped as before)** — new
  `test_antialias_decimation_rejects_folding_frequencies` (in-band tones
  unity, 40/60/120 Hz crushed ≥ 60 dB, ramp transparency, boxcar-identical
  timeline) plus four updated pinned values with comments explaining the
  physics. Unified glue 17/17. The frozen standalone `405m22_esp32/` build
  keeps the boxcar untouched, per the originals-frozen rule.
- **Engineer tools (new, repo `engineer_tools/`):**
  `replot_noise_capture.py` replays any saved `*_noise_raw.npz/.csv`
  through the exact production pipeline plus arbitrary zero-phase band-pass
  / low-pass / boxcar variants (PNG per capture: raw trace, judged traces
  vs the red limit, raw spectrum; verdict-comparison table; `--boxcar 20`
  reproduces the historical pipeline). `filter_response_analysis.py`
  measures the pipeline's passband/aliasing and tests legacy-amp passband
  hypotheses against a measured capture spectrum.
- **Legacy-amp findings from the same analysis:** no physically sensible
  1 Hz-centered band-pass explains the ~700x effective chain factor from a
  4000x nameplate (best achievable ratio 0.235 vs the required 0.175, and
  only at a degenerate 12 Hz notch) — the 700-vs-4000 gap is a REAL GAIN
  difference (10:1 probe setting, amp range switch, or wrong nameplate),
  not a bandwidth effect. Asking the amp builder for the passband corners
  (input coupling C + feedback C) and the true midband gain / probe setting
  remains the open action; a known passband would let the rig replicate the
  amp's response digitally instead of scaling through one factor.
- Also noticed: `500-27_noise_raw.npz` and `_2` hold byte-identical
  waveforms (duplicate save, both 09:39) — check whether re-measure can
  save a stale buffer.

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
- **Tests**: unified glue 17/17; `m405m22` 165 (4 skipped, as before);
  `m406mca` 99 (4 new front-end-selection tests; on Windows the same two
  long-standing environment-only failures as v6.1 — the POSIX bash-installer
  test and the POSIX exclusive-tty assertion — both pass on Xubuntu).
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
