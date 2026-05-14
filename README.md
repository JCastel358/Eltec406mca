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

- `AIN0`: AM502 amplifier output waveform.
- `AIN1`: DC offset voltage.
- `AIN2`: blade sync signal.

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

The offset is measured automatically at the beginning of each test. The waveform
is expected to be triangular. The program segments the signal by blade-sync
cycles and watches the cycle peak-to-peak readings until the rolling average is
stable within 10%. It then uses the median peak-to-peak voltage from the stable
cycles. Because the waveform is read after the AM502, the program divides the
measured waveform by the configured AM502 gain before comparing against the
sensitivity limit.

Frequency, clipping, and stability issues are shown as warnings. The pass/fail
decision is based on sensitivity, polarity, offset, and whether the waveform
stabilized before the capture limit.

## Safety Notes

- Keep the AM502 output within the LabJack analog input range.
- Ensure the fixture, AM502/LabJack input, and sync signal have a valid common
  ground.
- If the T7 is claimed by another program, close LJStreamM/Kipling and press
  `Connect` again.
