# Eltec ESP32 + ADS1256 wiring — LEGACY standalone 406MCA rig (firmware v1.9)

**This is not the unified bench rig.** It documents the original single-battery
fixture — 6 V SLA on the AIN7 divider, emitter gate on **GPIO25** — that the
retired standalone 406MCA v6/v6.1 applications ran on, with firmware **v1.9**
(this wiring was first written at firmware v1.7 and is unchanged through v1.9).
Keep it only for a legacy rig that still runs those applications (preserved at
git tag `archive/pre-cleanup-2026-08-28`); such a rig must stay on v1.9. The
unified rig (`single_detector_rig`, all models) uses
`ESP32_ADS1256_Wiring_v2_0.md` and firmware v3.x.

The older `ESP32_ADS1256_Wiring.docx` described the historical 9 V/AIN1/bare-MOSFET
arrangement, must not be used, and was removed from the repository on 2026-08-28
(preserved at the same tag).

## ESP32 to ADS1256

| ESP32 DevKit pin | GPIO | ADS1256 module | Purpose |
|---|---:|---|---|
| VIN | — | 5V | ADS1256 module power |
| GND | — | GND | Common ground |
| D18 | 18 | SCLK | SPI clock |
| D23 | 23 | DIN | SPI MOSI |
| D19 | 19 | DOUT | SPI MISO |
| D5 | 5 | CS | Chip select |
| D4 | 4 | DRDY | ADC data-ready interrupt |
| 3V3 | — | PDWN | Tie high; module has no separate RESET pin |

The production board is a 30-pin DOIT ESP32 DevKit V1 with a CP2102 USB bridge.
Firmware target: `esp32:esp32:esp32doit-devkit-v1`.

## Rig signals

| Source | Destination | Firmware use |
|---|---|---|
| Buffered DUT 406MCA output | ADS1256 AIN0, ground beside AIN0 | Sensor offset and chopped waveform, PGA x2 (±2.5 V) |
| Optional fixed reference 406MCA | ADS1256 AIN1, ground beside AIN1 | `REF?` and `STREAM,START,REF`, PGA x2 |
| 6 V SLA divider midpoint (99.7k upper/99.6k lower, measured) | ADS1256 AIN7, ground beside AIN7 | `BAT?`, PGA x1; v1.8 firmware scales by 2.001004 |
| ESP32 D25 | Dual-MOSFET module PWM/TRIG | Fixed 10 Hz, 50% emitter drive |
| 6 V SLA | MOSFET module DC+/DC- | Emitter power |
| Emitter | MOSFET module OUT+/OUT- | Chopped IR source |

GPIO25 connects directly to the installed dual-MOSFET trigger module because
that module has its own input conditioning. The old Word guide's 100 ohm series
resistor and 10k pulldown describe a different bare-MOSFET plan.

All grounds must be common: ESP32, ADS1256, sensor buffer, battery divider, and
MOSFET module.

## Digital PWM sync

Do not wire the old AIN2 loopback. The ESP32 includes its commanded PWM state as
the final `0` or `1` field in every streamed ADC record. The v5 application
checks that this digital state toggles before measuring.

This proves the firmware drive state, not a physical voltage at the MOSFET
terminal. The application also applies a signal-quality check to the captured
DUT waveform. A physical loopback can be added later if the fixture design
changes.

## Safety

- Never apply the 6 V battery directly to an ADS1256 input. It must pass through
  the divider.
- Never drive an ADS1256 analog input above AVDD (+5 V) or below ground.
- The current measured 99.7k/99.6k divider presents about 3.0–3.2 V on AIN7.
  A 100 nF capacitor is installed in parallel with the 99.6k lower resistor;
  it filters the divider tap and does not change the DC scale factor. AIN7 is
  near the buffered-input linear limit, so a full battery may read slightly low. A
  future divider change requires updating `BATTERY_DIVIDER_RATIO` in
  `Eltec.ino`.
- Disconnect power before changing fixture wiring.

## Bring-up on Xubuntu

1. Flash `Eltec.ino` v1.7 or newer with board **DOIT ESP32 DEVKIT V1**.
2. Close Arduino Serial Monitor; only one process can own the port.
3. Confirm the board and battery:

   ```bash
   cd Arduino/Eltec
   python3 esp32_rig_readout.py ports
   python3 esp32_rig_readout.py bat
   python3 esp32_rig_readout.py offset
   ```

4. Start the production app from **Eltec 406MCA ESP32 Tester** on the desktop.

The expected Linux port is `/dev/ttyUSB0`; auto-discovery validates the board's
`ELTEC-ESP32-ADS1256,v1.7` identity before using it. The signed-in user must be
in the `dialout` group.

## Current open items

- AIN1 is optional and is currently not connected; do not establish a reference
  baseline until it is wired.
- Sensitivity bands inherited from the old 9 V application must be qualified at
  6 V with known-good and known-bad sensors before production sign-off.
