# Eltec 406MCA Single-Sensor Tester

Code for industrial automation process.

This is the first single-sensor test program for the 406MCA sensitivity,
polarity, offset, noise, and SNR procedure. It uses the LabJack T7-Pro through the
`labjack.ljm` Python package and includes simulator mode so the operator flow
and analysis can be tested without hardware connected.

## Files

- `eltec_406mca_tester.py` - Tkinter desktop app.
- `test_406mca_analysis.py` - command-line checks for the signal analysis.
- CSV output defaults to:
  `C:\Users\<user>\Documents\Eltec_406MCA_Test_Results\406mca_results.csv`

## Default Wiring

- `AIN0`: sensor output or conditioned waveform signal.
- `AIN2`: blade sync signal.

The 406MCA procedure uses the rising edge of `AIN2` as the polarity reference.
The tester does not offer falling-edge polarity testing in the operator UI.

Close LabJack programs such as LJStreamM or Kipling before using hardware mode.
Only one process can claim the T7 USB connection at a time.

## Run

```powershell
cd C:\Users\vma\Documents\Eltec406MCATester
python eltec_406mca_tester.py
```

To run the math tests:

```powershell
cd C:\Users\vma\Documents\Eltec406MCATester
python test_406mca_analysis.py
```

To analyze voltage/distance sweep results and generate a Word report:

```powershell
cd C:\Users\vma\Documents\Eltec406MCATester
python analyze_406mca_snr_results.py
```

## Operator Workflow

1. Enter or scan the sensor ID.
2. Select the filter/setup being used.
3. Click `Start Test`.
4. Read the large `PASS` or `FAIL` result and the failure reason list.

The app logs one row per waveform test with:

`timestamp, sensor_id, model, filter_setup, distance_cm, input_voltage_v, offset_v, sensitivity_mv, noise_rms_mv, snr_db, polarity, pass_fail, fail_reasons`

## Current 406MCA Limits

- Frequency setup: `10 Hz +/- 0.1 Hz`.
- Offset: `0.3 Vdc` minimum, `1.2 Vdc` maximum.
- Polarity: positive for a positive change.
- Sensitivity minimum:
  - `-3 filter`: `25 mV`
  - `-27 filter`: `25 mV`
  - `-266 filter`: `30.9 mV`
  - `-273 filter + blackened tube`: `2.3 mV`
  - `-284 filter + extra -6 + blackened tube`: `4.0 mV`

## Measurement Notes

The offset is estimated from the average voltage of the `AIN0` waveform during
the same stable, complete blade-sync cycles used for the sensitivity reading.
The waveform is expected to be triangular. The program segments the signal by
blade-sync cycles and watches the cycle peak-to-peak readings until the rolling
average is stable within 10%. It then uses the median peak-to-peak voltage from
the stable cycles. The signal gain defaults to `1x` for direct sensor readings;
if an amplifier is used, enter its gain so the program can divide the measured
waveform before comparing against the sensitivity limit.

The LabJack AIN range setting is an input range, not an external gain correction.
LJM returns calibrated volts, so leave external gain at `1x` when reading the
sensor directly even if AIN0 is set to the `+/-1 V (x10)` range. During stream,
the app samples `AIN0` multiple times after the sync channel and keeps the final
reading to reduce settling error from high-impedance sensor outputs.

Polarity is measured against the rising blade-sync edge. The estimator searches
the early rising-edge response region for the strongest signed waveform change
and reports the response phase window and confidence as a percentage of the
cycle peak-to-peak value.

Noise is estimated from the same stable blade-sync cycles used for sensitivity.
The app aligns the stable cycles by phase, subtracts the average cycle shape,
and reports the residual RMS as gain-corrected noise in millivolts. SNR is
reported in dB from signal RMS divided by noise RMS. The optional `Distance cm`
and `Input voltage V` fields are logged with each row so runs at different
distances and input voltages can be compared in the CSV.

If the measured `AIN0` average is near `0 V`, the waveform output is not carrying
the detector offset. Check that the waveform output path is DC-coupled and
referenced to the same ground. If that output is intentionally AC-coupled or
centered at ground, the original offset cannot be recovered from `AIN0` alone.

Frequency, clipping, and stability issues are shown as warnings. The pass/fail
decision is based on sensitivity, polarity, offset, and whether the waveform
stabilized before the capture limit.

## Finding the Best Input Voltage for SNR

Use the same sensor, filter/setup, gain, LabJack range, wiring, emitter drive
method, and fixture alignment for the whole comparison. For each distance, sweep
the same voltage points. Keep every run's distance entered in `Distance cm` and
voltage entered in `Input voltage V` so the CSV can be grouped later.

1. Choose a safe voltage sweep before starting. Stay within the sensor, emitter,
   fixture, and LabJack input limits. If the safe range is unknown, start low and
   increase in small steps.
2. Pick 5 to 10 voltage points across the range you want to compare. Smaller
   steps near the expected best voltage are useful.
3. Pick the distances you want to test, such as `45 cm`, `55 cm`, and `65 cm`.
   Use the same voltage list at every distance.
4. Set the first distance, enter it in `Distance cm`, then run the full voltage
   sweep at that distance.
5. At each voltage, wait for the fixture and waveform to settle before clicking
   `Start Test`.
6. Run at least 3 tests per voltage and distance. Use 5 tests per point if the
   readings vary a lot.
7. Move to the next distance and repeat the same voltage sweep.
8. Watch for clipping warnings, unstable waveform warnings, offset failures, or
   polarity failures. Do not treat a voltage as the best choice if it only looks
   good because the waveform is clipping or unstable.
9. After the first sweep, repeat the best few voltage and distance combinations
   in reverse order. This helps catch drift from warm-up, sensor heating, or setup
   changes.
10. Compare each distance and voltage combination by the average `snr_db`. Higher
   is better. Also check `noise_rms_mv`, `sensitivity_mv`, pass/fail, and warning
   messages.
11. If two settings are within about 1 to 2 dB of each other, prefer the lower,
   safer voltage and the easier fixture distance unless there is a production
   reason to choose the more demanding setting.

To have Codex help choose the best voltage, provide the CSV rows from the sweep
or the CSV file, plus any hard limits such as maximum voltage, maximum current,
temperature concerns, required fixture distance, or a required minimum
sensitivity. The useful columns are `distance_cm`, `input_voltage_v`, `sensor_id`,
`filter_setup`, `sensitivity_mv`, `noise_rms_mv`, `snr_db`, `pass_fail`, and
`fail_reasons`.
## Safety Notes

- Keep the AIN0 signal within the LabJack analog input range.
- Ensure the fixture, sensor/LabJack input, and sync signal have a valid common
  ground.
- If the T7 is claimed by another program, close LJStreamM/Kipling and press
  `Connect` again.
