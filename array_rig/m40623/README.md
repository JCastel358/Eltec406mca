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
the bench probe's known-voltage check tells the two apart).

## DAQ configuration (recorded on every row)

| Constant | Value | Why |
| --- | --- | --- |
| `DAQ_RANGE_CODE` | 2 = 0–5 V on all groups | TP120 offsets (0.3–1.2 V) and a railed part (~5 V) both visible; 76.3 µV/LSB |
| `DAQ_SCAN_HZ` | 1000 scans/s per channel | the noise pipeline then runs with the single rig's exact numbers (decimate 20, 621 taps, 310-sample edge context) |
| `DAQ_OVERSAMPLE` / `DAQ_DROP_CONVERSIONS_AFTER_MUX` | 3 / 1 | 4 conversions per channel per scan, the first (right after the mux hop) dropped, the rest averaged; 200 kS/s aggregate, 400 KB/s USB (~40 % of each ceiling) |
| `STREAM_BUFFER_BYTES` × `STREAM_BUFFER_COUNT` | 64 000 × 32 | multiple of 512 B and of the 400-byte scan; ~5 s of slack before the driver reports data loss |
| `STREAM_RETRY_LIMIT` | 2 | a stream that fails the integrity check (rate off > 1 %, driver pool exhausted, callback error) is retried, then the tray is NOT MEASURED |
| trigger byte | 0x05 | onboard 8254 pacing clock triggers one whole scan per tick |

`daq_bench_probe.py` proves these on the real unit (`info`, `selfcal`,
`config`, `scan` = -HG check, `slots` = which conversion slot is unsettled,
`floor` = instrument noise via the cal-mode ground reference, `stream 60` =
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

- `daq_bench_probe.py` — see above; `--simulate` runs every command on `SimulatedDaq`.
- `engineer_tools/array_noise_parity.py` — pairs legacy-fixture readings with
  array captures and proposes the pin-level limits.
- `engineer_tools/replot_noise_capture.py --position 2-4` — replays a saved
  tray capture through the production pipeline or alternative bands.

## Tests

```
python -m unittest discover -s array_rig/m40623/tests
```

`test_daq_backend.py` (fake DLL + fake clock), `test_array_analysis.py`
(golden parity + verdicts), `test_array_tester.py` (paths, CSV, numbering,
lock, noise phase with retries, save, re-measure, GUI smoke; results root
redirected to a temporary directory and a guard asserts nothing lands in
Documents).
