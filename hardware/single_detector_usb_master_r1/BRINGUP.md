# Engineering acceptance procedure

This procedure is for the separate USB-master R1 prototype. It records the evidence needed after CAD checks; no listed physical test has yet been performed. Use the final schematic, BOM and board revision matching the validation hashes.

## Before module insertion

Confirm the installed jack polarity and exact socket pin numbering against the retained native interface drawing. ESP32 and ADC modules are customer inserted; the two jacks are customer soldered. All other purchased components, including socket strips and the emitter trimmer, belong in the assembly BOM.

Inspect the received board for polarity and pin1 on protection devices, regulators, analog switches, optocouplers and polarized capacitors. Confirm continuity of GND and the separate EM_GND domain; there must be no accidental conductive bridge between them. Confirm neither battery positive reaches ESP VIN/USB5V or ESP3V3.

Initially use current-limited bench supplies at the confirmed nominal6.4V and keep the emitters/detectors disconnected. The pack's actual full-charge voltage and BMS limits must be documented before pack-endpoint qualification. Do not assume a6.4V label is the maximum voltage.

## Required measurements

| Check | Evidence to record | Acceptance basis |
|---|---|---|
| USB cable behavior | Exact model, continuity/data enumeration with switchON/OFF | OFF removes downstream5V; ON supports ESP serial data. A charging-only cable cannot run the app |
| ESP power path | USB5V, moduleVIN andESP3V3 with both batteries present/absent | No battery-derived power on an unplugged or switched-off USB domain |
| Held ADC supply | Startup/steady/removal waveforms at carrier header and actual moduleAVDD | Datasheet operating/absolute limits and the final power-corner report; header voltage alone is insufficient |
| ADC load-release overshoot | Highest actual AVDD peak, including PDWN/DRDY transitions, battery steps and instrument uncertainty | Remain within the 5.25 V conversion operating ceiling. The nominal setpoint has 70.2 mV headroom, but the stacked static corner leaves only 4.64 mV. Undervoltage firmware cannot detect overvoltage excursions |
| Module input filter | Installed module revision, L1DCR, AVDD capacitance and full-load header-to-AVDD drop | Agreement with seller10uH/22uF+1uF+100nF circuit and the power-stage voltage-drop budget |
| Startup sequence | USB5V, ESP3V3, held5V, ADC_READY_RAW, ISOLATE_H, PDWN and both switched battery rails | ADC/interface power and qualification precede live sensor signals and emitter permission |
| Normal shutdown | Same rails plus highest ADC input, starting at maximum validated operating load | Signals disconnect and ADC inputs discharge before the relevant supply limits are crossed |
| Abrupt battery removal | Detector battery removed while USB remains; repeat emitter battery independently | No accepted faulted capture or battery/USB backfeed; actual module input stays within absolute ratings |
| Rapid cycling | Short and long USBOFF intervals, including incomplete reservoir discharge | No stuck permission, false-ready pulse or failed restart requiring unplugging all supplies |
| Battery depletion and recovery | Slowly reduce detector supply through dropout while recording readiness, switched rails and app status | Hardware may retry/cycle near dropout. Every interrupted measurement must fail, and emission must remain stopped until explicitly restarted after stable power returns |
| Faulty detector output | Controlled source through the intended harness/input resistance, within the validated battery range | Input current and held-rail behavior agree with analysis; out-of-range readings fail rather than becoming valid offsets |
| Emitter regulator | Actual emitter voltage/current at commissioned trim, cold/warm duty-cycle load | Match the existing validated emitter drive; thermal/current limits include cold resistance and simultaneous channels |
| Measurement correlation | Known inputs and real reference detector, each app's front end and duty/frequency | Quantify gain, DC offset, noise and saturation changes; do not reuse old calibration without comparison |

The holder, wire exit, USB plug access, module underside clearance and antenna region must also be checked in the actual enclosure. The preliminary86x145mm outline is not a measured enclosure-fit approval. No mounting-hole locations are inferred from the photos.

## Firmware and app checks

Use the separate R1 sketch after confirming the actual ESP32 flash configuration. Record the firmware hash and returned v3.3 identity. Electrical shutdown is hardware controlled.

1. Turn the USB switch on with both batteries already connected. Each app must connect and complete a valid test without unplugging either battery.
2. Repeat with a missing battery, then insert it while USB remains on. Live commands must fail while unavailable, and ADC initialization must recover after qualified power returns. PWM/capture must remain stopped until requested again.
3. Interrupt power during the last OFFSET sample/delay, REF channel restoration, front-end register readback, streamed SPI read and immediately before STREAM,STOP. Each interrupted operation must report an error instead of a valid result.
4. Apply a brief detectable status fault pulse that ends before its ISR is serviced. The sticky fault event must still invalidate the affected capture.
5. Select FE,V19, interrupt and restore carrier power, then verify the selected gain/buffer settings and fresh calibration before a new capture. Repeat FE,V20.
6. End a valid capture, then interrupt power after its software stop boundary. Preserve the completed valid capture and reject subsequent live operations until reinitialization succeeds.

Record raw scope captures and app logs alongside date, test setup, supply ranges, load, temperature and instrument configuration. CAD reports, firmware compilation and mocked host-protocol checks do not replace these measurements.
