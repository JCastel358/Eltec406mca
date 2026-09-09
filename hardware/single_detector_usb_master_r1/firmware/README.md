# USB-master R1 firmware

This separate sketch is for the new carrier only. It derives from the repository's unified rig firmware; the original `Arduino/Eltec/Eltec.ino` is unchanged. The generated patch and source/output hashes document the exact differences.

- GPIO35 is an input driven by a carrier logic gate powered from ESP3V3. LOW means the hardware power conditions are ready; HIGH means not ready. It is not an emitter output.
- Startup waits for qualified power before ADC reset, configuration and self-calibration. Later initialization retries support late battery insertion and power recovery while USB remains connected.
- Any captured power fault invalidates an ongoing measurement. PWM and streaming stop; recovery reinitializes the ADC but does not restart an interrupted test automatically.
- Hardware implements electrical shutdown and interface isolation independently of software execution. The firmware prevents the application from treating a supply-loss capture as valid.
- The existing SPI/DRDY/emitter pins and serial protocol are retained. The version is3.3. `BAT?` reports unavailable because this carrier grounds unusedAIN6/7 and has no battery divider.

Reproduce with KiCad's Python:

```powershell
& 'C:/Program Files/KiCad/10.0/bin/python.exe' hardware/single_detector_usb_master_r1/tools/validate_firmware.py
```

The validator locates the installed Arduino IDE CLI, records its version and installed cores, derives the separate sketch and compiles for `esp32:esp32:esp32`. It never selects a serial port or uploads. Review `reports/firmware_validation.json` and `reports/firmware_compile.log` for the actual result. Confirm the exact development-board flash configuration before eventual commissioning.

Required hardware validation remains: switch-on initialization, late battery insertion, supply interruption during OFFSET/REF/stream/STOP, rapid switch cycling, and recovery with each application's chosen front-end setting. A compiler pass does not establish those physical behaviors.
