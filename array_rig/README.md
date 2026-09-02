# Eltec Array Rig — fifty detectors at once on a USB DAQ

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
board; the app shows the step greyed out.

## Run it

- Windows: double-click the **Eltec Array Rig** desktop icon, or
  `array_rig\run_eltec_array_tester.cmd`. Install the icon once with
  `powershell -ExecutionPolicy Bypass -File array_rig\install_windows_launcher.ps1`.
- Xubuntu: `./array_rig/run_eltec_array_tester.sh`; icon via
  `./array_rig/install_xubuntu_launcher.sh` (`--uninstall` removes it).
- The selector remembers the last model (`%LOCALAPPDATA%\eltec-array-rig\state.json`
  / `~/.local/state/eltec-array-rig/state.json`) and launches the model app
  as a **subprocess** with the model directory as cwd. Launcher logs:
  `…\eltec-array-rig\launcher.log`. The model app can also be started
  directly (`array_rig\m40623\run_eltec_40623_array_tester.cmd`) and with
  `--simulate` (or `ELTEC_ARRAY_SIMULATE=1`) it runs without hardware.
- Hardware: the ACCES **"USB-AIO16-64MA Install"** driver package must be
  installed (it provides `AIOUSB.dll`; the DAQ loads its firmware from the
  host at every plug-in, so allow a few seconds after connecting). A 64-bit
  Python is required (the 64-bit DLL lives in `System32`). No extra Python
  packages beyond the single rig's (`numpy`, `tkinter`, `matplotlib` for
  the grid snapshot).

The single-detector rig and this rig can run at the same time — different
hardware, different state folders.

## Layout

```
array_rig/
  eltec_array_tester.py      selector GUI: model dropdown → launches the model app as a SUBPROCESS
  sensor_versions.py         registry (one SensorVersion per model) + REQUIRED_HARDWARE
  tray_history.py            shared: <lot>_attempts.csv writer (tray events) + numbering helpers
  run_eltec_array_tester.*   launchers; install_*_launcher.* = optional desktop icon
  tests/                     selector glue + tray history
  m40623/                    the 40623 build (TP120) — see its README
      eltec_40623_array_tester.py   GUI + flow + limits + CSV/npz/PNG writers (TrayController is the Tk-free core)
      daq_backend.py                ctypes wrapper over AIOUSB.dll, de-interleave, stream integrity, SimulatedDaq
      array_analysis.py             numpy port of the single rig's noise DSP + TP120 offset classes + verdict model
      daq_bench_probe.py            engineering CLI (never issues verdicts)
      tests/                        unit + flow tests; golden_noise_reference.py is the frozen DSP oracle
      run_… / install_…             per-model launchers
```

Ownership rule inside a model directory: `daq_backend.py` is the hardware
boundary (no Tk), `array_analysis.py` is pure math (no I/O, no hardware), the
tester owns orchestration, UI and files, the probe is engineering only.

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
python -m unittest discover -s array_rig/m40623/tests          # backend, analysis (golden parity), tester flow
```

Stdlib `unittest`, from the repository root, no hardware needed (the tests
use a scripted fake DLL and `SimulatedDaq`, and redirect the results root to
a temporary directory).
