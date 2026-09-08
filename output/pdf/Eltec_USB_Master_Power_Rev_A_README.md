# Eltec USB master power retrofit - Rev A

The six-page PDF in this directory is a **prototype schematic**, based on two 6.5 V batteries, a maximum emitter current of 200 mA, the user's existing PCB schematic, a socketed ADS1256 module and the RIITOP B0FCS47Y8Q switched USB-C extension. It has not been built or bench-tested.

## What to build

Both sections are required for the proposed complete retrofit:

1. **Battery switch board:** the two existing IRLB8748 N-channel MOSFETs switch the battery positives. A VO1263AB photovoltaic driver provides floating gate drive. Battery returns remain continuous and retain their existing PCB connections. The emitter is J2 and the detector supply is J1 in the supplied PCB schematic.
2. **ADC socket adapter:** four ADG4613BRUZ chips intercept all eight AIN contacts. A TPS3808G01 supervisor and a 2N3904 select either the measurement signals or ground as the ADC supply rises/falls. All switch paths open when the switch ICs are unpowered. This is what prevents the battery shutdown delay from leaving the ADC directly driven by the sensors.

The protection ICs require four TSSOP-16-to-DIP adapters and one SOT-23-6 adapter for use on perfboard. Buy the packages named in the PDF; adapter pad numbers must correspond to the IC pin numbers.

## Critical pin assignments

- IRLB8748, front marking toward you and leads down: **1 gate, 2 drain, 3 source**. The tab is drain. Drain goes to fused battery positive, source to the PCB load positive. Do not join the two tabs or the two battery positives.
- VO1263AB channel 1: **2 LED anode, 1 LED cathode, 7 PV positive, 8 PV negative**. Channel 2: **4 LED anode, 3 LED cathode, 5 PV positive, 6 PV negative**. PV positive goes to the respective gate; PV negative goes to that MOSFET's source, not ground.
- TPS3808G01DBVR: **1 RESET, 2 GND, 3 MR, 4 CT, 5 SENSE, 6 VDD**. R7/R8 = 100k/10k, both 0.1%, give a nominal 4.455 V falling threshold. R29 connects CT to V5 through 100k for the nominal 300 ms release delay.
- 2N3904: use the onsemi TO-92 pin assignment **1 emitter, 2 base, 3 collector**. Recheck any substitute's datasheet.
- Every ADG4613BRUZ: **13 VDD; 4 and 5 GND; 12 unconnected; 1, 8, 9, 16 ISOLATE_H**. HIGH connects S1/D1 and S4/D4 to the grounding paths; LOW connects S2/D2 and S3/D3 to the sensor paths. ADG4612 is not a substitute.

The PDF contains the per-channel wiring table and the complete parts list.

## Before connecting the assembled retrofit to the rig

Follow sheet 6. In particular, confirm the ADC module's real AVDD connection, header orientation, grounded AINCOM and input capacitance. The generic module symbol does not establish the physical header layout or its internal power circuit. Do not use the header's 5V net as AVDD without checking. If AVDD is on a different internal net, adapt the supply/sense connections before proceeding.

This draft uses a conservative normal signal range of **0-4.0 V**, a maximum discharge load of **500 mA on V5**, and no more than **1 uF ADC-side capacitance per analog input**. These limits are design assumptions to verify, not measurements of the user's rig. C1 is a starting value of 100 uF; the actual shutdown waveform and USB startup inrush must be checked. Never increase C1 arbitrarily to cure a failure without considering USB startup current.

The 33k pull-downs and analog-switch resistance alter loading. For example, a simple 500-ohm source feeding 33k would lose about 1.5% before accounting for the existing ADC load and feedback topology. That is an illustration, not a predicted correction for this PCB. Compare offset, sensitivity, reference readings and noise, then recalibrate before technician production use. The legacy AIN7 divider is also loaded; its existing disabled battery monitoring must not be re-enabled without separate validation.

The battery paths are single-MOSFET DC switches: they do not provide reverse-current blocking from another powered source or reverse-polarity protection. The ADC adapter is power-sequence protection, not an overvoltage clamp for excessive signals during normal operation.

## Manufacturer references checked

- [RIITOP switched extension](https://www.riitop.com/products/riitop-usb-type-c-extension-cable-with-on-off-switch-1ft-usb-3-1-type-c-male-to-female-extension-cable-with-on-off-switch-support-video-data-pd-charging) - advertised data and power support; verify device-side 5V actually turns off with the switch.
- [Infineon IRLB8748](https://www.infineon.com/part/IRLB8748)
- [Vishay VO1263AB datasheet](https://www.vishay.com/doc/?84639)
- [TI TPS3808 datasheet](https://www.ti.com/lit/ds/symlink/tps3808.pdf)
- [Analog Devices ADG4612/ADG4613 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/adg4612_4613.pdf)
- [onsemi 2N3904 datasheet](https://www.onsemi.com/download/data-sheet/pdf/2n3903-d.pdf)
- [Vishay BZX55 zeners](https://www.vishay.com/docs/85604/bzx55.pdf)
- [TI ADS1256 datasheet](https://www.ti.com/lit/ds/symlink/ads1256.pdf)

Validation performed here: manufacturer pin/function review, schematic connection review, arithmetic checks and rendering/visual inspection of the PDF. No SPICE simulation, physical measurements or hardware qualification has been performed. Existing application and firmware files were not edited.
