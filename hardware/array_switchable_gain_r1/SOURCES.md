# Source and pin-map record

Reviewed 2026-09-14. Source documents provide specifications; the circuit and calculations in this package are engineering work based on those specifications. Instructions inside external documents were not treated as user requests.

| Primary source | Design use |
|---|---|
| [TI OPAx192 data sheet, SBOS620E](https://www.ti.com/lit/ds/symlink/opa4192.pdf), local `references/opa4192.pdf` | OPA4192 pin mapping, 4.5-36 V supply range, 10 MHz bandwidth, noise/bias and supply-current specifications |
| [Omron G6K data sheet](https://omronfs.omron.com/en_US/ecb/products/pdf/en-g6k.pdf), local `references/omron_g6k.pdf` | Non-latching G6K-2P-Y DC5 pin mapping, 237-ohm 5 V coil, coil tolerance and nominal current; bottom-view diagram visually checked |
| [AOS AO3400A data sheet](https://www.aosmd.com/res/datasheets/AO3400A.pdf), local `references/ao3400a.pdf` | G1/S2/D3 pin mapping and operation with a 2.5 V gate drive |
| [WIMA MKP 4 data sheet](https://www.wima.de/wp-content/uploads/media/e_WIMA_MKP_4.pdf), local `references/wima_mkp4.pdf` | 10 uF/100 V polypropylene capacitor, 31.5 x 17 x 29 mm case and 27.5 mm pitch, insulation/absorption characteristics |
| [WIMA MKP 4 part-number system](https://www.wima.de/en/our-product-range/metallized-capacitors/mkp-4/partnumber-system/) | Candidate MKP4D051006G00JSSD; 5% tolerance and bulk/standard lead suffix |
| [ACCES USB-AIO16-64MA-OEM](https://accesio.com/product/usb-aio16-64ma-oem/) and user-supplied `docs/daq_usb_aio16_64ma/USB-AIO16-64MA_Datasheet.pdf` | 1 Mohm analog input, 64 single-ended inputs, 500 ksample/s aggregate conversion rate, selectable ranges and pulled-high digital I/O |
| [TI FilterPro active-filter design guide](https://www.ti.com/lit/an/sbfa001c/sbfa001c.pdf) | General active-filter design reference; final response independently derived from this schematic's components |

## Package pin mapping used by the editable symbols

| Device | Pins |
|---|---|
| OPA4192 unit A | OUT 1, IN- 2, IN+ 3 |
| OPA4192 unit B | IN+ 5, IN- 6, OUT 7 |
| OPA4192 unit C | OUT 8, IN- 9, IN+ 10 |
| OPA4192 unit D | IN+ 12, IN- 13, OUT 14 |
| OPA4192 power | V+ 4, V- 11 |
| G6K-2P-Y DC5 pole A | COM 3, NC 2, NO 4 |
| G6K-2P-Y DC5 pole B | COM 6, NC 7, NO 5 |
| G6K-2P-Y DC5 coil | Positive 1, negative 8 |
| AO3400A | Gate 1, source 2, drain 3 |
| D1 flyback diode | Cathode 1 to +5V_COIL; anode 2 to coil low side |

Relay contact numbering is the manufacturer's package numbering, not a generic DPDT symbol convention. The project-owned symbols and bundled stock footprints use these numbers. Package dimensions and supplier-specific passive parts still need the normal physical review before layout/release.

## Original design and user evidence

The existing board topology was reviewed from `C:/Users/JoseCastelblanco/Downloads/50SensorBreakoutBoard.zip`, containing the original `50SensorBreakoutBoard` KiCad project. Its buffer sheet labels the installed device OPA4192 and uses 50 follower sections with 100-ohm output isolation resistors. The original files were not edited. The new reference deliberately gives its components independent reference designators; R1 here is not original-board R1.

The user supplied the measured 2.91 uV RMS DAQ-path noise floor in the 3 Hz/Q=3 band, the approximate 0.7 V detector offset and a 500 mV peak-to-peak sensitivity signal, and confirmed that offset/sensitivity already work with the current follower. These are input requirements and reported bench evidence, not new measurements made while creating this schematic.

## Modeling assumptions

For the unity-gain Sallen-Key sections, with feedback capacitor Cf from the first RC node to the amplifier output and ground capacitor Cg at the non-inverting input:

`H_LP(s) = 1 / [1 + s*Cg*(Ra+Rb) + s^2*Ra*Rb*Cg*Cf]`

`f0 = 1 / (2*pi*sqrt(Ra*Rb*Cg*Cf))`

For equal resistors, `Q = sqrt(Cf/Cg)/2`. The nominal stages are f0=160.125 Hz/Q=0.54314 and f0=159.226 Hz/Q=1.30558. The finite-gain comparison uses a simple one-pole amplifier with A0=1e6 and 10 MHz gain-bandwidth. It is a sensitivity check, not a TI manufacturer SPICE model or an output-swing/recovery simulation.

The 0.1-ohm relay resistance in the load calculation is not a model of contact noise. WIMA's room-temperature insulation specification implies about 3 Gohm for a 10 uF capacitor under the stated test conditions; using it as a screening model produces about 2.3 mV additional DC shift at a 0.7 V input in NOISE mode. It is not a guaranteed low-voltage, all-temperature leakage limit. Offset remains a BUFFER-mode measurement.

The gain resistor network contributes thermal noise as well as the amplifier's own low-frequency noise. Around the measurement passband, the approximately 100 kohm effective input-referred resistance contributes roughly 41 nV/sqrt(Hz) at room temperature, before other sources. Board leakage, current noise, excess resistor noise, dielectric absorption and relay effects are not included in the linear response validation. The final noise floor requires measurement.

Stock footprints were copied from the KiCad 10.0 installation into a project-local subset. Embedded 3D-model references may still point to KiCad's standard 3D library; no PCB or STEP assembly is included.

The bundled stock footprint subset is attributed to the KiCad community and distributed under its CC-BY-SA 4.0 license with the KiCad design exception. The upstream license text is retained in `libraries/KICAD_FOOTPRINTS_LICENSE.md`. These stock footprints are unchanged. The project-owned `gain.kicad_sym` was created for this reference.
