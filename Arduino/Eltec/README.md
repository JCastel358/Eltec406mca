# ESP32 + ADS1256 rig firmware

The firmware that turns a 30-pin DOIT ESP32 DevKit V1 and a blue ADS1256
module into the sensor rig's data-acquisition front end: a software-timed PWM
emitter gate, a 1000 SPS single-channel ADC stream tagged with the PWM state,
and an ASCII serial command protocol at 500000 baud. One build serves every
sensor model; the host application selects what it needs at runtime.

## Files

| File | What it is |
| --- | --- |
| `Eltec.ino` | The live, editable sketch — **v3.2**. Its header comment is the authoritative command reference and version history. Byte-identical to the newest snapshot in `versions/`. |
| `versions/` + [`versions/README.md`](versions/README.md) | Frozen copy of every released build (v1.5 … v3.2), provenance, **which firmware belongs on which rig**, revert commands, the v1.9 reconstruction story. Read-only. |
| `flash_firmware.py` | One-command compile + upload + verify. Finds the Arduino IDE's bundled `arduino-cli`, auto-detects the CP210x port, confirms `IDN?` and `GATE?` afterwards. |
| `run_flash_firmware.cmd` / `.sh` | Double-click wrappers for the flasher (Windows / Xubuntu). |
| `esp32_rig_readout.py` | Host-side serial wrapper (`Esp32Rig`) + CLI for bench checks (`ports`, `offset`, `ref`, `pwm`, `gate`, `stream`, `test`, `noisecmp`). |
| `live_waveform.py` | Rolling matplotlib scope of one ADC channel with the sync bit, link-health stats, an emitter toggle (SPACE), a live time window (`]` / `[`, 0.25–60 s, ceiling set by `--max-window`) and `--freq` / `--duty` drive selection (`--duty` needs v3.2). |
| `ESP32_ADS1256_Wiring_v2_0.md` | **Current wiring** of the unified bench rig (two isolated batteries, GPIO33 gate). |
| `ESP32_ADS1256_Wiring_legacy_v1_9.md` | Wiring of the retired standalone 406MCA rigs (single 6 V SLA on AIN7, GPIO25 gate, firmware v1.9). Keep only for such a rig. Firmware header comments up to v3.2 still call it by its old name `ESP32_ADS1256_Wiring_v1_7.md` (renamed 2026-08-28; snapshots are never edited). |

## Which firmware goes on the board

- **Unified bench rig (`single_detector_rig`, all models): v3.2.** The bench
  board currently runs **v3.1** — v3.2 (adds `PWM,DUTY`) is compiled but not
  yet flashed or bench-verified; the 449 M18 mode refuses anything older than
  v3.2, the 405 M22 and 406 MCA modes also run on v2.1–v3.1.
- **Legacy standalone 406MCA rig (retired v6/v6.1 apps): v1.9 only.** Those
  apps were qualified on the gain-2 buffered front end and never send `FE`
  commands; v2.0+ halves the ADC resolution and changes the noise floor.
- **IR telescope: v2.2** (separate `Eltec_IR_Telescope` workspace; the only
  build with dual-channel `STREAM,START,BOTH`). The same board is reflashed
  between telescope sessions and rig use.

Details and the full table: [`versions/README.md`](versions/README.md).

## Build and flash

```bash
python Arduino/Eltec/flash_firmware.py --check      # what does the board run? (flashes nothing)
python Arduino/Eltec/flash_firmware.py              # compile Eltec.ino, upload, verify IDN?/GATE?
python Arduino/Eltec/flash_firmware.py --list       # serial ports
python Arduino/Eltec/flash_firmware.py --port COM7  # explicit port
python Arduino/Eltec/flash_firmware.py --sketch versions/Eltec_v1_9   # put a board back on an archived build
```

Windows: double-click `run_flash_firmware.cmd`. Xubuntu: `run_flash_firmware.sh`.
Board: **DOIT ESP32 DEVKIT V1** (`esp32:esp32:esp32doit-devkit-v1`), CP210x
bridge (COM3 on the bench laptop, `/dev/ttyUSB0` on Xubuntu — user in
`dialout`). Manual equivalent:

```bash
arduino-cli compile --fqbn esp32:esp32:esp32doit-devkit-v1 Arduino/Eltec
arduino-cli upload -p COM3 --fqbn esp32:esp32:esp32doit-devkit-v1 Arduino/Eltec
# then IDN? must answer ELTEC-ESP32-ADS1256,v3.2
```

In the Arduino IDE the board is not auto-identified (plain CP210x): pick
**Tools > Board > esp32 > DOIT ESP32 DEVKIT V1** by hand. Close the Serial
Monitor before running any Python tool — only one program can own the port.

**Version bump procedure:** edit `Eltec.ino` → bump the `IDN?` string (every
flash-relevant change) → compile/flash/verify → snapshot into
`versions/Eltec_vX_Y/Eltec_vX_Y.ino` → add the row in `versions/README.md` →
update `REQUIRED_FIRMWARE` in `single_detector_rig/sensor_versions.py` →
CHANGELOG entry → **commit the same day** (v1.9 had to be reconstructed because
the sketch was edited twice without a commit).

## Serial protocol (v3.2)

Each command and reply is one `\n`-terminated ASCII line at 500000 baud. On
boot the board repeats `READY,ELTEC-ESP32-ADS1256` every 2 s until the first
command. **Opening or closing the port resets the board** (DTR), so nothing —
PWM, gate pin, duty, front end — survives a port close; hosts re-program what
they need after every connect.

| Command | Reply | Notes |
| --- | --- | --- |
| `IDN?` | `ELTEC-ESP32-ADS1256,v3.2` | Version check — every host refuses builds older than it needs. |
| `STATUS?` | `STATUS,pwm=<0\|1>,streaming=<0\|1>,vref=<V>,rate=<SPS>,pwm_hz=<Hz>,pwm_duty=<%>` | |
| `PWM,ON` / `PWM,OFF` | `OK,PWM,ON` / `OK,PWM,OFF` | Emitter drive at the current frequency/duty. |
| `PWM,FREQ,<hz>` | `OK,PWM,FREQ,<hz>` | v1.9+. 0.1–20 Hz, immediate, not persisted (boots 10 Hz). |
| `PWM,DUTY,<pct>` | `OK,PWM,DUTY,<pct>` | v3.2+. 1–99 % ON, not persisted (boots 50 %). The streamed sync bit follows the real ON/OFF state. |
| `PIN,<n>` | `OK,PIN,<n>` | Retarget the gate pin: 2/12/13/14/25/26/27/32/33 (2 = onboard LED for a meter-free check). Not persisted; boot default **33**. |
| `GATE,ON` / `GATE,OFF` | `OK,GATE,…` | Hold the gate steady (bring-up). |
| `GATE?` | `GATE,pin=<n>,drive=<0\|1>,read=<0\|1>` | Pad read-back; `drive=1,read=0` ⇒ pin held low externally (short / blown driver). |
| `FE?` | `FE,gain=<1\|2>,buf=<0\|1>,fs=<V>` | v2.1+. Current ADS1256 sensor front end. |
| `FE,V20` / `FE,V19` | `OK,FE,gain=1,buf=0` / `OK,FE,gain=2,buf=1` | Boot default = V20 (gain 1, buffer off, ±5 V — 405/449). V19 = the 406MCA-qualified front end (gain 2, buffer on, ±2.5 V, linear only to 3.0 V). `FE,GAIN,<1\|2>` / `FE,BUF,<0\|1>` change one thing. Rejected while streaming; SELFCAL + read-back before OK; not persisted. |
| `OFFSET?` / `REF?` | `OFFSET,<V>` / `REF,<V>` | Median of 24 reads ~3 ms apart on AIN0 / AIN1. |
| `BAT?` | `BAT,<V>` | AIN7 divider × ratio. **Inaccurate and unused on the current fixture** (no battery on AIN7; unbuffered input loads the divider). |
| `STREAM,START` / `STREAM,START,REF` | `STREAM,BEGIN,1000,SENSOR\|REF` then `D,<t_us>,<raw_code>,<volts>,<sync>` per sample | AIN0 / AIN1 at 1000 SPS. |
| `STREAM,STOP` | `STREAM,END,<count>,<adc_overruns>` | Overruns must be zero for a valid capture. |
| anything invalid | `ERR,<message>` | |

Counts → volts: `volts = code × (2·VREF/PGA) / 8388607` with VREF = 2.5 V
(PGA 1 → ±5 V, LSB ≈ 0.6 µV; PGA 2 → ±2.5 V). Every streamed line carries both
the raw code and the converted volts so the host can verify the math.

## Firmware facts worth knowing before editing

- v1.7 fixed a silent failure: earlier builds could leave the ADS1256 at its
  30 kSPS reset default while claiming 1000 SPS. The sketch now disables
  implicit auto-calibration, self-calibrates explicitly, honours ADS1256
  command timing, reads back STATUS/MUX/ADCON/DRATE before `READY`, latches
  DRDY falling edges in an ISR, and reports overruns at `STREAM,END`.
- The PWM is software-timed in `loop()` via `micros()`; the drive level
  doubles as the sync bit.
- Mux constants: `MUX_SENSOR = 0x08` (AIN0), `MUX_REF = 0x18` (AIN1),
  `MUX_BATTERY = 0x78` (AIN7), all vs AINCOM. Some module revisions do not tie
  AINCOM to ground — the fix is to measure against a neighbouring GND pad via a
  spare input (AIN6; AIN7 is taken). Not needed on the bench board.
- **Mux cycling (only if dual-channel streaming is ever re-added): read the
  conversion BEFORE touching the mux.** The throughput-optimised order
  (`WREG MUX` → `SYNC` → `WAKEUP` → `RDATA`) is wrong on this board — `SYNC`
  restarts the converter and the output register is not safe to read across
  it; it produced 4.8 V single-sample jumps and full-scale hits in v2.2's first
  build. Reading first costs ~50 µs per pair (397 → 379 SPS) and removes the
  corruption completely. Do not "optimise" it back. (v2.2 lives in the
  telescope workspace; this build has no dual-channel code.)
- GPIO33 is RTC-capable but not a DAC pin; the sketch releases the RTC hold
  latch at attach. GPIO25/26 (the old gate pin) are DAC pins and got a DAC
  detach as well.
- Never feed an ADS1256 input more than AVDD (+5 V) or below ground.

## Troubleshooting (bench-proven)

| Symptom | Cause / fix |
| --- | --- |
| Serial monitor blank after upload | The READY banner repeats every 2 s now; set line ending to "New Line" and send `IDN?`. |
| `ERR,ADS1256 not responding` | SPI wiring: DOUT/DIN swapped is the classic (DOUT→D19, DIN→D23); also CS→D5, DRDY→D4, and 5 V (not 3V3) to the module. |
| `pwm on` / `gate on` "did nothing" | The port close reset the board and dropped the drive. `esp32_rig_readout.py gate on` / `pwm on` hold the port open and prompt before exiting — measure while the prompt shows. |
| Meter shows 0 V on the gate pin | `PIN,2` + `GATE,ON` lights the onboard LED (proves flash + command path + drive). `GATE?` `read=1` while the meter says 0 V ⇒ wrong probe point or ground. The read-back cannot see an open wire — also probe at the module's TRIG terminal. Check the wire is on **D33** (moved from D25 on 2026-08-25). |
| Stream drops / gaps / duplicates on Windows | Host-side: the CP210x driver grants only a 512-byte receive queue and Windows on battery throttles a backgrounded GUI. The app backends block in the driver read, opt out of power throttling and attribute `CE_RXOVER`. Keep the window visible, use AC power, no USB hubs; USB selective suspend is disabled in the power plan. Any nonzero `adc_overruns` invalidates a capture regardless. |
| Stream goes silent mid-capture ("stream stalled … `[tag]`", 2026-09-03) | The app stops the stream and reads `STREAM,END`: `[board-reset]` = the counter restarted (the board rebooted; the ROM banner arrives garbled at 500000 baud, so the reason is not shown - only READY is legible), `[board-silent]` = same count as the host (DRDY stopped), `[host-stall]` = firmware count ahead of the host (the computer stopped reading), `[no-reply]` = no answer. The app restarts the capture by itself; the tag is in the batch's `_attempts.csv`. |
| `BAT?` reads ~0 V or nonsense | Expected on the current fixture (no battery on AIN7). |
| Multi-kHz or wrong stream rate | Firmware older than v1.7 — reflash. |

## Adding a new channel or command

Extend `Eltec.ino` with a runtime command the host sends after connect (the
way `PWM,FREQ`, `FE,...` and `PWM,DUTY` were added) — do not fork per model.
The pending AIN6 sensor-battery channel is the next expected addition
(`docs/ENGINEER_HANDOVER.md` §10).
