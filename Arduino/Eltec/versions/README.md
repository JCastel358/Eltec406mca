# Eltec.ino firmware version archive

Frozen copies of every known `Arduino/Eltec/Eltec.ino` build, so a rig can be
put back on an older firmware without digging through git history.

`Arduino/Eltec/Eltec.ino` remains the **live, editable** sketch. Everything in
this folder is a read-only snapshot — do not develop here.

Each version lives in its own folder whose name matches its `.ino`, so the
Arduino IDE and `arduino-cli` can open/compile it directly with no renaming.

| Folder | IDN? string | Provenance | Front end | Notes |
| --- | --- | --- | --- | --- |
| `Eltec_v1_5` | `...,v1.5` | commit `6d5e14a` | PGA 2, buffer on | First committed rig sketch. `PIN,2` LED gate test. |
| `Eltec_v1_7` | `...,v1.7` | commit `1d2adf0` | PGA 2, buffer on | Verifies ADC config, reports stream overruns. |
| `Eltec_v1_8` | `...,v1.8` | commit `33faa76` (HEAD) | PGA 2, buffer on | Battery scaling uses the measured 99.7k/99.6k divider. |
| `Eltec_v1_9` | `...,v1.9` | **reconstructed** (see below) | PGA 2, buffer on | Adds `PWM,FREQ,<hz>` (0.1-20 Hz) + `pwm_hz` in `STATUS?`. **This is the firmware 406MCA v6/v6.1 rigs run.** |
| `Eltec_v2_0` | `...,v2.0` | working tree, 2026-08-12 | **PGA 1, buffer OFF** | 405 M22 / TP412 build: AIN0/AIN1 read linearly past 2.5 V for the 0.8-3.0 V offset band. |
| `Eltec_v2_1` | `...,v2.1` | working tree, 2026-08-12 | **PGA 1, buffer OFF at boot; runtime-switchable** | Adds `FE,V19`/`FE,V20`/`FE,GAIN`/`FE,BUF`/`FE?` so the v1.9 and v2.0 front ends can be A/B-compared (noise) without reflashing. Boots identical to v2.0; every reset (incl. port open) reverts to v2.0 behavior. |
| `Eltec_v2_2` | `...,v2.2` | working tree, 2026-08-17 | same as v2.1 | Adds `STREAM,START,BOTH`: AIN0+AIN1 interleaved by mux cycling, `P,<t0>,<v0>,<t1>,<v1>,<sync>` lines at **379 SPS per channel measured** (424 nominal). For the two-detector IR telescope, which now lives in its own workspace (`C:\Users\JoseCastelblanco\Documents\Eltec_IR_Telescope` — that workspace carries its own copy of this sketch). Purely additive — single-channel streaming is byte-identical, so it is a drop-in replacement for v2.1. **Flashed and verified on the bench rig 2026-08-17.** Reads each conversion before touching the mux; see the mux-cycling warning in `Arduino/Eltec/README.md` before changing that order. |
| `Eltec_v3_0` | `...,v3.0` | working tree, 2026-08-18 | same as v2.1 | **The unified test-rig baseline** — the firmware for `tech_app/eltec_rig` (one app, sensor version chosen in a dropdown). Functionally identical to v2.1: single-channel streaming, `PWM,FREQ`, runtime `FE,...` front-end switch; the telescope's dual-channel code is NOT in it. 405 M22 testing uses the boot-default v2.0 front end; 406MCA testing sends `FE,V19` after connect to restore the qualified gain-2 buffered front end. |
| `Eltec_v3_1` | `...,v3.1` | working tree, 2026-08-25 | same as v2.1 | v3.0 with the emitter gate moved from **D25 to D33** (`pinGate = 33`); the perf-board wire moved with it. Nothing else changed — `PIN,<n>` still retargets at runtime, so a board still wired to D25 runs this build if the host sends `PIN,25`. The unified app sends `PIN,33`. |
| `Eltec_v3_2` | `...,v3.2` | working tree, 2026-08-26 | same as v2.1 | **Current rig baseline.** v3.1 plus a runtime **duty cycle**: `PWM,DUTY,<pct>` (1–99 %, boot default 50 %) and `pwm_duty=<%>` in `STATUS?`. Added for the **449 M18 frequency-tracking test (TP443)**, whose legacy fixture chops with a 20/80 blade — the unified app's 449 M18 mode drives 5 Hz and 18 Hz at 20 % duty. Purely additive: nothing is sent for the other models, and a port open resets the board to 50 %, so 405 M22 / 406MCA behaviour is byte-identical to v3.1. Compiled 2026-08-26 (290 KB); **not yet flashed/verified on the bench**. |

## Which firmware belongs on which rig

- **The unified test rig running `tech_app/eltec_rig`: v3.2** (required by the
  449 M18 mode for `PWM,DUTY`; the 405 M22 and 406MCA modes also run on v3.1 —
  the emitter gate
  is on **D33** from 2026-08-25 — v3.0/v2.1/v2.2 also work because the app
  sends `PIN,33` itself after connect, but they boot driving D25; the 406MCA
  path needs the `FE,...` commands, so v2.0 and older are not enough). The app's 405 M22 mode uses the boot-default gain-1 unbuffered
  front end; its 406MCA mode sends `FE,V19` after every connect to restore
  the gain-2 buffered front end that model was qualified on.
- **Legacy standalone 406MCA rigs running the retired `v6_esp32` or
  `v6_1_esp32` apps (git tag `archive/pre-cleanup-2026-08-28`): v1.9.** Those
  apps never send `FE` commands, so on a
  v2.x/v3.x board they would measure on the wrong front end; v2.0 also halves
  the ADC resolution (LSB 298 nV -> 596 nV) and changes the noise floor.
- **IR telescope: v2.2**, now maintained in the separate
  `Eltec_IR_Telescope` workspace. Dual-channel streaming does not exist in
  any other build, and the telescope app refuses to start without it. v2.2 is
  otherwise identical to v2.1/v3.0, so the same board can run the 405 M22
  app without reflashing — but 406MCA testing on v2.2 still requires an app
  that sends `FE,V19` (i.e. the unified app, not legacy v6/v6.1).
- **Legacy standalone 405 M22 rig running the retired `405m22_esp32` app (git
  tag `archive/pre-cleanup-2026-08-28`): v2.0 through v3.0.** That app refuses
  to talk to a v1.9 board — the TP412 offset
  band goes to 3.0 V and the old buffered input hard-clipped at 2.5 V. It
  never sends `FE` commands, and opening the port resets the board to the
  v2.0 front end, so every v2.x/v3.x build behaves identically for it.

## Why v1.9 is reconstructed rather than extracted

v1.9 was never committed. `HEAD` (`33faa76`) holds v1.8 and the working tree
had already moved on to v2.0, so no v1.9 blob exists anywhere in the repository
— it is absent from `git log`, `git stash`, the reflog, and `git fsck`'s
dangling objects (the clone is from 2026-08-10 and has no earlier local
history).

It was therefore rebuilt from v2.0 by reverting exactly the four functional
v2.0 edits and their comments, keeping v1.9's `PWM,FREQ` feature:

| v2.0 | v1.9 (restored to v1.8's value) |
| --- | --- |
| `PGA_SENSOR = 0` (gain 1, +/-5 V) | `PGA_SENSOR = 1` (gain 2, +/-2.5 V) |
| `adsWriteReg(REG_STATUS, 0x00)` (buffer off) | `adsWriteReg(REG_STATUS, 0x02)` (buffer on) |
| `(status & 0x06) == 0x00` read-back | `(status & 0x06) == 0x02` read-back |
| `"ELTEC-ESP32-ADS1256,v2.0"` | `"ELTEC-ESP32-ADS1256,v1.9"` |

**The reconstruction is verified**: `diff Eltec_v1_8.ino Eltec_v1_9.ino`
contains *only* the `PWM,FREQ` feature (the new command, `pwmSetFrequency()`,
`pwmHalfPeriodUs`, the `pwm_hz` field in `STATUS?`, the version string, and
their documentation) and nothing else. Both sketches compile for
`esp32:esp32:esp32doit-devkit-v1`.

The one thing that cannot be guaranteed is the *comment wording* of the real
v1.9; the code is byte-equivalent to v1.8 + the PWM,FREQ feature.

## Flashing an archived version

Board: **DOIT ESP32 DEVKIT V1** (`esp32:esp32:esp32doit-devkit-v1`).

Easiest: `python3 Arduino/Eltec/flash_firmware.py --sketch versions/Eltec_v1_9`
(add `--list` to see the ports, `--check` to ask a board what it is running).
It finds `arduino-cli` and the port itself and confirms `IDN?` afterwards.

Arduino IDE: `File > Open` the folder's `.ino`, pick the board, pick the port,
upload. Note the board is a plain CP210x, so the IDE cannot identify it - the
port shows with no board name and **Tools > Board > esp32 > DOIT ESP32 DEVKIT
V1** has to be picked by hand.

`arduino-cli` (the Arduino IDE 2.x bundle ships one — on this Windows host it
is at `%LOCALAPPDATA%\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe`):

```bash
# put a 406MCA rig back on v1.9 (COM3 on Windows, /dev/ttyUSB0 on Xubuntu)
arduino-cli compile --fqbn esp32:esp32:esp32doit-devkit-v1 Arduino/Eltec/versions/Eltec_v1_9
arduino-cli upload -p COM3 --fqbn esp32:esp32:esp32doit-devkit-v1 Arduino/Eltec/versions/Eltec_v1_9
```

Always confirm the result with `IDN?` before running a batch — the whole point
of the version string is that stale firmware is detectable.

## Adding the next version

When `Arduino/Eltec/Eltec.ino` is bumped, snapshot the **outgoing** build here
first:

```bash
mkdir -p Arduino/Eltec/versions/Eltec_v2_2
cp Arduino/Eltec/Eltec.ino Arduino/Eltec/versions/Eltec_v2_2/Eltec_v2_2.ino
```

then add its row to the table above. Committing the live sketch on every
version bump keeps this from being needed again, but the snapshot is free.
