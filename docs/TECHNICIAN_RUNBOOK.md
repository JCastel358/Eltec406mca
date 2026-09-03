# Technician runbook — Eltec sensor test rigs

How to test a batch of sensors on the ESP32 bench rig (one part at a time,
§1–§3) or a tray of fifty on the DAQ array rig (§3b). No programming
knowledge needed. If something on this page does not match what you see on
screen, stop and tell the rig engineer — do not improvise.

---

## 1. Before you start (2 minutes)

| Check | What to look for |
| --- | --- |
| Laptop | Plugged into **AC power** (on battery Windows slows the app down and captures fail with "timestamp gaps"). |
| ESP32 board | USB cable from the rig's ESP32 to the laptop. Only one program may use it — close the Arduino Serial Monitor or any other rig tool. |
| Emitter battery (6.5 V) | Connected to the MOSFET module. Powers the emitters only. |
| Sensor battery (9 V) | Connected to the sensor buffer board. Powers the sensors only. The app shows **"Battery: not monitored"** — that is normal, neither battery is measured. |
| Fixture | Reference sensor seated in its permanent slot; test slot empty and clean. |
| Environment | **No fans** blowing on or near the fixture, no vibration, laptop charger away from the USB cable if possible. The sensors are microphonic — a desk fan shows up as noise. |

## 2. Start the app

Two icons: **Eltec Test Rig** = one part at a time on the ESP32 rig (this
section and §3); **Eltec Array Rig** = fifty at a time on the DAQ rig (§3b).
They can run at the same time.

- Double-click the **Eltec Test Rig** desktop icon, or
  `single_detector_rig\run_eltec_rig_tester.cmd` (Windows) /
  `single_detector_rig/run_eltec_rig_tester.sh` (Xubuntu).
- The selector opens full screen. Pick the **sensor version** in the dropdown
  and press **Start tester**. The last choice is remembered.

| Dropdown entry | Use for | Status |
| --- | --- | --- |
| Model 405 M22 (1 Hz, TP412) | 405 M22 parts | production |
| Model 406 MCA (10 Hz) | 406 MCA parts | production |
| Model 449 M18 (5 Hz + 18 Hz, TP443) | 449 M18 parts | **calibration pending** — every result says `CALIBRATION PENDING`; readings are recorded but limits are not enforced yet |

Only one tester can run at a time (they share the board). Close it to return
to the selector.

If the tester refuses to connect with a **firmware** message (for example
"requires v3.2"), the board needs reflashing — call the engineer.

## 3. Run a batch

The screens are the same for every model: **Batch information → Measuring →
Result**. Enter moves forward. There is no screen asking you to load a
sensor: load it, then press Start (first part) or Next (every part after).

### Batch information

- **Batch / lot number** — a new number starts a batch; an existing number
  resumes it at the next sensor.
- **Tester name** — your name (goes on every row).
- **Filter setup** — the optical filter fitted to the fixture for this batch
  (405 M22: -625 / -628 / -629; 406 MCA: the -284 default unless told
  otherwise). Wrong filter = wrong limits.

### Sensor numbers: a number is only used up by a PASS

The number on screen (`500-7`) is the number the part will ship under, so it
is only spent when a part **passes**. If `500-7` fails, put it aside, load
the next part, press **Next** — it is `500-7` again. Whichever part finally
passes is `500-7`; only then does the app move to `500-8`. The failures are
still recorded (they count in the yield), they just do not take a number.

- Seat the first part in the test slot before you press **Start** — it is
  read straight away.
- 405 M22 only: the **noise soak** toggle (on the setup card, and on the
  result card for the part Next will read). Leave it off for normal parts;
  switch it on for a part you suspect of intermittent noise or that has a
  noise history. It resets after every sensor.

### Measuring — what happens and how long it takes

| Model | Steps on the progress bar | Typical time | Keep in mind |
| --- | --- | --- | --- |
| 405 M22 | 1 offset → 2 noise (emitter off) → 3 sensitivity (1 Hz) → settled offset re-read | 1–2 min (up to 3 attempts of 60 s if the part is slow to stabilise) | Do not touch the fixture during the noise step. |
| 406 MCA | offset → 10 Hz capture → settled offset re-read | 15–60 s (up to 3 attempts of 20 s), plus up to 20 s more if the offset is still settling | If the status line says the part is being held while its offset settles, leave it alone — it is being given a chance to pass. |
| 449 M18 | offset → 5 Hz capture → 18 Hz capture → settled offset | 1–2 min | Both drives run back to back per part. |

**Keep the app window visible and the laptop awake while it measures.** A
minimised window on battery power is the most common cause of a "stream
integrity" error.

### Result

The banner tells you the verdict:

| Banner | Meaning | What to do |
| --- | --- | --- |
| **PASS** (green) | Good part | Save. |
| **PASS · NEAR LIMIT** (green banner, amber card) | Passed, but sensitivity is within the measurement margin of the limit | The part **passes** and keeps its number. No quarantine. |
| **PASS · CALIBRATION PENDING** (449 M18) | Normal for this model until its calibration is done | Save; the numbers are recorded. |
| **PASS** with an amber **OFFSET WAS STILL SETTLING** card (406 MCA) | Passed, but the offset had not stopped moving when it was recorded | The part **passes** and keeps its number. Re-measure only if that number matters to you. |
| **FAIL** (red) | Bad part | The failure mode is preselected (HO high offset, LO low offset, LS low sensitivity, RP reversed polarity, N noisy, Unstable, SB sensor bad, FT frequency tracking). Confirm or correct it, add a comment if useful, save. |
| Amber **TP443 SPEC 4 — MEASURE THE WHOLE TRAY** (449 M18) | Sampling instruction, not a verdict | Test every part of that tray. |
| Red card **"nothing was recorded"** | The rig, not the part, had a problem | Press **Next** to read it again (nothing was written, so the number has not moved). If it keeps failing, **Record as NOT MEASURED** (the row says why) and tell the engineer. |
| Dialog **"Is a sensor loaded?"** | The input reads like an empty slot | If a part really is loaded, answer **Yes** — it is recorded as FAIL / SB (shorted or dead). If the slot is empty or the part is badly seated, answer **No**, reseat, measure again. |

Optional on the result screen: **Show test details** (numbers), **Show
waveform**, **Comment**, **Capture waveform** (saves a picture), 405 M22
**Save noise capture** (keeps the raw noise recording for the engineer).

### The buttons at the bottom

There are two: `Stop` (red, left) and `Next` (green, right).

- **Next (Enter)** — reads the sensor that is in the rig **now**. If a
  verdict is on screen it records that row first. So the rhythm is: read the
  banner → swap the part in the fixture → press Next. The number that comes
  back is the next one if the part passed, and the **same one** if it did
  not.
- **Stop (Esc)** — press it at any time, including in the middle of a
  measurement. During a measurement it stops the reading immediately:
  nothing is recorded and the number does not move, so press Next when you
  are ready to read that part again. With no measurement running it ends the
  batch and shows the summary; a verdict that is still on screen is written
  first, without asking.

There is no Re-measure button: a reading you do not like is saved as what it
says (on a FAIL that costs nothing — the number stays open and you test the
part again), or stopped with **Stop** before it finishes so nothing is
written at all.

## 3b. Run a tray on the array rig (model 40623)

The array rig tests **offset and noise only** (no sensitivity yet — that step
is greyed out). Every result is marked **CALIBRATION PENDING**: offsets are
judged with provisional limits, noise is **measured and recorded but not yet
failed** (there is no limit for this rig yet). Red tiles are real failures.

1. Power the array PCB, plug the DAQ's USB in, wait five seconds (it loads
   its own program on plug-in). Double-click **Eltec Array Rig**, pick
   **Model 40623 array**, press **Start tester**.
2. **Lot** — lot number, tray number, your name → **Start lot (connect DAQ)**.
3. **Load & offset** — load the parts (tab orientation as on the legacy
   fixture). The grid shows every position's offset live:
   - **red "HO – pull"** = high offset. Pull the part now (it is already
     recorded as a failure with its sensor number).
   - **yellow "empty? click"** = reads 0 V: click the tile to say **empty**
     (grey) or **loaded** (blue). The rig cannot tell an empty socket from a
     dead part — you can.
   - **amber** = low reading. Leave it: offsets rise for a while after
     power-on; it is judged at the end.
4. **Lock tray** — sensor numbers are assigned across the loaded positions
   row by row (the first number continues the lot; you can change it).
5. **Start noise test** — a 5-minute stabilisation countdown (TP120), then
   about 20 s of settling, then the **60 s** capture. Keep the window
   visible, no fans, no vibration, laptop on AC. *Skip the rest of the
   stabilisation wait* is allowed only when the parts have already been
   powered for five minutes (the shortened wait is recorded).
6. **Read the tiles**: green / blue-grey = good (blue-grey means "measured,
   no noise limit yet" — normal today), **red** = offset failure (HO, LO,
   D), **purple** = noisy (only once a noise limit exists), grey hatched =
   not measured (rig fault — see §5).
7. **Save tray** — writes one row per part, the raw capture and a picture of
   the grid. **Re-measure tray** runs the noise test again as a new attempt
   (same sensor numbers). **Next tray** clears the grid.

Rejects go on the reject tray with the failure mode from the tile (HO, LO,
D, N); good parts go back on the original tray. Do not pull or insert parts
during the capture.

## 4. Where the results go

One folder per model under your `Documents`:

| Model | Folder |
| --- | --- |
| 405 M22 | `Documents\Eltec_405M22_Test_Results\405m22_esp32\` |
| 406 MCA | `Documents\Eltec_406MCA_Test_Results\v6_1_esp32\` |
| 449 M18 | `Documents\Eltec_449M18_Test_Results\449m18_esp32\` |
| 40623 array | `Documents\Eltec_40623_Test_Results\40623_array_daq\` (one row per position per tray; `noise_captures\` holds the raw tray captures — never delete) |

The batch file is `<model>_lot_<number>.csv`; next to it `…_attempts.csv`
lists every measure / re-measure / skip. Waveform pictures go to
`waveform_snapshots\`. **Do not rename, move or delete anything in these
folders** — the engineer backs them up.

## 5. If something goes wrong

| You see | Likely cause | Do this |
| --- | --- | --- |
| "No ESP32 rig found" / no serial port | USB unplugged, another program holds the port, or a second tester is open | Check the cable, close the Arduino Serial Monitor / other rig tools, make sure only one tester is running, try again. |
| Firmware version message at connect | Board runs the wrong firmware for this model | Engineer (reflash — one command). |
| "Front end" mismatch (406 MCA) | Board did not accept the 406 front-end setting | Engineer. |
| "timestamp gaps", "duplicate timestamps", "stream integrity" | Laptop on battery, window minimised, USB hub, charger EMI | The app restarts the capture by itself (up to twice; nothing is recorded from a bad one). If it still fails: plug into AC, keep the window visible, avoid hubs, **Measure again**. Persistent → engineer. |
| "ESP32 … stream stalled" | The stream went quiet for 2 s. The app restarts the capture by itself; if it keeps failing, the message ends with a tag that says who stopped: `[host-stall]` = the laptop stopped reading (window minimised, battery, another program); `[board-reset]` = the ESP32 rebooted (USB power or cable); `[board-silent]` = the ADC stopped (power-cycle the rig); `[no-reply]` = board or USB gone | Do what the tag says, **Measure again**. If it happens first thing in the morning, tell the engineer whether the tester was left open overnight and where the laptop was plugged in; the batch's `_attempts.csv` keeps every restart with its tag. |
| Several **LS (low sensitivity)** failures in a row | Possibly the **emitter**, not the parts (automatic emitter monitoring is currently off) | Stop, tell the engineer before condemning the parts. |
| Every part fails **N (noisy)** (405 M22) | Fan, vibration, charger, or a fixture fault | Remove fans/chargers, retest one known-good part; still failing → engineer. |
| **Unstable** | Part never settled within the time limit | Re-measure once; if it repeats, save it as Unstable. |
| App does not start | Missing Python or package | Engineer. The launcher log is `%LOCALAPPDATA%\eltec-rig\launcher.log` (Windows) / `~/.local/state/eltec-rig/launcher.log` (Xubuntu). |
| "Battery: not monitored" | Normal | Nothing — neither battery is measured on this fixture. |
| Array rig: "No ACCES device found" | DAQ USB unplugged, or plugged in less than five seconds ago (it loads its program first) | Check the cable, wait five seconds, **Start lot** again. Still nothing → engineer (driver package). |
| Array rig: "stream integrity", "buffer pool exhausted", "no data from the stream", tray NOT MEASURED | Laptop on battery or window minimised during the capture, USB hub, another program hogging the laptop, another program using the DAQ (the engineer's tools); "no data" can also be the DAQ's USB cable | The app retries by itself, then marks the tray NOT MEASURED. AC power, keep the window visible, no hub, close any other DAQ program, check the DAQ's USB cable, **Re-measure tray**. Persistent → engineer. |
| Array rig: every tile yellow "empty? click" | PCB not powered, or the ribbon cable is off | Power the PCB, check the DB37 cables, wait for the tiles to update. |
| Array rig: a whole row red or 0 V | Cable / row supply | Engineer — do not fail the parts. |
| Array rig app does not start | Missing Python or the ACCES driver | Engineer. The launcher log is `%LOCALAPPDATA%\eltec-array-rig\launcher.log` (selector) / `%LOCALAPPDATA%\eltec-40623-array\launcher.log` (tester). |

## 6. Do not

- Do not edit any file in the `Eltec_TestRig` folder or the results folders.
- Do not reflash the ESP32 or open the Arduino IDE unless the engineer asks.
- Do not change the filter setup mid-batch (start a new batch instead).
- Do not run the old standalone apps — they were retired on 2026-08-28.
- Do not use **Simulator** mode for real parts (it shows an amber SIMULATOR
  badge and tags the rows `data_source=simulator`; the array tester shows a red
  SIMULATOR badge and writes `simulated=YES`).
- Array rig: do not treat a blue-grey "no limit yet" tile as a noise pass or
  fail — the noise limit for this rig has not been set. Do not pull or insert
  parts during the capture. Do not skip the stabilisation wait on parts that
  were just powered.
- Array rig: do not run the engineer's DAQ tools (the bench probe, the
  readout, the live viewer) while the tester is open — only one program can
  use the DAQ at a time. Close one before starting the other.
