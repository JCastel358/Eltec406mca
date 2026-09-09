# Original circuit electrical audit

Date: 2026-09-09. Status: evidence for redesign; this is not a qualification of the original or replacement PCB. The original schematic/PCB were not saved or edited. Connectivity below comes from a **fresh KiCad 10.0.6 XML netlist**, not tracing the raster image by eye.

## Reproduction and scope

Working directory: repository root. Commands executed:

```powershell
& 'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe' sch export netlist --format kicadxml --output 'hardware/single_detector_usb_master_r1/reports/original_netlist.xml' 'hardware/single_detector_usb_master_r1/source_reference/ESP32_test_board/ESP32_test_board.kicad_sch'
& 'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe' sch erc --format json --severity-all --exit-code-violations --output 'hardware/single_detector_usb_master_r1/reports/original_erc.json' 'hardware/single_detector_usb_master_r1/source_reference/ESP32_test_board/ESP32_test_board.kicad_sch'
```

Netlist export exited 0. ERC exited 1 with **18 findings: 3 errors and 15 warnings**, not a pass. The outputs are [original_netlist.xml](../reports/original_netlist.xml) and [original_erc.json](../reports/original_erc.json). Source schematic SHA256: `8CF750AB7967E7993EF7BA2EA656DF6E8EEC73FAEB421930F8CC7B3901AD3F88`. See [source mechanical audit](source_mechanical_audit.md) for provenance and all original placement coordinates.

The schematic has 54 electrically represented components and 76 exported nets, including no-connect nets. The PCB adds a non-electrical logo footprint. Photo markings, module internals, actual voltage/current waveforms and manufacturing substitutions are outside the proof supplied by this netlist.

## Power domains as actually drawn

| Domain | Positive net | Return net | Connected loads |
| --- | --- | --- | --- |
| Detector battery J1 | `Net-(O1-V+)`, J1.1 | `Net-(O1-V-)`, J1.2 | All six sensors, O1/O2/O3, 300k/100k divider, analog ADC ground header pins |
| Emitter battery J2 | `Net-(U3-VI)`, J2.1 | `Net-(Q2-S)`, J2.2 | U3 regulator and emitter-side FET/opto circuits only |
| USB-derived 5 V | `Net-(U1-VIN)`, U1.16 | `Net-(U2-GND)`, U1.2/U2.2 | ADS1256 module pin 1 |
| ESP32 3.3 V | `Net-(U1-3V3)`, U1.1 | ESP32 module internal ground | ADS1256 module PDWN pin 8 |
| Opto input return | No separate supply; GPIO-driven LEDs | `Net-(U1-GND-Pad17)`, U1.17 | U4 LED cathodes 2/4/6 |

There is **no explicit carrier-net connection** between `Net-(U2-GND)` and `Net-(O1-V-)`, nor between ESP32 ground pins U1.2 and U1.17. The operating assembly evidently relies on internal module connections for those relations; that is an inference requiring continuity verification. The emitter return `Net-(Q2-S)` is genuinely separate in the carrier netlist, coupled to the ESP32 through optocouplers. Therefore the repository's older instruction to make all emitter/detector grounds common does not describe this native board. Do not short the isolated emitter return in the redesign merely to reconcile an old note.

J1/J2 have schematic pins `1`, `2`, and `MP`, while the assigned GCT footprint uses physical pads `1`, `2`, and `3`. The mechanical pin is intentionally unconnected, but the **MP versus 3 symbol/footprint mismatch must be resolved** in the new library rather than carried through as an unexplained parity exception.

## Six analog buffer paths

Every amplifier is a unity-gain follower: its output is connected directly to its inverting input. The 500-ohm resistor is outside the feedback loop, in series with the ADC input. It is **not a gain-setting resistor**. Each sensor OUTPUT node also has 100k to detector ground.

| Sensor | Non-inverting input and sensor-output net | Follower feedback/output net | Series resistor path | ADC module pin |
| --- | --- | --- | --- | --- |
| Test1.2 | O1.3, R9.1; `Net-(O1-+IN_A)` | O1.1 = O1.2; `/efh` | R3.2 at op amp, R3.1 at ADC | U2.9 AIN0 |
| Ref1.2 | O1.5, R10.1; `Net-(O1-+IN_B)` | O1.7 = O1.6; `Net-(O1--IN_B)` | R4.1 at op amp, R4.2 at ADC | U2.10 AIN1 |
| Test2.2 | O2.3, R11.1; `Net-(O2-+IN_A)` | O2.1 = O2.2; `Net-(O2--IN_A)` | R5.1 at op amp, R5.2 at ADC | U2.11 AIN2 |
| Ref2.2 | O2.5, R12.1; `Net-(O2-+IN_B)` | O2.7 = O2.6; `Net-(O2--IN_B)` | R6.1 at op amp, R6.2 at ADC | U2.12 AIN3 |
| Test3.2 | O3.3, R13.1; `Net-(O3-+IN_A)` | O3.1 = O3.2; `Net-(O3--IN_A)` | R7.1 at op amp, R7.2 at ADC | U2.13 AIN4 |
| Ref3.2 | O3.5, R14.1; `Net-(O3-+IN_B)` | O3.7 = O3.6; `Net-(O3--IN_B)` | R8.1 at op amp, R8.2 at ADC | U2.14 AIN5 |

All O1/O2/O3 pin 8 connect to J1+, pin 4 to J1-. C2/C3/C4 are individual 100nF rail decouplers, one per dual package. R9-R14 pin 2 and every sensor pin 3 connect to J1-. The custom symbol is named `Eltec Part:LMC6062` but its displayed value is OPA2196 and original production BOM selects TI OPA2196IDR/C2057972. Create a properly identified OPA2196 symbol; do not substitute the library-name LMC6062 when procuring parts.

The source resistor is part of the detector's operating circuit, not merely an arbitrary pull-down. Preserve 100k and choose a low-noise resistor until a documented analog change is approved. The original 500-ohm output resistors interact with the unbuffered ADS1256 switched-capacitor input; changing them or adding protection impedance changes loading and requires measurement/calibration review.

### AIN7 detector-battery divider

J1+ -> R2 300k -> U2.16/AIN7 -> R1 100k -> J1-. C1 100nF is across R1. No connection to the emitter battery exists. The unloaded ratio is 1/4, so 6.5 V gives 1.625 V; divider Thevenin resistance is 75k and ideal RC time constant 7.5 ms. These are arithmetic results from the actual netlist, not a verified ADC calibration.

The repository wiring note still discusses an older approximately 2:1 divider, an unused battery connection, and a proposed future AIN6 divider. Those descriptions conflict with this supplied PCB: AIN7 is actually wired through 300k/100k, while AIN6 is no-connect. Current firmware defines sensor/reference measurement on AIN0/AIN1 and does not automatically use the other two physical pairs. Do not present the three-pair PCB as a simultaneously supported three-station application without firmware work. Evidence: `Arduino/Eltec/Eltec.ino` MUX_SENSOR/MUX_REF definitions and `PIN,<n>` command implementation.

## Emitter regulator and three drive channels

U3 is AMS1117 adjustable, SOT-223: pin 3 input J2+, pin 2 output, pin 1 ADJ. Historical production BOM selects Advanced Monolithic Systems C6188. R15=220 ohms connects output to ADJ. RV1 is a 1k Bourns-3296W-style trimmer; its pins 1 and 2 (wiper) are tied to ADJ, pin 3 goes to emitter return. C6=22uF bypasses ADJ to return; C7=22uF bypasses output to return.

For effective trimmer resistance R between joined pins 1/2 and pin 3, the ideal setpoint is `VOUT = VREF*(1 + R/220) + IADJ*R`. With nominal VREF=1.25 V and typical IADJ=55uA, this is about **1.25-6.99 V**, before tolerances and dropout. The upper setpoint cannot be regulated from a 6.5 V battery. The exact C6188 manufacturer sheet specifies up to 10mA minimum load for guaranteed adjustment regulation; R15 supplies only 5.68mA when the emitters are off. [AMS1117 manufacturer sheet attached to C6188](https://datasheet.lcsc.com/datasheet/pdf/e6935943fc6b1bbf350a1a0f3e90dc4a.pdf?productCode=C6188)

**Confirmed misplaced connection:** `Net-(D1-K)` consists solely of D1.1 (cathode) and C5.2. D1.2 (anode) connects to regulator output. U3.3/J2.1 are on a different net with no C5/D1 connection. Thus C5 is not an input bypass capacitor, and D1 is not an output-to-input protection diode. Correct the topology or remove the unnecessary diode with engineering justification in the replacement. The drawing of an intended protection circuit does not override the exported connectivity.

Original C5/C6/C7 are 1206 MLCC footprints, all historical BOM C12891. The AMS sheet's all-condition stability guidance uses a 22uF solid tantalum output capacitor. The existing ceramic implementation has no stability evidence in the supplied files; select a regulator/capacitor combination with explicit manufacturer support in the redesign. This is an unresolved design qualification, not a claim that the built unit has been observed oscillating. [AMS1117 C6188 stability guidance, p4](https://datasheet.lcsc.com/datasheet/pdf/e6935943fc6b1bbf350a1a0f3e90dc4a.pdf?productCode=C6188)

| Channel | ESP32 -> opto LED | Opto output -> FET gate | FET and emitter |
| --- | --- | --- | --- |
| 1 | U1.24 GPIO33 -> R16 330 -> U4.1; U4.2 -> U1.17 | U4.16 collector at VOUT; U4.15 emitter -> R19 100 -> Q2.1; R20 100k from U4.15 to return | Q2.3 drain -> E1.2; Q2.2 source -> return; E1.1 -> VOUT |
| 2 | U1.23 GPIO25 -> R17 330 -> U4.3; U4.4 -> U1.17 | U4.14 collector at VOUT; U4.13 emitter -> R21 100 -> Q3.1; R22 100k from U4.13 to return | Q3.3 drain -> E2.2; Q3.2 source -> return; E2.1 -> VOUT |
| 3 | U1.22 GPIO26 -> R18 330 -> U4.5; U4.6 -> U1.17 | U4.12 collector at VOUT; U4.11 emitter -> R23 100 -> Q4.1; R24 100k from U4.11 to return | Q4.3 drain -> E3.2; Q4.2 source -> return; E3.1 -> VOUT |

U4 is **Lite-On LTV-847S C114599**, SMDIP-16, not the through-hole LTV-847 candidate in the earlier sourcing note. Its fourth channel pins 7/8/9/10 are no-connect. Q2/Q3/Q4 are AO3400A, gate=1, source=2, drain=3. The regulator output also supplies opto collectors, so the gate drive shrinks with the trimmer setting; very low VOUT does not guarantee a fully enhanced FET. Hardware must disable all three emitters during startup/shutdown regardless of current firmware's selected GPIO. Current firmware starts with GPIO33 and can switch one active gate pin via `PIN,25` or `PIN,26`; it is not a three-channel PWM controller. [Lite-On LTV-8x7 manufacturer sheet](https://optoelectronics.liteon.com/upload/download/DS-70-96-0016/LTV-8X7%20series%20%20Rev.S.PDF), [AO3400A manufacturer sheet](https://www.aosmd.com/res/data_sheets/AO3400A.pdf)

The schematic values E1/E2/E3 are IR-56, but the manufacturer and exact installed emitter revision are not established. The user supplied maximum emitter current of 200mA. Until clarified, a conservative three-channel power-path design must allow 200mA per channel and 600mA aggregate, plus regulator/control current. This is a design assumption, not a measured simultaneous load or an emitter-voltage requirement.

## All module pins

Pins in these tables are **carrier symbol/footprint pin numbers**, not bare ESP32/ADS1256 IC package pins. NC means the source schematic intentionally leaves that carrier pin unconnected.

### ESP32 U1, 30 positions

| Pin | Function | Other carrier connection |
| --- | --- | --- |
| 1 | 3V3 | U2.8 PDWN |
| 2 | GND | U2.2 digital/power ground |
| 3 | D15 | NC |
| 4 | D2 | NC |
| 5 | D4 | U2.6 DRDY |
| 6 | RX2 | NC |
| 7 | TX2 | NC |
| 8 | D5 | U2.7 CS |
| 9 | D18 | U2.3 SCLK |
| 10 | D19 | U2.5 DOUT |
| 11 | D21 | NC |
| 12 | RX0 | NC |
| 13 | TX0 | NC |
| 14 | D22 | NC |
| 15 | D23 | U2.4 DIN |
| 16 | VIN | U2.1 5V |
| 17 | GND | U4 LED cathodes 2/4/6 |
| 18 | D13 | NC |
| 19 | D12 | NC |
| 20 | D14 | NC |
| 21 | D27 | NC |
| 22 | D26 | R18.1, emitter 3 control |
| 23 | D25 | R17.2, emitter 2 control |
| 24 | D33 | R16.2, emitter 1 control |
| 25 | D32 | NC |
| 26 | D35 | NC |
| 27 | D34 | NC |
| 28 | VN | NC; defective duplicate physical pad, below |
| 29 | VP | NC |
| 30 | EN | NC on carrier |

U1 consists of two 1x15 rows at 2.54mm pitch, separated by 25.4mm. On the source PCB, pins 1..15 run from (86.08,87.00) toward decreasing X; pins 16..30 run from (86.08,112.40) toward decreasing X. The source contains a duplicate pin 28 at pin 27's position (58.14,112.40), in addition to its correct position (55.60,112.40). Remove the duplicate in the replacement and validate all 30 unique positions. Source pads have 1.00mm drill/2.00mm diameter.

### ADS1256 U2, 24 positions

| Pin | Function | Other carrier connection |
| --- | --- | --- |
| 1 | 5V | U1.16 VIN |
| 2 | GND | U1.2 GND |
| 3 | SCLK | U1.9 GPIO18 |
| 4 | DIN | U1.15 GPIO23 |
| 5 | DOUT | U1.10 GPIO19 |
| 6 | DRDY | U1.5 GPIO4 |
| 7 | CS | U1.8 GPIO5 |
| 8 | PDWN | U1.1 3V3 |
| 9 | AIN0 | Test1 via O1A/R3 |
| 10 | AIN1 | Ref1 via O1B/R4 |
| 11 | AIN2 | Test2 via O2A/R5 |
| 12 | AIN3 | Ref2 via O2B/R6 |
| 13 | AIN4 | Test3 via O3A/R7 |
| 14 | AIN5 | Ref3 via O3B/R8 |
| 15 | AIN6 | NC |
| 16 | AIN7 | R2/R1/C1 detector-battery divider |
| 17 | GND0 | Detector ground J1- |
| 18 | GND1 | Detector ground J1- |
| 19 | GND2 | Detector ground J1- |
| 20 | GND3 | Detector ground J1- |
| 21 | GND4 | Detector ground J1- |
| 22 | GND5 | Detector ground J1- |
| 23 | GND6 | NC on carrier |
| 24 | GND7 | Detector ground J1- |

U2 is a digital/power 1x8 row plus an analog 2x8 pattern, 2.54mm pitch. Global X for each 8-pin row is 53.21 + 2.54*n mm, n=0..7. Digital Y=80.975mm; AIN Y=23.825mm; analog ground Y=26.365mm. Digital-to-AIN separation is 57.15mm. The two analog rows are **not ordinary alternating odd/even symbol numbering**; a new standard 2x8 socket symbol requires explicit mapping to the old functional rows. Source pads use 1.00mm drill/2.00mm diameter. Photo identification `GY-220722-V1` does not disclose internal AVDD/DVDD/power-path wiring; verify the actual module.

## Detector/emitter connection geometry and pin sequence

The original footprint groups are soldered-wire attachment pads, not stocked three-pin connectors and not detector package sockets. Test1/Ref1/Test2/Ref2/Test3/Ref3 all use pin 1=V+, pin 2=OUTPUT, pin 3=GND/CASE. Pins 1/2 are 2.54mm circular plated pads with 1.00mm drills, spaced 5.08mm apart. Pin 3 is a separate **2.54 x 7.00mm surface pad without a drill**. For Test1 the global centers are pin1=(16,81.5025), pin2=(16,76.4225), pin3=(21.842,78.5815)mm. The other five copies have the same local topology; full placement coordinates are in the mechanical audit.

E1/E2/E3 use pin1=emitter positive and pin2=FET-switched return. They are 2.00mm diameter pads with 1.20mm drills, 2.54mm spacing. Pin2 is **not permanently common ground**. Replacing these pad groups with populated detachable connectors needs new exact connector parts and matching cable pinouts; do not order fictitious `Sensor`/`Emitter` components from the original generic footprint names.

The general Model 405 manufacturer drawing shows a 5.08mm pin circle, 0.43mm lead diameter and a **bottom view**: pin1 V+, pin2 OUTPUT, pin3 GND/CASE. This identifies the general detector's functions, but the carrier wire pads do not have that package geometry. No detector-body orientation can be approved by confusing the carrier top view with the detector bottom view.

## Detector supply and test-condition evidence

Three local PDFs were read with the PDF skill and all their pages were rendered and visually inspected. They are sources, not instructions overriding the user. Source pages remain untouched; temporary review images are under `reports/detector_pdf_review/`.

| Source | What it actually establishes | Limits of applicability |
| --- | --- | --- |
| `C:/Users/JoseCastelblanco/Downloads/405M22.pdf` | Three scanned TP412 pages, printed pages 2/3/4 of 5. Noise: +8V to detector test box, +/-8V to amplifier, 100k source resistor. Offset: **+8V +/-0.08V** detector supply, 100k source resistor. Both use 0.80-3.00V offset band. Sensitivity: 1Hz, 100k source resistor; no explicit rail listed on that page. | A model-specific **test condition**, not a full M22 absolute maximum/operating-range data sheet. Nominal 6.5V operation differs from the documented noise/offset test condition. |
| `C:/Users/JoseCastelblanco/Downloads/Data-Sheet-Model-405.pdf` | General Model 405, Form DS405 E-01/2011, two pages. Operating voltage 5-10V, typical characterization at V+=5V/Rs=100k. Recommends 100k-1M low-noise metal-film source resistor. Pin sequence 1 V+, 2 OUT, 3 CASE/GND in bottom view. | Does not separately specify 405M22 or override TP412's +8V test setup. |
| `C:/Users/JoseCastelblanco/Downloads/406MBN.pdf` | Two scanned TP383 pages for **406MBN**, printed p3 sensitivity and p2 noise. Noise uses +8V detector box and +/-8V amplifier with 100k Rs. Sensitivity supplies +/-5V to amplifier fixture 9000332. | **Not 406MCA.** No basis to transfer MBN specifications or infer detector rail from the sensitivity amplifier supply. |
| Repository `single_detector_rig/m449m18/README.md` | TP443 frequency-tracking implementation; explicitly says the offset page was unavailable. | No exact 449M18 supply-voltage rating found. |
| Repository `docs/CALIBRATION_RECORD.md`, wiring notes, model READMEs | Historical rig used 9V sensor battery; prior plan discussed ~8V to match TP412; application thresholds depend on fixture calibration. | User's current explicit two-6.5V-battery statement supersedes historical installed-power descriptions. These notes do not prove hardware calibration after that change. |

PDF SHA256 hashes:

- 405M22.pdf: `14FF86DEDDE1482FEDDFCE23053588FEF0526CB7CA7C48262B0298E2A248FE1F`.
- 406MBN.pdf: `A37FF4C79F01D6E15CC367927E1DDC87AD9539AA03F2002EADD7EB2842318865`.
- Data-Sheet-Model-405.pdf: `5F5771A74BFC361F4E8C3EB8F3378A791271891F7EEF6778AD8C009C2562D3E9`.

The public **standard Model 406** manufacturer page lists 3-15V operation, 100k source-resistor characterization and 0.3-1.2V offset; this is useful family context but does not establish the exact 406MCA variant. The current public standard 405 page agrees with the local general 405 voltage band. No exact public 449M18, 405M22 full rating sheet, or 406MCA full rating sheet was recovered in this audit. [Eltec standard Model 406](https://eltecinstruments.com/products/pyroelectric-detectors/model-406/), [Eltec standard Model 405](https://eltecinstruments.com/products/pyroelectric-detectors/model-405/)

**Design implication:** do not lower the detector supply to 5V simply to simplify ADC protection. Keep the user's current battery arrangement as the stated baseline while recording the TP412 mismatch. A design targeting formal +8V test matching from 6.5V batteries needs additional voltage conversion and low-noise validation; the supplied evidence does not authorize calling a different rail calibration-equivalent.

## Original electrical risks to resolve in the replacement

1. **Unsequenced live analog sources:** all six detector buffers and AIN7 remain driven from J1 when USB/ADC power is absent. The ADS1256 analog absolute maximum is AVDD+0.3V; 500-ohm series resistors do not guarantee an unpowered input stays inside it. At a hypothetical 6.5V railed buffer and zero ADC supply, a 0.3V clamp assumption gives 12.4mA, exceeding the IC's 10mA continuous input-current limit. This is a worst-case calculation, not a measured event. Protect every driven input, including the battery divider, under startup, shutdown and faults. [TI ADS1256 data sheet, p2](https://www.ti.com/lit/ds/symlink/ads1256.pdf)
2. **D1/C5 connectivity defect:** repair the isolated cathode/capacitor node described above. Ensure the actual regulator input has its specified local bypass.
3. **Emitter regulation limits:** provide guaranteed no-load regulation, supported output capacitor stability, an intentional voltage-adjustment range and battery/dropout/thermal limits. A 1A catalog label is not a 1A thermal guarantee for the small original copper area.
4. **Implicit ground joins and module supply internals:** new schematic must explain the intended return paths and controlled supply boundaries. Explicitly preserve emitter isolation if used. Verify which USB-derived carrier voltage is actually available and how ADC AVDD/DVDD are generated.
5. **Symbol/footprint integrity:** fix duplicate ESP32 pad28, barrel-jack MP/3 mismatch, inaccurate module/power pin electrical types and missing project libraries. Do not treat a missing library warning as an electrical circuit defect, but do make the new project self-contained.
6. **Calibration and input loading:** preserve 100k source resistors and document effects of any new series resistance, clamps, muxes, buffer supply change or RC filtering. Existing reference crosstalk observations in repository notes need investigation; they are not automatically fixed by adding power switches.
7. **Default emitter state:** hardware off-state enforcement must cover GPIO high-impedance/reset behavior and all three channels; software alone cannot guarantee USB-off sequencing.

## ERC findings, individually classified

| Finding | Count | Original items | Replacement action |
| --- | --- | --- | --- |
| Power input not driven | 3 errors | U3.3 input, U1.17 ground, U1.2 ground | Model external power sources and joins accurately; add power flags only to truly externally driven nets. A flag alone does not fix D1/C5 or sequencing. |
| Missing `Eltec Part` library | 14 warnings | E1/E2/E3, U1/U2, O1/O2/O3, Test1/2/3, Ref1/2/3 | Supply validated project-local libraries and correct part identities. |
| Bidirectional pin connected to power output | 1 warning | U2.8 PDWN to U1.1 3V3 | PDWN is an input at the ADS1256 IC; correct module symbol after actual module mapping verification. |

Original project ignored-check settings also list `single_global_label`, `four_way_junction`, `simulation_model_issue` and `footprint_filter`. The all-severity command reports exclusions but does not reverse globally ignored-check configuration. Therefore this is the original configured ERC result, **not a claim that every possible check was enabled**. The new project must justify its own settings and have no unexplained exclusions. This audit ran no new-board ERC/DRC and no bench tests.
