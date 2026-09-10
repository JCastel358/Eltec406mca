# 40623 noise measurement and acceptance

## What this rig measures

Confirmed by the user on 2026-09-10: detector connector -> custom PCB with
1x op-amp buffers -> ACCES USB-AIO16-64MA. The legacy filter and rectifier
are not in this signal path. No paired readings from the old fixture are
available yet.

The supplied [9000232 rev B drawing](../../docs/40623_legacy_fixture/9000232_revB_filter.jpeg)
specifies a 3 Hz multiple-feedback band-pass, Q=3, approximately 1 Hz
bandwidth, and nominal overall gain 1000. The
[9000272 drawing](../../docs/40623_legacy_fixture/9000272_revJ_rectifier_hold.jpeg)
specifies absolute-value rectification, gain 10, 10-second smoothing and a
100-second peak-hold circuit. The
[TP120 noise page](../../docs/40623_legacy_fixture/TP120_noise_page5.jpeg)
sets 10.0-37.9 mV at the final DC meter under vacuum after at least 60 seconds
following reset. These are not raw detector RMS or peak-to-peak limits.

`legacy_noise.py` applies a causal, unity-center-gain second-order 3 Hz/Q=3
digital band-pass to uniformly sampled detector volts. It subtracts the
initial DC value to avoid a startup step. It excludes the first five seconds
of filter startup from RMS and mean-absolute statistics and records that
effective analysis duration. It also calculates an absolute-value signal
smoothed with a 10-second exponential time constant from zero at capture
start. A normal operator capture is 60 seconds; shorter engineering captures
are measurable but cannot receive an acceptance verdict.

The UI shows 3 Hz RMS in microvolts and the DC offset together. CSV and raw
NPZ files retain RMS, mean-absolute value, final smoothed magnitude, quality
reasons, acquisition identity and method ID. A nominal meter estimate
(smoothed magnitude times 1000 times 10) is archived for investigation only;
it is **never compared with TP120 limits**. The complete analog transfer
function and nonlinear peak-hold response have not been validated. The
older 0.85-22 Hz windowed peak-to-peak values remain diagnostics for replay
and comparison, not the hardware acceptance method. The explicitly marked
simulator demo retains its separate example peak-to-peak limits.

## Meaning of the result

- **CALIBRATION PENDING / REVIEW:** measured data exists, but there is no
  validated relationship to the old fixture. This is not a pass, including
  in the saved CSV and the programmatic `passed` property.
- **NOT MEASURED / NOT READ:** data is present but cannot support a noise
  decision (for example, too short, clipped, constant, below a qualified
  resolution floor, or acquired with mismatched calibration settings).
  The recorded measurements and explanation remain available.
- **PASS / HIGH / LOW:** only enabled by an explicit paired calibration of
  a named 3 Hz software metric for this acquisition setup. Offset failures
  remain separate and take precedence in the overall detector result.

The existing 0.3-1.2 V offset window is unchanged and remains provisional
pending confirmation of detector bias/loading against TP120.

## What is needed to qualify noise acceptance

1. Record a loaded-channel background using a suitable quiet source at a
   representative detector DC level, through the same PCB, cables and DAQ
   settings. A grounded input at the bottom of a unipolar range is clipped
   and is not a usable noise-floor measurement. Check anti-alias behavior.
2. Measure the same identified detectors on both rigs under matching vacuum
   and bias conditions. Include quiet, noisy and marginal units, not only
   passing ones. Keep raw captures, legacy meter readings and repeat runs.
3. Demonstrate that the chosen RMS/rectified software metric separates the
   legacy decisions reproducibly; establish both acceptance boundaries and
   a minimum usable resolution above the measured background. Do not divide
   the old meter limits by a nominal gain and call that calibration.
4. Record the paired evidence in a versioned JSON calibration and rerun
   independent reference units. Requalify after changes to PCB, wiring,
   gain, range, sampling, oversampling or the measurement method.

The 0-5 V ADC range has a 76.294 microvolt raw code step. Band limiting and
oversampling can improve measurements when the noise/dither permits it,
but do not prove recovery of an undithered signal smaller than one code.
The software records a white-quantization estimate as an estimate, not a
measured instrument floor. Additional analog gain/filtering may be needed;
the reference measurements must establish this.

## Calibration file

Set `ELTEC_ARRAY_NOISE_CALIBRATION` to the absolute path of a qualified JSON
file before launching the app. No file is supplied with fabricated limits.
A missing setting leaves acceptance pending; a malformed specified file
produces an actionable error rather than silently reverting.

The schema requires `schema_version: 1`, the exact `method_id` from
`legacy_noise.METHOD_ID`, `calibration_id`, `provenance`, `metric`, `low_mv`,
`high_mv`, `background_metric_mv`, `minimum_resolvable_metric_mv`, and
`acquisition`. All metric values use detector-referred millivolts; metric is
one of `band_rms_mv`, `band_mean_abs_mv`, or `smoothed_abs_final_mv`.
The acquisition object must match the saved analysis's `acquisition` object,
including actual sample rate, DAQ serial in setup ID, gain, range, resolution,
oversampling and dropped conversions. The setup ID assumes the physical
buffer PCB/wiring associated with that serial has not changed: invalidate
the calibration whenever that hardware changes.

The loader requires `0 <= background < minimum_resolvable < low <= high`.
Measurements at or below the demonstrated resolution remain unresolved,
never an automatic dead-detector verdict. The full calibration evidence
and metric are exported with results. Existing lot CSVs gain the new columns
atomically while preserving old rows and any custom columns; historical
values are not reinterpreted or retroactively changed.

## Readout repair and bench check

The September 9 logs showed only a few in-range offsets at shifting positions
even though the user had loaded the entire fifth row. The app had used
`ADC_GetScan` immediate reads, while the successful waveform viewer used
continuous bulk scans. Operator offsets now use that same bulk path: discard
0.2 seconds of startup, collect a 1-second median per channel, drop the first
conversion after each MUX switch, and enforce stream integrity/timeouts.
The precise driver cause of the old immediate readings is not established.

Before relying on the repair on the bench, close other DAQ programs, load
the known fifth-row tray and repeat Measure offset. CH40-49 must stay at
5-1 through 5-10, matching the waveform viewer. Numerical values are always
visible. For sockets whose physical occupancy cannot be inferred from their
buffer output, use Choose loaded sockets; a map change requires a fresh read.
This selection never remaps or fabricates channel data.

Automated regression covers repeated row-5 reads with fragmented driver
callbacks, startup transients and corrupt first MUX samples. Hardware
validation and paired noise qualification remain separate from those tests.

## Re-analyse an existing capture

From the `m40623` directory:

```powershell
python replay_legacy_noise.py "C:\captures\tray.npz" --output "C:\captures\tray_3hz.json"
python replay_legacy_noise.py "C:\captures\tray.npz" --output "C:\captures\tray_3hz.csv"
```

The output path is required and must be a new `.json` or `.csv` file. The replay
does not connect to hardware and does not modify the original capture or an
existing report. It uses the saved actual timer rate when available and copies
the range, oversampling, dropped-conversion count, DAQ serial, occupancy, and
sensor numbers into the evidence. Missing actual rate allows exploratory
replay at the nominal rate with a warning; missing acquisition settings are an
error. Empty and unknown sockets cannot acquire detector verdicts. Replay
reports noise only and does not recompute offset or overall detector verdicts.


Add `--calibration "C:\qualification\40623_noise.json"` only after qualification.
