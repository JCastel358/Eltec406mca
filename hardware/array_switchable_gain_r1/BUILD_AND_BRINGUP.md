# Wiring and one-channel bring-up

R1 engineering reference, 2026-09-14. Use this sequence to obtain the bench evidence that the schematic calculation cannot supply.

## Connections

| Connector | Pin | Connection |
|---|---:|---|
| J1 DETECTOR | 1 | Detector output formerly connected to the follower's non-inverting input |
| J1 | 2 | Detector analog return, AGND |
| J2 TO SAME DAQ CH | 1 | Existing single-ended DAQ analog input, selected 0-5 V range |
| J2 | 2 | DAQ analog ground / AGND |
| J3 POWER | 1 | Regulated +5 V analog supply, `+5VA` |
| J3 | 2 | Analog supply return, AGND |
| J3 | 3 | Separate regulated +5 V relay supply, `+5V_COIL` |
| J3 | 4 | Relay supply return, PGND |
| J4 optional control | 1 | Static 0/3.3 V or 0/5 V noise-mode request |
| J4 | 2 | Control return, PGND |
| JP1 selector | 1-2 shunt | Manual mode from SW1; default for bench work |
| JP1 selector | 2-3 shunt | Automatic mode from J4 |

Never bridge all three JP1 pins. No shunt leaves the relay off. SW1 open means BUFFER; closed means NOISE. R30 joins AGND and PGND at one star in the one-channel reference. Route the coil current back to its supply at that star, not along the detector return. In an array, provide one system ground connection instead of duplicating this star link across 50 channels. Check existing DAQ/control/supply return connections when planning the complete harness so they do not create parallel coil-current paths.

## Before connecting the DAQ

1. Check the actual package markings and pin numbering against `SOURCES.md`. In particular, G6K-2P-Y DC5 is the non-latching relay variant. Its coil uses pins 1 and 8 with the shown polarity.
2. With power removed, confirm relay continuity: 3-2 and 6-7 closed, 3-4 and 6-5 open. Check that the R1-R5 string remains connected when the relay changes state. Inspect C1 type, amplifier orientation and the flyback diode polarity.
3. Fit JP1 1-2 and leave SW1 open. Use current-limited supplies. Start with a known quiet, low-impedance source at about 0.7 V in place of the detector. Do not use a grounded, rail-clipped ADC trace as a low-noise reference.
4. Check +5VA and +5V_COIL, then the supply currents. Expect roughly 4 mA typical for the prototype quad plus small external loads with K1 off, and about 21 mA additional coil current with K1 on. Verify that no amplifier oscillates using a suitable scope/probe and the intended load/cable.
5. In BUFFER, verify J2 follows the source. With a 500 mV peak-to-peak signal centered on 0.7 V, expect about 0.45-0.95 V. Compare amplitude and offset against the original proven follower path.

## Confirm noise-mode gain and filter

6. Remove the sensitivity stimulus. Keep the input at a quiet detector-like DC level in BUFFER while C1 charges. Its nominal RC time constant is 1.01 s. Start with at least 20 seconds after the last large change, then determine the real settling interval from repeated captures. This is an initial test delay, not a qualified guarantee; dielectric absorption and detector/thermal recovery can take longer.
7. Select NOISE and discard switching samples. Confirm that `GAIN_OUT`, `LP1_OUT`, `FILTER_OUT` and J2 remain away from either supply rail. Allow additional measured settling after relay energization and any coil heating.
8. Apply a small calibrated sine, for example 1 mV peak-to-peak at the actual J1 input, centered on 0.7 V. Measure its true input amplitude with a suitable source/divider arrangement. Nominal J2 amplitudes are about 99.9 mV peak-to-peak at 3 Hz, 100.2 mV at 50 Hz and 99.3 mV at 100 Hz. Extend the sweep to 200, 500 and 1,000 Hz and compare with `exports/response.csv`. Verify the intended channel configuration and sample rate can capture those test frequencies.
9. Return to BUFFER before any 500 mV peak-to-peak sensitivity test. After that test, remove the stimulus and repeat the settling checks before measuring noise. Record recovery after realistic worst-case sensitivity exposures, not only small-signal switching.

## Establish the actual measurement floor

10. Connect the same DAQ/cable and use the production capture settings. With a quiet source at a representative detector offset and source impedance, capture both modes after settling. Separately measure DAQ-only noise using an unclipped, quiet input. Establish the amplifier-plus-DAQ input-referred floor in the original 3 Hz/Q=3 judged band; the target inferred from the user's existing report is approximately 0.42 uV RMS or better for the stated margin. This threshold belongs to that band and nominal calibration chain, not to a 100 Hz-wide RMS measurement.
11. Also inspect the spectrum through 100 Hz and above for hum, oscillation, aliased energy and switching artifacts. About 40 dB rejection at 500 Hz is finite attenuation. For continuous 0-100 Hz recording, 1 ksample/s per channel or higher is a reasonable starting point; 2 ksample/s per channel provides more separation from Nyquist if the acquisition configuration supports it. The current backend takes FOUR conversions per channel per scan, discards the first for multiplexer settling and averages the remaining three. Fifty channels at 2 ksample/s therefore require 400 kconversions/s, below the specified 500 kconversions/s aggregate ceiling before any additional driver constraints. The dropped conversion still consumes throughput. Verify actual rates and averaging behavior in the backend. Keep one fixed rate during a comparison.
12. Compare input-referred floor and calibrated gain across channels, temperature and repeated mode cycles before scaling. Evaluate a shorted/quiet source with the appropriate source impedance; an ideal low-impedance source alone cannot reproduce detector source-noise and input-current-noise effects. Repeat with real detectors and their required settling procedure.

## Software integration requirements

The package does not change the rig software. To integrate automatic mode control:

- Configure and drive the selected control output LOW before selecting JP1 AUTO or enabling relay power. The DAQ has pulled-high digital lines at startup, so the drawing's gate pull-down does not guarantee a safe automatic startup with the DAQ connected.
- Command BUFFER for offset and sensitivity; stop the stimulus and wait for settling before commanding NOISE. Return to BUFFER before allowing the stimulus to run again. Add a physical interlock if the application requires protection independent of software/operator sequencing.
- Save requested/confirmed mode, range, sample rate, gain calibration and discarded settling interval with each capture. The present circuit has no relay-position feedback. Software should not call a command a confirmed contact state without an added readback or verification measurement.
- Divide noise voltage by the calibrated transfer magnitude appropriate to its frequency/band. Do not divide the measured DC offset by 100. A broadband noise result needs frequency-dependent gain correction, rather than one correction for the entire spectrum.
- Retain the existing validated offset/sensitivity processing at 1X. Review software bandwidth, digital filters and anti-aliasing before extending a legacy 3 Hz noise method to 100 Hz.
- The current noise-analysis default decimates 1,000 samples/s by 20 to 50 samples/s. That output cannot represent a 100 Hz signal. Analyze the higher-rate data with a suitable measurement filter, and apply proper digital anti-alias filtering before any later rate reduction. Averaging three closely spaced conversions is not a substitute for a designed anti-alias response.
- The one-channel MOSFET drive is not a qualified fifty-channel digital fanout design. For a common array-mode line, check total gate/pull-down load and use an appropriate control buffer/distribution scheme; all relays need not be individually controlled.

## Assembly completion items

Select and qualify actual passive manufacturer parts, connector mates, manual switch and JP1 shunt. Confirm C1 dimensions/lead fit and update any generic footprints to match purchased components. Complete the PCB or controlled rework layout, check cable-load stability and measure the noise/recovery performance before considering fabrication of the full array. No detector, amplifier or ADC should be treated as qualified by a flat, clipped trace.
