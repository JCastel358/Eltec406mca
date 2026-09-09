# USB master power architecture — implemented schematic candidate

Updated 2026-09-09. This report describes `tools/power_stage.py`, not the superseded perfboard proposal. The native schematic is a reviewable candidate. No board routing, SPICE sign-off, oscilloscope measurements, production qualification, or assembly order is implied.

## Scope and confirmed interfaces

The user confirmed two HeyFuture LiFePO4 packs labeled 6.4 V, 6 Ah, 38.4 Wh, and chose to preserve the detector battery voltage. The design does not replace the detector rail with 5 V or boost it to the 8 V condition in the historical TP412 procedure. Exact charged voltage, BMS cutoff, permissible charging arrangement, cable inductance, and startup transients remain to be established from the actual packs. The electrical review uses 7.3 V as an explicit provisional maximum; it is not a verified label specification.

Detector/USB ground remains continuous. Emitter ground remains optically isolated. Both battery positive leads are switched. The raw USB voltage from the ESP32 module is sensed only; it never supplies the detector reservoir and has no intentional connection to either battery positive rail.

Native interfaces: `SENSOR_BAT_SW`, `EM_BAT_SW`, `EM_GND`, `ADC_5V_HELD`, `ADC_IO_3V3`, `ISOLATE_H`, `MASTER_PERMIT`, `SPI_PERMIT`, `ADC_PDWN_N`, and `ESP_POWER_NOT_READY`. GPIO35 receives the last signal: LOW means ready, HIGH means not ready. Firmware must wait for readiness and reinitialize the ADC after every readiness loss.

## Evidence for the actual ADC module

The user's purchase is [the ADC listing](https://www.amazon.com/Channel-ADS1256-Acquisition-Collecting-Precision/dp/B0DBSWRQZS). The seller's [schematic image](https://m.media-amazon.com/images/I/71Qz8ZfCIFL._SL1500_.jpg), also saved as `research/adc_seller_image_05.jpg`, shows:

- Header 5 V feeds AVDD through L1, 10 uH. AVDD has 22 uF, 1 uF and 100 nF bypass capacitors.
- An AMS1117-3.3 regulator fed from AVDD powers the module's digital circuitry. Carrier `ADC_IO_3V3` is a separate supply and must not be connected to that internal regulator output.
- Every analog header input has 100 ohms in series and 100 nF to ground at the ADS1256 pin.
- ADR03 reference, OPA350 reference buffer, 49.9-ohm isolation resistor and reference capacitors are present.
- Module PDWN has a 10k pull-up to internal 3.3 V.

The image is strong seller evidence, not verification of the assembled GY-220722-V1 board. In particular, L1's DC resistance and transient response are unknown. Header voltage cannot simply be called actual ADS AVDD. The calculations below require the actual AVDD to remain no more than 100 mV below the measured header rail throughout shutdown. This must be checked at the ADS supply pin; assuming an arbitrary 1-ohm inductor without confirming module current is insufficient.

## Implemented power stage

J1/J2 retain the original jack locations and use the original GCT footprint. F1/F2 are Littelfuse 468 Slo-Blo 1206 candidates, 1 A detector and 1.5 A emitter. Fuse interrupt rating and time/current behavior must be matched to actual battery faults and inrush. They are not precision current limiters.

Q16/Q19 AO3401A devices protect reversed battery polarity: drain toward the jack, source toward the protected load, gate referenced to that battery's return. This orientation is not an ideal-diode controller and does not prohibit reverse current during normal positive-voltage operation. Q17/Q20 AO3401A high-side masters have 10k gate/source pull-ups, 1k gate-sink resistors and 100 nF gate/source capacitors. These capacitors reduce edge speed; MOSFET SOA and measured inrush still need approval.

The emitter master uses an LTV-817S-B optocoupler. The emitter regulator/PWM stage is independently owned by `tools/emitter_stage.py`. Its emitter return stays `EM_GND`. Hardware also interrupts the three PWM optocoupler LED cathode returns through Q22, so an ESP output left high does not keep optical drive active after permission disappears.

U10 is [LT3041ADE#TRPBF](https://www.analog.com/media/en/technical-documentation/data-sheets/lt3041.pdf), listed as LCSC C7452883. Pins 1/2/3 connect to protected detector battery; 5 is enable; 8 PGFB connects to IN to disable fast start; 9 SET has the precision setting network and three parallel 100 nF C0G capacitors (300 nF total); 10/11/15 are ground; 12 OUTS senses the local output-capacitor node; 13/14 supply the held rail; 4 VIOC and 6 PG float. The stock KiCad DFN footprint references the same DE mechanical drawing 05-08-1708 and has the correct 1.7 by 3.3 mm exposed pad.

RSET is 51.1k plus 698 ohms, both 0.1%, 10 ppm/K. Exact candidate parts are [RN73C1J51K1BTDF](https://www.te.com/en/product-6-1879134-9.html) and [RN73C1J698RBTDF](https://www.te.com/en/product-9-1676970-4.html). Their assembly sourcing must be confirmed. Generic 25 ppm parts are not interchangeable here. The nominal rail is 5.1798 V. Applying 99..101 uA full-temperature SET current, +/-2 mV offset, initial resistor tolerance and a conservative 100 K resistor excursion gives:

- Minimum: 99 uA x 51,798 ohms x 0.998 - 0.002 V = 5.115746 V.
- Maximum: 101 uA x 51,798 ohms x 1.002 + 0.002 V = 5.244061 V.

These resistor/current/offset bounds exclude external SET leakage. The selected exact C0G network and qualified assembled-board leakage budget extend the analyzed static range to 5.110758..5.245359 V, as detailed below. These are static regulation bounds, not transient overshoot bounds. The ADS1256 maximum operating AVDD is 5.25 V, so output overshoot remains a material verification item. The LDO specifies 460 mV maximum dropout at 100 mA; its dropout definition is a 1% loss of regulation. A pack too low to maintain the regulated rail is handled by the independent rail comparator.

LT3041 explicitly permits OUT above IN and blocks output-to-input current. This replaces the rejected TPS7A49 plus SS14 proposal, whose diode could not guarantee the regulator's OUT-IN absolute limit of 0.3 V. Nevertheless, LT3041 can sink up to 15 mA to ground through overshoot-recovery circuitry when disabled, in addition to SET-resistor current. The small SET-open reverse-leakage figure is not an adequate hold-up current budget.

R11=300 ohms programs approximately 500 mA current limit. The datasheet's 450..550 mA example is tested at IN=2.2 V/OUT=0, not a blanket guaranteed 6.4 V battery inrush limit. C11/C13/C39 are three local Murata GRM32ER71E226KE15L 22 uF/25 V X7R ceramics. The LT3041 requires at least 20 uF effective output capacitance; capacitor ESR below 20 milliohms and mounted ESL below 2 nH remain the layout/qualification targets. Three parts address the combined small-signal/DC-bias concern found in the exact manufacturer curves; these typical curves do not establish a guaranteed combined-condition minimum. The 820 uF Nichicon PCJ0J821MCL4GS polymer reservoir is additional to them. It replaces the earlier wet-electrolytic candidate so cold impedance does not consume the rail-step allowance. The exact [Nichicon PCJ catalog](https://www.nichicon.com/getmedia/dac1415c-535b-498d-bb65-4c9bced98c46/e-pcj.pdf) specifies 10 milliohms ESR at 20 C/100 kHz, 1.25 maximum impedance ratio at -55/+105 C, and at most 150% of initial ESR limit after endurance. Initial capacitance minimum is 656 uF; a further 20% endurance change gives 524.8 uF at the catalog measurement condition. These provide substantial margin to the 300 uF analysis target. The catalog temperature-impedance ratio is not a low-frequency capacitance bound, and combining temperature and endurance factors is an engineering envelope rather than an explicit combined-condition guarantee. At 120 mA, multiplying the ESR limit by 1.5 and 1.25 gives an illustrative 2.25 mV step, before board impedance. Include its 1.033 mA maximum leakage in the held-load budget. The footprint is the stock CP_Elec_8x11.9, matching the 8 mm diameter/11.9 mm length case. The carrier's AP2112K-3.3 supplies SPI receiver buffers and control logic only.

## Readiness and default-off control

USB and ESP3.3 are independently monitored by TPS3840PL42 and PL30. Two AO3414 MOSFETs complete a ground path only when both monitors are good. AO3414 has a specified on-resistance at 1.8 V, unlike generic 2N7002 drive assumptions for a marginal 3.3 V supervisor output.

Otherwise `POWER_OFF_NODE` rises. Q12 disables U10, Q13 asserts the reference supervisor's MR, and Q24 independently clamps `ADC_READY_RAW` low through a current-limited comparator output. D10/D11 diode-OR the protected battery and held rail for this default-off bias. Therefore removing the detector battery before removing USB does not eliminate the subsequent shutdown signal. D12 limits Q24's gate voltage below its 8 V absolute rating.

The precision monitor uses [LM4050AIM3-4.1/NOPB](https://www.ti.com/lit/ds/symlink/lm4050-n.pdf), LCSC C2156509, a 4.096 V shunt reference. Its analyzed range is -40..85 C, which limits the component-level analysis; it does not establish a rig environmental rating. Its full-temperature tolerance is +/-18 mV, with up to 1.2 mV additional change from 73 uA to 1 mA. A 1.5k feed resistor keeps normal reference current below 1 mA and above its required minimum around the rail-trip region.

U14 TPS3840PL34 monitors the reference and delays qualification using 100 nF C0G. The delay is approximately 61.9 ms nominal. Current TPS3840 Rev E equation6 specifies the minimum using -ln(0.36), giving 33.97 ms from RCT=350k and C=95 nF; including the selected capacitor post-humidity -7.5% envelope gives31.42 ms. The earlier41.2ms figure incorrectly used the nominal logarithm in a minimum bound and has been corrected. A 3.8 V supervisor was considered but is not an orderable part and is not used. The reference capacitor charging network has R*C approximately159 microseconds at its initial maximum capacitance, before additional input capacitance; even allowing the post-humidity capacitance envelope, qualification exceeds180 such time constants. Before the shunt reaches breakdown, its node follows the held rail and is above the comparator's divided rail input. Fast startup is additionally suppressed by a default-on gate clamp.

U18 [TLV3601DCKR](https://www.ti.com/lit/ds/symlink/tlv3601.pdf), LCSC C2974371, compares the 221-ohm/1k divided held rail with the reference. Its documented POR holds its output low during low supply. Q23 is a default-on AO3414 clamp on `ADC_READY_RAW`; Q26, also AO3414 with 1.8 V specified drive, releases the clamp only when the reference has qualified. C23 couples abrupt held-rail rises into the clamp gate so the clamp does not wait for a resistive gate-charging delay. R40=150 ohms now sits between Q26 drain and REF_NOT_READY: it limits C23 release current below35.4mA and adds approximately15us only to startup clamp release. The direct Q24 and comparator shutdown paths are unaffected. All enable-driver sources are grounded. An earlier floating common-return proposal was rejected because its parasitic charging could create permission glitches.

`ISOLATE_H` is pulled to the held rail by 1k, not 10k. Twelve ADG4613 control pins can draw 216 uA total; the 1k choice preserves a valid HIGH down to the ADG's 3 V guaranteed supply limit. Q14 sinks about 5 mA during measurement. HIGH opens signal paths, closes ADC grounding paths and asserts PDWN. Both battery master enables are also removed when the raw comparator gate signal falls.

U19 is an ESP-powered [SN74LVC1G132 Schmitt NAND](https://www.ti.com/lit/ds/symlink/sn74lvc1g132.pdf). Its inputs tolerate 5.5 V and have partial-power-down protection. Inputs are `ADC_READY_RAW` and `REF_READY_RAW`, each defined by a pulldown when unpowered. Its output is `ESP_POWER_NOT_READY`: LOW only when both conditions are HIGH. Using an ESP-powered gate avoids reporting a false ready state from an ADC-powered logic gate during late collapse. Schmitt inputs accept the finite RC edge rates. A 10k output pulldown limits specified 10 uA Ioff to 0.1 V while ESP is off.

## Numerical shutdown envelope and its limits

The calculations distinguish stopping valid conversions from preventing input overstress. They do not assume that the ADC must remain in its operating range after it has been isolated and powered down.

Using each 221-ohm/1k divider resistor's 0.1% tolerance plus 25 ppm/K for a 65 K excursion gives divider factors 1.2198428..1.2221633. Include reference +/-19.2 mV, comparator +/-5 mV offset plus the full 5 mV maximum hysteresis conservatively, and +/-5 uA input bias through the top resistor:

- Static header trip: 4.959749..5.042776 V.
- Maximum high-side header level required to establish 50 mV comparator underdrive: 5.103884 V. The conservative regulated minimum including the selected SET leakage budget,5.110758 V, exceeds this by6.87mV.
- Header at 50 mV falling overdrive: at least 4.898641 V.
- Actual AVDD at that point, with the explicit 100 mV module-loss bound: at least 4.798641 V.

The comparator's 4.5 ns maximum applies to 50 mV overdrive AND underdrive with a 5 pF load. It must not be quoted as the delay of the entire gate network. The real network includes 1k isolation, MOSFET gate charge, the 1k `ISOLATE_H` pull-up and ADG controls. A total 10 microsecond response budget is an explicit acceptance limit for the assembled path, not a bench result or a claim derived from typical capacitances. At Ceffective=300 uF and total held discharge=120 mA, 10 microseconds costs 4 mV. Reserving another 25 mV for abrupt rail step leaves actual AVDD at 4.769641 V when conversion permission must be removed, above 4.75 V by about 19.6 mV.

The monitor's offset is characterized at the datasheet's stated common-mode condition. The actual approximately 4.1 V common mode and actual capacitive load must be included in comparator verification; typical CMRR must not be silently promoted to a worst-case guarantee. The numeric threshold envelope above is therefore the design acceptance target. It is not a substitute for validating the chosen components at the actual operating point.

With the selected post-buffer ADG4613 architecture, each ADC input is grounded through 220 ohms, ADG Ron up to17 ohms, and module100 ohms. Allowing 1% resistors and 120 nF maximum module capacitance gives tau=40.824 microseconds and about133.4 microseconds from5.2441 V to0.2 V. Include carrier/trace capacitance separately. During that discharge AVDD may fall below4.75 because PDWN and SPI isolation are already asserted. The full AIN-versus-AVDD waveform must still remain within the ADS1256 absolute limits throughout; merely checking the final zero volts is insufficient.

For a declining held rail at120 mA/300 uF, the existing499-ohm+Ron+module100-ohm input path has approximately74 microseconds time constant and approximately30 mV tracking lag with120 nF. Adding25 mV abrupt step and100 mV module supply loss is still below the ADS upper protection allowance of300 mV. Once the ground path closes, input voltage falls rapidly. This reasoning depends on the explicit current/capacitance/module-loss bounds and on the new rail-to-rail input buffer choice; it cannot be transferred to arbitrary module revisions or old buffer power rails.

After complete power loss, the ADG4613's powered-off isolation and a33k ADC-node bleeder bound two3-uA drain leakage paths to approximately0.198 V, before bleeder tolerance. Those bleeders belong to the root analog stage. Their 33k values must not be increased to82k, which would permit0.492 V.

## Trial build qualification and release evidence

1. Confirm battery endpoints, center polarity and BMS behavior; verify reverse-polarity and input transients with current-limited lab supplies first.
2. Confirm the actual module follows the seller schematic, especially L1, the100-ohm/100-nF AIN filters and the internal3.3 V power path. Measure actualAVDD-header difference and totalheld-domain current.
3. Qualify the selected exact local ceramics and the820-uF polymer reservoir for effective C >= 300 uF and the assumed rail-step limit across the intended environment and service life. Nichicon specifies initial tolerance, endurance change and temperature impedance ratio, but does not directly guarantee every combined frequency/temperature/aging transient bound used here. C10 is now22uF25V tantalum with19.8uF initial minimum and no MLCC DC-bias reduction; C36 is the parallel local22uF ceramic. The exact choices replace the insufficient generic nominal10uF input proposal.
4. Verify comparator thresholds/common mode, reference startup, C23 default-on clamp timing, USB/ESP supervision, aggregate control delay<=10us, PDWN timing and allsixAIN-versus-actualAVDD shutdown waveforms. Include abrupt USB loss, slowbrownout, rapidon/off, missingbattery, batteryremovalbeforeUSB, and latelowvoltagecollapse.
5. Verify the qualified static5.1108..5.2454 V envelope, assembled SET leakage budget and transientovershoot below5.25 V. The remaining maximum-voltage margin is only4.6mV; if observed transient or long-term errors consume it, the setting andthresholdbudgets must be revised together.
6. Verify masterPFET SOA/inrush with sensor andemitter loads. The emitter stage uses a650mA conservative inputdesignenvelope for3channels; the user's200mA wording is notproof of aggregate current.
7. Complete exactJLCPCBA sourcing/footprint review, schematicERC, boardDRC afterrouting, and test firmware's hardware-ready wait/reinitialization. Inventory mentioned here is a source lookup, not an assemblyorder or guarantee.

Structural validation performed on the implemented power stage:86 physical parts instantiated, unique references, all connected pin numbers present in their symbols and assigned physicalfootprints. The stock LT3041 footprint was independently checked against the manufacturer drawing. These checks do not validate analog performance or transient behavior.


## Late supply collapse and remaining detector charge

Below the ADG4613 guaranteed operating supply range, do not claim that its truth table continues to select ground. The relevant safety argument changes: the buffer supply, switch supply and ADC header supply all share `ADC_5V_HELD`. If an unspecified switch state reconnects a buffer to an ADC input, the buffer output can only be bounded relative to that same held rail, with the module AVDD path and input-capacitor tracking lag accounted for. The calculated 154.64 mV envelope includes the required 100 mV header-to-AVDD difference, 25 mV abrupt step and 29.64 mV stored-input tracking lag. It is below the ADC's 300 mV positive input allowance. This is conditional on output being confined to the shared rail and on the stated slope bound; the ADG truth table itself is not used to justify the 2.7..0.8 V interval.

An ADC input capacitor at the beginning of this interval stores at most 0.5 x 120 nF x (2.7 V)^2 = 0.4374 uJ. The held reservoir at 2.7 V stores at least 0.5 x 300 uF x (2.7 V)^2 = 1.0935 mJ. The module input capacitors cannot dictate the held-supply trajectory, but their finite charge still requires the tracking bound rather than an instantaneous-zero assumption.

Sensor supply capacitors may remain charged after the high-side master begins turning off. That is a separate injection path at each amplifier input, addressed by the analog stage's 10k series input protection and the selected amplifier's manufacturer limits. For a provisional 7.3 V sensor maximum and a zero-volt held rail, a conservative source-current ceiling through each 10k/1% resistor is 7.3 V / 9.9k = 0.7374 mA (4.424 mA for six). This is not proof that the detector output can actually reach its rail, nor a claim about amplifier clamp voltage. Any current accepted into the held supply tends to slow its fall and must be included in off-state balance. The buffer input-current rating and output behavior while its own supply crosses zero still require a manufacturer-supported limit; the reservoir and ADG bleeders alone cannot establish that behavior.

## Reproducible calculation and netlist checks

Run `python tools/power_corners.py` after the native schematic/netlist export. The helper writes `reports/power_corners.json` with all numerical inputs, conditional margins and 83 pin/net assertions against KiCad's actual XML export. It explicitly checks the LT3041 parallel OUT pins/OUTS, R30 reference feed, default-off gate clamps, separate battery domains, emitter optocoupler and ESP GPIO35 readiness. It checks unconnected VIOC/PG pins as well. This does not replace ERC, thermal analysis, component sourcing, simulator models or waveform qualification.

## Exact component closure for trial layout

All86 instantiated power parts now have exact MPNs. New physical parts are C36,C37,C38,C39 andR40; existing references and external power interfaces are retained. These choices are frozen for native integration and reversible trial layout. Layout can proceed with explicit acceptance targets; they are not claims of bench qualification or fabrication release.

| Reference | Exact selection | Electrical purpose and bound |
|---|---|---|
| C10 | TPSD226K025R0200,22uF25V10%,D case |19.8uF initial minimum, no MLCC DC-bias reduction,200milliohm maximum100kHz ESR. |
| C36 | GRM32ER71E226KE15L,22uF25V10%,1210 | Local ceramic input bypass, parallel with C10. |
| C11,C13,C39 | Same Murata22uF part, three in parallel |66uF nominal; qualification target at least20uF effective after actual DC bias, AC amplitude, temperature and aging. |
| C15,C37,C38 | GRM31C5C1H104JA01K,100nF50V5%C0G,1206 |300nF SET bypass; calculable leakage with far less capacitance variation than the former4.7uF X7R proposal. |
| C14,C23,C24 | Same Murata100nF C0G1206 | Reference decoupling, default-clamp feed-forward, and reference qualification timing. |
| C17,C18 | CL21B475KAFNNNE,4.7uF25VX7R,0805 | AP2112 local input/output bypass. Effective capacitance must exceed1uF at the actual operating point. |
| Other100nF decouplers | CL10B104KB8NNNC,50VX7R,0603 | Ordinary supply bypass; these do not substitute for C0G timing or SET parts. |
| C20,C22 | GRM1885C1H103JA01D,10nF50V5%C0G,0603 | USB/ESP qualification timing. |
| D12 | Diotec BZT52C6V8GW,SOD123 | Active replacement for the obsolete Diodes7-F order code. |
| R40 | RC0603FR-07150RL,150ohm1%,0603 | Q26 startup discharge limiter. |
| U16 | LTV-817S-TA1-B | Tape-packaged SMDIP4 version, B CTR bin; isolation domains unchanged. |

The input network follows LT3041 guidance to use a local ceramic with an additional higher-ESR tantalum for long input wires. C10 is already connected when the battery is plugged in; normal operator USB toggling does not repeatedly hot-plug C10. Its25V rating is substantially above the provisional7.3V pack maximum. Tantalum ESR is specified as a maximum, not a guaranteed damping resistance; battery-lead ringing remains a wiring/layout test. [LT3041 input/stability guidance](https://www.analog.com/media/en/technical-documentation/data-sheets/lt3041.pdf), [Kyocera AVX TPS catalog](https://datasheets.kyocera-avx.com/TPS.pdf).

The exact Murata22uF part's manufacturer curves show approximately20% DC-bias capacitance loss at5.25V and a separate roughly30% small-AC-amplitude reduction. Applying both illustrative factors plus10% tolerance and15% X7R reduction gives18.85uF for two parts, so a third part was added. The corresponding illustrative total is28.27uF. These are separate typical curves, not a guaranteed combined-condition limit; actual effective capacitance/impedance and the mounted LT3041 loop must meet the stated20uF/ESR/ESL targets. The independent review saved the source and curve image locally. [Manufacturer GRM32ER71E226KE15 data](https://static.chipdip.ru/lib/973/DOC031973527.pdf).

The Samsung4.7uF AP2112 bypass has a4.7-to-1 nominal margin to the1uF regulator minimum and a25V rating. Samsung lists the selected suffix in mass production and explicitly describes its chart data as typical. Thus a combined-condition effective1uF acceptance limit remains necessary; no nominal-value-only stability claim is made. [CL21B475KAFNNNE manufacturer page](https://product.samsungsem.com/mlcc/CL21B475KAFNNN.do).

The Murata C0G reference sheet, datedJanuary10,2025, specifies room-temperature insulation resistance greater than500ohm·F, post-durability greater than50ohm·F, and post-humidity greater than25ohm·F. The last condition is measured after the specified test and recovery at room temperature, not an unrestricted operating-temperature leakage guarantee. Using three capacitors,105% tolerance,107.5% post-humidity capacitance, and the weakest25ohm·F product gives71.11nA total leakage at5.25V. An additional assembled-board SET leakage acceptance of±25nA allows for contamination/parasitic paths; this is a measured acceptance target, not a free guarantee. With the existing precision setting resistors the resulting static envelope is5.110758..5.245359V:6.87mV margin to the comparator prior-underdrive requirement and4.64mV below the ADS operating maximum. Maintain a clean SET region, short ground return and appropriate guard/layout; qualify leakage at the actual warm operating condition. [Exact Murata C0G reference sheet](https://www.mouser.ca/datasheet/3/76/1/GRM31C5C1H104JA01-01A.pdf).

The300nF SET network has nominal RC corner10.24Hz and nominal90% start time35.8ms with fast-start disabled. It does not inherit LT3041's4.7uF/1uVrms noise figure. At nominal values the initial rise is333V/s. A conservative263.6nF post-test capacitance and101uA SET current give383V/s; a1.10mF total startup output-capacitance envelope then needs421mA of charging current, before load. Adding the deliberately conservative120mA load gives541mA, so some startup corners can enter the nominal500mA current limit and extend startup. This is an anticipated regulated startup condition, not a USB inrush path: power comes from the detector battery, and readiness/firmware postpone useful conversions. The current-limit450..550mA characterization remains tied to its stated2.2V/zero-output test; verify the actual battery-current waveform, capacitor charging, thermal behavior, complete settling and load-release overshoot. The nominal current limit is not a blanket guaranteed battery fault current.

D12's selected [Diotec April2026 datasheet](https://diotec.com/request/datasheet/bzt52c2v4gw.pdf) gives6.40..7.20V at5mA and maximum+0.07%/K coefficient. The125C calculation7.704V remains below AO3414's8V absolute gate limit. R13 limits normal clamp current well below5mA. This does not claim that arbitrary wiring surges are clamped below8V; that waveform belongs to input-transient qualification. The [manufacturer product page](https://diotec.com/en/product/BZT52C6V8GW.html) identifies the active SOD123GW part.

TLV3601 has at most80mV VOH loss at1mA. The steady RAW-node load is well below1mA; allowing approximately50mV acrossR16 still leaves2N7002 Q14/Q18/Q21 gates above4.5V at the4.8986V falling-detection header level. Their use does not require extrapolating2N7002 on-resistance down to3.3V during the measurement window. Q26, driven by the lower reference-supervisor level, is AO3414. The small150ohm R40 bounds its capacitor-release pulse without slowing the independent USB-fault clamp.

## Battery depletion and retries

A marginal detector battery can produce hardware retries. When a falling held rail asserts PDWN and disables control loads, the lighter load can let the LDO output recover. U14 currently requalifies USB/ESP/reference faults, but a comparator-only low-battery event does not reset its timer. Comparator hysteresis alone therefore does not guarantee a quiet, latched-off battery-depletion state. No unverified diode-to-MR modification was added: TPS3840 requires a guaranteed sub600mV LOW and MR no higher than its own supply.

The revision firmware captures power-fault edges, stops and invalidates an active operation, and reinitializes the ADC after recovery. It does not automatically resume emitter operation. Hardware protection remains independent of firmware, but rapid repeated brownouts and repeated rail recovery must be included in the waveform checks above, especially before the comparator has spent time at the50mV prior-underdrive condition. A weak pack can require charging before reliable startup; this design does not promise uninterrupted operation through battery exhaustion or chatter-free analog hardware in every depleted-pack condition.
