# ESP32 + ADS1256 Eltec Rig — Session Memory

> **Purpose of this file:** hand-off context for any Claude Code session working on
> this project. Read it first. **Keep it fresh** — whenever you change wiring,
> firmware behavior, the serial protocol, calibration numbers, or the plan, update
> the relevant section here in the same session so the next session isn't working
> from stale facts. Prune anything that becomes wrong; a short accurate file beats a
> long outdated one. Last updated: 2026-07-14.

---

## 1. What this project is

Replacing a **LabJack T7-Pro** with an **ESP32 + ADS1256 (24-bit ADC)** as the
data-acquisition hardware for the **Eltec 406MCA infrared emitter test rig**.

The rig tests 406MCA IR sensors: an ESP32-generated 10 Hz / 50% PWM chops a
black-body emitter, the 406MCA sensor sees the chopped IR and produces a small AC
signal riding on a DC offset, and the ADC reads that signal back to measure
**sensitivity** (mV pk-pk) and **polarity**.

The original Windows app that drove the LabJack lives at:
- `c:\Users\vma\Documents\Eltec406MCATester\tech_app\v4_emitter\eltec_406mca_emitter_tester.py` (GUI)
- `c:\Users\vma\Documents\Eltec406MCATester\tech_app\v1_single_sensor\eltec_406mca_tester.py` (measurement engine — pure numpy, ports untouched)

**Target host:** **Xubuntu**. The historical Windows/LabJack application remains
unchanged in `tech_app/v4_emitter`; the ESP32/Xubuntu edition lives separately
in `tech_app/v5_esp32`.

---

## 2. Current status (2026-07-14)

- ✅ **Separate v5 Xubuntu application implemented:**
  `tech_app/v5_esp32/eltec_406mca_esp32_tester.py`. It preserves the v4 guided
  batch workflow, CSV/snapshots, logo, simulator, live preview, Fast/Validation/
  Full modes, battery lockout, offset, sensitivity, polarity, and result flow,
  but uses the ESP32 backend. The historical v4 LabJack application was not
  modified. Results are separated under
  `~/Documents/Eltec_406MCA_Test_Results/v5_esp32/`.
- ✅ **Xubuntu packaging implemented and installed:** an XFCE Desktop launcher and
  Applications-menu entry use `assets/eltec_logo.png`; the shell launcher logs
  to `~/.local/state/eltec-406mca-esp32/launcher.log`.
- ✅ **Firmware v1.7 compiled and flashed on the connected board.** Linux sees
  the CP2102 as `/dev/ttyUSB0` and through its stable `/dev/serial/by-id/` link.
  The Xubuntu user is already in `dialout`.
- ✅ **Stream correctness fixed and verified.** Older firmware enabled ADS1256
  auto-calibration and immediately sent more register writes, so the DRATE write
  could be ignored and the ADC could remain at its 30 kSPS reset default while
  the protocol claimed 1000 SPS. v1.7 disables implicit ACAL, performs explicit
  calibration, obeys ADS1256 command timing, reads back all controlling
  registers, latches DRDY falling edges in an interrupt, and reports overruns.
  A live 1000-sample check returned 1000.0 Hz with matching counts, zero gaps,
  zero torn records, and zero overruns.
- ✅ **Non-recording v5 hardware workflow completed using digital PWM sync only:**
  battery 6.136 V, DUT offset 0.684 V, PWM 9.997 Hz, and a 36-cycle Fast capture.
  The connected setup measured 0.668 mV, positive polarity, and failed the
  inherited sensitivity band. This proves the end-to-end transport/workflow;
  it is not a calibration reference.
- ✅ The old LabJack physical **AIN2 loopback is intentionally not required**.
  Firmware tags every sample with its own digital GPIO25 PWM state, and v5
  verifies that state toggles before measuring.
- ⚠ **6 V calibration remains open.** The inherited sensitivity bands came from
  the old 9 V rig and must be qualified or replaced using known-good and
  known-bad sensors before v5 verdicts are production-authoritative.
- ⚠ **AIN1 reference sensor is still floating/not wired.** `REF?` and the CLI
  reference-emitter-health workflow remain unusable until it is connected and a
  6 V baseline is recorded. The v5 production GUI does not require AIN1.
- ✅ GPIO25 remains the confirmed emitter-gate pin. Opening or closing the CP210x
  serial port resets the board, so the CLI's `gate on`/`pwm on` commands keep the
  port open while a manual observation is made.

---

## 3. Files in this folder

| File | What it is |
|---|---|
| `Eltec.ino` | ESP32 firmware. PWM generation + ADS1256 driver + serial command protocol. |
| `esp32_rig_readout.py` | Host-side Python. Serial wrapper (`Esp32Rig` class) + CLI + light analysis. Runs on Windows and Linux. |
| `ESP32_ADS1256_Wiring_v1_7.md` | Current 6 V/AIN7/GPIO25 wiring and safety guide. |
| `ESP32_ADS1256_Wiring.docx` | Historical 9 V/AIN1/bare-MOSFET guide — do not use for v1.7. |
| `ESP32_memory.md` | This file. |
| `../../tech_app/v5_esp32/eltec_406mca_esp32_tester.py` | Xubuntu GUI, kept separate from historical v4. |
| `../../tech_app/v5_esp32/esp32_backend.py` | Strict ESP32 discovery/protocol/stream-integrity backend. |

---

## 4. Hardware

- **ESP32:** 30-pin DOIT ESP32 DevKit V1 (CP210x USB-serial bridge).
- **ADS1256:** common blue module. Header pins: `5V, GND, SCLK, DIN, DOUT, DRDY,
  CS, PDWN` and paired `GND/AIN0..AIN7`. **Note: this module has PDWN but NO RESET
  pin.** On-board 2.5 V reference.

### Wiring (8 wires board-to-board)

| ESP32 pin | GPIO | ADS1256 pin | Purpose |
|---|---|---|---|
| VIN | — | 5V | Module power — **5 V, not 3V3** |
| GND | — | GND | Common ground |
| D18 | 18 | SCLK | SPI clock |
| D23 | 23 | DIN | SPI data → ADC (MOSI) |
| D19 | 19 | DOUT | SPI data ← ADC (MISO) |
| D5 | 5 | CS | Chip select |
| D4 | 4 | DRDY | Data-ready flag (input) |
| 3V3 | — | PDWN | Tie high = chip always awake |

### Rig signal wiring

- DUT sensor buffer output → **AIN0** (ground to the GND pad beside AIN0). PGA ×2, ±2.5 V FS.
- **Reference sensor** (permanently mounted in the fixture) → **AIN1** (ground
  beside AIN1). PGA ×2, ±2.5 V FS. Trends emitter health — see §6 baseline.
- Battery divider midpoint (100k/100k, ÷2) → **AIN7** (ground beside AIN7). PGA ×1, ±5 V FS.
- **Rig battery: 6 V 4.5 Ah sealed lead-acid** (switched from 9 V PP3 on
  2026-07-10 — ~10× capacity, powers the MOSFET driver module + emitter).
  Fresh ~6.4 V → ~3.2 V at AIN7. ⚠ Emitter runs dimmer at 6 V than 9 V, so
  sensitivity numbers are NOT comparable with 9 V-era (LabJack) measurements —
  reference baseline and any healthy bands must be established at 6 V.
- **D25 (GPIO25)** → PWM/TRIG input of a **dual-MOSFET driver module** ("Dual
  High-Power MOSFET Trigger Switch Drive Module", DC 5–36 V, 15 A, trigger accepts
  3.3–20 V logic), **direct wire — no series resistor** (the module has onboard
  input conditioning; the 100 Ω/10 kΩ from the original bare-MOSFET plan are not
  used). Module DC+/DC− ← 6 V battery, OUT± → emitter, trigger GND common with
  ESP32 GND. (History: the plan said D25; a 2026-07-09 note claimed the perf
  board was soldered to D26 — that was wrong. It is on **D25**, confirmed
  2026-07-13; firmware + docs re-aligned.)
  **Trigger input internals** (ProtoSupplies teardown of this module family:
  two paralleled AOD4184A FETs, no optocoupler): TRIG → 100 Ω series → both
  gates, with a 10 kΩ pulldown and an indicator LED + 1.8 kΩ on the trigger
  net. That's only ~1 mA of load at 3.3 V — it can NOT drag a healthy GPIO to
  0 V, no series resistor needed, and the trigger LED should glow (dimly at
  3.3 V) from the gate signal alone, even with no battery on DC+.
  TODO: verify the module's trigger stays off during ESP32 boot (watch LED while
  pressing EN); if it glitches on, add a 10 kΩ trigger-to-GND pulldown.
- Old LabJack **AIN2 sync loopback is NOT used** — ESP32 knows its own PWM state
  and tags each sample with a digital sync bit.

### ⚠ Safety rules

- **ADS1256 inputs must never exceed +5 V or go below GND.** The LabJack tolerated
  ±10 V; the ADS1256 does not. The battery MUST go through the ÷2 divider — the
  6 V SLA (~6.4 V fresh) divides to ~3.2 V, comfortably inside range. (Even the
  old 9 V PP3 at ~9.6 V divided to a safe ~4.8 V.)
- Everything shares one ground.

---

## 5. Firmware (`Eltec.ino`) key facts

- Required production version: **v1.7 or newer**. The GUI and CLI reject older
  firmware because older streams can have a false sample rate or repeated data.
- Serial: **500000 baud**, ASCII, `\n`-terminated lines.
- **Boot heartbeat:** repeats `READY,...` (or `ERR,...`) every 2 s until the first
  command arrives, then goes quiet. This was added because the original one-shot
  banner was missed by a late-attaching serial monitor (looked dead but wasn't).
- PWM: software-timed 10 Hz / 50% on D25 via `micros()` in `loop()`. Drive level
  doubles as the sync bit.
- ADS1256: streams one channel at **1000 SPS** (chip's own clock, `DRATE=0xA1`) —
  AIN0 (DUT) by default, AIN1 (reference) via `STREAM,START,REF`. Mux constants:
  `MUX_SENSOR=0x08` (AIN0), `MUX_REF=0x18` (AIN1), `MUX_BATTERY=0x78` (AIN7),
  all vs AINCOM.
- v1.7 explicitly self-calibrates after PGA/channel setup, reads back STATUS,
  MUX, ADCON, and DRATE before declaring READY, and honors ADS1256 t6/t10/t11
  command timing. A GPIO4 falling-edge ISR latches each DRDY event. `STREAM,END`
  reports any ADC overrun; a nonzero value invalidates the capture.
- **Counts→volts** (the core conversion, `countsToVolts()`):
  ```
  volts = code * (2 * VREF / GAIN) / 8388607     (2^23 - 1, VREF = 2.5 V)
  ```
  Sensor GAIN=2 (±2.5 V FS), Battery GAIN=1 (±5 V FS). Battery volts then ×2.0
  (divider ratio) in firmware.

### Serial protocol

| Command | Reply |
|---|---|
| `IDN?` | `ELTEC-ESP32-ADS1256,v1.7` |
| `STATUS?` | `STATUS,pwm=<0\|1>,streaming=<0\|1>,vref=<V>,rate=<SPS>` |
| `PWM,ON` / `PWM,OFF` | `OK,PWM,ON` / `OK,PWM,OFF` |
| `GATE,ON` / `GATE,OFF` | hold the gate steady HIGH/LOW (bring-up/debug) |
| `GATE?` | `GATE,pin=<n>,drive=<0\|1>,read=<0\|1>` — pad readback; drive=1/read=0 ⇒ pin held low externally (short/overload/blown driver) |
| `PIN,<n>` | `OK,PIN,<n>` — retarget gate pin at runtime (2/12/13/14/25/26/27/32/33; 2 = onboard blue LED for visual tests), not persisted |
| `BAT?` | `BAT,<volts>` (median of 12, ×divider, from AIN7) |
| `OFFSET?` | `OFFSET,<volts>` (median of 24, ~3 ms apart, DUT on AIN0) |
| `REF?` | `REF,<volts>` (same median read, reference sensor on AIN1) |
| `STREAM,START` | `STREAM,BEGIN,1000,SENSOR` then `D,<t_us>,<raw_code>,<volts>,<sync>` per sample (AIN0) |
| `STREAM,START,REF` | `STREAM,BEGIN,1000,REF` — same format, reference sensor on AIN1 |
| `STREAM,STOP` | `STREAM,END,<count>,<adc_overruns>` (overruns must be zero) |
| (bad input) | `ERR,<message>` |

---

## 6. Host script (`esp32_rig_readout.py`) usage

```
python3 esp32_rig_readout.py test [-o run1.csv]   # FULL sequence — the main command
python3 esp32_rig_readout.py bat                  # battery only
python3 esp32_rig_readout.py offset               # sensor DC offset only
python3 esp32_rig_readout.py ref --set-baseline   # record known-good emitter baseline (do ONCE)
python3 esp32_rig_readout.py ref                  # emitter health check vs baseline
python3 esp32_rig_readout.py ref --dc             # quick AIN1 DC read (wiring checks)
python3 esp32_rig_readout.py pwm on|off           # manual emitter drive (holds port open until Enter)
python3 esp32_rig_readout.py gate on|off          # steady gate + pad readback (holds until Enter)
python3 esp32_rig_readout.py gate on --pin 2      # onboard-LED visual check of the gate path
python3 esp32_rig_readout.py stream -s 5 -o cap.csv  # raw capture, no warm-up
python3 esp32_rig_readout.py ports                # list serial ports
add --port COM3  (or --port /dev/ttyUSB0) to skip auto-detect
```

Ubuntu normally provides `python3` rather than a `python` command. Commands such
as `bat` are positional subcommands: use `...py bat`, not `-bat` or `--bat`.

`test` order (mirrors the app's Measure step): battery → offset (PWM off) →
PWM on → 5 s warm-up → reference-sensor check (4 s, AIN1, vs baseline) →
1000 Hz DUT capture → analyze (sensitivity, polarity, PWM freq).

### Reference baseline (emitter health)

Stored in `emitter_ref_baseline.json` next to the script (created by
`ref --set-baseline` with a known-good emitter). Every `ref` / `test` run
compares the reference sensor's pk-pk against it: drift ≥10% warns,
≥25% flags the emitter as suspect (`REF_DRIFT_WARN_PCT` / `REF_DRIFT_FAIL_PCT`
— initial guesses, tune with real data). **Not recorded yet.**

- Needs `pip install pyserial`. Analysis in the script uses only stdlib
  (`statistics`), so no numpy required for the CLI.
- Auto-detects the ESP32 by USB VID/PID (CP210x/CH340/CH9102/FTDI/native).
- **Linux "permission denied" on /dev/ttyUSB0:** `sudo usermod -a -G dialout $USER`,
  then log out/in.

### Healthy-value bands

- Battery (6 V SLA, set 2026-07-10): block below 5.8 V, warn below 6.0 V; fault
  if <3.0 V or >7.5 V. (The original app's 7.2/7.7/10.5 bands were for the 9 V PP3.)
- Sensor offset: healthy 0.3–1.2 V; "no sensor / floating" outside 0.05–2.5 V.

---

## 7. Known gotchas / troubleshooting

- **Serial monitor blank after upload:** the READY banner is now repeated every
  2 s, so this should be fixed. Set the line-ending dropdown to **"New Line"** to
  send commands; type `IDN?` + Enter to test.
- **`ERR,ADS1256 not responding`:** SPI wiring — most common is DOUT/DIN swapped
  (DOUT→D19, DIN→D23). Also check CS→D5, DRDY→D4, and that 5V really reaches the
  module.
- **Readings look like garbage even when wired right:** some module revisions don't
  tie AINCOM to ground internally. Fix: jumper a spare input to its neighboring GND
  pad and measure against it instead of AINCOM. **AIN7 is no longer spare** (battery
  divider lives there since 2026-07-09) — use **AIN6**: `MUX_SENSOR` 0x08→0x06,
  `MUX_REF` 0x18→0x16, `MUX_BATTERY` 0x78→0x76 in `Eltec.ino`, re-flash. **Not
  currently needed** — this board works as-is.
- **"pwm on / gate on didn't do anything":** two causes, both fixed. (1)
  2026-07-09: `Esp32Rig.close()` sent `PWM,OFF` unconditionally, cancelling
  `pwm on` on exit. (2) 2026-07-13, the real killer: **closing the COM port
  resets the ESP32** (verified experimentally; releasing DTR/RTS before close
  does not prevent it on this Windows/CP210x setup), so NO drive ever survives
  the CLI exiting — the old "leaves the PWM running after exit" claim was
  wrong. `gate on` / `pwm on` now hold the port open by default and prompt
  before turning the drive off; `--no-hold` exits immediately (with a warning
  that the drive will drop).
- **"D25 measures 0 V" (investigated 2026-07-13):** the chip-side drive is
  proven good (`GATE?` readback high under load, follows the 10 Hz PWM), so if
  a meter shows 0 V check, in order:
  0. No-meter sanity check: `PIN,2` + `GATE,ON` lights the onboard blue LED
     (firmware v1.7+) — proves flash, command path, and gate drive by eye.
     Also: the module's trigger-net LED should glow dimly on `GATE,ON` even
     with no battery — if it doesn't, the D25→module wire is suspect.
  1. Was the gate actually still on? **Opening OR closing the COM port resets
     the board** and silently drops the gate/PWM — this WAS the root cause of
     the original "0 V on D25" report. `python3 esp32_rig_readout.py gate on`
     now holds the port open and prompts before exiting: measure while the
     prompt is showing.
  2. Right pin? Older docs wrongly said D26 — the wire is on **D25**, 8th pin
     from the top on the EN side of the 30-pin board (…D32, D33, **D25**, D26,
     D27…).
  3. `gate on` now prints the `GATE?` pad readback. `read=1` while the meter
     says 0 V ⇒ wrong probe point or wrong ground reference.
  4. The readback cannot see an open circuit — also measure at the module's
     TRIG terminal to catch a broken solder joint / cracked wire between the
     perf board and the module.
- **`BAT?` reads ~0 V:** battery not connected, or the divider tap → AIN7 wire
  is off. With the module unpowered the emitter can't fire even though the PWM
  is running — "PWM looks dead" at the system level can really be "no battery".
- **Only one program can own the COM port** — close the Arduino Serial Monitor
  before running the Python script.
- **Wrong/multi-kHz stream rate:** v1.5 and the interim v1.6 build are unsafe.
  They could leave DRATE at the ADS1256's 30 kSPS reset value or consume a
  still-low DRDY repeatedly. Flash v1.7+. The v5 backend also rejects timestamp
  gaps, rate error, count mismatch, malformed records, and reported ADC overruns
  instead of saving a result.
- **Stream dropping samples:** close the serial monitor, avoid USB hubs, try a
  shorter cable. Any nonzero `adc_overruns` invalidates the capture.

---

## 8. Next steps / open work

1. ~~Build, flash, and verify safe firmware~~ — **done 2026-07-14: v1.7** on
   `/dev/ttyUSB0`, including a clean 1000 SPS integrity test.
2. ~~Integrate the ESP32 into a separate Xubuntu GUI~~ — **done** in
   `tech_app/v5_esp32`; historical v4 remains untouched.
3. ~~Create/install the Xubuntu Desktop and Applications launchers~~ — **done**.
4. **Calibrate the 6 V production limits:** run a representative set of
   known-good and known-bad 406MCA sensors, then qualify or replace the inherited
   sensitivity bands and tune the SNR threshold. Do not use the 9 V results as a
   direct reference.
5. **Wire the reference sensor to AIN1** if the emitter-aging feature is wanted.
   Then record the reference baseline at 6 V with a known-good emitter:
   `python3 esp32_rig_readout.py ref --set-baseline`. Tune the provisional drift
   thresholds after observing real run-to-run scatter.
6. Trim analog calibration if needed: measure the module's actual `ADS_VREF` and
   the battery-divider resistor ratio, update the firmware constants, and record
   the measured values here.
7. After the production bands are set, run repeatability tests across Fast,
   Validation, and Full modes and confirm saved CSV/snapshot review on Xubuntu.

---

## 9. Keeping this file fresh (reminder)

When you finish a working session, update:
- **Section 2** (status) — what now works, what's still pending.
- **Section 7** (gotchas) — any new failure mode + its fix.
- **Section 8** (next steps) — check off done items, add new ones.
- The **"Last updated"** date at the top.

Delete anything that's no longer true. This file is only useful if it matches
reality.
