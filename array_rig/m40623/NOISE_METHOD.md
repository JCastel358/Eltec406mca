# 40623 noise measurement and acceptance

## Current M25/HUB legacy-equivalent estimate

Added 2026-11-12 for the current M25/HUB profile. The older direct-buffer
method described below is a separate acquisition path.

`mod50_legacy_noise.py` adds a **Legacy-equivalent estimate (mV)** alongside
the unchanged DAQ-output RMS calculations. It is a nominal behavioural
model of the old meter, **not a calibrated equivalent measurement**.
`noise_pass_fail_enabled` is false and `acceptance_limits_mv` is null. The
old 10.0–37.9 mV limits never enter this calculation or the decision logic.
The technician display calls this value **Noise at 3 Hz** and plots the
stored held trace. Green shading at 10.0–37.9 mV is a labelled historical
reference only; reports retain both RMS values, the raw waveform excerpt and
the full method explanation. No display-derived value changes acceptance.

From application revision 2026-11-12.2, noise has a fixed 90-second warm-up
before the meter resets and capture begins. The earlier five-window mean/RMS
stability gate and noise-only “never settled” flag no longer apply. Warm-up
counts verified DAQ sample time, saves startup samples separately, and retains
the existing transport, clipping and hardware checks. Offset settling is
unchanged. The 90-second duration is user-authorized and still provisional.

The full-rate signed DAQ waveform passes through:

1. Initial-sample DC subtraction, then a causal, single-pass 3 Hz/Q3
   second-order band-pass. The bilinear transform is prewarped at 3 Hz.
   Unlike the existing FFT RMS metric, this path does not truncate at 1 or
   100 Hz and does not use a display excerpt, RMS-to-average factor,
   one-second detrending or forward/backward filtering.
2. Nominal old/new gain ratio = 1000 / 1000 = 1. The M25 already amplifies
   before conversion; another x1000 must not be applied to its DAQ output.
   There is no measured per-channel gain/phase or frequency-response
   equalization. The nominal ratio is not an input-referred calibration.
3. Absolute value with the nominal rectifier gain of 10.
4. A zero-initialized, single-pole exponential smoother with tau = 10 s:
   `s[n] = s[n-1] + (1-exp(-1/(fs*10))) * (abs(filtered[n])*10 - s[n-1])`.
5. An ideal peak hold: `h[n] = max(h[n-1], s[n])`, starting at zero and
   retaining the maximum since reset for this recording (up to 100 s).

The drawing's **100-second peak hold** is treated as a retention interval,
not as a measured decay time constant. No automatic discharge, rolling
100-second maximum, droop, hold acquisition error or circuit offset is
invented. All model states reset at recording start and on each retry;
states/data are never carried across an acquisition interruption. This
cold-start model does **not** reconstruct the old analogue filter/smoother
state before its hold reset. That difference and the ideal hold assumption
must be assessed during paired fixture comparison.

The final held reading is `legacy_equivalent_mv`. The first sample boundary
at or after 60 s supplies `legacy_equivalent_60s_mv`, allowing comparison
at a consistent elapsed time; waiting 100 s may capture a later noise burst.
The actual DAQ sample rate sets timing. The final smoother value, time of
the held peak, readout times and reduced smoothed/held traces are retained
in JSON. All computations precede display reduction. CSV includes the two
readings, smoother/peak fields, method ID, status, elapsed times and complete
model assumptions. Raw NPZ metadata includes method parameters and filter
coefficients before processing; original converter counts remain available.

At least 60 s of valid, uninterrupted data is needed for a displayed
estimate. Incomplete recordings, clipped/over-headroom or flat signals,
uninstalled positions and inferred no-signal positions do not receive a
legacy number. The original waveform evidence remains available. No
legacy amplitude changes a detector/slot verdict, history or yield count.
Existing signal-quality checks remain in place. Old reports are not
recalculated; missing legacy values show as unavailable.

Drawing sources: [9000232 rev B](../docs/40623_legacy_fixture/9000232_revB_filter.jpeg),
[9000272 rev J](../docs/40623_legacy_fixture/9000272_revJ_rectifier_hold.jpeg), and
[TP120 noise procedure](../docs/40623_legacy_fixture/TP120_noise_page5.jpeg).
Qualification still requires measured background/transfer and repeated
paired detectors covering both specification boundaries and burst noise,
under matching vacuum, bias, temperature and readout timing.

## Earlier direct-buffer measurement

Confirmed by the user on 2026-09-10: detector connector -> custom PCB with
1x op-amp buffers -> ACCES USB-AIO16-64MA. The legacy filter and rectifier
are not in this signal path. No paired readings from the old fixture are
available yet.

The supplied [9000232 rev B drawing](../docs/40623_legacy_fixture/9000232_revB_filter.jpeg)
specifies a 3 Hz multiple-feedback band-pass, Q=3, approximately 1 Hz
bandwidth, and nominal overall gain 1000. The
[9000272 drawing](../docs/40623_legacy_fixture/9000272_revJ_rectifier_hold.jpeg)
specifies absolute-value rectification, gain 10, 10-second smoothing and a
100-second peak-hold circuit. The
[TP120 noise page](../docs/40623_legacy_fixture/TP120_noise_page5.jpeg)
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
   `daq_noise_floor_check.py` (below) does this comparison in one capture.
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

## Instrument floor check

`daq_noise_floor_check.py` answers "can this acquisition setup resolve a
part's noise at all?" from one capture (`capture`, or `replay` of a saved
one) in which every socket has a role: empty as-is, grounded, quiet source,
loaded, plus the DAQ's onboard reference with `--daq-reference`. It prints
the 3 Hz/Q=3 band RMS per role, the ideal quantization of every DAQ range,
and compares the empty-socket floor with TP120's window brought to the pin
on the nominal x1000 x10 chain - a scale for the comparison, never a limit -
together with the break-even chain gain (the real gain below which the
measured floor would already be acceptable), the best case any oversampling
or range change could reach under the DAQ's 500 kS/s aggregate ceiling
(with the capture command that measures it) and the gain needed ahead of
the DAQ. It never issues a part verdict and never writes a calibration file.

Two traps it handles. A grounded input on the unipolar 0-5 V range sits on
code 0 and reads flat because the converter clips, not because the noise is
gone: such sockets are reported CLIPPED and excluded from every floor
figure. A part still settling puts slope / (Q w0) of its DC ramp through the
band-pass (17 mV over 60 s reads as 5 uV): such channels are flagged
SETTLING. The empty sockets as-is are a legitimate same-path background -
their buffers rest a few millivolts above zero, unclipped. First result:
CALIBRATION_RECORD §4b.1, "Instrument floor through the PCB".

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
python daq_noise_floor_check.py replay "C:\captures\tray.npz" --report "C:\captures\tray_floor.json"   # floor assessment, roles from the saved map
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
