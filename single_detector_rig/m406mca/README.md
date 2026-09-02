# Eltec 406MCA ESP32 Tester v6.1

V6.1 is an isolated evaluation build of the ESP32/ADS1256 tester. It keeps the
v6 hardware workflow, reference-unit gate, simulator, failure mode selection,
and serial-integrity checks. It also has a stricter DUT stability/measurement
retry policy.

The new-fixture sensitivity gate uses the paired-fixture calibration described
below. Raw sensitivity is always preserved, while the UI and CSV also report a
legacy-equivalent value. Offset, polarity, stability, signal-quality, battery,
reference-unit, and hardware-integrity gates remain unchanged.

This directory is the live **406 MCA model of the unified rig app**
(`single_detector_rig`, normally launched from its selector). The standalone
v6 and v6.1 builds it descends from were retired on 2026-08-28 and are
preserved at git tag `archive/pre-cleanup-2026-08-28`.

## V6.1 DUT stability policy

The DUT is captured in one uninterrupted AIN0/PWM-sync stream. A cycle's
stability value is the absolute change between its robust high peak and the
preceding cycle's robust high peak. The threshold remains `<= 0.100 mV`.

The attempts are:

| Attempt | Stable deltas required | Measurement cycles required | If measurement exceeds 0.100 mV |
| --- | ---: | ---: | --- |
| 1 | 10 consecutive | 20 | Discard the window and start attempt 2 |
| 2 | 10 consecutive | 20 | Discard the window and start attempt 3 |
| 3 | 10 consecutive | 20 | Record the DUT as `Unstable - Unstable` |

Every cycle selected for sensitivity must remain at or below the threshold.
Any over-threshold delta discards the entire in-progress measurement window;
discarded cycles never contribute to the official sensitivity.

The existing 20-second deadline still governs qualification and
requalification. If a required 10-delta confirmation completes on or before
20 seconds, its 20-cycle measurement window may finish after the
deadline. A retry that cannot requalify by the deadline is also unstable.

The completed result's sensitivity, polarity, noise, and SNR are calculated
only from the successful attempt's complete 20-cycle window. CSV rows
also record the attempt number, number of kicked measurement windows, active
qualification length, and active measurement length.

## New-fixture sensitivity calibration and gate

The final raw ESP32 sensitivity is translated to the legacy fixture's display
scale with the provisional paired-sensor factor:

```text
legacy-equivalent sensitivity = raw ESP32 sensitivity * 1.582
```

The factor is applied only after the stable 20-cycle sensitivity result exists.
It is never applied to the waveform, offset, stability deltas, noise, SNR, or
polarity. Both raw and legacy-equivalent sensitivity are written to new batch
CSVs, together with the factor, calibration ID `lot_520_paired_v1`, and gate
boundaries. Use a new calibration ID whenever later evidence changes the
factor or guard-band policy.

The legacy filter-specific sensitivity minimum remains the center of a raw
`+/-0.10 mV` retest band. For the default
`-284 filter + extra -6 + blackened tube` setup, the raw policy is:

| Raw sensitivity | Outcome |
| ---: | --- |
| `< 2.43 mV` | `FAIL` — low sensitivity |
| `2.43-2.63 mV` inclusive | `PASS · NEAR LIMIT` — passes, with a re-measure suggestion |
| `> 2.63 mV` | `PASS` for the sensitivity gate |

Since 2026-08-25 a reading inside the band is a plain **PASS** (it is within
the conversion factor's margin of error): the result page shows the green
banner plus an amber "near the limit — suggestion: re-measure" card, the CSV
records `sensitivity_gate_outcome = NEAR LIMIT`, and no quarantine record
exists any more (older CSVs with `RETEST` rows are shown as failures in the
summary — they were quarantine records, not passes). Another definitive
failure (offset, polarity, SNR, stability) still makes the overall outcome
`FAIL`.

This calibration is provisional. Continue collecting repeated known-low and
borderline parts before narrowing/removing the band. The simulator has a
`Borderline sensitivity` case for exercising the near-limit path.

## Reference gate

The AIN1 reference-unit gate uses a dedicated delta threshold:

1. Battery check and PWM off.
2. PWM on and immediate AIN1 stream.
3. Five consecutive peak deltas at or below `0.250 mV`.
4. Average the next five complete cycles.
5. Require that reading to be inside the fixed `+/-25%` baseline window. When
   AIN1 is above the window, read the AIN0 DC offset before invalidating the
   reference calibration. If AIN0 is above the `1.2 V` production high-offset
   limit (and still within the plausible connected-sensor range), treat the
   AIN1 spike as interference from that bad DUT, preserve the reference
   calibration, and continue so the DUT is recorded as `HO - High offset`.
   A normal-offset DUT, a low AIN1 result, or an implausible AIN0 value keeps
   the existing reference lockout and recalibration requirement.

Calibration files created with the former `+/-10%` policy are automatically
loaded with the current `+/-25%` window, so recalibration is not required solely
for this policy change.

V6.1 first looks for its own baseline at:

```text
~/Documents/Eltec_406MCA_Test_Results/v6_1_esp32/reference_sensor_calibration.json
```

If that file does not exist, it reads the compatible v6 baseline so the
evaluation build can be tried immediately. This fallback is read-only. Any new
calibration or invalidation is saved to the v6.1 location and never changes the
v6 file.

## Version isolation

V6.1 writes only to these version-specific locations:

```text
Results:      ~/Documents/Eltec_406MCA_Test_Results/v6_1_esp32/
Launcher log: ~/.local/state/eltec-406mca-esp32-v6-1/launcher.log
Menu entry:   ~/.local/share/applications/com.eltec.406mca-esp32-tester-v6-1.desktop
Desktop icon: ~/Desktop/Eltec 406MCA ESP32 Tester v6.1.desktop
```

This separation allows v6 and v6.1 results to be compared without mixing CSV
schemas or batches.

## Run

Normally: start the unified selector (`single_detector_rig/run_eltec_rig_tester.cmd`
/ `.sh`) and pick **Model 406 MCA**. Standalone, from the repository root:

```bash
./single_detector_rig/m406mca/run_eltec_406mca_esp32_tester.sh
```

Or from this directory:

```bash
./run_eltec_406mca_esp32_tester.sh
```

The optional Xubuntu desktop/menu launcher is installed only when explicitly
requested:

```bash
./single_detector_rig/m406mca/install_xubuntu_launcher.sh
```

Remove only the v6.1 launcher with:

```bash
./single_detector_rig/m406mca/install_xubuntu_launcher.sh --uninstall
```

### Windows

Double-click `run_eltec_406mca_esp32_tester.cmd` (runs the GUI under
`pythonw.exe`, logs to `%LOCALAPPDATA%\eltec-406mca-esp32-v6-1\launcher.log`,
pops an error dialog if the app cannot start — `ELTEC_LAUNCHER_NO_DIALOG=1`
suppresses it). Optional Desktop + Start Menu shortcut, per-user, no admin
rights (`-Uninstall` removes it):

```bat
powershell -ExecutionPolicy Bypass -File single_detector_rig\m406mca\install_windows_launcher.ps1
```

Results on Windows: `%USERPROFILE%\Documents\Eltec_406MCA_Test_Results\v6_1_esp32\`.
Port discovery is by USB VID/PID on both hosts.

## Hardware and dependencies

The tester requires `Arduino/Eltec/Eltec.ino` v1.7 or newer. On the unified
bench rig (firmware v2.1+, wiring in `Arduino/Eltec/ESP32_ADS1256_Wiring_v2_0.md`)
the backend sends `FE,V19` after connect to restore the qualified gain-2
buffered front end; a legacy standalone rig on firmware v1.9 uses
`Arduino/Eltec/ESP32_ADS1256_Wiring_legacy_v1_9.md`. Signals:

- GPIO33 (GPIO25 on a legacy v1.9 rig): fixed 10 Hz / 50 percent emitter PWM;
- ADS1256 AIN0: DUT sensor;
- ADS1256 AIN1: permanently mounted reference sensor (gate currently disabled);
- ADS1256 AIN7: on a legacy rig only, the 6 V SLA battery through the measured
  99.7k/99.6k divider (2.001004 ratio) with a 100 nF capacitor across the
  lower resistor — the unified fixture has no battery on AIN7 and the gate is
  off.

Required packages are Python 3, Tkinter, NumPy, and pyserial. Matplotlib is
optional for higher-quality waveform snapshots.

```bash
sudo apt install python3 python3-tk python3-numpy python3-serial
sudo apt install python3-matplotlib libnotify-bin desktop-file-utils xdg-user-dirs
```

The signed-in user must have permission to access `/dev/ttyUSB*` or
`/dev/ttyACM*`, normally through the `dialout` group.

## Engineering stability evidence

The calibration CLI remains an evidence tool; it does not issue part verdicts
or edit `stability_settings.json`.

```bash
python3 single_detector_rig/m406mca/stability_calibration.py capture \
  --sensor-id KNOWN_GOOD_01

python3 single_detector_rig/m406mca/stability_calibration.py summarize \
  ~/Documents/Eltec_406MCA_Test_Results/v6_1_esp32/calibration/*_cycles.csv
```

## Tests

Run the isolated v6.1 suite from the repository root:

```bash
python3 -m unittest discover -s single_detector_rig/m406mca/tests -v
```

The suite covers the three-attempt state machine, identical 10/20 windows,
third-kick unstable classification, 20-second retry deadline, direct streaming
integration, CSV telemetry, launcher isolation, reference behavior, serial
integrity, simulator behavior, and GUI workflow.
