# Independent power and analog review

2026-09-09. Review of the integrated generator before routing. This file records engineering decisions and arithmetic; it is not a release certificate. No changes were made to the original project or to another agent's generator during this review.

## Corrections required before netlist freeze

1. Replace TPS7A4901 plus the OUT-to-IN SS14 bypass. A Schottky's advertised forward voltage does not establish compliance with the regulator's OUT-minus-IN absolute limit of +0.3 V. Prefer the explicitly reverse-protected LT3041 described below.
2. Replace the 4.6 V ADC threshold. A threshold tolerance calculation must distinguish initial trip, hysteresis, detection overdrive, and propagation delay. A 4.6 V nominal detector cannot claim to stop operation before an ADS1256's 4.75 V operating floor.
3. Replace 2N7002 switches whose proof relies on strong conduction at the lowest logic supply. Their threshold specification is not an on-resistance guarantee. Use transistors with guaranteed behavior at the actual drive voltage, or a qualified logic/POR circuit. The power-stage owner is resolving the full startup truth table.
4. Move the signal/ground selectors after the opamps. Keep their switches and the opamps on the held ADC rail. Keep the detector supplies on the original battery voltage. Use the compensation network below to avoid introducing the otherwise approximately 1.5% extra normal-mode attenuation.
5. For the changed opamp supply, OPA2325IDR is an actual zero-crossover candidate. OPA2192 and OPA2197 are not zero-crossover substitutes.

## Regulator: LT3041ADE#TRPBF

The [April 2026 LT3041 Rev. A datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/lt3041.pdf), Tables 1–3, specifies full-temperature SET current 99–101 µA, output offset ±2 mV, and 460 mV maximum dropout at 100 mA. Input-to-output and input-to-enable differential ratings are ±22 V. Reverse-battery and reverse-output protection remove the SS14 clamp problem. At IN=0 V, OUT=5 V, SET open, reverse-output current is 16 µA typical, 1 mA maximum at 25°C. The actual SET-connected topology has an additional limit: page30 allows up to15mA OUT-to-GND current through output-overshoot recovery when IN>2.5V, plus SET current. Budget this during hold-up; the SET-open leakage specification is insufficient. The device is a 14-lead DFN, 4×3 mm, with grounded exposed pad.

| Pin | Signal | Candidate connection |
|---|---|---|
| 1, 2, 3 | IN | Protected detector battery |
| 4 | VIOC | Float when unused, confirmed page27 |
| 5 | EN/UV | Qualified master regulator enable |
| 6 | PG | Unused if a separate supervisor is fitted |
| 7 | ILIM | Current-programming resistor to GND |
| 8 | PGFB | Protected IN to disable fast startup; direct raw reverse input would require the datasheet's diode arrangement |
| 9 | SET | Precision resistor chain and bypass capacitor to GND; final chain51.1kΩ+698Ω, see revision below |
| 10, 11, 15 | GND / EP | GND, with thermal copper |
| 12 | OUTS | Kelvin sense of the local held output |
| 13, 14 | OUT | ADC_5V_HELD |

Use two parallel 10 µF output ceramics; each requires ESR below 20 mΩ and ESL below 2 nH. Reservoir electrolytics supplement these capacitors. SET bypass controls startup. PGFB is not a replacement for a precision ADC operating-range supervisor.

Independent voltage calculation, using 51.5 kΩ ±0.1%, initially ignoring resistor temperature coefficient:

```
Vmin = 99 µA × 51.5 kΩ × 0.999 − 2 mV = 5.0914015 V
Vmax = 101 µA × 51.5 kΩ × 1.001 + 2 mV = 5.2087015 V
```

For an independently bounded ±25 ppm/°C resistor over −40…85°C, the largest departure from 25°C is 65°C. A conservative combined resistance tolerance is 0.1% + 0.1625% = 0.2625%. This gives approximately 5.08312…5.21715 V. A 5.15 V nominal label is therefore reasonable; a ±0.5% label is not. Preserve margin for load transients and board leakage. At maximum regulated output and 100 mA, input must exceed approximately 5.677 V to cover the specified dropout, before upstream losses. Thus the nominal 6.4 V battery works with headroom, but its low-voltage endpoint must be addressed by the supervisor rather than presumed regulated indefinitely.

**Final setpoint revision in the inspected generator:** R10=51.1kΩ and R39=698Ω in series, each0.1%/10ppm/K, giving51.798kΩ and nominal5.1798V. Using even the larger25→125°C resistor departure gives tolerance0.2% and output5.115745996…5.244061196V. The earlier51.5kΩ analysis is historical. This change gives approximately59.7mV of comparator input underdrive at the minimum rail and maximum AIM-reference trip, satisfying the previously missing50mV prior-state condition. The static upper-rail margin to5.25V is only5.938804mV: SET leakage and positive load transients must be included explicitly. The full ISET/VOS specifications already include their declared input/output/load ranges; do not add the separate line/load terms a second time. At100mA the corresponding dropout headroom requires at least5.7041V at IN before upstream losses.

The official distributor listing is [LCSC C7452883](https://lcsc.com/product-detail/Voltage-Regulators-Linear-Low-Drop-Out-LDO-Regulators_Analog-Devices-LT3041ADE-TRPBF_C7452883.html), matching LT3041ADE#TRPBF. This establishes a catalog identity, not live JLC allocation or stock reservation.

The stock KiCad footprint `Package_DFN_QFN:DFN-14-1EP_3x4mm_P0.5mm_EP1.7x3.3mm` cites the same DE package drawing05-08-1708 as the manufacturer. Independently inspected pads: pin1 at(−1.45,−1.50), pin7 at(−1.45,+1.50), pin8 at(+1.45,+1.50), pin14 at(+1.45,−1.50), and grounded pin15 exposed pad1.7×3.3mm. It matches the required package; routing still needs the prescribed local capacitors and thermal copper.

### Why LT3042 is the fallback

[LT3042 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/3042fb.pdf): full-temperature SET current is 98–102 µA; the familiar ±1% figure is initial accuracy. At 51.5 kΩ ±0.1%, its upper output can reach 5.26025 V including +2 mV offset, above 5.25 V. A 51.0 kΩ set resistor instead gives 4.9910…5.2092 V before resistor temperature drift. Maximum dropout is specified through 50 mA, not at the provisional 100 mA total load. Its 200 mA typical-dropout figure is not a guaranteed limit. LT3041 avoids both weaknesses. LT3042's stronger reverse-leakage limit is useful, but does not compensate for the tighter supervisor window.

## Supervisor review: do not confuse timing columns or overdrive

The power-stage owner is converging the replacement. These independently checked exclusions prevent recurring errors:

| Device family | Actual limitation for this design |
|---|---|
| [TPS3840](https://www.ti.com/lit/ds/symlink/tps3840.pdf) | Existing DL46 trip is 4.531…4.669 V. Its specified maximum delay has explicit input-slew/overdrive conditions. Increasing the nominal threshold must also leave room for its substantial hysteresis. |
| [TPS3842](https://www.ti.com/lit/ds/symlink/tps3842.pdf) | Threshold error is ±1.5% over the specified conditions; the headline 0.5% is typical. The 1% hysteresis option extends to 1.5%. The familiar 7/9 µs detection values are nominal, not maximum. Table was visually checked. |
| [TPS3899](https://www.ti.com/lit/ds/symlink/tps3899.pdf) | Fixed thresholds are ±2% and the adjustable threshold ±2.5%; hysteresis spans several percent. It does not solve the narrow operating window. |
| [TPS3702](https://www.ti.com/lit/ds/symlink/tps3702.pdf) | Do not invent an AX50 ordering code: the 5 V option in the released table is CX50. A divider with another released variant requires a complete input-current budget. |
| [TPS3703](https://www.ti.com/lit/ds/symlink/tps3703.pdf) | Candidate accuracy is attractive, but the 30 µs maximum timing condition uses 5% overdrive. A nominal 4.97 V trip reaches that timing condition only at approximately 4.7215 V, already below 4.75 V before module losses. |

A precision reference plus a comparator with a guaranteed maximum delay at small overdrive is a concrete alternative when these supervisor windows cannot fit. [TLV3501](https://www.ti.com/lit/ds/symlink/tlv3501.pdf) specifies a 12 ns full-temperature maximum with 5 mV overdrive, unlike typical-only microsecond candidates. Reference tolerance, comparator offset/hysteresis, pull-up/logic behavior, and startup qualification still need to be included. This suggestion has been sent to the power-stage owner; it is not yet a completed control netlist.

For the retained ADS1256 module, header voltage is not AVDD. The seller circuit has 10 µH L1 ahead of AVDD with 22 µF + 1 µF + 100 nF locally. The final inequality must include L1 DCR times the actual module branch current and its transient response. The whole held-domain current is not automatically the current through L1. This review cannot invent the inductor's DCR from a photograph.

## Postbuffer selector and compensated load

Recommended channel topology:

```
sensor ──10k── opamp(+)
   │            opamp(−)──33k──GND
 100k                │
   │                 499R feedback to opamp OUT
  GND

opamp OUT ──499R── signal switch ── ADC header ── module100R ── ADC silicon
                                       │                            │
                             33k permanent bleed                 module100nF
                                       │                            │
                                      GND                          GND

ADC header ── grounding switch ──220R──GND
```

Both switch functions use the ADG4613; the two drain pins of each selector are joined at the ADC header node. Opamp V+ and ADG VDD remain ADC_5V_HELD. The original detector 100 kΩ resistor stays at the sensor output. All compensation and output resistors should be 0.1% with controlled temperature coefficient.

This changes amplifier gain to `G=1+499/33000=1.015121212`. With infinite ADC impedance, the total transfer is `33499/(33499+Ron)`. That expression cancels the new 33 kΩ loading to first order; it does not account for the retained ADC's existing input load.

Let `Rs=499`, `Rb=33000`, `Rm=100`, and `Z` be an approximate resistive ADC input impedance in ohms. Then:

```
Tnew = G*Rb*Z / ((Rs+Ron)*(Rb+Rm+Z) + Rb*(Rm+Z))
Told = Z/(Z+500+Rm)
```

The following results were independently evaluated using those equations. They exclude resistor tolerances, opamp errors, frequency-dependent switched-capacitor behavior, and internal ADC calibration.

| Z scenario | Ron | New transfer | Change relative to original |
|---|---:|---:|---:|
| 125 kΩ | 12.5 Ω | 0.99482246 | −0.04024% |
| 125 kΩ | 17 Ω | 0.99465434 | −0.05713% |
| 150 kΩ | 12.5 Ω | 0.99562000 | −0.03975% |
| 150 kΩ | 17 Ω | 0.99545749 | −0.05607% |
| 80 MΩ | 12.5 Ω | 0.99961945 | −0.03731% |
| 80 MΩ | 17 Ω | 0.99948518 | −0.05073% |

The old500Ω value is confirmed for R3–R8 in the original exported netlist. ADG4613 Table3 gives12.5Ω typical,17Ω maximum at the stated5V single-supply conditions. Earlier12.2Ω and old499Ω comparison drafts are superseded by this table.

For single-ended buffer-off readings, the manufacturer's Figure11/Table10 model is more informative than a resistor to ground. At PGA1, Za=260kΩ connects each selected input to AVDD/2 and Zb=220kΩ connects the inputs. With AINCOM directly grounded as shown in the seller schematic, Rin=Za||Zb≈119.167kΩ and the equivalent bias source is Vbias=(AVDD/2)*Zb/(Za+Zb). For any driver Thevenin voltage Vth and resistance Rout, `Vadc=(Vth*Rin+Vbias*Rout)/(Rin+Rout)`. Old Rout=600Ω; new Rout=((499+Ron)||33000)+100Ω and Vth=(1+499/33000)*33000/(33000+499+Ron)*Vsensor.

At AVDD5V this model predicts an existing old-path bias of5.740mV. New bias is5.775mV at Ron12.5Ω and5.817mV at Ron17Ω, increases of35.2µV and76.7µV. Including gain, new-minus-old readings are about−0.286/−0.381mV at a0.8V sensor and−1.170/−1.638mV at a3V sensor. These are typical impedance-model predictions, not guaranteed calibrated accuracy. This affine model should accompany any claim about preserving absolute offset readings.

Thus this approach preserves the existing transfer closely; it does not create an absolute unity-gain path to better than 0.051%. The [ADS1256 datasheet](https://www.ti.com/lit/ds/symlink/ads1256.pdf), electrical table, specifies typical differential impedance of 150/PGA kΩ with its buffer off at 7.68 MHz. 125 kΩ is a sensitivity scenario, not a guaranteed device specification. With the buffer enabled, the absolute input operating ceiling is AVDD−2 V. Existing 406MCA gain-2/buffer-on operation and 405/449 gain-1/buffer-off operation must remain distinct. The module's 100 Ω/100 nF network remains part of settling and discharge calculations.

At 300 K the thermal noise of `499||33000` is about 2.85 nV/√Hz, referred to the amplifier input before its noise gain. The added 10 kΩ input resistor contributes approximately 12.87 nV/√Hz. These terms must be combined in quadrature with the sensor/source impedance and opamp noise, rather than describing the feedback pair as the whole added noise.

### Leakage and shutdown

[ADG4612/4613 datasheet](https://www.analog.com/media/en/technical-documentation/data-sheets/adg4612_4613.pdf), 5 V table: normal on-channel leakage can reach 120 nA at 85°C. Ahead of a 100 kΩ source this creates a conservative 12 mV offset scenario. After the low-impedance buffer, that mechanism is greatly reduced. Its 17 Ω maximum on resistance is used above. With VDD=0 and the stated isolation test conditions, each connected drain can leak up to 3 µA. Two joined drains and 33 kΩ therefore give `6 µA*33 kΩ=0.198 V`, before tolerance. This is below an unpowered ADC's +0.3 V limit under those conditions.

Do not silently extend the 0.198 V result to every intermediate supply voltage. The powered isolation-mode leakage limit is different, and the switching/logic requirements apply over their specified supply range. The active grounding switch must first discharge the module capacitor while the held rail and logic are valid. The permanent bleed then handles the final off state. The selector's automatic source-overvoltage trip is approximately VDD+1.8 V, so it is not itself a +0.3 V rail clamp. Keeping the opamp on the held rail avoids exposing its enabled switch to a battery-railed defective sensor.

Table11 guarantees isolation for VDD0…0.8V and normal logic-controlled operation from2.7V upward. The text's looser≤1V isolation statement should not replace the conservative table limit. The0.8…2.7V interval is not assigned a guaranteed switch state. Its safe system envelope must therefore use the already-disabled sensor supply, discharged module capacitors, shared opamp/ADC held rail, and bounded residual energy; a VDD0-only leakage calculation does not settle it.

Twelve ADG control inputs can draw12×18µA=216µA at their specified high-input condition. R17=10kΩ±1% could lose2.182V; R17=1kΩ±1% limits that to0.218V and preserves VIH≥2V across the normal supply range. This correction was sent to the power-stage owner. Include its additional approximately5mA normal pull-down load in the held-domain budget.

## Opamp choice and valid measurement range

Use **OPA2325IDR**, SOIC8, if the parent selects the zero-crossover update. It has the conventional dual pin sequence: 1 OUTA, 2 −INA, 3 +INA, 4 V−, 5 +INB, 6 −INB, 7 OUTB, 8 V+. [Manufacturer datasheet](https://www.ti.com/lit/ds/symlink/opa2325.pdf), Tables 6.7 and 7.3.3: 2.2–5.5 V operation; ±150 µV initial offset; 7.5 µV/°C maximum drift; input bias ≤500 pA through85°C, ≤10 nA through125°C; 0.8 mA maximum quiescent current per channel through125°C. Six channels require 4.8 mA maximum, plus output loads. The input clamp is explicitly usable with current limited below 10 mA, so retain 10 kΩ input protection. Rail headroom is at most 30 mV at 10 kΩ load over temperature. Settling figures are typical, not maximum.

Independent implications: 500 pA through a conservative 110 kΩ source path is 55 µV. At125°C that bound becomes 1.1 mV; do not report the room-temperature pA figure as a full-temperature guarantee. Added precision should not be confused with zero temperature drift. The relevant benefit here is removing crossover distortion when the driver supply changes. With a provisional7.3V maximum battery and a10kΩ±1% input resistor, even the conservative zero-clamp-voltage current is7.3V/9.9kΩ=0.737mA, below10mA. This protects the opamp's input current; it does not by itself prevent phantom powering. All six sensor supplies must therefore use the qualified battery master, and residual sensor energy/input clamp current must remain in the held-domain discharge audit. A hypothetical external signal source left powered independently is outside this harness assumption.

OPA2192 is a lower-noise precision part but its [datasheet](https://www.ti.com/lit/ds/symlink/opa2192.pdf) still has a common-mode transition between V+−3 V and V+−1.5 V; it does not solve this particular crossover concern. OPA2197 has the same issue. Retaining OPA2196 on the new held rail would move its crossover region into part of the 405 offset range.

The actual 405 acceptance range is **0.8–3.0 V**, in `single_detector_rig/m405m22/eltec_405m22_esp32_tester.py` lines127–128. This range remains comfortably inside the held-powered compensated driver's headroom. The approximately5V range mentioned in `sensor_versions.py` is for reporting rejected high-offset parts. A defective approximately5V sensor may now be reported at the driver's saturation ceiling; that number is not an accurate measurement of its true voltage. It still exceeds the3.0V failure limit. Document this diagnostic clipping rather than raising the buffer supply and defeating ADC protection.

## Audit of the replacement generator

Static inspection of `tools/analog_stage.py` on2026-09-09 confirms the intended connections for all six channels. U33…U35 use the OPA2325 dual pin map given above. Each R210…R21510kΩ protects its noninverting input; R250…R255499Ω returns OUT to the inverting input; R260…R26533kΩ returns the inverting input to ground. R220…R225499Ω connects each output to the signal switch. R240…R24533kΩ permanently bleeds the joined switch drains/ADC header node. R230…R235220Ω limits each grounding-switch path. Original100kΩ sensor load resistors and JDET pin1 supply/pin2 signal/pin3 ground are preserved.

For each ADG4613 TSSOP, S2 pin14 and S3 pin11 receive signals; S1 pin3 and S4 pin6 receive the220Ω ground paths; joined D2/D1 pins15/2 and D3/D4 pins10/7 go to paired ADC inputs. All four control pins1/8/9/16 use ISOLATE_H. Pins13/4/5 are heldVDD/GND/GND and pin12 is unconnected. These pin maps agree with manufacturer Table8 and truth Table10. This is a generator audit; the final exported netlist must still be checked after regeneration.

**OPA2325IDR is verified as [LCSC C2058909](https://www.lcsc.com/product-detail/C2058909.html)**. The official page's structured Product record, retrieved2026-09-09 using PowerShell Invoke-WebRequest after the web reader failed, identifies mpnOPA2325IDR, skuC2058909, SOIC8, and inventory2458 at lookup time. This is catalog evidence, not JLC allocation. **C2877688 is OPA325IDBVT, a single SOT23 amplifier, and is not a valid identity for the dual SOIC part.** No exact LCSC identity was verified yet for ADG4613BRUZ/BRUZ-REEL7; C207399/C657391 are the different ADG4612. A quoted JLC sourcing path is needed before the switch BOM is released.

The newly inspected `tools/power_stage.py` has LT3041, LM4050BEM3-4.1/NOPB, TLV3601DCKR and TPS3840PL34DBVR reference qualification. Their static package maps are consistent: LT3041 as above; LM4050 SOT23 pin1K/pin2A/pin3NC; TLV3601 SC70 pin1OUT/pin2GND/pin3IN+/pin4IN−/pin5VCC; TPS3840 pin1RESET/pin2VDD/pin3GND/pin4MR/pin5CT. LM4050 pin3 may be grounded. The comparator drives ADC_READY_RAW through1kΩ and Q24 can independently clamp that net during USB/ESP failure.

**Startup correction remains required in the inspected common-return version.** Q14, Q18 and Q21 have their sources on QUALIFIED_RETURN, which Q23 disconnects until reference qualification. These are correct steady-state AND functions, but they can conduct transiently while charging the floating common node. A brief ISOLATE_H-low/MASTER_PERMIT-high/status-low pulse is possible before qualification. The100nF battery-master gate capacitors suppress energy; they do not establish glitch-free READY or SPI permission. This was sent to the owner with a recommendation to clamp ADC_READY_RAW directly while the reference is unqualified, then use grounded sources. The final revised topology needs reinspection.

**Subsequent correction inspected:** Q23 is now an AO3414 shunt on ADC_READY_RAW, biased on by R38 from the held rail, with C23 feed-forward. Q26 pulls its gate low only after reference qualification. Q14/Q18/Q21/Q22 now have grounded sources. U19 is an ESP-powered SN74LVC1G132 Schmitt NAND of ADC_READY_RAW and REF_READY_RAW, so status no longer depends on the late-collapse state of the held-domain inverter. Its DBV pin map1A/2B/3GND/4Y/5VCC is consistent. R17 is now1kΩ. These edits remove the identified floating-return topology fault. Q26 was still2N7002 at this inspection, despite only approximately3.9V gate drive; the owner was asked to use AO3400A or AO3414 with a guaranteed lower-voltage on-resistance. C23 and the clamp-release timing must be included in final startup/collapse checks.

### Selected comparator/reference arithmetic

The [LM4050-N datasheet](https://www.ti.com/lit/ds/symlink/lm4050-n.pdf),4.1V electrical table, gives BEM extended-temperature error±29mV and current-related variation≤1.2mV for78µA…1mA. R30=1.5kΩ feeds it from the held rail. Confirmed selected order identity is LM4050BEM3-4.1/NOPB, [LCSC C1880345](https://www.lcsc.com/product-detail/C1880345.html); live allocation is unresolved. The obsolete AEM order must not be placed into the BOM merely to recover4mV of margin.

The [TLV3601 datasheet](https://www.ti.com/lit/ds/symlink/tlv3601.pdf) bounds offset at±5mV, hysteresis at5mV maximum and input bias at5µA. Its4.5ns full-temperature timing is conditional on50mV overdrive **and50mV underdrive**, plus5pF output load. The1kΩ series resistor and multiple MOS gates add a separate delay. Its known-low POR behavior is described explicitly, but the2.1V POR value is typical; do not manufacture a tight guaranteed POR threshold. The7mA maximum single-comparator supply current belongs in the held-load calculation.

Independent conservative corner, with each221Ω/1kΩ sense resistor±0.1% and25ppm/K over−40…85°C (`t=0.002625`):

```
Amin = 1 + 0.221*(1-t)/(1+t) = 1.2198427877
Amax = 1 + 0.221*(1+t)/(1-t) = 1.2221633037
TripMin = (4.096 - .029 - .0012 - .005 - .005)*Amin
          - 5e-6*221*(1+t) = 4.9463304777 V
TripMax = (4.096 + .029 + .0012 + .005 + .005)*Amax
          + 5e-6*221*(1+t) = 5.0562197573 V
HeaderAt50mVOverdrive = TripMin - .050*Amax = 4.8852223125 V
AVDDWith100mVDrop = 4.7852223125 V
OperatingMargin = 35.2223125 mV above 4.75 V
```

This is an independent−40…85°C check; use the actual resistor temperature rating/TCR envelope in the release calculation. It deliberately includes the entire maximum hysteresis in both directions instead of assuming a favorable half-width. Comparator input current is included through the divider Thevenin resistance after referring the threshold to the header. Reference aging, actual module loss and load transients still consume margin.

For the inspected470µF reservoir candidate,300µF effective and120mA discharge imply0.4mV/µs droop. A25mV abrupt step consumes25mV of the above35.22mV margin, leaving about25.6µs for assertion of PDWN/measurement-invalid. This is distinct from the longer allowed time for grounding the analog capacitors before any absolute voltage limit is reached. Do not require analog pins to reach0.3V before AVDD leaves its operating range, but do prove their voltage relative to the falling AVDD at every point. The final timing analysis must distinguish these two conditions explicitly.

**Later selected reference:** the owner changed U17 to **LM4050AIM3-4.1/NOPB**, [LCSC C2156509](https://www.lcsc.com/product-detail/C2156509.html). Its−40…85°C tolerance is±18mV, rather than the BEM±29mV used above; the analog-switch/ADC precision specifications already use an85°C ceiling. This is an explicit temperature-envelope choice. Repeating the same arithmetic gives trip4.95974875…5.04277596V, a50mV-overdrive header of4.89864059V, and48.64059mV margin after100mV module loss. After a25mV step the300µF/120mA example has approximately59.1µs for measurement invalidation. The BEM calculation remains a documented fallback comparison, not the selected BOM tolerance.

### Low-voltage transistor selection

[AO3414 datasheet](https://www.aosmd.com/sites/default/files/res/data_sheets/AO3414.pdf) supplies a real1.8V drive specification: RDS(on)≤85mΩ at VGS1.8V/ID2.5A,25°C. SOT23 pins1G/2S/3D, VDS20V, VGS±8V. Ciss≤320pF is specified at VDS10V; it is not a universal capacitance bound over every startup operating point. This part is appropriate for the USB/ESP-supervisor gates Q10/Q11, subject to the actual supervisor VOH bound. Do not use its±8V gate rating indiscriminately on a battery node with an unverified maximum.

## Items still owned by integration

- Complete the regulator enable, supervisor/comparator, and startup-low truth table with actual selected parts.
- Set reservoir capacitance from that completed timing and the final current budget. A nominal3mF bank is not a substitute for the calculation.
- Check both normal run and source/battery removal, including module AVDD lag and ADC input discharge. Distinguish operating-range guarantees from absolute-limit guarantees.
- Recompute exact resistor/TCR corners and gain error after the final footprint/BOM choices. This review's analytical values are reproducible design arithmetic, not characterization results.
- Regenerate and audit the integrated schematic/netlist after the power-stage owner removes the startup transient identified above. The analog generator already matches the postbuffer compensated topology.


### Final sourcing-pass inspection of the power revision

The subsequently inspected generator has changed Q26 to AO3414, removing the stated low-gate-drive ambiguity. Q14/Q18/Q21 remain 2N7002LT1G. This is acceptable for the required cutoff window: TLV3601 specifies VOH no more than 80 mV below VCC at 1 mA load; its normal DC gate-net load is below 1 mA. At the conservative 4.89864 V detection header, allowing 80 mV output loss and about 50 mV through R16 still leaves approximately 4.77 V at those gates, above their 4.5 V on-resistance specification point. Replacing them with higher-capacitance MOSFETs is unnecessary for this reason. This argument applies during the detection window, not indefinitely through the late rail collapse.

C12 is now Nichicon PCJ0J821MCL4GS, 820 µF polymer, 6.3 V, ±20%, rather than the earlier 470 µF candidate. Its stated 20°C initial capacitance minimum is 656 µF and the compounded endurance minimum is 524.8 µF. The 10 mΩ ESR specification, endurance multiplier and temperature impedance ratio do not constitute a broadband transient or low-temperature capacitance guarantee. The calculation therefore correctly retains **qualified effective capacitance at least 300 µF**, maximum 120 mA held-domain discharge, and maximum 25 mV abrupt rail step as explicit inputs. The 300 µF numerical examples above remain applicable to these acceptance inputs; they are not claims about a measured capacitor.

The current Q26 drain is still directly connected to REF_NOT_READY in the snapshot reviewed at 14:24 UTC. Insert 100 Ω between that node and Q26 drain (new net REF_CLAMP_RELEASE) to limit the C23 release-current pulse to approximately 52 mA. C23 is 100 nF, so this adds about 10 µs to startup clamp release; direct Q24 USB/ESP fault shutdown and the comparator falling path are unchanged. This correction is accepted and sent to the power-stage owner; it must appear in the final native netlist before the issue is closed.

The corrected SET arithmetic is explicitly `99e-6*51798*0.998-0.002 = 5.115745996 V` and `101e-6*51798*1.002+0.002 = 5.244061196 V`. An earlier independent-review transcription differed by 22 µV; the owner calculation was correct. The ADC upper operating margin is 5.938804 mV before additional transient or PCB leakage effects.


### Final passive correction inspected

The latest generator has Q26 drain on REF_CLAMP_RELEASE and **R40 = 150 Ω** to REF_NOT_READY. It supersedes the earlier 100 Ω proposal. At 5.2441 V and −1% resistance, the initial discharge bound is about 35.32 mA; the nominal C23 time constant is 15 µs. Direct Q24/comparator fault assertion remains independent of this startup release path. The topology issue is closed in the generator; the final exported netlist must retain it.

C11/C13/C39 are now three parallel Murata GRM32ER71E226KE15L 22 µF/25 V/X7R/10% ceramics. C10 is the already-reviewed 22 µF tantalum and C36 adds local input ceramic bypass. C17/C18 are 4.7 µF/25 V Samsung CL21B475KAFNNNE for the AP2112. C15/C37/C38 are three parallel 100 nF C0G SET capacitors, so claims tied to a 4.7 µF SET capacitor no longer apply. C14/C23/C24 use the same exact 1206 C0G family, with the generator helper overriding its old generic footprint argument correctly.

The archived manufacturer Murata curve sheet was visually inspected on page 3. It shows approximately −20% DC-bias change at 5.25 V and an additional small-AC-amplitude effect approaching −30%. These are separate typical curves, not a guaranteed combined-temperature/voltage envelope. The illustrative multiplication `3*22uF*0.8*0.7*0.9*0.85 = 28.2744uF` provides more design margin than the former pair's 18.8496 µF; it does not prove a production minimum. Retain the explicit effective combined output capacitance ≥20 µF, individual ESR and mounted ESL acceptance requirements, and qualify AP2112 local effective capacitance ≥1 µF. The 2020 Murata reference sheet itself directs users to obtain current approval specifications.

Reference copy: `murata_grm32er71e226ke15_reference.pdf`, rendered page `murata_grm32er71e226ke15_curves.png`; manufacturer-authored reference hosted at https://static.chipdip.ru/lib/973/DOC031973527.pdf. This is curve evidence, not a current approval drawing. No circuit generator was modified by this independent sourcing review.

Reference PDF SHA-256: `35ECFCD99C9B9E0653F76712A6E2C5C35E81907EEA444AD8E857CAC2435FC104`.
