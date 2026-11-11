# Battery vs. regulated supply — dark noise

**2026-10-23 hardware fault:** the user measured approximately 15.2 V at the
regulator output / detector power input. Keep that regulator disconnected.
Replace it and verify the output before reconnecting the rig; check the exposed
detector and input circuitry before trusting another comparison. The saved run
`20261023_083949_406MCA_09c07df5` is not a valid comparison of a healthy 5.05 V
supply against the battery. Original recordings and reports are preserved.

**Confirmed divider correction:** the installed AIN7 divider is **300 kΩ /
100 kΩ (4:1)**, confirmed by the user on 2026-10-23. The firmware still applies
the old 2.001004× divider factor. This script removes that factor and applies
4×: `supply voltage = BAT? response / 2.001004016 × 4`.

The saved battery indication of 3.3013 V therefore corresponds to about
**6.60 V**, using nominal resistor values; it is not forced to the meter's
6.69 V. The saved 7.4333 V supply indication would scale to 14.86 V, but is
**outside the buffered ADC's measurement range** and cannot be trusted as an
accurate voltage. With this 4:1 divider, 15.2 V ideally puts 3.8 V at AIN7,
not the previously assumed 7.6 V. That exceeds the buffered operating range,
but does not by itself establish an ADC absolute-maximum violation with a
5 V analog supply. Use an independent meter; AIN7 is not hardware protection.

Double-click **run_power_noise_compare.cmd**. Select **405M22** or **406MCA**.

1. Close the production tester and any serial viewer. Install one detector on
   the usual AIN0 input. Unplug the emitter and cover the detector against
   changing light, heat, and airflow. Leave the same detector and signal wiring
   in place for both recordings.
2. Connect the approximately **6.69 V detector battery**. Tick the setup
   checkbox, then click **Record battery**. Wait 90 seconds: 30 s settling,
   then 60 s recording.
3. Disconnect the detector battery and connect the **regulated approximately
   5.05 V output** of the 12 V supply. Connect only the regulated output to the
   detector power input; do not connect the two sources together. Verify the
   regulator output with a meter before connection, tick the setup checkbox
   again, then click **Record supply** and wait another 90 seconds.
4. Read the RMS and peak-to-peak results. **Open results** opens a simple saved
   report. Lower values mean less measured noise. Negative supply change means
   the supply measured lower than the battery. AIN7 supply-voltage estimates
   are shown before and after each recording.

The ESP32/ADC remain on their usual USB power. The source change is manual.
The program reads the detector supply through the existing AIN7 connection;
it does not switch power. Blank port means automatic ESP32 discovery; enter
the rig's COM port if multiple boards are attached.

Results are saved under **`~/Eltec_Power_Noise_Comparisons/`**, in a unique
folder for each comparison: `report.html`, `summary.csv`, `session.json`, raw
sample CSVs and diagnostics for each attempt. Stop/error attempts are retained
but excluded from the comparison. Reopen the program for another detector or
another comparison. Existing production files, calibration and firmware are
unchanged.

## What the numbers mean

- **RMS:** `sqrt(mean((V - mean(V))**2))`, reported in mV. It excludes the DC
  offset but includes drift and spikes; it is not the driven-cycle residual
  RMS used by the production tester.
- **Peak-to-peak:** full 60-second maximum minus minimum, in mV. It includes
  rare spikes, so both sources use equal recording lengths.
- **Supply change:** `100 * (supply / battery - 1)`, separately for each metric.
  RMS determines the headline; the report flags conflicting peak-to-peak ranking.
- Measurements are at AIN0 versus AINCOM, at 1000 samples/s, without extra
  software filtering, decimation, detrending, or production gain multipliers.
  Fixture and ADC filtering set the actual bandwidth; this is not a claim of
  flat response through 500 Hz or a measurement of all supply switching noise.
- The report also retains mean voltage and first-to-last-five-second mean shift
  to help identify drift. The fixed settling wait does not prove stabilization.

**Calculation review, 2026-10-23:** RMS uses division by the number of samples
`N`, as required for the RMS of the recorded waveform; it is not the `N-1`
estimate of population variance. Peak-to-peak includes every measurement sample,
and both calculations exclude settling and stream-drain samples. Voltage-to-mV
conversion is exactly 1000. Supply percent change uses battery as the denominator.

Noise now uses the full signed ADC counts rather than the firmware's rounded
six-decimal voltage text. The scale is
`raw_code * (2 * ADC_reference_voltage / ADC_gain) / 8388607`, matching the rig
firmware's `countsToVolts()`. Reference voltage is read from `STATUS?`, and
runtime full scale is checked against `FE?`. For the nominal 2.5 V ADC reference,
full scale is 5 V on the 405M22 profile and 2.5 V on the 406MCA profile. This
preserves changes smaller than the transmitted decimal voltage's 1 µV step.
The raw CSV's `volts` column now contains the full-count conversion;
`firmware_volts` separately preserves the original rounded text value. Scaling
metadata is retained in each attempt JSON and `session.json`. Format version 4
records the confirmed 4:1 divider. Summary CSV columns
`supply_voltage_before_v` / `supply_voltage_after_v` contain the corrected
estimates, with separate status and original firmware-response columns.

AIN7 uses the production backend's `read_battery_voltage()` / `BAT?` operation.
That response is **already scaled with the old firmware divider**. The script
divides by that factor before applying the physical 4:1 factor; it does not
multiply the existing reading by four. It reads once before settling and once
after the AIN0 stream has stopped. JSON retains the original firmware response,
inferred ADC-pin voltage, both divider factors, and corrected estimate.
A failed AIN7 query is labelled **unavailable**; it does not invalidate
otherwise good AIN0 noise measurements. A detected out-of-range AIN7 reading
rejects the attempt. AIN7 voltage is never used as the ADC reference, as a
divisor for RMS, or as a measurement of supply ripple during the capture.

On 406MCA, buffered AIN7 readings are labelled **4:1 estimate**. With nominal
5 V AVDD, the buffered input limit is 3 V at AIN7 (12 V before the divider).
On 405M22, the buffer is off and the ADC can load the divider; readings are
labelled **indicative** and may read low. Nominal resistor values are not a
meter calibration. These input limitations are described in the
[TI ADS1256 datasheet](https://www.ti.com/lit/ds/symlink/ads1256.pdf).
The script leaves the model's buffer settings unchanged. Its range check
cannot disconnect power or prove a supply safe; an overloaded ADC may return
misleading readings. Check the regulator independently before connection.

**6.69 V and 5.05 V are different detector operating conditions.** This answers
which complete setup measured quieter for this detector, including detector,
fixture and ADC noise. It does not isolate the supply's intrinsic noise or
prove statistical significance from one capture each. For a close result,
repeat the comparison. A separate matched-voltage comparison would be needed
to isolate the effect of changing the power source. No production pass/fail
thresholds are applied.

The model choice retains the production ADC profile: 405M22 gain 1/buffer off;
406MCA gain 2/buffer on. Runtime profiles are set and read back on v2.1+;
fixed-profile 405 v2.0 and legacy 406 v1.9 are supported without flashing.
OFF is explicitly acknowledged before every stream; no ON command is sent.
Missing, reordered, duplicate or malformed samples, ADC overruns, nonzero sync,
flat waveforms and near-full-scale ADC codes reject a recording instead of
declaring it quiet. A disagreement between raw-code scaling and the firmware's
voltage also rejects the recording. A physically missing detector is not
automatically detected.

## Optional commands

Uses the existing rig Python dependencies: NumPy, pyserial, Tkinter.

```powershell
python engineer_tools/power_noise/power_noise_compare.py
python engineer_tools/power_noise/power_noise_compare.py --seconds 120 --settle 60 --port COM3
python engineer_tools/power_noise/power_noise_compare.py --simulate --output tmp/power_noise_demo
python -m unittest discover -s single_detector_rig/tests -p test_power_noise_compare.py
```

Simulation never opens a serial port; all demo files are labelled simulated.
