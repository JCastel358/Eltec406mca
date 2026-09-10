# Array app 0.4 validation, September 10, 2026

Changes were made in the user-requested checkout:
`C:/Users/JoseCastelblanco/Documents/Eltec_TestRig/array_rig`.
No DAQ hardware was opened, no detector acceptance limits were invented, and
no changes were committed or pushed.

## Evidence for the readout repair

The user had all ten fifth-row sockets populated. September 9 offset history
showed inconsistent in-range readings at other positions and all 50 positions
classified as loaded, preventing noise capture. The waveform viewer's bulk
path worked for the user; operator offsets had used immediate `ADC_GetScan`.
The app now uses the bulk path for operator offsets. The immediate API's
documented 128-word buffer/indexing contract was checked; the precise DLL
failure mechanism has not been established.

`test_offset_stream.py` exercises the actual ctypes callback and deinterleaver
with CH40-49 distinct voltages over three starts, fragmented callbacks,
corrupt first MUX conversions and startup transients. It also checks missing,
malformed and incomplete data, cancellation and stream-integrity failures.
No-data errors cannot become empty-detector measurements.

## Automated checks

- Bundled Python 3.11: 298 model tests ran successfully with one module skipped
  because that runtime does not include matplotlib. The 34 selector/history
  tests passed with no skips.
- Installed Python 3.14.5, NumPy 2.4.6, matplotlib 3.10.9: all **337 model
  tests passed, no skips**, including waveform graphics, Tk operator flow,
  snapshot output, noise processing, replay and retries.
- After the final replay compatibility change (`drop_first` metadata from the
  waveform viewer), all **7 replay tests passed** on the installed runtime.
  This adds one test to the previously passing full model suite; current
  discovery lists 338 model tests. Together with the 34 selector/history
  tests, 372 distinct tests have passed for the delivered changes.
- `git diff --check` passed.

The bundled run emitted a Tk theme-destruction diagnostic during teardown;
its tests still passed. The installed-runtime run completed cleanly.
The readout's pre-existing `Thread._stop` name collision, found by the first
baseline run, was repaired rather than ignored.

Noise checks include independent sinusoidal center gain and -3 dB edges,
DC rejection, rectification without signed cancellation, channel independence,
minimum capture duration, both converter rails, unresolved constant signals,
calibration mismatch, low/pass/high metric bounds and resolution-floor gates.
End-to-end tests prove old wideband peak limits cannot qualify hardware
results, pending/missing noise cannot become PASS, calibration evidence is
retained, and failed CSV schema upgrades leave old files intact.

## Recorded DAQ replay

The existing real capture
`Documents/Eltec_40623_Test_Results/40623_array_daq/noise_captures/lot_Test/tray_1_raw.npz`
was re-analysed at its recorded 1000 Hz actual timer rate. The output is
`output/array_rig_validation/2026-09-10_historical_noise_3hz.json` in the
repository. Its saved map contains seven loaded and 43 empty positions;
all seven loaded noise results remain NO_LIMIT. This historical map is not
assumed to describe the later failed fifth-row run. The original capture was
not changed. The replay is measurement evidence, not fixture qualification.

## Remaining physical checks

Repeat offset reads with the known fifth-row tray and compare its ten values
and positions against the working waveform viewer, with only one DAQ program
running at a time. Then capture under the required vacuum conditions.

Noise acceptance needs paired legacy readings and a measured instrument
background/resolution check through the 1x PCB. The legacy nominal gains and
3 Hz/Q=3 drawing support the implemented measurement band, but do not qualify
TP120 meter thresholds on raw DAQ volts. See [NOISE_METHOD.md](NOISE_METHOD.md).
