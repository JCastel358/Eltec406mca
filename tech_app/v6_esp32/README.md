# Eltec 406MCA ESP32 Tester v6 for Xubuntu

This folder is the isolated v6 successor to the v5 ESP32/ADS1256 tester. It
keeps the guided batch workflow, battery and offset checks, waveform snapshots,
simulator, sensitivity/polarity/SNR gates, and strict serial diagnostics while
replacing fixed capture modes with adaptive peak-delta stabilization. Versions
v1 through v5 remain separate and unchanged.

## Reference sensor and test order

ADS1256 AIN1 is the permanently-mounted reference sensor and is a required
emitter-health gate. On the Batch information screen, the **AIN1 reference**
card simply shows whether the reference unit is calibrated. With a
known-good/new emitter installed, press **Calibrate reference unit**. For each
reading the app starts streaming immediately when PWM turns on; there is no
fixed warm-up delay. It requires five consecutive cycle-to-cycle robust-peak
deltas at or below `0.100 mV`, then averages the peak-to-peak response of the
next five complete cycles. Five of those stable reference readings are averaged
and saved here:

```text
~/Documents/Eltec_406MCA_Test_Results/v6_esp32/reference_sensor_calibration.json
```

All five calibration readings must themselves be repeatable within 10 percent
of their average. Calibration failure leaves DUT testing locked.

Every production measurement then follows this order:

```text
battery -> PWM on + immediate AIN1 peak-stability check -> five-cycle average -> +/-10% gate
        -> PWM off -> AIN0 DUT offset -> PWM on -> AIN0 adaptive capture
```

The app does not read AIN0 unless the fresh AIN1 reading is inside the saved
window. An out-of-window reading immediately stops the sequence, invalidates
the saved calibration, and blocks further DUT tests. Replace/check the emitter,
then use the visible **Calibrate AIN1 reference** button to establish a new
five-reading baseline. Each completed batch CSV records the calibration,
allowed window, fresh AIN1 reading, and percent drift used for that DUT.

Simulator mode uses a clearly identified synthetic in-range reference and does
not create or overwrite the hardware calibration.

The completed sensor screen shows only the large green **PASS** or red **FAIL**
verdict by default. Use **Show test details** to reveal offset, sensitivity,
polarity, confidence, SNR, stability telemetry, and failure reasons. The
separate **Show waveform** control remains available independently.

## Adaptive DUT measurement

After the AIN1 gate passes, the DUT sequence is:

```text
PWM on -> continuous AIN0/sync capture -> stabilize -> measure 10 cycles
```

The same uninterrupted stream supplies sync validation, the live preview,
stability diagnostics, and the official measurement. A complete cycle is the
AIN0 data between consecutive rising PWM-sync edges. Its robust high peak is
the median of the highest 10 percent of its samples, using at least five
samples. The application compares each robust peak with the preceding cycle.

The first three complete rising-edge sync cycles must each be at
`10.0 +/- 0.1 Hz`. Missing edges, a lone transition, or wrong cadence are rig
errors and are never recorded as an unstable part.

The tracked `stability_settings.json` initially requires five consecutive
absolute peak deltas at or below `0.100 mV`. A larger delta resets the
confirmation count. Missing or invalid settings disable measurement and show a
configuration error; v6 never substitutes hidden defaults for production use.

Stability must be reached no later than 20 seconds after PWM activation. Once
stable, v6 uses the next 10 complete cycles for sensitivity, polarity, and SNR.
Official sensitivity remains the median of the 10 per-cycle raw `max - min`
values. This post-stability measurement may finish after the 20-second
stability deadline.

If the deadline expires first, v6 records a stability-timeout FAIL, leaves
official sensitivity and polarity unmeasured, and retains the captured waveform
and peak/delta diagnostics. Saving a timeout automatically creates a PNG plus
full-sample and per-cycle CSV sidecars under `waveform_snapshots/`; manually
capturing a waveform creates the same troubleshooting bundle. PWM is turned
off on success, timeout, cancellation, and errors.

The `0.100 mV` threshold is provisional. Capture several representative
known-good parts and review the resulting delta distribution before changing
the tracked settings. The inherited sensitivity and SNR limits also still need
production qualification for the current 6 V fixture.

## Firmware and rig

The tester requires `Arduino/Eltec/Eltec.ino` v1.7 or newer. It rejects older
firmware, bad sample rates, timestamp gaps, malformed records, count mismatches,
and ADC overruns. Firmware supplies the digital GPIO25 PWM state with each
AIN0 or AIN1 sample, so the former physical LabJack AIN2 loopback is not
required.

Use `Arduino/Eltec/ESP32_ADS1256_Wiring_v1_7.md` for the current fixture:

- GPIO25 drives the 10 Hz, 50 percent emitter PWM.
- ADS1256 AIN0 is the buffered DUT sensor input.
- ADS1256 AIN1 is the permanently-mounted reference sensor input.
- ADS1256 AIN7 reads the 6 V SLA through the battery divider.

## Start the application

From this directory, run:

```bash
./run_eltec_406mca_esp32_tester.sh
```

The launcher resolves the repository path from its own location, uses
`python3`, and writes only to the v6 log:

```text
~/.local/state/eltec-406mca-esp32-v6/launcher.log
```

Set `ELTEC_PYTHON` to use a virtual environment:

```bash
ELTEC_PYTHON="$HOME/.venvs/eltec/bin/python" ./run_eltec_406mca_esp32_tester.sh
```

## Optional Xubuntu launcher

The launcher is not installed automatically. To add the v6 entry for the
current user, explicitly run:

```bash
./install_xubuntu_launcher.sh
```

The installer creates these v6-specific files, so it does not replace v5:

```text
~/.local/share/applications/com.eltec.406mca-esp32-tester-v6.desktop
~/Desktop/Eltec 406MCA ESP32 Tester v6.desktop
```

The desktop display name is **Eltec 406MCA ESP32 Tester v6**. If XFCE marks the
desktop icon untrusted, right-click it and choose **Allow Launching** once.
Remove only the v6 entries with:

```bash
./install_xubuntu_launcher.sh --uninstall
```

## Stability calibration CLI

The standalone tool shares the production robust-peak extractor and strict
ESP32 backend. It gathers evidence only: it does not issue production verdicts
or rewrite `stability_settings.json`.

Capture a known-good part for the default 20 seconds:

```bash
python3 stability_calibration.py capture --sensor-id KNOWN_GOOD_01
```

Useful capture overrides are `--duration SECONDS`, `--port PORT`,
`--output-dir DIR`, and `--settings JSON`. Each run records raw timestamp,
stream/PWM elapsed time, voltage, and sync samples; per-cycle peaks,
peak-to-peak values, deltas, and confirmation progress; and a summary with the
earliest qualifying stability. Battery and offset are checked first, capture
starts immediately after PWM is enabled, and PWM is always disabled before the
command exits. The capture is discarded before any artifact is written if its
first three sync cycles are not at the required cadence.

Summarize cycle CSVs from several known-good parts:

```bash
python3 stability_calibration.py summarize \
  ~/Documents/Eltec_406MCA_Test_Results/v6_esp32/calibration/*_cycles.csv
```

The summary retains combined percentiles for every captured delta and also
reports post-stabilization p50/p90/p95/p99/max values. That steady-state region
includes each part's stabilization-confirming delta and every later delta, so
subsequent drift remains visible alongside each part's stabilization time. Use
`--settings JSON` to evaluate another reviewed settings file without changing
production configuration.

## Dependencies and device access

Required packages are Python 3, Tkinter, NumPy, and pyserial. Matplotlib is
optional but produces higher-quality snapshots. On Ubuntu/Xubuntu:

```bash
sudo apt install python3 python3-tk python3-numpy python3-serial
sudo apt install python3-matplotlib libnotify-bin desktop-file-utils xdg-user-dirs
```

The signed-in user must be in the `dialout` group to access `/dev/ttyUSB*` or
`/dev/ttyACM*` devices. Sign out and back in after adding the group:

```bash
sudo usermod -aG dialout "$USER"
```

## Paths and tests

- Application: `tech_app/v6_esp32/eltec_406mca_esp32_tester.py`
- Backend: `tech_app/v6_esp32/esp32_backend.py`
- Settings: `tech_app/v6_esp32/stability_settings.json`
- Calibration tool: `tech_app/v6_esp32/stability_calibration.py`
- Results: `~/Documents/Eltec_406MCA_Test_Results/v6_esp32/`
- AIN1 reference baseline: `~/Documents/Eltec_406MCA_Test_Results/v6_esp32/reference_sensor_calibration.json`
- Launcher log: `~/.local/state/eltec-406mca-esp32-v6/launcher.log`

An exact existing batch number resumes at the next sensor and appends to that
batch CSV. Completed rows are not rewritten, and snapshot filenames remain
collision-safe. V6 uses its own CSV schema and results root; no v5 data is
migrated.

Run the v6 suite from the repository root:

```bash
python3 -m unittest discover -s tech_app/v6_esp32/tests -v
```
