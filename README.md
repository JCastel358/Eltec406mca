# Eltec 406MCA Single-Sensor Tester

Code for industrial automation process.

This is the first single-sensor test program for the 406MCA sensitivity,
polarity, and offset procedure. It uses the LabJack T7-Pro through the
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

## Operator Workflow

1. Enter or scan the sensor ID.
2. Select the filter/setup being used.
3. Click `Start Test`.
4. Read the large `PASS` or `FAIL` result and the failure reason list.

The app logs one row per waveform test with:

`timestamp, sensor_id, model, filter_setup, offset_v, sensitivity_mv, polarity, pass_fail, fail_reasons`

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

If the measured `AIN0` average is near `0 V`, the waveform output is not carrying
the detector offset. Check that the waveform output path is DC-coupled and
referenced to the same ground. If that output is intentionally AC-coupled or
centered at ground, the original offset cannot be recovered from `AIN0` alone.

Frequency, clipping, and stability issues are shown as warnings. The pass/fail
decision is based on sensitivity, polarity, offset, and whether the waveform
stabilized before the capture limit.

## Safety Notes

- Keep the AIN0 signal within the LabJack analog input range.
- Ensure the fixture, sensor/LabJack input, and sync signal have a valid common
  ground.
- If the T7 is claimed by another program, close LJStreamM/Kipling and press
  `Connect` again.
