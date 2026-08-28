# Eltec ESP32 + ADS1256 wiring (firmware v2.0 — 405 M22 / TP412 fixture)

This is the wiring reference for the 405 M22 fixture running firmware
**v2.0** (ADS1256 sensor channels at PGA gain 1 with the input buffer OFF, so
the TP412 0.8-3.0 V offset band reads linearly). Rigs running the 406MCA
v6/v6.1 applications stay on firmware v1.9 and the older
`ESP32_ADS1256_Wiring_v1_7.md`. The historical `ESP32_ADS1256_Wiring.docx`
(9 V/AIN1/bare-MOSFET arrangement) must not be used.

## Power (batteries isolated 2026-08-12 — this fixed the emitter spike)

| Battery | Powers | Monitored? |
|---|---|---|
| 6.5 V battery | Emitters ONLY (through the MOSFET module DC+/DC-) | No |
| 9 V battery | Sensors: DUT buffer + AIN1 reference buffer supplies | No (TODO: AIN6) |

The two supplies share only ground. Neither battery is measurable on the
legacy AIN7 divider: a 9 V pack through the ~2:1 divider would sit near
4.5 V on the pin, and with the v2.0 unbuffered front end the resistive
divider is loaded anyway. The host applications therefore run with the
battery gate disabled ("Battery: not monitored").

**TODO (hardware):** measure the sensor battery on **AIN6** through a >=4:1
divider (e.g. 300k/100k -> 9.6 V taps at 2.4 V), then update the firmware
(new mux entry + ratio) and re-enable the host battery gate. The plan is to
step the sensor supply down to ~8 V (or use an 8 V battery) so noise
readings stay comparable to TP412's +8 V bench supply.

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

| Source | Destination | Firmware use (v2.0) |
|---|---|---|
| Buffered DUT 405 M22 output | ADS1256 AIN0, ground beside AIN0 | Offset, driven waveform, and emitter-off noise; PGA x1 (±5 V), buffer off |
| Fixed reference sensor | ADS1256 AIN1, ground beside AIN1 | `REF?` and `STREAM,START,REF`, PGA x1 |
| (legacy) 99.7k/99.6k divider tap | ADS1256 AIN7 | `BAT?` still answers but is inaccurate and unused on this fixture |
| ESP32 D33 | Dual-MOSFET module PWM/TRIG | 1 Hz, 50% emitter drive (`PWM,FREQ,1` sent by the 405 M22 app) |
| 6.5 V battery | MOSFET module DC+/DC- | Emitter power (emitters only) |
| Emitter | MOSFET module OUT+/OUT- | Chopped IR source |

GPIO33 connects directly to the installed dual-MOSFET trigger module because
that module has its own input conditioning. The gate moved D25 -> D33 on
2026-08-25 (firmware v3.1 boots on GPIO33); a board still wired to D25 works
if the host sends `PIN,25` after connect.

All grounds must be common: ESP32, ADS1256, sensor buffers (9 V side), and
MOSFET module (6.5 V side).

## Digital PWM sync

Do not wire the old AIN2 loopback. The ESP32 includes its commanded PWM state
as the final `0` or `1` field in every streamed ADC record. With the emitter
off (the TP412 noise capture) this field is a constant `0` by design.

## Safety

- Never drive an ADS1256 analog input above AVDD (+5 V) or below ground.
  With the input buffer OFF there is no AVDD-2 V linearity ceiling anymore,
  but the absolute AVDD limit still stands.
- The unbuffered inputs present a switched-capacitor load. AIN0/AIN1 are
  driven by low-impedance op-amp buffers, which is fine; high-impedance
  sources (like the legacy AIN7 resistive divider) read low — do not trust
  `BAT?` on this fixture.
- Disconnect power before changing fixture wiring.

## Bring-up on Xubuntu

1. Flash `Eltec.ino` **v2.0** with board **DOIT ESP32 DEVKIT V1**.
2. Close Arduino Serial Monitor; only one process can own the port.
3. Confirm the board and the DUT offset:

   ```bash
   cd Arduino/Eltec
   python3 esp32_rig_readout.py ports
   python3 esp32_rig_readout.py offset
   python3 live_waveform.py --freq 1        # SPACE toggles the emitter
   ```

4. Start the app from **Eltec 405 M22 ESP32 Tester** on the desktop and run
   **Calibrate reference unit** (mandatory after flashing v2.0 — older
   baselines are rejected by schema version).

The expected Linux port is `/dev/ttyUSB0`; auto-discovery validates the
board's `ELTEC-ESP32-ADS1256,v2.0` identity before using it. The signed-in
user must be in the `dialout` group.

## Current open items

- Sensor-battery monitoring on AIN6 (divider + firmware channel + host
  thresholds) — see the Power section.
- The TP412 sensitivity ranges gate only after the ~50-sensor comparison
  batch yields the fixture calibration factor; the 300 mV noise limit is
  provisional for the same reason.
