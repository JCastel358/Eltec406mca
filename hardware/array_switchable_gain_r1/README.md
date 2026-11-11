# Switchable 1X / 100X detector amplifier

Revision R1, 2026-09-14. **Editable, one-channel engineering reference; bench qualification pending.**

This circuit preserves the follower connection for the offset and 500 mV peak-to-peak sensitivity measurements you have already checked. A DPDT relay changes the same amplifier to approximately 100X AC gain and connects a hardware low-pass filter for noise measurements. Both modes use the same DAQ input. The original 50-channel project is unchanged.

## Open the design

Open `project/switchable_gain.kicad_pro` in KiCad 10. The root schematic links to two child sheets; open those sheet boxes to see the filter and power/control circuit. Keep the `project` and `libraries` directories together. Project symbols and the required stock footprints are bundled.

| Deliverable | Location |
|---|---|
| Editable schematic, three sheets | `project/switchable_gain.kicad_sch` |
| Printable A3 schematic, three pages | `exports/switchable_gain_schematic.pdf` |
| Parts list, one reference channel | `exports/BOM.csv` |
| Nominal response plot and data | `exports/calculated_response.png`, `exports/response.csv` |
| Wiring and first bench measurements | `BUILD_AND_BRINGUP.md` |
| Electrical and calculation evidence | `reports/VALIDATION.md`, `reports/validation.json` |
| Manufacturer references and pin mapping | `SOURCES.md` |

## What each mode does

| Test | Relay K1 | Signal delivered to the DAQ |
|---|---|---|
| Offset | Unpowered | Direct follower, approximately 1X DC gain |
| Sensitivity | Unpowered | Direct follower, approximately 1X AC gain; noise filter bypassed |
| Noise | Powered | Approximately 100X AC gain followed by a four-pole low-pass filter |

At 1X, a 0.7 V offset with a symmetric 500 mV peak-to-peak signal occupies approximately 0.45-0.95 V. This fits the existing 0-5 V DAQ range. At 100X, that same AC signal would demand 50 V peak-to-peak and saturate the amplifier. Return to BUFFER before turning on the sensitivity stimulus. This reference includes mode selection, not a hardware interlock with the illumination source.

K1 pole A shorts the gain resistor string in BUFFER. When that short opens, R1-R5 remain connected, so the amplifier always has a feedback path. Pole B selects the direct or filtered output. The contacts are shown with the coil unpowered. Contact bounce and differing pole transition times mean switching samples must be discarded.

The gain network is:

`Rf = 5 x 2.00 Mohm = 10.000 Mohm; Rg = 101 kohm; C1 = 10 uF polypropylene`

`H_gain(s) = 1 + s*Rf*C1 / (1 + s*Rg*C1)`

Its high-frequency gain is 100.0099 and its low-frequency transition is 0.1576 Hz. DC gain approaches unity in the ideal model; capacitor leakage and amplifier bias make the noise-mode DC level unsuitable as the offset measurement. C1 stays connected and charges through Rg during BUFFER operation.

## Bandwidth and filtering

Two unity-gain Sallen-Key sections approximate a fourth-order Butterworth filter. Their corner is near 160 Hz so gain remains close to 100 through 100 Hz. Setting the -3 dB corner at 100 Hz would instead reduce a 100X passband to about 70.7X at 100 Hz. Hardware filtering has a gradual transition, not a sharp boundary at 100 Hz.

| Frequency | Calculated noise gain at DAQ | Relative to 100X |
|---:|---:|---:|
| 3 Hz | 99.863X | -0.012 dB |
| 50 Hz | 100.171X | +0.015 dB |
| 100 Hz | 99.341X | -0.057 dB |
| 160 Hz | 70.617X | -3.022 dB |
| 200 Hz | 37.649X | -8.485 dB |
| 500 Hz | 1.040X | -39.657 dB |
| 1 kHz | 0.065X | -63.740 dB |

Values include a nominal 100-ohm output resistor, 0.1-ohm relay contact and the DAQ's specified 1 Mohm input load. Actual gain requires calibration. With the specified resistor and capacitor tolerances, the evaluated component endpoints give 97.88-100.82X at 100 Hz. Temperature drift and aging are additional effects.

The filter follows the gain stage. Large interference can overload that stage before the filter attenuates it; verify headroom at `GAIN_OUT` as well as `DAQ_OUT`. The 100 Hz passband also passes 50/60 Hz mains interference; shielding and grounding remain relevant.

The exact 100 Hz measurement-band shaping can be performed digitally. This four-pole analog filter is one implementation, not a demonstrated minimum order. A simpler analog filter combined with higher-rate sampling and digital filtering may be adequate after an alias/noise budget and bench validation. Removing all external filtering requires evidence that out-of-band signals and amplifier noise folding into the measurement band are negligible. Digital filtering cannot remove noise already aliased into that band during acquisition; see [Analog Devices' aliasing explanation](https://www.analog.com/en/resources/interactive-design-tools/adcalias.html). The reference schematic retains the four-pole filter pending that comparison.

## Integration with the existing hardware

U1A represents one of the existing OPA4192 follower sections. Retain the detector's existing bias/source resistor and supply arrangement. Break the permanent output-to-inverting-input follower connection and replace it with the R1-R6/C1/K1A network. Route that channel's output through K1B and R7 to its existing DAQ pin. Keep the output isolation resistor only once. Actual board rework needs a trace/pad plan; this package does not identify cut locations or update the old PCB.

For a separate one-channel prototype, populate U1A-C as drawn and connect J1 to the detector signal that previously fed the follower. Cascading the prototype after an existing follower adds another amplifier's noise and would require evaluating that combined path separately.

The added filter uses two amplifier sections per channel. A full 50-channel design needs 150 active sections: 38 OPA4192 quads total. The original 13 quads can supply the 50 gain stages; 25 additional quads supply the 100 filter stages. The one-channel drawing uses one quad and parks its unused section. It is not an expanded 50-channel schematic or a routed PCB.

Fifty selected relay coils consume about 1.06 A nominal, approximately 1.23 A at +5% voltage and -10% coil resistance. Provide a separate regulated 5 V coil supply, at least 1.5 A with appropriate practical margin. Do not put this load on the existing LT1763 analog regulator. Thirty-eight amplifier packages consume about 152 mA typical before external loads; the datasheet maximum over temperature gives 228 mA. Check regulator temperature and supply distribution before expansion.

## Parts and layout decisions still needing qualification

- C1 has a concrete polypropylene candidate, WIMA `MKP4D051006G00JSSD`, 10 uF, 100 V, 5%, with a 31.5 x 17 x 29 mm body and 27.5 mm lead pitch. This is physically large and is a prototype choice, not an availability or purchasing approval. Its assigned stock footprint contains `MKS4` in the name because that outline matches; the required dielectric remains **MKP polypropylene**, not MKS polyester.
- R1-R6 should be low-noise thin-film parts. The 10 Mohm feedback path requires a clean, dry board and short wiring. Solder flux leakage, capacitor dielectric absorption, relay thermal gradients and low-frequency amplifier noise can matter at microvolt levels.
- C2-C6 require 1% C0G/NP0 or selected polypropylene parts. Their generic 0805 footprints are preliminary; confirm supplier availability and change footprints for the actual chosen parts. Do not substitute X7R/Y5V in this signal path. Supply decouplers C10-C13 may use X7R.
- SW1 is an externally wired panel/bench SPST switch, so no PCB footprint is assigned. Add a 2.54 mm shunt for JP1 and suitable connector mates/wire to the assembly kit; these mechanical accessories are not represented as separate electrical parts in the BOM.
- Relay contact resistance is small in the linear calculation, but switching microvolt signals and contact reliability at very low current require bench evidence. Keep coil heat and return currents away from the detector and feedback network.

## Verification status

KiCad 10.0.6 reads all sheets and exports the netlist and PDF. ERC reports **zero violations, zero ignored checks and zero exclusions**. The validation script passes 31 connectivity/numerical checks, solves the exported circuit in both relay states, compares it to the stated transfer function, and evaluates 4,096 component endpoint combinations. All three PDF pages were visually inspected.

This establishes schematic connectivity and the stated linear response. It does not establish measured input noise, overload recovery, settling, dielectric absorption, cable/ADC stability, contact reliability or production calibration. No PCB, DRC, fabrication output or changes to acquisition software are included.

Your reported 2.91 uV RMS DAQ floor in the **3 Hz/Q=3 band** would contribute about 0.029 uV input-referred after 100X gain if that DAQ floor remains unchanged. That is only the ADC contribution, not the new total floor and not a prediction for the entire 0-100 Hz band. Amplifier, resistor and other circuit noise must be measured using the same judged band. Use the bench sequence in `BUILD_AND_BRINGUP.md` before reproducing the channel 50 times.

## Regenerate

The native schematic is directly editable in KiCad. `tools/build_schematic.py` records the initial design; running it again overwrites the generated schematic, so preserve later manual edits separately. `tools/validate.py` exports and checks the current native files. It requires Python with NumPy/Matplotlib and KiCad 10; the current scripts use the installed Windows KiCad 10 path. Manufacturer PDFs in `references` are reference material, not design instructions.
