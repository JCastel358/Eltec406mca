# 50SensorNoise R4 software

2026-09-17 engineering implementation. Legacy hardware and captures remain the
default. No live DAQ or DIO was accessed during implementation. Measured board
calibration, source impedance, overload recovery and production limits remain
physical qualification work.

## Operator use

The existing launcher supports an explicit R4 profile:

```powershell
python array_rig/m40623/eltec_40623_array_tester.py --frontend r4 --board-serial R4-001 --dedicated-r4-dio
```

Use `--simulate` without hardware. The R4 window has one global DC/Noise control,
a 1–100 Hz / 3 Hz Q=3 analysis selector, Capture, Stop, Detectors changed, and
Reset statistics. It waits for analog settling before capture, saves the signed
record before analysis, and shows per-channel quality. Reset clears displayed
statistics; it does not reset coupling capacitors. Use Detectors changed after
any detector/holder reconnection, power change or disturbance; it reapplies the
90-second gate. Power/readback faults also restart this gate automatically.

The R4 profile opens a dedicated window from the same application entry point.
It does not reuse legacy TP120 acceptance or tray/vacuum workflow. Real R4
measurements remain NO_LIMIT until a separate qualified acceptance method is
introduced. This implementation deliberately contains no factory-pass override.

Command-line capture and replay:

```powershell
python array_rig/m40623/daq_r4.py --board-serial R4-001 --dedicated-r4-dio --frontend-mode noise --seconds 100 --calibration r4_measured.json --output noise.npz
python array_rig/m40623/daq_r4.py --replay noise.npz --analysis 3hz_q3 --output replay.npz
```

`--band LOW HIGH` selects a detector-specific interval within 1–100 Hz. `--range`
allows diagnostic ADC ranges; a range different from calibration invalidates
input-referred calibration. `--diagnostic-reason TEXT` permits early unsettled
capture and permanently records why; this record remains INVALID. It cannot
bypass missing power or incorrect control readback. A cancelled/faulted capture
is retained with its final diagnostics and cannot become passing data.

## Hardware contract and ownership

Only the R4 device owner configures or drives these dedicated byte ports:

| DAQ bit | Direction | Function |
|---|---|---|
| DIO0 | Output | ROUTE_NOISE command |
| DIO1 | Output | NOISE_GAIN_REQUEST command |
| DIO2–7 | Output | Reserved, driven low |
| DIO8 | Input | Effective route = route request AND power ready |
| DIO9 | Input | Effective high gain, including hardware interlock |
| DIO10 | Input | POWER_READY: both ±5 V rails in both banks valid |
| DIO11–15 | Input | Reserved |

Both ports must be exclusively assigned to the R4 harness. The explicit
`--dedicated-r4-dio` declaration prevents silent reassignment of other hardware.
The Windows API uses direction mask `0x01`: byte 0 output, byte 1 input. API
signatures and group semantics were checked against the installed ACCES USB
Software Reference and [ACCES's reference](https://accesio.com/files/USB%20Software%20Reference.html).
The revision-matched DB25 cable uses DIO0 pin 1, DIO1 pin 2, DIO8 pin 10,
DIO9 pin 11, DIO10 pin 12 and GND pin 9; verify connector mating orientation
against the hardware cable drawing before wiring.

Connect initializes DC/unity and requires 90 seconds after power/connect. Enter
Noise: stop/drain, gain off, AC route, 200 ms delay (exceeds the hardware recovery
interlock), gain on, ±1 V ADC range, 90-second settling. Enter DC: stop/drain,
gain off, 50 ms delay, DC route, 0–5 V range, at least 1 second settling without
shortening an existing longer gate. Every command is checked against effective
readbacks; capture checks them before, throughout, and after sampling. Controls
remain static during capture. Hardware readback failure inhibits acquisition.

Default acquisition is 50 channels at 2,000 scans/s, four conversions/channel,
first conversion discarded and three averaged: 400 kconversions/s. The actual
granted timer rate is used for time, frequency, PSD and saved replay. Sustained
throughput, adjacent-channel settling and real-DAQ timing need bench validation.

A process crash can leave outputs latched. Hardware defaults/interlocks remain
necessary. Readback polling cannot detect an arbitrarily short inter-poll fault;
physical qualification must evaluate whether a fault latch is needed. Software
only detects clipping visible at the ADC. It cannot certify absence of internal
amplifier saturation or powered-off analog input damage.

## Measured calibration format

Calibration is strict JSON, schema 1, `frontend_profile: "50SensorNoise_R4"`, with
`calibration_id`, `board_serial`, `daq_serial`, and measurement `provenance`.
`acquisition` must contain exactly the intended `dc` and `noise` fingerprints:
range_code, sample_rate_hz (actual), oversample_extra, and drop_first.

`modes` has exactly `dc` and `noise`. Each contains channel strings `"0"` through
`"49"` for channels actually calibrated. Each channel entry contains:

- `frequency_hz`: strictly increasing measured frequencies covering 1–100 Hz;
- `gain_v_per_v`: measured complete-chain gain at those frequencies;
- `phase_deg`: corresponding measured phase, unwrapped during interpolation;
- `relative_uncertainty`: positive fractional measurement uncertainty;
- DC entries additionally require `offset_v` and `dc_gain`.

Recommended frequency points are 1, 3, 10, 30, 50 and 100 Hz. Record source
impedance, supply conditions, temperature, instruments and traceability in
provenance. Calibration identity must match board and DAQ, and its range/rate/
conversion fingerprint must match each capture. Missing channel calibration
reports UNCALIBRATED and does not divide values by nominal 1000.

The calibration is embedded in every raw NPZ, alongside board identity,
channel map, controls/readbacks, settling, diagnostic reason, ADC configuration,
actual rate, simulation flag, and final diagnostics. Raw data are signed float64
DAQ volts before gain correction or rectification. Historical NPZ files are
explicitly rejected by the R4 replay tool and remain readable by legacy tools.

## Analysis definition and limits

`r4_finite_record_fft_v1` analyzes the entire completed record at its actual rate.
It removes the mean, uses a rectangular finite-record FFT, divides by the
measured complex gain with linearly interpolated amplitude and unwrapped phase,
and selects inclusive bins in the requested band. The one-sided PSD uses the
usual factor of two except DC/Nyquist; band RMS agrees with integrated PSD.
The 3 Hz/Q=3 option additionally weights these 1–100 Hz bins by the nominal
second-order analog bandpass transfer. This defined metric is not exact
legacy-fixture or causal filter emulation. Short/nonperiodic records have finite
record leakage; a minimum 100-second record is recommended for routine work.

Raw peak-to-peak, input band RMS, mean absolute value, exponentially smoothed
absolute value and held peak are distinct. The smoother starts at zero, defaults
to tau=10 seconds, and is marked settled after five time constants. Hold starts
at the first accepted sample and tracks the smoothed absolute signal over the
first 100 seconds, then freezes; interval completion is recorded. Starting a new
capture resets these statistics. Analysis never modifies raw samples. Capture
and replay use the identical function; no fixed 50 SPS assumption is used for
R4, so the complete 100 Hz band remains available.

Records shorter than ten cycles at the lower band edge are INVALID.
Quarter-record RMS ratio >1.5 marks nonstationary data INVALID. This is an
engineering guard, not a characterized detector acceptance rule. ADC clipping,
unsettled/overridden capture, nonfinite samples, control/power mismatch and final
stream errors also prevent valid calibrated status. Even a calibrated valid
record reports NO_LIMIT, never PASS; legacy DMM 10–37.9 mV limits are not applied.

The five BAV199 dual-diode protection devices per channel limit connection-step
overload. Noise records with any raw sample outside ±0.8 V are INVALID, even
when the ADC has not clipped. The check precedes filtering and mean removal.
The prototype precision range is 20–30 °C; record board temperature during
physical calibration and qualification. Simulation predicts increasing clamp
compression at higher amplitudes or temperatures and does not establish an
industrial-temperature accuracy specification. There is no onboard temperature
measurement in this revision.

## Integrity repairs to legacy acquisition

The backend now retains pacing-clock stop status, bulk-end API status, the final
bulk worker status, and cleanup errors. Shutdown still joins/drains its consumer
on failure. Unconfirmed DLL shutdown retains callback references and prevents
backend reuse. Live streaming promotes final diagnostics to its error status so
the existing viewer exits nonzero for failures discovered during shutdown.
Save-on-exit includes final integrity JSON. Still-running snapshots explicitly
record unavailable final integrity. Historical NPZ fields remain readable;
`sample_rate_hz` now records actual rate and `nominal_sample_rate_hz` preserves the
requested rate.

Tests use simulated devices and fault-injected DLL doubles only. They cover
mode sequencing, power loss, settling, partial captures, calibration gain,
band edges, PSD power, hold/smoothing, stationarity, replay/raw equivalence,
historical-file rejection and final shutdown faults. Physical qualification is
separate from these software checks.

Validation recorded 2026-09-17: the 386-test array-rig regression suite passed
(`R4_TEST_RESULTS.txt`); after adding DIO-byte and launcher routing coverage,
all 19 focused R4 tests passed (`R4_FOCUSED_TEST_RESULTS.txt`). After adding the
raw ±0.8 V headroom guard, all 20 focused tests passed
(`software_headroom_tests.txt` in the hardware validation package). Syntax
compilation and a simulated connect/close smoke check of the Tk window also
passed. A further three-case check exercised the actual Tk worker with normal
closure, an injected shutdown fault and an injected mode-command fault:
failures returned exit code 2 and an unsuccessful mode change displayed
Unavailable. The window now displays only the last confirmed mode while a
change is pending, and retains failure exit status for invalid captures or
worker/shutdown faults (`software_gui_fault_tests.txt`). No physical DAQ,
control output or assembled R4 board was exercised.
