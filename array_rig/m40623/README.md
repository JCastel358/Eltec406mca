# Eltec 40623 Array Tester (TP120) — 50 positions on the DAQ array rig

The 40623 build of the array rig. It measures the two TP120 tests that need
no emitter, fifty detectors at a time: the **offset check** and the **noise
test**. Sensitivity / polarity (the 3 Hz chopper test) waits for the emitter
board. Everything it records is **CALIBRATION PENDING / PROVISIONAL** — see
"Limits" below and `docs/CALIBRATION_RECORD.md` §4b.

Normally launched from the array selector (`../eltec_array_tester.py`); runs
standalone with this directory as cwd:

```
array_rig\m40623\run_eltec_40623_array_tester.cmd          (Windows)
./array_rig/m40623/run_eltec_40623_array_tester.sh         (Xubuntu)
python array_rig/m40623/eltec_40623_array_tester.py --simulate   (no hardware; ELTEC_ARRAY_SIMULATE=1 does the same)
```

`ELTEC_ARRAY_RESULTS_ROOT=<dir>` redirects the results folder. For a
hardware-free demonstration, check **Simulation** in the array selector or
use `--simulate` above. The demo uses a fast virtual clock and isolated
example noise limits to show red and green results; production noise limits
remain unset. The badge reads **SIMULATION**; files carry a simulation flag
and **SIMULATION ONLY** noise-limit provenance. Demo files go under
`tempfile.gettempdir()/eltec-array-simulation`, or `<dir>/simulation` when
`ELTEC_ARRAY_RESULTS_ROOT` is set.

## September 10 readout and noise update

Offset checks now use the working waveform viewer's bulk stream, preserving
channel order and discarding startup/MUX transients. Numerical offset and noise
readings remain visible on each socket. Choose loaded sockets provides an
explicit partial-tray map and requires a fresh offset check after changes.

Noise now uses a nominal **3 Hz/Q=3 band-pass** with RMS and rectified metrics.
No paired legacy/background references are available yet; valid noise readings
remain **REVIEW / CALIBRATION PENDING**, including in CSV, never PASS.
See [NOISE_METHOD.md](NOISE_METHOD.md) for the measurement, qualification,
replay instructions and the bench check still required for the readout repair.

## Hardware chain

```
detector (row r, column c)  ->  unity-gain buffer  ->  DAQ single-ended input CH((r-1)*10 + (c-1))
5 rows x 10 columns                                    ACCES USB-AIO16-64MA DAQ-PACK, USB 2.0
positions labelled row-col (TP120: "1-3" = row 1, part 3)   two DB37s: CH0-CH49
```

The two DB37 cables are **not** a contiguous split of the fifty channels, and
this is the easiest thing on the rig to get wrong. The DAQ splits its 64
single-ended inputs across the two connectors in two blocks of sixteen each
(DAQ-PACK M Series guide, tables 5-1 and 5-2), so each cable carries two
disjoint blocks of the tray rather than one half of it:

| DAQ DB37 | DB37 pins 1-8, 10-17 | DB37 pins 20-35 | array positions |
| --- | --- | --- | --- |
| J2 (AIMUX-64 J3) | CH0-CH15 | CH32-CH47 | 1-1 .. 2-6 and 4-3 .. 5-8 |
| J1 (AIMUX-64 J4) | CH16-CH31 | CH48, CH49, rest unused | 2-7 .. 4-2 and 5-9, 5-10 |

Pins 9, 18, 19 and 36 of each DB37 are AGND, so the channel numbers step over
them. The breakout PCB already follows this pinout - the mapping above is the
board as fabricated, traced from the KiCad netlist - which is why the software
needs no remap: it asks the DAQ for the contiguous scan CH0-CH49 and the
driver returns them in channel order whichever cable each one arrived on. The
connector split matters when you meter a pin by hand, not when you read a
capture.

DAQ facts the code is built around (`docs/daq_usb_aio16_64ma/`, bench-verified
2026-09-02): 16-bit SAR behind two multiplexer stages; the input range is
set per **group of four** channels; the scan is one **contiguous** range;
hardware oversampling = extra back-to-back conversions per channel that
arrive **raw** in streamed mode; **no anti-alias filter**; the device loads
its firmware from the host at plug-in and re-enumerates. Volts are always
computed here from raw counts with our own range table, never taken from
the driver's `ADC_GetScanV` (a `-HG` high-gain unit would mis-scale those;
the bench probe's known-voltage check tells the two apart). Two more facts
found on the unit 2026-09-02: the DLL's immediate-read entry points
(`ADC_GetScan` / `ADC_GetScanV`) rewrite the device's configuration block
(trigger byte 0x05 → 0x04: the pacing clock no longer triggers a scan) and
never restore it, so `AiousbDaq._reassert_config()` re-writes and
read-back-verifies the block after every immediate read and before every
stream start; and a stream that delivers nothing for `STREAM_NO_DATA_TIMEOUT_S`
(5 s) during the quiet wait or the capture is abandoned into the retry path
(`StreamTimeoutError`, "no data from the stream …") instead of holding the
tray.

## DAQ configuration (recorded on every row)

| Constant | Value | Why |
| --- | --- | --- |
| `DAQ_RANGE_CODE` | 2 = 0–5 V on all groups | TP120 offsets (0.3–1.2 V) and a railed part (~5 V) both visible; 76.3 µV/LSB |
| `DAQ_SCAN_HZ` | 1000 scans/s per channel | the noise pipeline then runs with the single rig's exact numbers (decimate 20, 621 taps, 310-sample edge context) |
| `DAQ_OVERSAMPLE` / `DAQ_DROP_CONVERSIONS_AFTER_MUX` | 3 / 1 | 4 conversions per channel per scan, the first (right after the mux hop) dropped, the rest averaged; 200 kS/s aggregate, 400 KB/s USB (~40 % of each ceiling) |
| `STREAM_BUFFER_BYTES` × `STREAM_BUFFER_COUNT` | 64 000 × 32 | multiple of 512 B and of the 400-byte scan; ~5 s of slack before the driver reports data loss |
| `STREAM_RETRY_LIMIT` | 2 | a stream that fails the integrity check (rate off > 1 %, driver pool exhausted, callback error) is retried, then the tray is NOT MEASURED |
| `STREAM_NO_DATA_TIMEOUT_S` | 5.0 s | a stream that delivers nothing for this long during the quiet wait or the capture is abandoned (`StreamTimeoutError`, "no data from the stream …") so the retry policy above runs and the tray ends NOT MEASURED instead of hanging; generous next to the ~0.16 s callback buffers |
| trigger byte | 0x05 | onboard 8254 pacing clock triggers one whole scan per tick |
| engineering immediate reads (operator now uses bulk) | `ADC_GetScan` | the DLL rewrites the trigger byte on every immediate read (0x05 → 0x04, scan mode without the timer; oversample forced to at least 1) and never restores it; the backend re-writes its block after each read and before every stream start (bench finding 2026-09-02: three offset reads followed by a stream started without re-writing the block gave 0 scans in 12 s) |

`daq_bench_probe.py` proves these on the real unit (`info`, `selfcal`,
`config`, `scan` = own-scale volts per channel (the -HG question is settled
only by a known, metered voltage on an input: the driver's `ADC_GetScanV`
uses the same counts × span / 65536 formula, so its column is a sanity check
of our arithmetic, never a gain check), `slots` = which conversion slot is
unsettled, `floor` = instrument noise via the onboard full-scale reference
(the ground reference clips at code 0 on a unipolar range), `stream 60` =
rate / pool / leftover, `crosstalk`). Its numbers go into
`docs/CALIBRATION_RECORD.md` §6.

## The flow (what the technician sees)

The Eltec-branded screen has two entry fields, **Tech name** and **Batch
number**, and a 5 × 10 map of round sockets. Each socket keeps its row-column
position label, numeric offset/noise and result status visible by default.
**Show more** adds assigned sensor numbers; **Show less** hides those numbers.
Click a completed socket for measurements and quality/calibration details. Shading gives the sockets depth
while keeping every row and column visible.

1. **Load the tray.** Load as many detectors as needed, up to fifty. Tray
   and sensor numbers are assigned automatically.
2. **Measure offset.** Switch the rig's physical power on immediately
   before pressing the button. The app connects, configures and calibrates
   the DAQ on demand, then reads all fifty offsets. Empty-looking sockets
   are rechecked together after two seconds and marked grey automatically.
   Green means in range;
   red means out of range. Low or dead-looking readings may still be
   settling after power-on: recheck before discarding a detector. Every
   offset read is retained for audit, including reads before replacements.
3. **Replace and repeat.** Replace red detectors and press **Measure
   offset** again until the loaded sockets are green. If replacements run
   out, remove the remaining bad detectors and measure offset again to
   detect the newly empty sockets.
   Marking a socket loaded again requires a new offset read before noise.
   In simulation, click a red or empty socket to simulate loading a
   replacement; right-click to mark a socket empty.
4. **Confirm vacuum.** Turn on the vacuum yourself and wait for the gauge
   to reach the required setting, then confirm the vacuum setting and that
   the detected count and socket map match the physical tray. This records
   operator confirmation; there is no pressure
   telemetry or configured setpoint. The app only observes detector signals
   and does not switch rig power or vacuum.
5. **Measure noise.** This becomes available only after successful offsets
   for every loaded socket and vacuum/map confirmation. The app runs an
   adaptive waveform settling check (3–20 s), then a
   sixty-second capture at 1000 scans/s per channel, with 310 samples of real
   context on either side for the anti-alias filter. Integrity failures are
   retried. **Stop** interrupts the run and allows a retry.
6. **Read the results.** The map shows failures in red and passes in green
   when limits are defined. Production noise limits are pending, so a part
   with valid data and no other failure shows amber **REVIEW**. Measurements
   remain visible; **Show more** displays assigned sensor numbers.
   The settled offset (mean of the last two seconds) is checked again;
   HO / LO / D fail. Results save automatically: CSV rows, raw capture
   (`.npz`), grid snapshot (`.png`) and the tray event log. **Next tray** is
   available after saving completes.

The operator does not need separate connect, lock, save, capture-duration or
skip-wait controls. `TrayController` retains its legacy engineering API;
the desktop workflow above applies the fixed operator procedure.

Automatic empty detection uses two median-of-three offset reads, separated
by two seconds on hardware (the simulator advances virtually). Both must
be at or below one ADC code above zero, about 76 µV on the 0–5 V range.
This is a provisional empty inference, not proof of physical absence: a
dead/shorted detector or an unpowered fixture can produce the same signal.
The existing vacuum confirmation therefore also confirms the detector
count and map. A manual **loaded** choice always wins over the inference;
if a grey socket contains a detector, click it and recheck its offset.
The existing < 0.05 V dead-detector threshold and offset limits are unchanged.

The settling check compares both raw maxima and minima in consecutive
one-second blocks, on loaded channels only. Capture can begin after three
seconds when both extrema changed by at most 0.1 mV across two consecutive
block pairs. Monitoring minima as well as maxima catches changes in
amplitude or baseline that equal block means could miss. Noise transients
can use the full twenty seconds; reaching that deadline records a warning
and starts the full sixty-second capture, without changing the verdict.
Callback chunks are split at the settling boundary so the final quiet
samples, captured waveform and filter edge context remain contiguous.

This is an explicit operator-requested timing change dated 2026-09-08.
TP120's photographed noise procedure specifies five minutes after power-on
and separately 15–20 seconds after selecting a detector on the switch box.
The array app now uses `NOISE_STABILISATION_S = 0` and the bounded check
above rather than another five-minute countdown. The policy is archived as
`adaptive_extrema_v1_2026-09-08`; the inherited 0.1 mV tolerance schedules
capture only and is not a calibrated noise acceptance limit.

## Limits and what "provisional" means

| Test | Applied now | Provenance | Status |
| --- | --- | --- | --- |
| Offset | 0.3–1.2 V; < 0.05 V on a loaded socket = D; ≥ 4.9 V = HO (railed) | TP120 rev W offset check (fixture 9000054: +8 V, 100 kΩ source resistor) | **PROVISIONAL** until the PCB loading is confirmed to match 9000054 |
| Noise | none — `NOISE_PP_LIMIT_LOW_MV = NOISE_PP_LIMIT_HIGH_MV = None` | TP120's 10.0–37.9 mV are DMM readings behind amplifier 9000232 + rectifier-hold 9000272 (under vacuum, 60 s hold); no pin-level equivalent exists | **PENDING** — qualify a named 3 Hz metric with paired legacy/background readings, then supply a versioned calibration JSON (NOISE_METHOD.md) |

The active noise measurement is the causal 3 Hz/Q=3 method in
`legacy_noise.py`; see [NOISE_METHOD.md](NOISE_METHOD.md). Qualification
requires paired legacy readings and a demonstrated background/resolution
floor for a named software metric. A strict JSON calibration can be supplied
through `ELTEC_ARRAY_NOISE_CALIBRATION`; malformed/mismatched records cannot
silently enable acceptance. The old 0.85-22 Hz windowed peak-to-peak pipeline
and its frozen golden tests remain diagnostics and simulator-demo behavior.
Its 15% window allowance is not the hardware 40623 noise acceptance rule.

Because the raw capture is saved, `replay_legacy_noise.py` can produce 3 Hz
JSON/CSV evidence from an existing NPZ without changing the original.

## Colours

| Socket | Meaning |
| --- | --- |
| neutral, **WAITING** / **RECHECK** | loaded, awaiting its first or a fresh measurement |
| red | offset out of range (HO / LO / D), or failed noise when limits are defined |
| green | offset OK during screening; overall PASS after noise when limits are defined |
| amber, **REVIEW** | noise measured, but production noise limits are not set; not a noise pass |
| grey, **EMPTY** | marked empty or inferred near zero; confirm against the physical tray |
| **NOT READ** | incomplete measurement or rig fault; retry after resolving the cause |

## Files written (outside the repository — `docs/DATA_MAP.md`)

```
Documents\Eltec_40623_Test_Results\40623_array_daq\
├── 40623_array_lot_<lot>.csv               one row per loaded position per tray attempt
├── 40623_array_lot_<lot>_attempts.csv      tray events, offset-read audit and capture/save history
├── noise_captures\lot_<lot>\tray_<n>_raw.npz   waveform_v float32 [50, N] + left/right_context_v [50, 310],
│                                               channels, positions, sensor_numbers, occupancy, metadata strings
└── grid_snapshots\lot_<lot>\tray_<n>.png       the coloured grid as a data sheet
```

The GUI calls the lot identifier **Batch number**; filenames and CSV fields
retain `lot` for compatibility. Every screening read remains in the offset
audit even when the detector is replaced before noise. Each
`offset_measured` event stores all fifty position readings, occupancy,
offset class and applied bounds in its JSON `detail`, together with the
read number, out-of-range positions and simulation flag. The empty-detection
policy, threshold, both reads, recheck wait, occupancy source and inferred
empty positions are retained as well. Screening reads
do not reserve sensor numbers; the final loaded sockets receive their
numbers when noise starts.

CSV columns (`CSV_FIELDS`): identity (lot, tray, attempt, position, row,
col, DAQ channel, sensor number/id, tester, model, procedure, occupancy),
offsets (insertion read, settled value, settle delta, class, limits, gate
status), noise (worst/median pk-pk mV, windows total/over/clipped, limits,
allowance, provenance, verdict, band note), timing (stabilisation wait,
quiet wait + settled flag, capture seconds, versioned noise timing policy,
settling criterion/delta/block count, minimum/maximum wait and stop reason), verdict (pass_fail, verdict,
verdict_status, structured fail reasons, warnings, failure-mode tag,
comments), stamps (`calibration_status`, `calibration_id`), DAQ (serial,
range code, oversample, dropped conversions, scan rate, granted timer Hz,
pool events, stream attempts), file paths, app version, simulated flag.
Older files keep their header when columns are added.

The `noise_settled` tray event and raw capture's `quiet_diagnostics_json`
retain the loaded channel list and every one-second window's maxima,
minima, corresponding deltas, elapsed time and settled/deadline outcome.
These records preserve the evidence even when an older batch CSV header
does not include the new timing columns.

## Failure modes (TP120 page 7)

HO high offset, LO low offset, SH shorted FET, D dead / no output (→ replace
FET); N noisy (→ replace crystal); NL noise low (this rig's name for "under
the low limit"); NM not measured (rig fault); Drop (handling). The saved
failure tag is derived from the measurement verdict.

## Engineering

Engineering tools only: none issues a verdict, none writes a file unless
given an explicit output path (never under `Documents`), and only one
program can own the DAQ at a time — close the tester before running the
probe, the readout or the viewer, and vice versa. The DLL does not refuse a
second program (bench check 2026-09-02): its configuration and stream calls
go through and the first program's stream silently stops delivering (its
integrity check then fails, or the tester's no-data timeout trips), and a
self-calibration attempted while another program streams fails with Win32
status 13. All three take
`--simulate` (no hardware) and default to the tester's acquisition constants
(`--range --hz --oversample --drop --start --end --connect-timeout`; the
readout and the viewer also `--no-selfcal`).

- `daq_bench_probe.py` — see above; `--simulate` runs every command on `SimulatedDaq`.
- `daq_rig_readout.py` — the array rig's counterpart of
  `Arduino/Eltec/esp32_rig_readout.py`, built on `daq_backend.py` only.
  Python API: `ArrayRig(simulate=False, range_code=2, scan_hz=1000,
  oversample=3, drop_first=1, start_channel=0, end_channel=49,
  self_calibrate=True, …)` with `connect()` (connect → configure →
  `ADC_SetCal(':AUTO:')` → configure, the tester's order), `close()` / a
  context manager, `read_offset_voltage(position, reads=24)` and
  `read_offsets()` (median of 24 immediate scans — the ESP32 `OFFSET?`
  depth — all fifty from one read set), `capture(seconds, positions=None,
  quiet_wait_s=0)` → `Capture` (own-scale volts `[channels, N]`; `t_s` /
  `t_us` synthesised from the granted pacing-clock frequency — the DAQ has
  no per-sample timestamps; `channel()`, `means()`, `peak_to_peak_mv()`,
  `band_limited_pp_mv()` = the tester's judged-band pipeline, `subset()`,
  `to_csv()` = `t_us,<position>,…` volts to 6 decimals, `to_npz()` = the
  bench probe's key layout + `drop_first`, replayable by
  `engineer_tools/noise_band/replot_noise_capture.py` given the file path),
  `set_range(code)`, `live_stream(buffer_s=60)` → `LiveStream` (a thread
  owning every device call: `start()`, `wait_ready()`, `snapshot()`,
  `latest()`, `stats()` — the delivery rate between the first and the newest
  chunk, lag, chunks, error, diagnostics — and `stop()`). A capture whose
  stream diagnostics report a problem raises. Honest differences from the
  ESP32 tool: no emitter / PWM / gate / sync (no emitter board yet — the
  tester's `DriveDevice` slot is where it will plug in), and all fifty
  channels always stream together, so selecting a position is free. CLI
  (positions as `row-col`, `CHn` or a channel number; the acquisition
  options above plus `--reads`, accepted before or after the command; exit
  0, 2 on a DAQ error):

  ```
  python array_rig/m40623/daq_rig_readout.py info
  python array_rig/m40623/daq_rig_readout.py offset                 # all fifty as a 5 x 10 table (context flags, never a verdict)
  python array_rig/m40623/daq_rig_readout.py offset 2-4 CH13 13     # one line per position
  python array_rig/m40623/daq_rig_readout.py stream -s 8 [-p 2-4 3-6] [-o cap.csv] [--npz cap.npz]
  python array_rig/m40623/daq_rig_readout.py noise -s 20 [--quiet-wait 3] [-p ...] [-o ...] [--npz ...]
  python array_rig/m40623/daq_rig_readout.py watch [-p 2-4 4-7] [-s 30] [--interval 1]   # text-mode live readout, Ctrl+C
  python array_rig/m40623/daq_rig_readout.py test -s 20             # identity -> offsets -> noise capture
  ```

- `daq_live_waveform.py` — counterpart of `Arduino/Eltec/live_waveform.py`:
  a matplotlib rolling scope of ANY position, switched live. Panels: (1) the
  wideband trace of the selected position with a stats box (mean, raw pk-pk,
  judged-band worst/median, rate, lag, chunks, errors); (2) the 5 × 10 grid
  of all positions coloured by the live offset in TP120's PROVISIONAL bands
  (grey < 0.05 V, amber < 0.3 V, blue 0.3–1.2 V, red > 1.2 V, dark red
  ≥ 4.9 V — context, never a verdict) or, with `g`, by judged-band pk-pk
  with NO limit (sequential colour, never pass/fail); (3) the judged-band
  trace of the selected position (the tester's pipeline on the displayed
  window, per-window pk-pk listed), in place of the ESP32 viewer's
  cycle-average panel. Keys: arrow keys move the selection on the grid
  (wrapping), `n` / `p` next / previous channel, a click on a tile, or the
  "Pos" button; SPACE = Hold / Run (the display freezes, the stream keeps
  running); `]` / `+` / `=` wider, `[` / `-` / `_` narrower, the "Window"
  button cycles the ladder 0.25 0.5 1 2 4 6 8 10 15 20 30 45 60 s clamped to
  `--max-window`; `g` toggles the grid metric; `s` saves the whole buffer of
  all channels to `<save-dir>/daq_live_<YYYYmmdd_HHMMSS>.npz` (the readout's
  npz layout); `q` closes. Options: `--position 1-1|CH13|13`,
  `-w/--window 4`, `--max-window 60`, `--grid-metric mean|noise`,
  `--save-dir DIR` (default: the current working directory), `--save-on-exit`,
  `--exit-after SECONDS` (unattended runs; works headless with
  `MPLBACKEND=Agg`), `--fps 20`, plus the acquisition options above. The
  closing line — "Stream closed: N scans received, rate …, lag …, chunks,
  <diagnostics>, integrity OK" — is the stream's health record. Drawing is a
  canvas timer with blitting, and the y-scale holds with hysteresis like a
  scope.

  ```
  python array_rig/m40623/daq_live_waveform.py --position 2-4 -w 8
  python array_rig/m40623/daq_live_waveform.py --simulate --exit-after 4     # unattended / headless smoke run
  ```

  Both tools were verified on the real unit 2026-09-02 (every command,
  integrity OK at 999–1000 scans/s; the inputs read ~0 V because the array
  PCB was not connected).
- `engineer_tools/array_parity/array_noise_parity.py` — pairs legacy-fixture readings with
  array captures and proposes the pin-level limits.
- `engineer_tools/noise_band/replot_noise_capture.py --position 2-4` — replays a saved
  tray capture through the production pipeline or alternative bands.

## Tests

```
python -m unittest discover -s array_rig/m40623/tests
```

`test_daq_backend.py` (fake DLL + fake clock, including the immediate-read
side effect and the block re-assert), `test_array_analysis.py` (golden
parity + verdicts), `test_array_tester.py` (paths, CSV, numbering, lock,
noise phase with retries and the no-data timeout, save, re-measure, GUI
smoke; results root redirected to a temporary directory and a guard asserts
nothing lands in Documents), `test_adaptive_settling.py` (extrema stability,
loaded-channel masking, 3–20 s timing, cancellation, contiguous sixty-second
capture across arbitrary callback sizes, and saved diagnostic evidence),
`test_daq_rig_readout.py` (59: position tokens,
connect / configuration / self-cal, immediate reads, captures and their CSV /
npz files with an npz replay through the replot tool, `LiveStream` fill /
wrap / stop / error paths and the rate rule, the CLI parser and every command
under `--simulate`, constants drift against the tester, a Documents guard),
`test_daq_live_waveform.py` (40, Agg backend — no window ever opens: the
window ladder, selection, judged-band trace and grid-metric helpers, tile
colours, the text formatters, viewer updates with hold, grid metric and
save, the CLI parser, the headless `--exit-after` path, a Documents guard)
and `test_engineer_tools_array.py` (the parity and replot tools on synthetic
tray captures).
