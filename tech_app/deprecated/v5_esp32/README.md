# Eltec 406MCA ESP32 Tester for Xubuntu

This folder contains the Xubuntu edition of the 406MCA emitter tester. It uses
the ESP32/ADS1256 rig and leaves the historical Windows/LabJack v4 application
unchanged.

It keeps the v4 guided batch workflow, CSV/snapshot output, simulator, live
waveform preview, Fast/Validation/Full capture modes, battery lockout, offset,
sensitivity, polarity, and pass/fail analysis. The hardware layer is replaced
with the ESP32/ADS1256 serial protocol on GPIO25/AIN0/AIN7.

## Firmware and rig status

The tester requires `Arduino/Eltec/Eltec.ino` **v1.7 or newer**. Version 1.7
verifies that the ADS1256 really accepted the 1000 SPS configuration, latches
real DRDY edges, and reports any ADC overrun. The application refuses older
firmware so a bad stream cannot create a test result.

The connected development rig was flashed and checked on 2026-07-14. A
1000-sample capture measured 1000.0 Hz with matching host/firmware counts, zero
timestamp gaps, zero malformed records, and zero ADC overruns.

The old physical LabJack AIN2 PWM loopback is intentionally not required. The
ESP32 supplies its own digital PWM state with every ADC sample and the tester
checks that this state toggles before measuring.

Use `Arduino/Eltec/ESP32_ADS1256_Wiring_v1_7.md` for the current 6 V fixture.
The older Word guide in that folder is retained only as historical 9 V wiring.

To reflash, open `Arduino/Eltec/Eltec.ino` in Arduino IDE, install the Espressif
ESP32 boards package, select **DOIT ESP32 DEVKIT V1**, select `/dev/ttyUSB0`, and
Upload. This workstation has ESP32 core 2.0.17 installed and the v1.7 sketch
compiles cleanly with it.

Important: the v4 sensitivity limits originated with the former 9 V setup.
The v5 rig uses a 6 V SLA, so qualify or update those pass/fail bands using
known-good and known-bad sensors before treating v5 sensitivity verdicts as
production-calibrated.

## Start the application

From this directory, run:

```bash
./run_eltec_406mca_esp32_tester.sh
```

The launcher resolves the repository location from its own path, starts the app
with `python3`, and writes startup errors and Python output to:

```text
~/.local/state/eltec-406mca-esp32/launcher.log
```

Set `ELTEC_PYTHON` to an executable path if a virtual environment should be
used, for example:

```bash
ELTEC_PYTHON="$HOME/.venvs/eltec/bin/python" ./run_eltec_406mca_esp32_tester.sh
```

## Add it to the Xubuntu desktop

Run the per-user installer (no `sudo` required):

```bash
./install_xubuntu_launcher.sh
```

It creates an Applications-menu entry in
`~/.local/share/applications/` and an executable, trusted XFCE launcher in the
current user's XDG Desktop directory. Both use the v5-specific desktop icon at
`tech_app/v5_esp32/assets/eltec_desktop_icon.png`, which places the Eltec logo
on a solid white background. Keep the repository in the same location after
installing; rerun the installer if it moves.

If XFCE still labels the icon untrusted, right-click it and choose **Allow
Launching** once. Remove both installed entries with:

```bash
./install_xubuntu_launcher.sh --uninstall
```

## Dependencies and device access

Required packages are Python 3, Tkinter, NumPy, and pyserial. Matplotlib is
optional but produces higher-quality saved waveform images. On Ubuntu/Xubuntu:

```bash
sudo apt install python3 python3-tk python3-numpy python3-serial
sudo apt install python3-matplotlib libnotify-bin desktop-file-utils xdg-user-dirs
```

The second line contains optional desktop/snapshot helpers. The signed-in user
must be in the `dialout` group to open `/dev/ttyUSB*` or `/dev/ttyACM*` ports:

```bash
sudo usermod -aG dialout "$USER"
```

Sign out and back in after changing group membership.

## ESP32 command-line checks

The helper script uses subcommands (no leading dash):

```bash
cd Arduino/Eltec
python3 esp32_rig_readout.py ports
python3 esp32_rig_readout.py bat
python3 esp32_rig_readout.py offset
```

Use `python3`, not `python`, on a standard Ubuntu installation. The GUI and CLI
auto-detect the production CP210x adapter; `--port /dev/ttyUSB0` can override
auto-detection when needed.

## Paths

- Application: `tech_app/v5_esp32/eltec_406mca_esp32_tester.py`
- ESP32 backend: `tech_app/v5_esp32/esp32_backend.py`
- In-app logo: `assets/eltec_logo.png`
- White-background desktop icon: `tech_app/v5_esp32/assets/eltec_desktop_icon.png`
- Test data: `~/Documents/Eltec_406MCA_Test_Results/v5_esp32/`
- Launcher log: `~/.local/state/eltec-406mca-esp32/launcher.log`

Entering an exact batch number that already has a CSV resumes with the next
sensor number and appends the new result to that same file. Existing completed
rows are not rewritten or deleted. Waveform snapshots also receive unique
filenames so a later capture cannot replace an earlier image. The JSON file in
the `autosave/` subfolder is only the current in-progress sensor recovery file
and is intentionally refreshed as that sensor advances through the workflow.
