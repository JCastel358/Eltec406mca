# Eltec Array Rig — fifty detectors at once on a USB DAQ

## Current M25/HUB hardware — updated 2026-11-12

Before **Start**, choose **Noise only** (default), **Offset only**, or
**Offset and noise**. Use **Loaded slots** to select the installed parts;
noise alone cannot reliably identify an empty slot. Unselected tests are
saved as **NOT_TAKEN**, with blank numeric values, not zeros or failures.
Offset plots use a common 0–1.5 V scale (expanded for excursions), while
the exact measurements and raw recordings remain saved.

Offset recording starts when **80% of loaded, nonexcluded parts** show five
stable one-second windows, or after **90 seconds** with a fallback warning.
Persistent offset drift is flagged for checking/retesting. Noise uses a
**fixed 90-second warm-up**, then a **100-second recording** with a fresh
software meter reset. It does not require noise amplitude to stabilize or
issue noise-only “not settled” faults. This timing is provisional; calibrated
noise acceptance remains disabled.

After a run, **Retest parts** lets the technician link each part's original
position to a destination slot, including a different slot. Move parts using
the established power-off procedure, restore readiness, confirm the mapping,
then start. Each valid retested stage replaces that part's current assessment;
stages not selected and earlier recordings remain saved. An interrupted retest
does not erase the earlier valid result. Batch screening yield counts each
linked part once, regardless of attempts. It means **no flagged issue among
assessed parts**, not a calibrated detector acceptance yield.

Three consecutive valid flagged checks at a slot produce a warning before
subsequent starts, across batches and app restarts. Unchecked suspect slots
are excluded from result analysis. Only check **verified repaired** after
checking/fixing that slot; this records the technician and resets its warning.
Empty/no-signal slots and acquisition interruptions do not build a failure
streak. Retest attempts still contribute physical-slot evidence. History starts
with this version, is local to this fixture/results root, and is separate for
simulation. The latest tracked tray is available again after reopening.

The current app uses the original Eltec logo and a short sequence of
screens: **Batch setup → Power-on checklist → 50-detector tray**. It opens
in your browser but runs entirely on this computer; no internet service or
Arduino update is involved. Keep the dashboard available during acquisition.
The tray screen keeps all 50 positions, **Start**, **Stop**, and progress
together. Select a detector to reveal its details and scroll to them;
**Back to tray** returns to the overview. There is no separate attention
table or Open report button.

Close the browser window with **X** to exit, then use the same shortcut to
open the app again. Closing requests Stop if a check is running; the app
finishes saving available data before exiting. An idle app exits a few
seconds after the page closes. Refreshing the page keeps the dashboard open.

Click a position for its **Mean offset** and **Noise at 3 Hz**. The offset
graph shows the recorded slope; the noise graph shows the held reading over
time with 10.0–37.9 mV shaded as a reference only. Saved HTML reports retain
RMS, waveform excerpts, min/max, slope and engineering notes, and work offline.
Red indicates a valid
offset failure or channel-specific signal problem; a green **offset pass** badge refers
only to the provisional offset window. Noise remains amber Review because
no acceptance limits have been commissioned.

Interrupted acquisition is shown separately from detector failures. A brief
DAQ delivery timeout can retry only the affected stage, up to three total
attempts, while retaining valid offset results. Every attempt records a fresh
continuous waveform; missing segments are never joined. If hardware routing,
range and readiness remain verified, an automatic retry keeps the completed
settling period. Stop, power/readback faults and unsafe shutdowns do not retry.

If noise still cannot finish, **Retry noise** retains the valid offset record.
After reopening the app, **Recover interrupted batch** offers the latest
eligible saved run. Confirm that the detectors, wiring, alignment and fixture
power have remained unchanged; otherwise use Retest parts for the same parts
or start a new batch for different parts. A saved-run
retry switches back to noise, warms up for 90 seconds and records a fresh 100 seconds.
It writes a new folder and preserves
the original evidence. Moving parts or cycling fixture power requires fresh
measurements rather than reusing the earlier offset acquisition.

Automatic no-signal detection checks two separated DC windows near zero.
Such slots are marked **No detector sensed — empty, dead or disconnected**
and excluded from noise result analysis, while retaining their raw data and
clickable details. This is an inference, not proof of an empty socket or a
dead detector. Floating empty inputs can also appear to have a signal.

The normal **Eltec Array Rig** launcher now opens the M25/HUB check for one
HUB and two M25 cards. Upper M25 connects to DAQ J2; lower M25 to J1. The
app uses the full modular channel map and both cards' status readbacks.

1. Close the live viewer/quick-check window and other DAQ applications.
   Load the detectors using the established power-off procedure. Restore
   supplies, cooling and physical RUN; prepare the vacuum/illumination for
   the measurement before starting. The app has no vacuum control.
2. Run `run_eltec_array_tester.cmd`, or directly run
   `m40623/run_eltec_40623_array_tester.cmd`. On the browser's setup screen,
   choose the model (currently **40623**), enter your name and batch number,
   and choose **Actual detectors**. Press **Enter** or the continue button.
3. Follow the illustrated checklist: detector box powered, main switch
   **ON**, then physical **START/RUN** pressed. Confirm the other fixture
   preparation described above. Press **Enter** again to open the tray;
   this navigation does not start acquisition.
4. Choose the test and loaded slots, then press **Start**. All 50 positions
   are scanned together; only selected parts enter the results. Each selected
   stage settles offset or warms up noise, then records **10 s offset** or **100 s noise**.
   Review any slot warning, leave unverified suspect slots empty, then continue.
   The app saves and returns to DC. Keep the fixture undisturbed during testing.
5. When the run has finished saving, select any detector to inspect its
   waveforms. Use **Next batch** or **Enter** to enter the next batch number
   while retaining the model and technician, then repeat the checklist.
   Use **Main setup** or **Esc** to return to the initial screen while idle.
   Use **Retest parts** for the same parts, so yield does not count them again.
   Every new run checks settling. **Stop** retains available data as incomplete
   and attempts to restore DC; wait for acquisition and saving to finish
   before changing batches or leaving the tray screen.

DC is checked against the existing **provisional 0.3–1.2 V window**.
Noise is DAQ-output RMS at 3 Hz/Q3 and 1–100 Hz, including front-end gain.
The live **Noise at 3 Hz** reading is the **Legacy-equivalent estimate (mV)**: nominal
gain scaling, causal 3 Hz/Q3 filtering, absolute-value rectification, 10 s
smoothing and an ideal resettable peak hold over the 100 s recording. Both
the 60 s reading and final held reading are saved alongside RMS. This is an
unvalidated old-meter-scale estimate; measured gain and hold droop are not
known. See [the method](m40623/NOISE_METHOD.md#current-m25hub-legacy-equivalent-estimate).
**REVIEW is not PASS**: no detector-input transfer calibration or noise
acceptance limits are applied. Sensitivity/polarity remain unimplemented.

Every run gets a unique folder under
`Documents/Eltec_40623_Test_Results/40623_array_daq/mod50_checks/`, containing
`report.html`, `results.csv`, `summary.json`, and the available DC/noise raw
NPZ files. All 57 physical rows and four conversions are saved, even with
fewer installed detectors. `ELTEC_ARRAY_RESULTS_ROOT` redirects output.
**Generated demo** uses synthetic signals, accelerated waits and separate
simulation files; it never contacts the DAQ.

The hardware's RUN, STOP, battery master and supply supervision stay manual.
The app controls signal routing/gain through the DAQ, so **no Arduino/ESP32
firmware update is required**. Read [the model guide](m40623/README.md) for
the exact switching, timing and saved-data contract.

## Earlier contiguous-channel hardware (`--frontend legacy` only)

The following original workflow describes the previous board. It is retained
for historical data and explicit legacy operation, not for M25/HUB hardware.

The second test rig of this repository. A 50-position PCB (5 rows × 10
columns, one unity-gain buffer per position) feeds the 64 single-ended inputs
of an **ACCES I/O USB-AIO16-64MA DAQ-PACK**; one Python desktop application
tests a whole tray of detectors per run. It mirrors the single-detector rig
(`../single_detector_rig/`) in structure: a **selector** with a model
dropdown, a **registry**, and **one directory per detector model** that is a
deliberate near-copy of its siblings. The two rigs share nothing but the
repository, the docs and (later) the ESP32 firmware for the emitter board.

| Model | Procedure | What it measures today | Status | Results (`Documents\…`) |
| --- | --- | --- | --- | --- |
| **40623** | TP120 rev W | offset check (0.3–1.2 V) and noise, fifty at a time | **CALIBRATION PENDING** — noise limits not derived, offset limits provisional | `Eltec_40623_Test_Results\40623_array_daq` |

Sensitivity / polarity (TP120's 3 Hz chopper test) waits for the emitter
board. The operator screen focuses on offset and noise, with the Eltec logo
and a 5 × 10 map of round detector sockets labelled by row and column.
Sockets always show numerical offsets and, after capture, noise alongside status.
**Show more** adds assigned sensor numbers. **Choose loaded sockets** supports
partial trays; changes require a fresh offset measurement.

## Technician flow

1. Enter **Tech name** and **Batch number**. Load up to fifty detectors.
2. Use the rig's **physical power switch immediately before pressing
   Measure offset**. The app connects to the DAQ and reads the signals; it
   does not switch detector power or vacuum. Empty-looking sockets are
   rechecked together after two seconds and marked grey automatically.
   Green means offset in range; red means out of range. Low or dead-looking
   readings can still settle: recheck before discarding a part.
3. Replace red parts and press **Measure offset** again, as often as needed.
   If replacements run out, remove the remaining bad parts and measure
   offset again so empty sockets are detected. A socket marked loaded again
   requires a fresh offset read.
4. Turn on the vacuum, wait for the gauge to reach the required setting,
   and confirm both the vacuum setting and that the detected detector count
   and map match the physical tray. Press **Measure noise** once all loaded
   sockets have good offsets. The app checks waveform settling for 3–20
   seconds, then captures sixty seconds of noise.
5. Read the socket colours and status. Use **Show more** for measurements
   and assigned sensor numbers. Results save automatically; use
   **Next tray** after the save completes. **Stop** interrupts a measurement
   so it can be retried.

Tray and sensor numbers are automatic. Every offset read is retained for
audit, including reads taken before a detector is replaced. Automatic empty
detection is an inference: both median offset reads must be within one ADC
code of zero (about 76 µV on the 0–5 V range). A dead or shorted detector can
also read zero, so check the detected map and count before noise. Click a
socket containing a detector to mark it loaded; that manual choice wins
over automatic detection. The vacuum check is the operator's gauge
confirmation; the app has no pressure telemetry or configured vacuum setpoint.

Settling compares each loaded channel's one-second waveform maximum and
minimum. Two consecutive changes within 0.1 mV allow capture to start after
three seconds. At twenty seconds capture starts with a recorded warning
if the signal has not settled. This tolerance schedules capture and does
not decide pass/fail. The user's 2026-09-08 timing decision removes the
app's five-minute countdown: TP120 itself separately calls for five minutes
after power-on and 15–20 seconds after switching detectors on the legacy
fixture. The changed timing policy and settling evidence are logged.

Noise uses the nominal **3 Hz/Q=3 band-pass** from fixture 9000232, with RMS
and rectified metrics. Paired legacy/background qualification is still pending:
valid measurements show amber **REVIEW**, and both the app and CSV explicitly
withhold PASS. Short/clipped/unresolved data remains NOT MEASURED. See
[m40623/NOISE_METHOD.md](m40623/NOISE_METHOD.md) for the method, calibration
contract and replay tool. Simulation demonstrates example passes and failures.

## Run it

- Windows: double-click the **Eltec Array Rig** desktop icon, or
  `array_rig\run_eltec_array_tester.cmd`. Install the icon once with
  `powershell -ExecutionPolicy Bypass -File array_rig\install_windows_launcher.ps1`.
- Xubuntu: `./array_rig/run_eltec_array_tester.sh`; icon via
  `./array_rig/install_xubuntu_launcher.sh` (`--uninstall` removes it).
- Normal startup launches the model app as a **subprocess** and opens its
  browser setup screen directly. The earlier desktop chooser is available
  with `python array_rig/eltec_array_tester.py --selector`; it remembers the
  last model (`%LOCALAPPDATA%\eltec-array-rig\state.json` /
  `~/.local/state/eltec-array-rig/state.json`). Launcher logs:
  `…\eltec-array-rig\launcher-*.log` (a separate file for each Windows launch;
  use the newest file). Xubuntu still uses `launcher.log`. The model app can
  also be started directly (`array_rig\m40623\run_eltec_40623_array_tester.cmd`). Choose
  **Generated demo** on the browser setup screen, or use `--simulate` (or
  `ELTEC_ARRAY_SIMULATE=1`), to run without hardware.
- Hardware: the ACCES **"USB-AIO16-64MA Install"** driver package must be
  installed (it provides `AIOUSB.dll`; the DAQ loads its firmware from the
  host at every plug-in, so allow a few seconds after connecting). A 64-bit
  Python is required (the 64-bit DLL lives in `System32`). No extra Python
  packages beyond the single rig's (`numpy`, `tkinter`, `matplotlib` for
  the grid snapshot).
- Engineering tools (no verdicts, files only at explicit paths), all in
  `array_rig\m40623\` and all with `--simulate`: `daq_bench_probe.py` (bench
  checks), `daq_rig_readout.py` (offsets, captures to CSV / npz, text-mode
  watch) and `daq_live_waveform.py` (rolling scope of any position) — see the
  m40623 README. Only one program can own the DAQ: close the tester before
  running one of them, and vice versa (a second program is not refused — it
  silently takes the stream away from the first).

The single-detector rig and this rig can run at the same time — different
hardware, different state folders.

Simulation shows a **SIMULATION** badge and runs the same operator flow on
a fast virtual clock. Click a red or empty socket to simulate loading a
replacement, then measure offset again; right-click to mark a socket empty.
Demo noise limits belong only to that simulator instance and results carry
a simulation flag and **SIMULATION ONLY** noise-limit provenance. Demo
files go under `tempfile.gettempdir()/eltec-array-simulation`, or the
`simulation` subfolder when `ELTEC_ARRAY_RESULTS_ROOT` is set.

## Layout

```
array_rig/
  eltec_array_tester.py      launches browser model setup as a SUBPROCESS; --selector keeps the earlier chooser
  sensor_versions.py         registry (one SensorVersion per model) + REQUIRED_HARDWARE
  tray_history.py            shared: <lot>_attempts.csv writer (tray events) + numbering helpers
  run_eltec_array_tester.*   launchers; install_*_launcher.* = optional desktop icon
  tests/                     selector glue + tray history
  m40623/                    the 40623 build (TP120) — see its README
      eltec_40623_array_tester.py   GUI + flow + limits + CSV/npz/PNG writers (TrayController is the Tk-free core)
      daq_backend.py                ctypes wrapper over AIOUSB.dll, de-interleave, stream integrity, SimulatedDaq
      array_analysis.py             numpy port of the single rig's noise DSP + TP120 offset classes + verdict model
      daq_bench_probe.py            engineering CLI (never issues verdicts)
      daq_rig_readout.py            engineering readout: ArrayRig API + CLI (offsets, captures to CSV / npz, text-mode watch) — the ESP32 readout's counterpart
      daq_live_waveform.py          engineering live viewer: matplotlib rolling scope of any position + the 5 x 10 grid — live_waveform.py's counterpart
      tests/                        unit + flow tests; golden_noise_reference.py is the frozen DSP oracle
      run_… / install_…             per-model launchers
```

Ownership rule inside a model directory: `daq_backend.py` is the hardware
boundary (no Tk), `array_analysis.py` is pure math (no I/O, no hardware), the
tester owns orchestration, UI and files; the probe, the readout and the live
viewer are engineering only — no verdicts, output files only at explicit
paths (never under `Documents`), and only one program on the DAQ at a time.

## Adding a model

The single rig's copy-per-model rule applies here, and additionally
**nothing is imported across the two rigs** (the only link is a frozen copy
of the 405 M22 noise functions used as a test oracle).

1. Copy `m40623/` to `m<model>/`, rename the tester and launcher identities
   (`eltec-<model>-array`, `com.eltec.<model>-array-tester.desktop`), and
   replace the spec constants in `array_analysis.py` (offset window, noise
   limits — `None` until derived) with provenance comments.
2. Append a `SensorVersion` to `sensor_versions.py` and extend the expected
   key set in `tests/test_eltec_array_rig.py`.
3. Add the suite to `run_all_tests.py`, a section to
   `docs/CALIBRATION_RECORD.md` (CALIBRATION PENDING until a paired lot
   exists — the 40623 section shows the pattern) and the results root to
   `docs/DATA_MAP.md`. CHANGELOG entry in the same commit.

## Tests

```
python run_all_tests.py                                        # every suite of both rigs
python -m unittest discover -s array_rig/tests                 # selector glue + tray history
python -m unittest discover -s array_rig/m40623/tests          # backend, analysis (golden parity), tester flow, readout, live viewer (Agg), engineer tools
```

Stdlib `unittest`, from the repository root, no hardware needed (the tests
use a scripted fake DLL and `SimulatedDaq`, and redirect the results root to
a temporary directory).
