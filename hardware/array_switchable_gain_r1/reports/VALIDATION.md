# Schematic validation

PASSED: native ERC, topology and stated linear calculations

- Native KiCad ERC: **0 violations, 0 ignored checks, 0 exclusions**.
- Exported-connectivity and numerical checks: **31 passed**.
- Component endpoint combinations evaluated: **4096**.
- No PCB exists in this package; PCB DRC and layout validation are not applicable.

## Calculated response

| Hz | 1X mode | Noise gain | Relative to 100X |
|---:|---:|---:|---:|
| 0.85 | 0.999901 | 98.3248 | -0.147 dB |
| 3 | 0.999901 | 99.8629 | -0.012 dB |
| 5 | 0.999901 | 99.9522 | -0.004 dB |
| 18 | 0.999901 | 100.0207 | 0.002 dB |
| 50 | 0.999901 | 100.1709 | 0.015 dB |
| 100 | 0.999901 | 99.3411 | -0.057 dB |
| 160 | 0.999901 | 70.6174 | -3.022 dB |
| 200 | 0.999901 | 37.6486 | -8.485 dB |
| 500 | 0.999901 | 1.0403 | -39.657 dB |
| 1000 | 0.999901 | 0.0650 | -63.740 dB |

## Component tolerance endpoints

R1-R6 and R10-R13: +/-0.1%; filter capacitors +/-1%; C1 +/-5%. The 100-ohm output load correction is nominal. Correlated resistor endpoints conservatively bound the sum Rf. These are component endpoints, not measured production limits.

| Hz | Minimum gain | Maximum gain |
|---:|---:|---:|
| 3 | 99.6491 | 100.0752 |
| 50 | 99.5192 | 100.8250 |
| 100 | 97.8791 | 100.8221 |
| 160 | 68.8938 | 72.3407 |
| 200 | 36.1776 | 39.1749 |
| 500 | 0.9937 | 1.0894 |
| 1000 | 0.0621 | 0.0681 |

## Model limits

- Linear model: not manufacturer SPICE, no saturation/recovery model.
- No claim of measured noise floor, dielectric absorption recovery, relay dry-contact noise or cable stability.
- C1 has a candidate case/footprint, not a sourcing or mechanical qualification. SW1 is a wired panel switch. Passive supplier selections and signal-capacitor footprints require procurement review. No PCB/DRC/fabrication outputs.
- DAQ loading modeled as datasheet 1 Mohm; ADC sampling kickback and cable parasitics require bench verification.
