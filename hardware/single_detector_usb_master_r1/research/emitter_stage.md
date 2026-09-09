# Isolated emitter stage — engineering rationale and pin audit

Status: implemented in `tools/emitter_stage.py`, function `add_emitter_stage(c)`. This adds 24 populated parts on two native schematic sheets. Component pin sets and explicit nets were compared with an independent KiCad 10.0.6 XML export. The schematic exported to PDF successfully and both sheets were visually inspected. The stage has no PCB placement or routing yet and makes no fabrication-release claim.

The parent task confirms the two batteries are HeyFuture 6.4 V, 6 Ah LiFePO4 packs, and the detector rail should retain its existing battery voltage. This stage changes **only the isolated emitter circuit**. A provisional 7.3 V fully charged input is used for calculations; the actual pack/charger maximum belongs in the overall power specification. The user supplied 200 mA maximum emitter current without explicitly distinguishing per-channel from aggregate. Accordingly, the three switch channels accommodate 200 mA each electrically; the regulator has a separate thermal envelope.

## Interfaces and ownership

| Net | Meaning / owner |
|---|---|
| `EM_BAT_SW` | Emitter battery positive after the isolated master switch, owned by `power_architecture` |
| `EM_GND` | Emitter battery return, optically isolated from USB/ADC ground |
| `GND` | USB/ESP32 side of the optical barrier only |
| `EM_LED_RETURN` | Common cathode return of the three PWM opto LEDs; the power stage sinks this to GND only while `MASTER_PERMIT` is valid |
| `ESP_EMIT1`, `ESP_EMIT2`, `ESP_EMIT3` | Retained GPIO33, GPIO25, GPIO26 commands |
| `EM_REG` | Common regulated positive for all three heaters |
| `EMIT1_LOW`, `EMIT2_LOW`, `EMIT3_LOW` | Individual switched heater returns, routed to the physical emitter connectors by the connector stage |

The power stage owns both positive battery master switching and the logic-side LED-return sink. The latter prevents an asserted GPIO from continuing to illuminate the optocoupler during ADC brownout or invalid power. Do not join `EM_GND` to `GND`, including via internal copper planes, scope clips or the emitter case. The source design's emitter optical isolation is retained.

## Regulator decision

U100 is Analog Devices **LT1963AEQ#PBF**, adjustable, five-lead Q/DD/TO-263 package. The manufacturer documents a 1.5 A regulator, 1.21 V adjustment reference, regulation conditions from 1 mA, reverse-current/battery protection and no required external protection diode. Its guaranteed dropout is 0.35 V at 500 mA and 0.55 V at 1.5 A over temperature; do not interpolate those into a guaranteed 600 mA specification. [ADI data sheet, pp. 2, 5–6, 13–20](https://www.analog.com/media/en/technical-documentation/data-sheets/lt1963a.pdf).

The independent original-netlist audit found D1 cathode connected only to C5, not to regulator IN. C100/C101 now connect directly from IN to `EM_GND`. The orphan diode is removed. The original AMS1117's load/capacitor concerns are also removed: the divider always loads the new regulator above 5.5 mA, and a specified tantalum output capacitor supplies a known capacitance and ESR. C102 is 22 µF ±10%, with maximum 0.2 Ω ESR. This fits ADI's ≥10 µF, ≤3 Ω output-capacitor requirements. The 100 nF bypass is additional; it is not relied upon for low-frequency stability. There is no ADJ capacitor and no external output-to-input diode. [ADI stability discussion, pp. 14–17](https://www.analog.com/media/en/technical-documentation/data-sheets/lt1963a.pdf).

R100 = 210 Ω from ADJ to return. R101 = 7.5 Ω in series with a Bourns 1 kΩ trimmer in the OUT-to-ADJ arm:

`Vset = 1.21 × [1 + (7.5 + Rtrim)/210] + Iadj × (7.5 + Rtrim)`

Using nominal `Iadj = 3 µA`, this gives **1.2532 to 7.0186 V setpoint**. The actual output cannot exceed what the battery and dropout allow. The original AMS1117 circuit's nominal electrical setpoint was about 1.25–6.99 V, so the broad adjustment capability is preserved instead of silently imposing a 4 V minimum. It is not the same potentiometer transfer law or physical screwdriver setting as the old circuit. The fixed resistors are not an independent overvoltage clamp. Trimmer ±10% and reference/resistor tolerance can demand a setpoint above the battery; a fully charged pack can now support more output voltage than the old higher-dropout regulator.

**Commissioning means transferring the established, measured emitter voltage with a voltmeter before attaching heaters.** Do not set the trimmer to its end stop. The data sheet's 6.7 ±0.4 V value is a temperature operating point, not a universal instruction to apply that voltage at any duty cycle. Changing the heater setting or drive waveform changes optical output and can invalidate existing calibration.

RV100 pins 1 and 2 (wiper) are strapped together on `EM_REG`; pin 3 goes toward R101/ADJ. An open wiper contact therefore leaves the full resistive track, rather than opening the feedback arm. A broken track or solder joint is still a fault. Clockwise motion moves the wiper toward terminal 3 and **decreases** output. [Bourns 3296 wiring diagram and ordering code](https://www.bourns.com/docs/product-datasheets/3296.pdf).

## Thermal envelope — separate nominal heater behavior from arbitrary loads

The IR-5x/IR56 manufacturer document specifies approximately **50 Ω in the hot state**, with these typical operating points: 4.0 V/80 mA, 5.5 V/110 mA and 6.7 V/134 mA. Its operating examples assume a 10 Hz rectangular waveform at 50% duty, ambient conditions and no radiator. Higher duty or continuous drive requires lower power for the same temperature. The source does **not** provide a guaranteed cold-resistance minimum. [HawkEye manufacturer document hosted by Boston Electronics, pp. 2, 4, 10](https://www.boselec.com/wp-content/uploads/Linear/IRSources/IRSourcesLiterature/IR5x-BEC.pdf).

For three simultaneously energized nominal 50 Ω heaters, the regulator's pass-device heating is:

`Ppass = (Vin − Vout) × 3 Vout / 50`

At 7.3 V input its maximum is approximately **0.799 W at Vout = 3.65 V**. This excludes regulator ground-pin dissipation, divider current and other heat sources. At the published operating points, pass-device heating is approximately 0.792 W at 4 V, 0.594 W at 5.5 V, and 0.241 W at 6.7 V, considering all three channels continuously energized at their nominal on-state current. At 50% duty the average heater-related pass loss is lower. This calculation explains why the real resistive heater load is more favorable than an arbitrary 600 mA constant-current load at the minimum trim setting; it does not establish cold-start performance.

For an arbitrary load, the correct relationship is:

`Ptotal = (Vin − Vout) × Iout + Vin × Ignd`

`Tjunction = Tambient + effective_thetaJA × Ptotal`

The ground-current term matters: this is not a zero-ground-current MOS LDO. Reserve and then measure it. The ADI table gives 25 mA maximum at 500 mA and 120 mA maximum at 1.5 A, but does not specify a 600 mA maximum. A useful **conditional design illustration**, not a published guarantee, is 40 °C ambient, a target junction ≤110 °C, measured effective θJA ≤40 °C/W, and a 0.4 W allowance for ground current and nearby heat. That leaves 1.35 W for pass loss:

| Output setting | Approximate maximum average output current under that conditional envelope | 600 mA continuous arbitrary-load result |
|---|---:|---|
| 1.253 V | 223 mA | Fails this envelope: ~3.63 W pass loss alone |
| 4.0 V | 409 mA | Fails this envelope: ~1.98 W pass loss alone |
| 5.0 V | 587 mA | Slightly above this envelope |
| 5.5 V | 750 mA, capped here at 600 mA | Fits the illustrative heat budget |
| 6.0 V | 1.04 A, capped here at 600 mA | Fits heat budget only if battery headroom is sufficient |

This is a checkable engineering envelope, not a hard current-limit setting. Include approximately 6 mA divider current in `Iout`. Do not advertise 600 mA continuous at every trim position. The silicon's 1.5 A rating and thermal shutdown do not qualify the PCB thermally.

ADI's Q-package table reports 23/25/33 °C/W on specified 2500 mm² boards with varying top copper and 2500 mm² back copper. Those are reference measurements, not the thermal resistance of this PCB. A four-layer power bay with broad isolated `EM_GND` copper, direct tab connection and thermal vias is a plausible starting point; final copper area, nearby sources, airflow and enclosure must be assessed. Keep the tab away from sensor buffers/ADC and do not bridge the isolation boundary to obtain more copper area. [ADI thermal layout data, pp. 18–19](https://www.analog.com/media/en/technical-documentation/data-sheets/lt1963a.pdf).

Bench qualification must record cold-start peak current, hot on-state current, worst permitted simultaneous duty, voltage droop/ringing, and equilibrium regulator/package temperature at maximum charged battery voltage in the final enclosure. If the actual load requires 600 mA continuously at low settings, enlarge the thermal solution or change regulator architecture rather than claiming that this layout already supports it.

## PWM changes and default-off behavior

All three original command mappings and optical isolation are retained. Each LED has a 240 Ω series resistor and the GPIO has a 100 kΩ pull-down. Nominal LED current is `(3.3−1.2)/240 = 8.75 mA`. At 2.64 V GPIO high, 1.4 V LED forward voltage, 1% high resistor and a few millivolts return-sink loss, current remains approximately 5 mA. Three simultaneously active LEDs therefore draw roughly 26 mA nominal; the power-stage return sink must accommodate all three.

The opto collectors connect to `EM_BAT_SW`, so MOSFET gate drive does not collapse when the adjustable heater voltage is reduced. The 100 Ω series gate resistor is followed by a **10 kΩ gate-to-source pull-down placed at the MOSFET**, providing a stronger off discharge than the original 100 kΩ. At 7.3 V, steady gate-drive loading is approximately 0.72 mA. Lite-On specifies 50% minimum CTR at IF=5 mA and VCE=5 V at 25 °C. CTR, saturation and switching waveforms must still be checked over actual current/temperature/load conditions; do not substitute an unqualified optocoupler solely by package. [Lite-On electrical table, Rev. S p. 9](https://optoelectronics.liteon.com/upload/download/DS-70-96-0016/LTV-8X7%20series%20%20Rev.S.PDF).

AO3400A is retained as the PWM device. Its manufacturer specifies maximum RDS(on) at 2.5 V and 4.5 V gate drive; the gate threshold is not used as an on-state design voltage. At 200 mA with 48 mΩ maximum at 2.5 V, room-temperature conduction loss is only 1.92 mW. The full ±12 V gate limit remains comfortably above the intended 2-cell pack rail, subject to switching transients. These are resistive heaters, so no inductive flyback diode is inserted across them. [AOS AO3400A data sheet](https://www.aosmd.com/res/data_sheets/AO3400A.pdf).

The fourth channel of U101 has all four pins NC. `EM_LED_RETURN` never connects to `EM_GND`. USB-off behavior is provided by both the optical-drive sink opening and the separate positive battery master opening. C100/C102 can retain energy briefly after turnoff; their discharge timing is distinct from ADC input-protection timing.

## Exact pin maps

| Part | Pin | Connection |
|---|---|---|
| U100 LT1963AEQ | 1 SHDN (active low) | EM_BAT_SW |
| | 2 IN | EM_BAT_SW |
| | 3 GND and exposed tab | EM_GND |
| | 4 OUT | EM_REG |
| | 5 ADJ | EM_ADJ |
| RV100 3296W | 1 CCW and 2 wiper | Both EM_REG |
| | 3 CW | EM_TRIM_TOP → R101 → EM_ADJ |
| U101 LTV-847S channel1 | 1 A, 2 K, 16 C, 15 E | EM_LED1_A, EM_LED_RETURN, EM_BAT_SW, EM_GATE1_DRIVE |
| U101 channel2 | 3 A, 4 K, 14 C, 13 E | EM_LED2_A, EM_LED_RETURN, EM_BAT_SW, EM_GATE2_DRIVE |
| U101 channel3 | 5 A, 6 K, 12 C, 11 E | EM_LED3_A, EM_LED_RETURN, EM_BAT_SW, EM_GATE3_DRIVE |
| U101 channel4 | 7 A, 8 K, 10 C, 9 E | All NC |
| Q101/Q102/Q103 AO3400A | 1 G | EM_GATE1/2/3 respectively |
| | 2 S | EM_GND |
| | 3 D | EMIT1_LOW/2_LOW/3_LOW respectively |
| C100 tantalum | 1 positive, 2 negative | EM_BAT_SW, EM_GND |
| C102 tantalum | 1 positive, 2 negative | EM_REG, EM_GND |

The tantalum body polarity band marks **positive/anode**. It is not the negative band convention of an aluminum electrolytic.

The stock `TO-263-5_TabPin3` footprint has two copper pads numbered 3: the lead and the thermal tab; both must be on `EM_GND`. Its lead pitch is 1.70 mm. The stock `SMDIP-16_W9.53mm` footprint matches the original U4 footprint. The Bourns footprint pads are 1 at (0,0), 2 at (−2.54,0), 3 at (−5.08,0), with 0.8 mm drills. The TPS case D body is 7.3 ×4.3 mm, maximum height3.1 mm, represented by the standard EIA7343-31 footprint. Package names and complete numbered pad sets were checked against the installed KiCad libraries.

The IR56 mechanical drawing shows **two power leads and a separate case-ground lead**. The existing carrier only has two heater wire connections. Do not treat the case lead as one of the heater power leads; retain/document actual fixture case wiring. [IR56 drawing, p. 10](https://www.boselec.com/wp-content/uploads/Linear/IRSources/IRSourcesLiterature/IR5x-BEC.pdf).

## Exact proposed BOM and sourcing evidence

| References | Qty | Manufacturer / exact MPN | Package / source status |
|---|---:|---|---|
| U100 | 1 | Analog Devices LT1963AEQ#PBF | TO-263-5 tab3; [LCSC C20415348](https://www.lcsc.com/product-detail/C20415348.html) identified, JLC orderable match not yet verified |
| U101 | 1 | Lite-On LTV-847S | SMDIP16; [JLC C114599](https://jlcpcb.com/partdetail/LiteOn-LTV847S/C114599); same MPN as original BOM |
| Q101–Q103 | 3 | Alpha & Omega AO3400A | SOT23; [JLC C20917](https://jlcpcb.com/partdetail/Alpha_OmegaSemicon-AO3400A/C20917), Basic SMT |
| RV100 | 1 | Bourns 3296W-1-102LF | 1k ±10%,25-turn,0.5W THT; [JLC C57089](https://jlcpcb.com/partdetail/Bourns-3296W_1102LF/C57089), Extended, must be populated |
| C100,C102 | 2 | Kyocera AVX TPSD226K025R0200 | D7343-31,22µF25V10%,200mΩ; [JLC C284839](https://jlcpcb.com/partdetail/KyoceraAVX-TPSD226K025R0200/C284839), Extended SMT Economic/Standard entry; manufacturer shown as “--”, quantity not exposed in opened page; LCSC cached listing showed zero stock |
| C101,C103 | 2 | Samsung CL21B104KBCNNNC | 0805,100nF50VX7R10%; [manufacturer current mass-production entry](https://product.samsungsem.com/mlcc/CL21B104KBCNNN.do); exact JLC mapping still to verify |
| R100 | 1 | Yageo RC0805FR-07210RL | 210Ω,1%,0.125W; [exact manufacturer sheet](https://yageogroup.com/component-documentation/download/specsheet/RC0805FR-07210RL) |
| R101 | 1 | Yageo RC0805FR-077R5L | 7.5Ω,1%,0.125W |
| R110,R115,R120 | 3 | Yageo RC0805FR-07240RL | 240Ω,1%,0.125W |
| R111,R116,R121 | 3 | Yageo RC0805FR-07100KL | 100kΩ,1%,0.125W |
| R112,R117,R122 | 3 | Yageo RC0805FR-07100RL | 100Ω,1%,0.125W |
| R113,R118,R123 | 3 | Yageo RC0805FR-0710KL | 10kΩ,1%,0.125W |

The regulator and capacitor source availability remains a concrete BOM completion task; existing web entries are not a purchase or allocation. Do not silently substitute a different LT1963A package, fixed-voltage suffix, unidentified capacitor manufacturer, or a 22µF MLCC with unverified biased capacitance. The exact TPSD226K025R0200 rating is present in the manufacturer's TPS table on PDF page9, printed page72; selected row visually inspected. [TPS manufacturer data sheet](https://datasheets.kyocera-avx.com/TPS.pdf).

Input capacitance from this stage is at most 24.31 µF nominal-tolerance maximum (22µF×1.1 +100nF×1.1); output is the same. Initial charge demand therefore includes up to approximately49µF through the battery switch/regulator, plus the heater startup load. ESR does not itself provide a guaranteed inrush limit. The master-stage designer was given this bound and a provisional ≥650mA emitter input requirement including LDO ground current. If C values are changed for sourcing, repeat the master-switch startup/SOA check.

## Verification record and remaining physical work

The independent export was generated in `tmp/emitter_stage_check`, separate from the main project and original files. Every declared connected pin matched KiCad's emitted XML net name. All24 physical parts have exactly the intended numbered pad sets in the selected stock footprints. PDF export passed and both sheet layouts were inspected before the final cathode-return net update; that update changes only labels/connectivity and is included in the final repeated net comparison.

No complete-board ERC/DRC result is asserted here. A standalone emitter sheet intentionally lacks its input power source, permit-return sink and external heater connectors. Main-project integration must connect those interfaces, place/rout copper and vias, preserve isolation, include the populated trimmer in BOM/CPL, and perform startup/thermal/PWM verification.

Local heater evidence: `C:/Users/JoseCastelblanco/Documents/Eltec_TestRig_Documents/IR5x-BEC.pdf`, SHA-256 `F501F1E08D13B717A4D226E3D0DBB9B626F6EAE8AECA7F48FF4A45C7669DFF1F`; 12 pages, pages2/4/10 visually inspected. A retrieved copy of the primary TPS manufacturer PDF is stored as `research/tps_manufacturer.pdf`. Lite-On's manufacturer PDF endpoints intermittently reject direct downloads; the electrical table was accessible from the primary-domain indexed result and the channel/pad mapping was also independently checked against the original native U4 symbol/footprint.
