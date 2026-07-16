# Eltec 406MCA Tester

Code for the 406MCA sensitivity, polarity, offset, noise, and SNR test process.
The tester apps use the LabJack T7-Pro through the `labjack.ljm` Python package
and include a simulator mode, so the operator flow and analysis can be exercised
without hardware connected.

The repo holds **four versions of the same technician data-collection app** (one
per physical test setup, plus the v4 visual refresh of the emitter rig app), the
**engineer tools** used to bring up and validate new setups, and **per-version
analysis** scripts.

## Repository layout

```
tech_app/                         Technician data-collection app, by version
├── v1_single_sensor/             Original single-sensor rig with the AM502 amplifier
│   ├── eltec_406mca_tester.py        (also the shared signal-math + LabJack engine)
│   └── Run 406MCA Tester.bat
├── v2_scope_verification/        Older scope-verification rig (guided lot flow)
│   ├── eltec_406mca_scope_verification_tester.py
│   └── Run 406MCA Scope Verification Tester.bat
├── v3_emitter/                   LabJack-driven emitter rig, no AM502 (unity-gain buffer)
│   ├── eltec_406mca_emitter_tester.py
│   ├── Run 406MCA Emitter Tester.bat
│   ├── Create Desktop Shortcut.ps1
│   └── assets/README.txt
└── v4_emitter/   ← CURRENT       Same emitter rig + measurement engine as v3, with the
    ├── eltec_406mca_emitter_tester.py   Eltec-branded animated UI (eltecinstruments.com look)
    ├── Run 406MCA Emitter Tester.bat
    ├── Create Desktop Shortcut.ps1
    └── assets/README.txt         (optional brand fonts go in assets\fonts\)

engineer_tools/                   Engineer setup / bring-up tools (live signal monitor)
├── eltec_406mca_signal_monitor.py
├── eltec_406mca_signal_monitor_ui.py
└── Run 406MCA Signal Monitor UI.bat

analysis/                         Analysis, by app version
├── v1_single_sensor/            analyze_406mca_snr_results.py, test_406mca_analysis.py
├── v2_scope_verification/       dataAnalysis.py, disagreementAnalysis.py
├── v3_emitter/                  analyze_emitter_results.py + Run Emitter Analysis.bat
├── v4_emitter/                  analyze_emitter_results.py + Run Emitter Analysis.bat (v4 data)
└── reports/                     Reference / generated documents (buffer + SNR write-ups)

assets/eltec_logo.png            Shared logo used by the apps
```

`eltec_406mca_tester.py` (v1) doubles as the **single source of truth for the
signal math and the LabJack device wrapper**; the v3/v4 emitter testers and both
engineer tools import from it, so it is kept alongside the other versions rather
than deprecated.

## Where test data is saved

Each version writes into its **own subfolder** of the results root so data can be
tracked and analyzed per version:

```
C:\Users\<user>\Documents\Eltec_406MCA_Test_Results\
├── v1_single_sensor\   406mca_results.csv
├── v2_scope_verification\
│   ├── 406mca_scope_verification_lot_<lot>.csv
│   ├── autosave\  waveform_snapshots\  analysis\
├── v3_emitter\
│   ├── 406mca_emitter_lot_<batch>.csv
│   ├── autosave\  waveform_snapshots\  analysis\
└── v4_emitter\
    ├── 406mca_emitter_lot_<batch>.csv
    ├── autosave\  waveform_snapshots\  analysis\
```

Each app creates its subfolder automatically on first save.

---

## v4 – Emitter Tester (current, visual refresh)

`tech_app/v4_emitter/eltec_406mca_emitter_tester.py` is the **same rig, wiring,
guided flow, measurement engine, and CSV schema as v3** with a complete visual
overhaul styled after [eltecinstruments.com](https://eltecinstruments.com/):

- Eltec-blue gradient app bar with the logo badge, an animated signal-trace
  line, and a live battery gauge pill (click it to re-check the 9V supply).
- Numbered step rail (`01 / 02 / 03`) with animated check-offs and connectors.
- Rounded soft-shadow cards with the Eltec technical-gradient accent strip,
  hover-animated rounded buttons, and an animated waveform toggle switch.
- Animated PASS/FAIL banner, count-up result tiles, and a scanning progress
  bar while a measurement runs.
- Dark navy oscilloscope panel (grid + glow traces + sweep beam) for the live
  AIN0/AIN2 view, with monospace technical readouts.

Optional: drop `Poppins`/`Manrope`/`JetBrains Mono` `.ttf` files into
`tech_app\v4_emitter\assets\fonts\` and the app loads them privately at startup
for an even closer match to the website type; otherwise it falls back to
Segoe UI / Consolas automatically (see `assets\README.txt`).

### Capture modes (v4 speed-up)

v3 needs 45-80 chopping cycles (4.5-8 s) per sensor because its stability rule
compares two 20-cycle averages. v4 adds a margin-based early exit plus shorter
DC reads (offset 24x3 ms instead of 80x10 ms; the battery check is reused when
under 30 s old). The **Capture mode** selector under *Advanced options* picks:

- **Fast (early exit)** — stops as soon as the waveform is stable over two
  6-cycle windows AND every metric is decisively clear of its limit
  (sensitivity ≥1.5x/≤0.5x the minimum, polarity confidence ≥1.5x threshold,
  SNR ≥2x/≤0.5x the gate), confirmed on two consecutive cycles. Clear sensors
  decide in ~1.5-2 s; marginal ones keep capturing up to a 30-cycle cap.
- **Validation (default)** — runs the full v3-length capture AND logs what
  Fast would have decided to the `fast_*` CSV columns plus a `fast_match`
  YES/NO. Run production batches in this mode first; the batch summary shows
  a "FAST PATH n/m MATCH" chip. Switch to Fast only after mismatch-free runs.
- **Full (v3 timing)** — exact v3 behavior.

The `FAST_*` constants at the top of the tester set the windows and decision
margins; tune them from validation-mode data. New CSV columns: `capture_mode,
capture_cycles, capture_seconds, fast_stop_cycle, fast_sensitivity_mv,
fast_polarity, fast_pass_fail, fast_match, data_source` (older batch CSVs keep
their original columns automatically).

### "Is everything plugged in?" guards (v4)

v3 silently switched to simulator mode when no T7 was found, so a technician
could unknowingly record synthetic numbers (battery pinned at 8.8 V). v4:

- **Simulator is explicit opt-in** (Advanced options), shows an amber
  **SIMULATOR** badge in the header, marks the result detail line
  "SIMULATED DATA", and tags CSV rows `data_source=simulator`.
- **No T7 detected** — battery shows `--` and Measure explains to plug in the
  LabJack instead of running.
- **Battery/AIN1 wiring fault** — readings outside 3.0-10.5 V (floating input,
  missing battery clip) show a red "CHECK WIRING" pill, a red banner, and
  block the Measure button.
- **Sensor pre-flight** — before capturing, the DC offset must be in the
  0.05-2.5 V plausible band; otherwise the test aborts with "No sensor
  detected - seat the sensor in the rig" and nothing is recorded.
- **PWM sync pre-flight** — a ~0.3 s peek at AIN2 after the PWM starts must
  see a square wave; otherwise the test aborts naming the DIO0/AIN2 wiring.

Results are logged to `…\v4_emitter\406mca_emitter_lot_<batch>.csv` (v3
columns + the capture telemetry above). Run it with
`Run 406MCA Emitter Tester.bat` or:

```powershell
cd C:\Users\vma\Documents\Eltec406MCATester\tech_app\v4_emitter
python eltec_406mca_emitter_tester.py
```

`Create Desktop Shortcut.ps1` works the same as v3's and names the shortcut
"Eltec 406MCA Emitter Tester v4".

---

## v3 – Emitter Tester (previous, technician-friendly)

`tech_app/v3_emitter/eltec_406mca_emitter_tester.py` is the step-by-step app for
the rig where the **LabJack drives the emitter itself** and the sensor is read
**without the AM502 amplifier**. A unity-gain (voltage-follower) op-amp buffer
feeds the LabJack a low-impedance signal while preserving the ~0.667 V DC offset
and the small AC waveform, so the external gain is always `1x`.

### Wiring for this rig

- `AIN0`: buffered sensor signal — carries BOTH the DC offset and the AC signal.
- `AIN2`: PWM / MOSFET-gate drive, looped back as the polarity/sync reference.
- `DIO0`: PWM output to the MOSFET gate that switches the emitter. Use a common
  ground with the emitter supply.

The LabJack generates a 10 Hz PWM (default `DIO0`, 50% duty) to switch the MOSFET
that drives the emitter. Because the PWM is also wired into `AIN2`, the rising-edge
polarity check works unchanged. Use the `+/-1 V (x10)` AIN0 range so the ~0.667 V
offset plus the small AC signal fit with good resolution.

### Guided flow

1. Enter the **batch number**, **tester name**, and **filter/setup**, then press
   `Enter` (the batch field is focused on launch, so you can type immediately).
2. **Place the sensor in the rig** and press `Enter`.
3. The app reads the DC offset (emitter off), turns the PWM emitter on, measures
   sensitivity and polarity, and shows the offset, sensitivity, and a `GOOD`/`BAD`
   polarity verdict. The screen turns **green for PASS** or **red for FAIL**.
   - `Comment` records a note for the sensor.
   - `Capture waveform` saves a PNG of the AIN0/AIN2 traces under the version's
     `waveform_snapshots\` folder.
   - `Show waveform` reveals the live AIN0 + AIN2 traces while it reads.
4. `Save + Next Sensor` (Enter) auto-increments the sensor number; `Save + Exit
   Batch` (Esc) saves and shows a batch summary.

Results are logged per batch to
`…\v3_emitter\406mca_emitter_lot_<batch>.csv` with columns: `timestamp,
batch_number, sensor_number, sensor_id, tester_name, model, filter_setup,
pwm_channel, pwm_hz, pwm_duty, offset_v, sensitivity_mv, polarity,
polarity_good_bad, pass_fail, fail_reasons, operator_comments,
waveform_snapshot_paths, battery_v, noise_rms_mv, snr_db`.

If no T7 is detected the app drops into simulator mode so the full flow can be
walked through without hardware.

### Run it / make a desktop icon

```powershell
cd C:\Users\vma\Documents\Eltec406MCATester\tech_app\v3_emitter
python eltec_406mca_emitter_tester.py
```

Or double-click `Run 406MCA Emitter Tester.bat`. To put a clickable ELTEC-logo
icon on the desktop, run once:

```powershell
cd C:\Users\vma\Documents\Eltec406MCATester\tech_app\v3_emitter
powershell -ExecutionPolicy Bypass -File ".\Create Desktop Shortcut.ps1"
```

It builds a multi-size `eltec_logo.ico` from `assets\eltec_logo.png` using
built-in Windows imaging (no Pillow needed) and points the shortcut at it.
Re-run it if the app folder moves or the logo changes.

The logo loads from `tech_app\v3_emitter\assets\eltec_logo.png` or the shared
repo-root `assets\eltec_logo.png`; if neither is present a drawn ELTEC logo is used.

---

## v2 – Scope Verification Tester

`tech_app/v2_scope_verification/eltec_406mca_scope_verification_tester.py` is the
guided lot-based app used on the older scope-verification rig. It logs per lot to
`…\v2_scope_verification\406mca_scope_verification_lot_<lot>.csv`.

```powershell
cd C:\Users\vma\Documents\Eltec406MCATester\tech_app\v2_scope_verification
python eltec_406mca_scope_verification_tester.py
```

---

## v1 – Single-Sensor Tester (original, AM502)

`tech_app/v1_single_sensor/eltec_406mca_tester.py` is the first single-sensor
program, reading the sensor through the AM502 amplifier. It also provides the
`Distance cm` / `Input voltage V` sweep fields used for the SNR study. It logs to
`…\v1_single_sensor\406mca_results.csv`.

```powershell
cd C:\Users\vma\Documents\Eltec406MCATester\tech_app\v1_single_sensor
python eltec_406mca_tester.py
```

### Default wiring

- `AIN0`: sensor output or conditioned waveform signal.
- `AIN2`: blade sync signal.

The procedure uses the rising edge of `AIN2` as the polarity reference. The tester
does not offer falling-edge polarity testing in the operator UI. Close LabJack
programs such as LJStreamM or Kipling before using hardware mode — only one process
can claim the T7 USB connection at a time.

---

## Engineer tools

`engineer_tools/eltec_406mca_signal_monitor.py` (CLI) and
`eltec_406mca_signal_monitor_ui.py` (Tkinter) watch the incoming LabJack signal
continuously instead of taking one pass/fail snapshot. They record a baseline and
report whether the current AIN0 waveform has changed enough to confirm the setup is
responding — useful when bringing up or debugging a new emitter/sensor rig. Both
reuse the signal math from the v1 tester.

```powershell
cd C:\Users\vma\Documents\Eltec406MCATester\engineer_tools
python eltec_406mca_signal_monitor_ui.py          # or: Run 406MCA Signal Monitor UI.bat
python eltec_406mca_signal_monitor.py --simulator  # CLI, no hardware
```

---

## Analysis

Each analyzer defaults to its version's results subfolder and writes reports into
an `analysis\` folder there (CSV exports + a self-contained HTML report).

```powershell
# v4 emitter (current): per-lot yield, offset/sensitivity/SNR stats, failure reasons, outliers
cd C:\Users\vma\Documents\Eltec406MCATester\analysis\v4_emitter
python analyze_emitter_results.py                 # or: Run Emitter Analysis.bat

# v3 emitter: same analysis over the v3 results folder
cd C:\Users\vma\Documents\Eltec406MCATester\analysis\v3_emitter
python analyze_emitter_results.py                 # or: Run Emitter Analysis.bat

# v2 scope verification: lot summaries, program-vs-operator disagreements, outliers
cd C:\Users\vma\Documents\Eltec406MCATester\analysis\v2_scope_verification
python dataAnalysis.py
python disagreementAnalysis.py

# v1 single-sensor: SNR distance/voltage sweep report (+ Word .docx) and math self-tests
cd C:\Users\vma\Documents\Eltec406MCATester\analysis\v1_single_sensor
python analyze_406mca_snr_results.py
python test_406mca_analysis.py
```

All analysis scripts use only the Python standard library so they run on the tester
PC without extra packages. Pass `--results-dir` / `--output-dir` to override the
defaults.

---

## Current 406MCA limits

- Frequency setup: `10 Hz +/- 0.1 Hz`.
- Offset: `0.3 Vdc` minimum, `1.2 Vdc` maximum.
- Polarity: positive for a positive change.
- Sensitivity minimum:
  - `-3 filter`: `25 mV`
  - `-27 filter`: `25 mV`
  - `-266 filter`: `30.9 mV`
  - `-273 filter + blackened tube`: `2.3 mV`
  - `-284 filter + extra -6 + blackened tube`: `4.0 mV`

## Measurement notes

The offset is estimated from the average voltage of the `AIN0` waveform during the
same stable, complete blade-sync cycles used for the sensitivity reading. The
waveform is expected to be triangular. The program segments the signal by blade-sync
cycles and watches the cycle peak-to-peak readings until the rolling average is
stable within 10%, then uses the median peak-to-peak voltage from the stable cycles.
The signal gain defaults to `1x` for direct sensor readings; if an amplifier is used,
enter its gain so the program divides the measured waveform before comparing against
the sensitivity limit.

The LabJack AIN range setting is an input range, not an external gain correction.
LJM returns calibrated volts, so leave external gain at `1x` when reading the sensor
directly even if AIN0 is set to the `+/-1 V (x10)` range. During stream, the app
samples `AIN0` multiple times after the sync channel and keeps the final reading to
reduce settling error from high-impedance sensor outputs.

Polarity is measured against the rising blade-sync edge. The estimator searches the
early rising-edge response region for the strongest signed waveform change and
reports the response phase window and confidence as a percentage of the cycle
peak-to-peak value.

Noise is estimated from the same stable blade-sync cycles used for sensitivity. The
app aligns the stable cycles by phase, subtracts the average cycle shape, and reports
the residual RMS as gain-corrected noise in millivolts. SNR is reported in dB from
signal RMS divided by noise RMS.

If the measured `AIN0` average is near `0 V`, the waveform output is not carrying the
detector offset. Check that the waveform output path is DC-coupled and referenced to
the same ground. Frequency, clipping, and stability issues are shown as warnings; the
pass/fail decision is based on sensitivity, polarity, offset, and whether the waveform
stabilized before the capture limit.

## Finding the best input voltage for SNR (v1 sweep study)

Use the same sensor, filter/setup, gain, LabJack range, wiring, emitter drive method,
and fixture alignment for the whole comparison. For each distance, sweep the same
voltage points, and enter each run's `Distance cm` and `Input voltage V` so the CSV
can be grouped later.

1. Choose a safe voltage sweep before starting. Stay within the sensor, emitter,
   fixture, and LabJack input limits. If the safe range is unknown, start low and
   increase in small steps.
2. Pick 5 to 10 voltage points across the range. Smaller steps near the expected best
   voltage are useful.
3. Pick the distances to test (such as `45 cm`, `55 cm`, `65 cm`) and use the same
   voltage list at every distance.
4. Set the first distance, enter it in `Distance cm`, then run the full voltage sweep.
5. At each voltage, wait for the fixture and waveform to settle before `Start Test`.
6. Run at least 3 tests per voltage/distance (5 if readings vary a lot).
7. Move to the next distance and repeat the same voltage sweep.
8. Watch for clipping, unstable-waveform, offset, or polarity warnings. Do not treat a
   voltage as best if it only looks good because the waveform is clipping or unstable.
9. After the first sweep, repeat the best few combinations in reverse order to catch
   drift from warm-up, sensor heating, or setup changes.
10. Compare each combination by average `snr_db` (higher is better); also check
    `noise_rms_mv`, `sensitivity_mv`, pass/fail, and warnings.
11. If two settings are within ~1–2 dB, prefer the lower, safer voltage and easier
    fixture distance unless production requires otherwise.

`analysis/v1_single_sensor/analyze_406mca_snr_results.py` groups these sweep rows and
writes a console summary, a group-summary CSV, and a Word `.docx` report. Useful
columns: `distance_cm`, `input_voltage_v`, `sensor_id`, `filter_setup`,
`sensitivity_mv`, `noise_rms_mv`, `snr_db`, `pass_fail`, `fail_reasons`.

## Safety notes

- Keep the AIN0 signal within the LabJack analog input range.
- Ensure the fixture, sensor/LabJack input, and sync signal share a valid common
  ground.
- If the T7 is claimed by another program, close LJStreamM/Kipling and press
  `Connect` again.
