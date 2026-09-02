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

`ELTEC_ARRAY_RESULTS_ROOT=<dir>` redirects the results folder (engineering
runs that must not leave rows in the production folder).

## Hardware chain

```
detector (row r, column c)  ->  unity-gain buffer  ->  DAQ single-ended input CH((r-1)*10 + (c-1))
5 rows x 10 columns                                    ACCES USB-AIO16-64MA DAQ-PACK, USB 2.0
positions labelled row-col (TP120: "1-3" = row 1, part 3)   J2/J1 DB37: CH0-CH49
```

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
| immediate reads | `ADC_GetScan` | the DLL rewrites the trigger byte on every immediate read (0x05 → 0x04, scan mode without the timer; oversample forced to at least 1) and never restores it; the backend re-writes its block after each read and before every stream start (bench finding 2026-09-02: three offset reads followed by a stream started without re-writing the block gave 0 scans in 12 s) |

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

1. **Lot** — lot number, tray number, tester name; *Start lot* connects the
   DAQ, writes the configuration, runs the self-calibration.
2. **Load & offset** — every position's DC offset is polled twice a second.
   A part over 1.2 V or railed turns **red** immediately ("HO – pull"): pull
   it now, before any time is spent on noise. A tile reading ~0 V is
   **yellow** ("empty? click"): click it to say EMPTY or LOADED — the app
   cannot tell an empty socket from a dead part. Low reads are **amber**
   ("settling"): not a verdict yet, offsets settle upward after power-on.
3. **Lock tray** — occupancy frozen, sensor numbers assigned **row-major over
   the loaded positions** starting after the highest number already in the
   lot (editable), the HO parts get their FAIL rows written **now**.
4. **Noise** — TP120's 5-minute stabilisation countdown (skippable; the
   actual wait is recorded on every row), then an adaptive quiet wait (3–20 s,
   never a verdict), then the capture: 60 s (TP120's hold time) or 20 s
   (engineering) of all fifty channels at 1000 scans/s, plus 310 samples of
   real history on each side to seat the anti-alias filter. Integrity is
   checked; a bad stream is retried.
5. **Judged** — tiles coloured (legend under the grid). The **settled
   offset** (mean of the last 2 s of the capture) is the offset verdict:
   HO / LO / D fail; the insertion read is recorded separately. Noise is
   judged per channel in the single rig's band; with no limit derived yet the
   tile shows the value and "no limit yet".
6. **Save tray** — one CSV row per loaded position, the raw capture
   (`.npz`), the grid snapshot (`.png`), the tray event log. *Re-measure*
   runs the noise phase again as attempt n+1 with the same sensor numbers.

## Limits and what "provisional" means

| Test | Applied now | Provenance | Status |
| --- | --- | --- | --- |
| Offset | 0.3–1.2 V; < 0.05 V on a loaded socket = D; ≥ 4.9 V = HO (railed) | TP120 rev W offset check (fixture 9000054: +8 V, 100 kΩ source resistor) | **PROVISIONAL** until the PCB loading is confirmed to match 9000054 |
| Noise | none — `NOISE_PP_LIMIT_LOW_MV = NOISE_PP_LIMIT_HIGH_MV = None` | TP120's 10.0–37.9 mV are DMM readings behind amplifier 9000232 + rectifier-hold 9000272 (under vacuum, 60 s hold); no pin-level equivalent exists | **PENDING** — derive with a paired lot (`engineer_tools/array_noise_parity.py`), then fill the constants, update the record, bump `CALIBRATION_ID` |

The noise *measurement* is the single rig's: Kaiser anti-alias FIR decimating
1000 → 50 SPS, per-1-s-window least-squares detrend, windowed peak-to-peak,
clipping re-checked on the raw window — an emergent ~0.85–22 Hz band. The
pure-Python originals are frozen in `tests/golden_noise_reference.py` and the
numpy port is checked against them on every test run. When limits exist the
structural rules are: HIGH if more than 15 % of the windows are over the high
limit (or clipped), LOW if the **median** window pk-pk is under the low limit
(a dead crystal is quiet in every window; the median ignores one bang). Both
rules are carried from the 405 M22 and marked "re-decide with the paired lot".

Because the raw wideband capture of every tray is saved, any band or limit
decided later can be replayed on real parts (`engineer_tools/replot_noise_capture.py`).

## Colours

| Tile | Meaning |
| --- | --- |
| blue | loaded (Phase A) / locked |
| red | offset FAIL — HO or railed (immediately, pull the part), LO or D (after the capture) |
| purple | noise FAIL (N) — only once a high limit exists |
| dark purple, hatched | noise low (NL) — only once a low limit exists |
| green | PASS (limits defined) |
| blue-grey | measured, **no noise limit yet** (today's normal "good" tile) |
| amber | low / settling read in Phase A (judged later) |
| yellow | reads ~0 V: click to mark EMPTY or LOADED |
| grey | empty socket |
| grey, hatched | not measured (rig fault after the retries) |

## Files written (outside the repository — `docs/DATA_MAP.md`)

```
Documents\Eltec_40623_Test_Results\40623_array_daq\
├── 40623_array_lot_<lot>.csv               one row per position per tray attempt (HO rows at lock time)
├── 40623_array_lot_<lot>_attempts.csv      tray events: locked / stabilisation_shortened / capture_started /
│                                           capture_retry / capture_error / judged / saved / remeasure
├── noise_captures\lot_<lot>\tray_<n>_raw.npz   waveform_v float32 [50, N] + left/right_context_v [50, 310],
│                                               channels, positions, sensor_numbers, occupancy, metadata strings
└── grid_snapshots\lot_<lot>\tray_<n>.png       the coloured grid as a data sheet
```

CSV columns (`CSV_FIELDS`): identity (lot, tray, attempt, position, row,
col, DAQ channel, sensor number/id, tester, model, procedure, occupancy),
offsets (insertion read, settled value, settle delta, class, limits, gate
status), noise (worst/median pk-pk mV, windows total/over/clipped, limits,
allowance, provenance, verdict, band note), timing (stabilisation wait,
quiet wait + settled flag, capture seconds), verdict (pass_fail, verdict,
verdict_status, structured fail reasons, warnings, failure-mode tag,
comments), stamps (`calibration_status`, `calibration_id`), DAQ (serial,
range code, oversample, dropped conversions, scan rate, granted timer Hz,
pool events, stream attempts), file paths, app version, simulated flag.
Older files keep their header when columns are added.

## Failure modes (TP120 page 7)

HO high offset, LO low offset, SH shorted FET, D dead / no output (→ replace
FET); N noisy (→ replace crystal); NL noise low (this rig's name for "under
the low limit"); NM not measured (rig fault); Drop (handling). The app
suggests the tag from the verdict; the technician can change it.

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
  `engineer_tools/replot_noise_capture.py` given the file path),
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
- `engineer_tools/array_noise_parity.py` — pairs legacy-fixture readings with
  array captures and proposes the pin-level limits.
- `engineer_tools/replot_noise_capture.py --position 2-4` — replays a saved
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
nothing lands in Documents), `test_daq_rig_readout.py` (59: position tokens,
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
