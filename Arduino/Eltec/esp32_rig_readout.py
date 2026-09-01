"""
Eltec 406MCA emitter rig - ESP32 + ADS1256 host-side readout.

Companion to Eltec.ino. Talks to the ESP32 over USB serial and mirrors the
LabJack device wrapper the tester app used (offset read, battery read, PWM
control, 1000 Hz waveform stream with sync bit).

Works unchanged on Windows AND Linux/Xubuntu - pyserial abstracts the port:
    Windows:  ports look like COM5           (CH340/CP210x driver)
    Xubuntu:  ports look like /dev/ttyUSB0   (driver is built into the kernel;
              if you get "permission denied" run:
                  sudo usermod -a -G dialout $USER
              then log out and back in)

Install (both platforms):
    pip install pyserial          # or on Ubuntu: sudo apt install python3-serial

Usage:
    python3 esp32_rig_readout.py ports                 # list serial ports
    python3 esp32_rig_readout.py bat                   # battery voltage
    python3 esp32_rig_readout.py offset                # sensor DC offset (PWM off)
    python3 esp32_rig_readout.py pwm on|off            # emitter drive
    python3 esp32_rig_readout.py stream -s 5 -o cap.csv  # raw capture to CSV
    python3 esp32_rig_readout.py ref --set-baseline    # record the reference
                                                      # sensor's known-good level
    python3 esp32_rig_readout.py ref                   # re-measure it and compare
                                                      # (emitter health check)
    python3 esp32_rig_readout.py test --skip-ref       # guided sequence without
                                                      # optional AIN1 reference:
                                                      # battery -> offset -> PWM on
                                                      # -> warmup -> AIN0 capture
                                                      # -> sensitivity analysis
    python3 esp32_rig_readout.py noisecmp              # emitter-off noise, captured
                                                      # back-to-back on the v2.0
                                                      # (gain 1, unbuffered) AND
                                                      # v1.9 (gain 2, buffered)
                                                      # front ends - shows whether
                                                      # a noise figure depends on
                                                      # the front end (fw v2.1+)
    add --fe v19 (or --fe v20) to stream/test/ref/offset to run that
    measurement on the other front end (firmware v2.1+; the board resets
    back to the v2.0 front end whenever the port closes)
    add --port COM5 (or --port /dev/ttyUSB0) to skip auto-detection

Reference sensor / emitter health:
    A second 406MCA is permanently mounted in the fixture on AIN1. It has no
    absolute spec - what matters is that its pk-pk response to the chopped
    emitter stays CONSTANT over the emitter's life. Record a baseline once with
    a known-good emitter (`ref --set-baseline`); every later `ref` or `test`
    run compares against it and flags drift (warn/fail thresholds below).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is not installed. Run:  pip install pyserial")

BAUD = 500000
MIN_FIRMWARE_VERSION = (1, 7)

# Installed battery-divider measurements. The upper resistor runs from
# battery+ to the AIN7 tap; the lower resistor runs from the tap to ground.
# Firmware v1.8+ uses this ratio natively. For v1.7 boards, the host corrects
# the old nominal x2 result by ACTUAL/NOMINAL so readings stay accurate before
# and after a firmware update. The 100 nF capacitor across the lower resistor
# filters noise but does not affect the DC ratio.
BATTERY_DIVIDER_R_TOP_OHMS = 99_700.0
BATTERY_DIVIDER_R_BOTTOM_OHMS = 99_600.0
BATTERY_DIVIDER_FILTER_CAPACITANCE_F = 100e-9
BATTERY_DIVIDER_RATIO = (
    BATTERY_DIVIDER_R_TOP_OHMS + BATTERY_DIVIDER_R_BOTTOM_OHMS
) / BATTERY_DIVIDER_R_BOTTOM_OHMS
LEGACY_FIRMWARE_BATTERY_DIVIDER_RATIO = 2.0
CALIBRATED_DIVIDER_FIRMWARE_VERSION = (1, 8)

# Mirror the tester app's rig constants (eltec_406mca_emitter_tester.py).
SAMPLE_RATE_HZ = 1000.0
PWM_FREQUENCY_HZ = 10.0               # firmware boot default (406MCA drive)
# Runtime-selectable drive (firmware v1.9+): the 405 M22 sensor is specified
# at 1 Hz, so its checks run with --freq 1. Range enforced by the firmware.
PWM_FREQ_FIRMWARE_VERSION = (1, 9)
PWM_MIN_FREQUENCY_HZ = 0.1
PWM_MAX_FREQUENCY_HZ = 20.0
# Runtime-selectable duty cycle (firmware v3.2+): the ON fraction of each
# period. The board boots at 50 % - the historical drive for the 405 M22 and
# 406MCA models, which is what they are calibrated on. The 449 M18 app drives
# 20 % to imitate the TP443 fixture's 20/80 chopper blade, so a bench session
# reproducing that drive needs the same 20 %. Not persisted: any reset
# (including opening the port) returns the board to 50 %.
PWM_DUTY_FIRMWARE_VERSION = (3, 2)
PWM_DEFAULT_DUTY_PERCENT = 50.0
PWM_MIN_DUTY_PERCENT = 1.0
PWM_MAX_DUTY_PERCENT = 99.0
# Runtime-selectable ADS1256 front end (firmware v2.1+): lets the v2.0
# (gain 1, buffer OFF) and v1.9 (gain 2, buffer ON) configurations be
# A/B-compared on the same board without reflashing - e.g. to check whether
# a noise reading depends on the front end. NOT persisted: any board reset
# (including simply opening the serial port) returns it to v2.0, so the
# switch is only meaningful within one session (--fe / noisecmp).
FE_FIRMWARE_VERSION = (2, 1)
FE_PRESETS = {
    "v20": "gain 1, buffer OFF, +/-5 V full scale (v2.0 / 405 M22, boot default)",
    "v19": "gain 2, buffer ON, +/-2.5 V full scale (v1.9 / 406MCA)",
}
FE_FULL_SCALE_V = {"v20": 5.0, "v19": 2.5}
# Emitter-off noise windows, mirroring the 405 M22 app's TP412 noise test.
NOISE_WINDOW_S = 1.0
NOISE_LIMIT_MV = 300.0            # per-window pk-pk limit (context only here)
EMITTER_WARMUP_S = 5.0            # EMITTER_WARMUP_S: thermal ramp before measuring
DEFAULT_CAPTURE_S = 8.0
# Battery: 6 V 4.5 Ah sealed lead-acid powering EVERYTHING (sensor buffer,
# sensors, emitter + MOSFET driver module) as of 2026-07-10. Thresholds are
# resting/light-load SLA voltages: ~6.3 V full, ~5.8 V is roughly 20% left.
#
# HARDWARE CAVEAT: the firmware runs the ADS1256 with its input buffer ON,
# which limits the linear input range to AVDD - 2 V = 3.0 V. Through the
# measured 99.7k/99.6k divider a fully charged SLA (~6.4 V) puts ~3.2 V
# on AIN7 -
# slightly over the limit - so readings at the very top of the range compress
# toward ~6.0-6.2 V. Readings below ~6.0 V (pin < 3.0 V) are accurate. A ~4:1
# divider (300k/100k, BATTERY_DIVIDER_RATIO = 4.0 in Eltec.ino) removes the
# compression entirely if exact top-end readings ever matter.
BATTERY_MIN_V = 5.8               # hard block level - recharge the battery
BATTERY_WARN_V = 6.0
BATTERY_FAULT_MIN_V = 3.0         # below this the divider is probably not wired
BATTERY_FAULT_MAX_V = 7.5         # above this it's not the 6 V battery (charger? 9 V?)
OFFSET_MIN_V = 0.3                # healthy 406MCA offset band
OFFSET_MAX_V = 1.2
SENSOR_OFFSET_MIN_PLAUSIBLE_V = 0.05
SENSOR_OFFSET_MAX_PLAUSIBLE_V = 2.5

# Reference sensor (permanently mounted on AIN1) - emitter health trending.
# No absolute spec: the reading just has to stay constant vs the recorded
# baseline. Thresholds are initial guesses - tune once real drift data exists.
# NOTE: the emitter runs from the shared 6 V battery, and its chopped
# amplitude scales strongly with supply voltage - so the reference reading
# sags along with the battery. Compare against the baseline only with a
# healthy battery (bat check OK), and expect some drift as it discharges;
# recharge before concluding the emitter itself has degraded.
REF_BASELINE_FILE = Path(__file__).with_name("emitter_ref_baseline.json")
REF_DRIFT_WARN_PCT = 10.0         # nag level
REF_DRIFT_FAIL_PCT = 25.0         # emitter probably degraded
REF_CAPTURE_S = 4.0               # ref capture length inside the `test` sequence

# USB-serial bridge chips commonly found on ESP32 dev boards: (VID, PID)
KNOWN_USB_IDS = {
    (0x10C4, 0xEA60): "CP210x",
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x55D4): "CH9102",
    (0x0403, 0x6001): "FTDI",
    (0x303A, 0x1001): "ESP32 native USB",
}


@dataclass
class Sample:
    t_us: int
    raw: int
    volts: float
    sync: int


def find_port() -> str:
    """Pick the most ESP32-looking serial port; works on Windows and Linux."""
    candidates = []
    for p in list_ports.comports():
        chip = KNOWN_USB_IDS.get((p.vid, p.pid)) if p.vid is not None else None
        if chip:
            candidates.append((0, p.device, chip))
        elif "USB" in (p.description or "").upper() or "usb" in p.device.lower():
            candidates.append((1, p.device, p.description or "?"))
    if not candidates:
        sys.exit("No ESP32 serial port found. Plug the board in, or pass --port "
                 "(use the 'ports' command to list what is available).")
    candidates.sort()
    _, device, chip = candidates[0]
    print(f"Using {device} ({chip})")
    return device


class Esp32Rig:
    """Serial wrapper mirroring the app's EmitterLabJackT7 surface."""

    def __init__(self, port: str):
        self.port_name = port
        self.ser: serial.Serial | None = None
        self.firmware_version: tuple[int, int] | None = None
        self.pwm_frequency_hz = PWM_FREQUENCY_HZ   # firmware boot default
        self.pwm_duty_percent = PWM_DEFAULT_DUTY_PERCENT      # ditto, 50 %

    # -- lifecycle -------------------------------------------------------- #
    def connect(self) -> None:
        self.ser = serial.Serial(self.port_name, BAUD, timeout=1.0)
        # Opening the port toggles DTR, which resets most ESP32 dev boards.
        # Give the firmware time to boot and swallow its READY banner.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            line = self._readline()
            if line.startswith("READY"):
                break
        self.ser.reset_input_buffer()
        idn = self._command("IDN?", "ELTEC")
        print(f"Connected: {idn}")
        version_match = re.search(r",v(\d+)\.(\d+)(?:\.\d+)?$", idn.strip())
        version = (
            (int(version_match.group(1)), int(version_match.group(2)))
            if version_match else None
        )
        if version is None or version < MIN_FIRMWARE_VERSION:
            self.close()
            raise RuntimeError(
                "Board firmware is stale; re-flash Eltec.ino v1.7 or newer. "
                "Older builds may repeat conversions or leave the ADS1256 at "
                "its reset sample rate, so their measurements are not safe to use."
            )
        self.firmware_version = version

    def close(self, disable_pwm: bool = True) -> None:
        # disable_pwm=False skips the explicit PWM,OFF on exit. NOTE (verified
        # 2026-07-13 on Windows/CP210x): closing the port resets the board
        # anyway - releasing DTR/RTS first does not prevent it - so NO drive
        # survives close(). That is why cmd_gate/cmd_pwm hold the port open
        # while the user measures instead of relying on state after exit.
        if self.ser is not None:
            if disable_pwm:
                try:
                    self._send("PWM,OFF")
                except Exception:
                    pass
            self.ser.close()
            self.ser = None

    # -- low-level protocol ------------------------------------------------ #
    def _send(self, cmd: str) -> None:
        assert self.ser is not None
        self.ser.write((cmd + "\n").encode("ascii"))

    def _readline(self) -> str:
        assert self.ser is not None
        return self.ser.readline().decode("ascii", errors="replace").strip()

    def _command(self, cmd: str, expect_prefix: str, timeout_s: float = 3.0) -> str:
        """Send a command and wait for a reply starting with expect_prefix.

        Stray stream lines ("D,...") are skipped; "ERR,..." raises.
        """
        self._send(cmd)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            line = self._readline()
            if not line or line.startswith("D,"):
                continue
            if line.startswith("ERR"):
                raise RuntimeError(f"{cmd} -> {line}")
            if line.startswith(expect_prefix):
                return line
        raise RuntimeError(f"Timed out waiting for '{expect_prefix}' after {cmd}")

    # -- LabJack-equivalent operations -------------------------------------- #
    def read_battery_voltage(self) -> float:
        reply = self._command("BAT?", "BAT,")            # firmware does the
        volts = float(reply.split(",")[1])               # median + divider math
        if (
            self.firmware_version is not None
            and self.firmware_version < CALIBRATED_DIVIDER_FIRMWARE_VERSION
        ):
            # v1.7 used a nominal 2.0 ratio. Correct its already-scaled result;
            # v1.8+ uses the measured values in firmware, so do not scale twice.
            volts *= (
                BATTERY_DIVIDER_RATIO
                / LEGACY_FIRMWARE_BATTERY_DIVIDER_RATIO
            )
        return volts

    def read_offset_voltage(self) -> float:
        reply = self._command("OFFSET?", "OFFSET,")
        return float(reply.split(",")[1])

    def read_ref_voltage(self) -> float:
        """DC level of the permanently-mounted reference sensor (AIN1)."""
        reply = self._command("REF?", "REF,")
        return float(reply.split(",")[1])

    def enable_emitter_pwm(self) -> None:
        self._command("PWM,ON", "OK,PWM,ON")

    def disable_emitter_pwm(self) -> None:
        self._command("PWM,OFF", "OK,PWM,OFF")

    def set_pwm_frequency(self, hz: float) -> None:
        """Set the emitter drive frequency (firmware v1.9+).

        The 406MCA rig runs at the 10 Hz boot default; the 405 M22 sensor is
        specified at 1 Hz. The setting is not persisted on the board.
        """
        if not (PWM_MIN_FREQUENCY_HZ <= hz <= PWM_MAX_FREQUENCY_HZ):
            raise ValueError(
                f"PWM frequency {hz:g} Hz out of range "
                f"({PWM_MIN_FREQUENCY_HZ:g}-{PWM_MAX_FREQUENCY_HZ:g} Hz)"
            )
        if (self.firmware_version is not None
                and self.firmware_version < PWM_FREQ_FIRMWARE_VERSION):
            raise RuntimeError(
                "PWM,FREQ needs firmware v1.9 or newer - re-flash Eltec.ino "
                "(the installed build only drives the fixed 10 Hz 406MCA rate)."
            )
        self._command(f"PWM,FREQ,{hz:g}", "OK,PWM,FREQ")
        self.pwm_frequency_hz = hz

    def set_pwm_duty(self, percent: float) -> None:
        """Set the emitter drive duty cycle in percent ON (firmware v3.2+).

        50 % is the boot default and the drive the 405 M22 and 406MCA models
        are calibrated on; 20 % is the 449 M18's TP443 20/80 blade equivalent.
        The firmware restarts the PWM phase, so the change is clean even
        mid-drive. Not persisted on the board.
        """
        if not (PWM_MIN_DUTY_PERCENT <= percent <= PWM_MAX_DUTY_PERCENT):
            raise ValueError(
                f"PWM duty {percent:g}% out of range "
                f"({PWM_MIN_DUTY_PERCENT:g}-{PWM_MAX_DUTY_PERCENT:g} %)"
            )
        if (self.firmware_version is not None
                and self.firmware_version < PWM_DUTY_FIRMWARE_VERSION):
            raise RuntimeError(
                "PWM,DUTY needs firmware v3.2 or newer - re-flash Eltec.ino "
                "with 'python Arduino/Eltec/flash_firmware.py' (the installed "
                f"build v{self.firmware_version[0]}.{self.firmware_version[1]} "
                "drives a fixed 50 % duty)."
            )
        self._command(f"PWM,DUTY,{percent:g}", "OK,PWM,DUTY")
        self.pwm_duty_percent = percent

    def set_front_end(self, preset: str) -> str:
        """Switch the ADS1256 analog front end (firmware v2.1+).

        preset: "v20" = gain 1 / buffer off (boot default),
                "v19" = gain 2 / buffer on (the 406MCA front end).
        The firmware SELFCALs and read-back-verifies before replying OK.
        Not persisted - the board reverts to v20 on any reset, including
        the DTR toggle when a host opens the port.
        """
        preset = preset.lower()
        if preset not in FE_PRESETS:
            raise ValueError(f"unknown front end {preset!r} (use v19 or v20)")
        if (self.firmware_version is not None
                and self.firmware_version < FE_FIRMWARE_VERSION):
            raise RuntimeError(
                "FE selection needs firmware v2.1 or newer - re-flash "
                "Eltec.ino (the installed build has a fixed front end)."
            )
        return self._command(f"FE,{preset.upper()}", "OK,FE")

    def read_front_end(self) -> str:
        """FE,gain=<1|2>,buf=<0|1>,fs=<V> (firmware v2.1+)."""
        return self._command("FE?", "FE,")

    def capture(self, seconds: float, progress: bool = True,
                channel: str = "sensor") -> list[Sample]:
        """Stream one channel for `seconds` and return the samples.

        channel: "sensor" = DUT on AIN0 (default), "ref" = reference sensor
        on AIN1. Each firmware line is D,<t_us>,<raw_code>,<volts>,<sync 0|1>;
        volts were already converted on the ESP32
        (code * 2*Vref/PGA / (2^23-1)).
        """
        assert self.ser is not None
        target = int(seconds * SAMPLE_RATE_HZ)
        start = "STREAM,START,REF" if channel == "ref" else "STREAM,START"
        self._command(start, "STREAM,BEGIN")
        samples: list[Sample] = []
        deadline = time.time() + seconds + 5.0
        while len(samples) < target and time.time() < deadline:
            line = self._readline()
            if not line.startswith("D,"):
                continue
            try:
                _, t_us, raw, volts, sync = line.split(",")
                samples.append(Sample(int(t_us), int(raw), float(volts), int(sync)))
            except ValueError:
                continue                     # torn line at start/end of stream
            if progress and len(samples) % 1000 == 0:
                print(f"  {len(samples)}/{target} samples...", end="\r")
        self._send("STREAM,STOP")
        adc_overruns = self._drain_to_stream_end()
        if adc_overruns:
            raise RuntimeError(
                f"ADC stream overran {adc_overruns} time(s); discard this capture "
                "and retry with the serial monitor closed."
            )
        if progress:
            print(f"  captured {len(samples)} samples          ")
        if len(samples) < target * 0.9:
            print(f"WARNING: expected ~{target} samples, got {len(samples)} - "
                  "check the serial link / baud rate.")
        return samples

    def _drain_to_stream_end(self, timeout_s: float = 2.0) -> int:
        """Consume buffered stream lines up to STREAM,END; return adc_overruns."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            line = self._readline()
            if line.startswith("STREAM,END"):
                fields = line.split(",")
                if len(fields) >= 4:
                    try:
                        return int(fields[3])
                    except ValueError:
                        raise RuntimeError(f"Malformed stream end marker: {line}")
                return 0
            if not line:
                break
        return 0


# --------------------------------------------------------------------------- #
# Analysis - a light version of the app's engine: per-cycle peak-to-peak off
# the sync edges, median across stable cycles, polarity from the sync phase.
# --------------------------------------------------------------------------- #
def analyze(samples: list[Sample]) -> dict:
    volts = [s.volts for s in samples]
    sync = [s.sync for s in samples]
    result: dict = {
        "n": len(samples),
        "offset_v": statistics.fmean(volts) if volts else float("nan"),
    }
    # Rising edges of the sync bit delimit PWM cycles.
    edges = [i for i in range(1, len(sync)) if sync[i] and not sync[i - 1]]
    result["cycles"] = max(0, len(edges) - 1)
    if len(edges) < 3:
        result["error"] = ("Fewer than 2 full PWM cycles in the capture - "
                           "is the PWM on and the stream long enough?")
        return result

    # Measured PWM frequency from the edge spacing (should be ~10 Hz).
    spans_us = [samples[edges[i + 1]].t_us - samples[edges[i]].t_us
                for i in range(len(edges) - 1)]
    result["pwm_hz"] = 1e6 / statistics.median(spans_us)

    # Per-cycle peak-to-peak; skip the first cycle (partial thermal settling).
    pp, hi_minus_lo = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        cyc = volts[a:b]
        cyc_sync = sync[a:b]
        if len(cyc) < 10:
            continue
        pp.append(max(cyc) - min(cyc))
        high = [v for v, s in zip(cyc, cyc_sync) if s]
        low = [v for v, s in zip(cyc, cyc_sync) if not s]
        if high and low:
            hi_minus_lo.append(statistics.fmean(high) - statistics.fmean(low))
    pp = pp[1:] or pp
    result["sensitivity_mv"] = statistics.median(pp) * 1000.0
    result["pp_spread_mv"] = (max(pp) - min(pp)) * 1000.0
    # The 406MCA output drops while the emitter is driven for one polarity and
    # rises for the other; the sign of (mean while sync high - mean while low)
    # is the polarity call. The full app engine adds confidence gating on top.
    mean_delta = statistics.fmean(hi_minus_lo) if hi_minus_lo else 0.0
    result["polarity"] = "POSITIVE" if mean_delta >= 0 else "NEGATIVE"
    result["polarity_delta_mv"] = mean_delta * 1000.0
    return result


# --------------------------------------------------------------------------- #
# Reference-sensor baseline (emitter health). Stored next to this script so it
# travels with the rig install; one JSON file, human-readable.
# --------------------------------------------------------------------------- #
def load_ref_baseline() -> dict | None:
    try:
        with open(REF_BASELINE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_ref_baseline(pp_mv: float, offset_v: float) -> None:
    data = {"pp_mv": round(pp_mv, 3), "offset_v": round(offset_v, 4),
            "recorded": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(REF_BASELINE_FILE, "w") as fh:
        json.dump(data, fh, indent=2)
    print(f"     baseline saved to {REF_BASELINE_FILE.name}: "
          f"{data['pp_mv']:.2f} mV pk-pk")


def report_ref_health(pp_mv: float) -> None:
    """Compare a fresh reference-sensor pk-pk reading against the baseline."""
    base = load_ref_baseline()
    if not base or not base.get("pp_mv"):
        print("     no baseline recorded yet - with a KNOWN-GOOD emitter run:")
        print("       python esp32_rig_readout.py ref --set-baseline")
        return
    drift = (pp_mv - base["pp_mv"]) / base["pp_mv"] * 100.0
    if abs(drift) >= REF_DRIFT_FAIL_PCT:
        state = "EMITTER SUSPECT - do not trust test results until checked"
    elif abs(drift) >= REF_DRIFT_WARN_PCT:
        state = "drifting - keep an eye on the emitter"
    else:
        state = "OK"
    print(f"     vs baseline {base['pp_mv']:.2f} mV "
          f"({base.get('recorded', '?')}): {drift:+.1f}%  [{state}]")


def save_csv(samples: list[Sample], path: str) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t_us", "raw_code", "volts", "sync"])
        for s in samples:
            w.writerow([s.t_us, s.raw, f"{s.volts:.6f}", s.sync])
    print(f"Saved {len(samples)} samples to {path}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_ports(_args) -> None:
    ports = list_ports.comports()
    if not ports:
        print("No serial ports found.")
    for p in ports:
        chip = KNOWN_USB_IDS.get((p.vid, p.pid), "") if p.vid is not None else ""
        print(f"  {p.device:<16} {p.description}  {chip}")


def cmd_bat(rig: Esp32Rig, _args) -> None:
    v = rig.read_battery_voltage()
    if v < BATTERY_FAULT_MIN_V or v > BATTERY_FAULT_MAX_V:
        state = ("FAULT - not a plausible 6V-battery reading; "
                 "check the battery clip / divider wiring")
    elif v <= BATTERY_MIN_V:
        state = "LOW - recharge the battery (testing would be blocked)"
    elif v <= BATTERY_WARN_V:
        state = "getting low"
    else:
        state = "OK"
    print(f"Battery: {v:.3f} V  [{state}]")
    if v > 6.0:
        print("  (readings above ~6.0 V may sit slightly low: the divider tap is at")
        print("   the ADS1256's buffered 3.0 V input limit near full charge)")


def cmd_offset(rig: Esp32Rig, args) -> None:
    _apply_fe(rig, args)
    v = rig.read_offset_voltage()
    if not (SENSOR_OFFSET_MIN_PLAUSIBLE_V <= v <= SENSOR_OFFSET_MAX_PLAUSIBLE_V):
        state = "no sensor detected (floating/railed input)"
    elif OFFSET_MIN_V <= v <= OFFSET_MAX_V:
        state = "OK (in the 0.3-1.2 V band)"
    else:
        state = "outside the healthy 0.3-1.2 V band"
    print(f"DC offset: {v:.4f} V  [{state}]")


def _retarget_pin(rig: Esp32Rig, args) -> None:
    if getattr(args, "pin", None):
        rig._command(f"PIN,{args.pin}", f"OK,PIN,{args.pin}")
        print(f"Gate drive retargeted to GPIO{args.pin} (until next reset)")


def _apply_freq(rig: Esp32Rig, args) -> None:
    """Apply --freq (405 M22 = 1 Hz) before the PWM is turned on."""
    freq = getattr(args, "freq", None)
    if freq is not None:
        rig.set_pwm_frequency(freq)
        print(f"Emitter drive frequency set to {freq:g} Hz (firmware v1.9+)")


def _apply_fe(rig: Esp32Rig, args) -> None:
    """Apply --fe before measuring (firmware v2.1+)."""
    fe = getattr(args, "fe", None)
    if fe:
        rig.set_front_end(fe)
        print(f"Front end: {fe} = {FE_PRESETS[fe.lower()]}")
        if fe.lower() == "v19":
            print("  NOTE: +/-2.5 V full scale - DC offsets above ~2.4 V clip"
                  " on this front end (that is why v2.0 exists)")


def cmd_pwm(rig: Esp32Rig, args) -> None:
    _retarget_pin(rig, args)
    _apply_freq(rig, args)
    if args.state == "on":
        rig.enable_emitter_pwm()
        print(f"Emitter PWM ON ({rig.pwm_frequency_hz:g} Hz, 50% duty)")
        if getattr(args, "no_hold", False):
            print("  WARNING: closing the port resets the board, so the PWM"
                  " stops as soon as this command exits (--no-hold given)")
        else:
            print("  holding the port open so the PWM keeps running -"
                  f" module LED should be BLINKING at {rig.pwm_frequency_hz:g} Hz")
            try:
                input("  press Enter (or Ctrl+C) to stop the PWM and exit... ")
            except (KeyboardInterrupt, EOFError):
                print()
            rig.disable_emitter_pwm()
            print("Emitter PWM OFF")
    else:
        rig.disable_emitter_pwm()
        print("Emitter PWM OFF")


def cmd_gate(rig: Esp32Rig, args) -> None:
    """Hold the emitter gate steady - hardware bring-up (needs firmware v1.3+)."""
    _retarget_pin(rig, args)
    pin = getattr(args, "pin", None) or 25
    if args.state == "on":
        rig._command("GATE,ON", "OK,GATE,ON")
        print(f"Gate held steady HIGH (3.3 V on GPIO{pin})"
              + (" - onboard blue LED should be SOLID on" if pin == 2
                 else " - module trigger LED should glow"))
        try:
            rb = rig._command("GATE?", "GATE,")
            print(f"  pad readback: {rb}  (drive=1/read=0 -> pin held low "
                  "externally: short/overload/damaged driver)")
        except Exception:
            pass  # pre-v1.4 firmware has no GATE?
        if getattr(args, "no_hold", False):
            print("  WARNING: closing the port resets the board, so the gate"
                  " drops as soon as this command exits (--no-hold given)")
        else:
            print("  holding the port open so the gate stays HIGH -"
                  " measure with the multimeter NOW")
            try:
                input("  press Enter (or Ctrl+C) to drive the gate LOW and exit... ")
            except (KeyboardInterrupt, EOFError):
                print()
            rig._command("GATE,OFF", "OK,GATE,OFF")
            print("Gate LOW (module off)")
    else:
        rig._command("GATE,OFF", "OK,GATE,OFF")
        print("Gate LOW (module off)")


def cmd_stream(rig: Esp32Rig, args) -> None:
    _apply_fe(rig, args)
    print(f"Streaming AIN0 for {args.seconds:g} s at {SAMPLE_RATE_HZ:g} Hz...")
    samples = rig.capture(args.seconds)
    if args.output:
        save_csv(samples, args.output)
    r = analyze(samples)
    print(f"  mean level: {r['offset_v']:.4f} V   cycles seen: {r['cycles']}")
    if "sensitivity_mv" in r:
        print(f"  pk-pk (median): {r['sensitivity_mv']:.2f} mV   "
              f"PWM measured: {r['pwm_hz']:.2f} Hz")


def cmd_ref(rig: Esp32Rig, args) -> None:
    """Measure the reference sensor (AIN1) to trend emitter health."""
    _apply_fe(rig, args)
    if args.dc:
        v = rig.read_ref_voltage()
        print(f"Reference DC level (AIN1): {v:.4f} V  (PWM untouched)")
        return
    _apply_freq(rig, args)
    print(f"Emitter PWM on, warming up {EMITTER_WARMUP_S:g} s (thermal ramp)")
    rig.enable_emitter_pwm()
    time.sleep(EMITTER_WARMUP_S)
    print(f"Capturing {args.seconds:g} s from the reference sensor (AIN1)")
    samples = rig.capture(args.seconds, channel="ref")
    rig.disable_emitter_pwm()
    r = analyze(samples)
    if "error" in r:
        print(f"     {r['error']}")
        return
    print(f"     Reference: {r['sensitivity_mv']:.2f} mV pk-pk, "
          f"mean {r['offset_v']:.4f} V, PWM {r['pwm_hz']:.2f} Hz")
    if args.set_baseline:
        save_ref_baseline(r["sensitivity_mv"], r["offset_v"])
    else:
        report_ref_health(r["sensitivity_mv"])


def _windowed_pkpk_mv(samples: list[Sample],
                      window_s: float = NOISE_WINDOW_S) -> list[float]:
    """Per-window pk-pk in mV, same 1 s windowing as the 405 M22 noise test."""
    n = int(window_s * SAMPLE_RATE_HZ)
    volts = [s.volts for s in samples]
    return [
        (max(volts[i:i + n]) - min(volts[i:i + n])) * 1000.0
        for i in range(0, len(volts) - n + 1, n)
    ]


def cmd_noisecmp(rig: Esp32Rig, args) -> None:
    """Emitter-off noise captured back-to-back on BOTH front ends (fw v2.1+).

    Directly answers "did the v2.0 gain/buffer change alter my noise
    reading?": same part, same wiring, same session - only the ADS1256
    front end differs between the two captures.
    """
    presets = ["v19", "v20"] if args.v19_first else ["v20", "v19"]
    print("Emitter is kept OFF for the whole comparison (noise, not response).")
    rig.disable_emitter_pwm()
    results: dict[str, tuple[list[float], float, int]] = {}
    for preset in presets:
        print(f"\n[{preset}] {FE_PRESETS[preset]}")
        rig.set_front_end(preset)
        print(f"  settling {args.settle:g} s after the front-end SELFCAL...")
        time.sleep(args.settle)
        print(f"  capturing {args.seconds:g} s from AIN0...")
        samples = rig.capture(args.seconds)
        if args.output:
            stem = args.output[:-4] if args.output.lower().endswith(".csv") \
                else args.output
            save_csv(samples, f"{stem}_{preset}.csv")
        wins = _windowed_pkpk_mv(samples)
        if not wins:
            print("  not enough samples for even one window - aborting")
            return
        fs = FE_FULL_SCALE_V[preset]
        clipped = sum(1 for s in samples if abs(s.volts) >= fs * 0.98)
        mean_v = statistics.fmean(s.volts for s in samples)
        results[preset] = (wins, mean_v, clipped)
        if clipped:
            print(f"  WARNING: {clipped} samples at the +/-{fs:g} V rail - this"
                  " front end is CLIPPING, its noise figure is NOT comparable"
                  + (" (DC offset too high for the v1.9 +/-2.5 V range)"
                     if preset == "v19" else ""))

    print(f"\nComparison ({len(results[presets[0]][0])} x {NOISE_WINDOW_S:g} s "
          "windows each, emitter off):")
    for preset in presets:
        wins, mean_v, clipped = results[preset]
        over = sum(1 for w in wins if w > NOISE_LIMIT_MV)
        flag = "  [CLIPPED - not comparable]" if clipped else ""
        print(f"  {preset}:  worst {max(wins):8.2f} mV pk-pk   "
              f"median {statistics.median(wins):8.2f} mV   "
              f"windows >{NOISE_LIMIT_MV:g} mV: {over}/{len(wins)}   "
              f"mean level {mean_v:.4f} V{flag}")
    print(f"  (405 M22 app context: PASS iff <=20% of windows exceed "
          f"{NOISE_LIMIT_MV:g} mV pk-pk)")
    print("  Board note: the front end reverts to v20 automatically on the "
          "next reset/port-open.")


def cmd_test(rig: Esp32Rig, args) -> None:
    """Full sequence in the same order as the tester app's Measure step."""
    _apply_fe(rig, args)
    print("1/6  Battery check")
    cmd_bat(rig, args)

    print("2/6  DC offset (PWM off)")
    rig.disable_emitter_pwm()
    time.sleep(0.2)
    offset_v = rig.read_offset_voltage()
    print(f"     offset = {offset_v:.4f} V")

    _apply_freq(rig, args)
    print(f"3/6  Emitter PWM on ({rig.pwm_frequency_hz:g} Hz), "
          f"warming up {EMITTER_WARMUP_S:g} s (thermal ramp)")
    rig.enable_emitter_pwm()
    time.sleep(EMITTER_WARMUP_S)

    if args.skip_ref:
        print("4/6  Reference sensor check - SKIPPED (AIN1 not connected)")
    else:
        print(f"4/6  Reference sensor check - emitter health "
              f"({REF_CAPTURE_S:g} s on AIN1)")
        ref_r = analyze(rig.capture(REF_CAPTURE_S, channel="ref"))
        if "error" in ref_r:
            print(f"     {ref_r['error']}")
        else:
            print(f"     reference = {ref_r['sensitivity_mv']:.2f} mV pk-pk")
            report_ref_health(ref_r["sensitivity_mv"])

    print(f"5/6  Capturing {args.seconds:g} s of DUT waveform (AIN0)")
    samples = rig.capture(args.seconds)
    rig.disable_emitter_pwm()
    if args.output:
        save_csv(samples, args.output)

    print("6/6  Results")
    r = analyze(samples)
    if "error" in r:
        print(f"     {r['error']}")
        return
    print(f"     DC offset       : {offset_v:.4f} V")
    print(f"     Sensitivity     : {r['sensitivity_mv']:.2f} mV pk-pk "
          f"(cycle spread {r['pp_spread_mv']:.2f} mV over {r['cycles']} cycles)")
    print(f"     Polarity        : {r['polarity']} "
          f"(delta {r['polarity_delta_mv']:+.2f} mV while emitter driven)")
    print(f"     PWM measured    : {r['pwm_hz']:.2f} Hz "
          f"(expected {rig.pwm_frequency_hz:g})")


def main() -> None:
    ap = argparse.ArgumentParser(description="ESP32 + ADS1256 Eltec rig readout")
    ap.add_argument("--port", help="serial port (COM5, /dev/ttyUSB0); auto-detect if omitted")
    sub = ap.add_subparsers(dest="command", required=True)

    fe_help = ("ADS1256 front end for this run (firmware v2.1+): v20 = gain 1/"
               "buffer off (boot default), v19 = gain 2/buffer on (406MCA-era; "
               "clips above ~2.4 V). Reverts to v20 when the port closes.")

    sub.add_parser("ports", help="list serial ports")
    sub.add_parser("bat", help="read the 6V battery")
    p = sub.add_parser("offset", help="read the sensor DC offset")
    p.add_argument("--fe", choices=["v19", "v20"], help=fe_help)
    p = sub.add_parser("pwm", help="emitter PWM on/off")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--pin", type=int,
                   help="retarget gate GPIO first (2/12/13/14/25/26/27/32/33; "
                        "2 = onboard LED)")
    p.add_argument("--freq", type=float,
                   help="drive frequency in Hz (firmware v1.9+; 405 M22 = 1, "
                        "default = board's 10 Hz)")
    p.add_argument("--no-hold", action="store_true",
                   help="exit immediately instead of holding the port open "
                        "(the drive drops when the port closes - board resets)")
    p = sub.add_parser("gate", help="hold emitter gate steady high/low (debug)")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--pin", type=int,
                   help="retarget gate GPIO first (2/12/13/14/25/26/27/32/33; "
                        "2 = onboard LED)")
    p.add_argument("--no-hold", action="store_true",
                   help="exit immediately instead of holding the port open "
                        "(the drive drops when the port closes - board resets)")
    p = sub.add_parser("stream", help="capture the waveform stream")
    p.add_argument("-s", "--seconds", type=float, default=DEFAULT_CAPTURE_S)
    p.add_argument("-o", "--output", help="save samples to this CSV file")
    p.add_argument("--fe", choices=["v19", "v20"], help=fe_help)
    p = sub.add_parser("noisecmp",
                       help="emitter-off noise A/B: v2.0 vs v1.9 front end "
                            "back-to-back (firmware v2.1+)")
    p.add_argument("-s", "--seconds", type=float, default=20.0,
                   help="capture length per front end (default 20 s, same as "
                        "the 405 M22 app's noise test)")
    p.add_argument("--settle", type=float, default=5.0,
                   help="settling wait after each front-end switch (default 5 s)")
    p.add_argument("-o", "--output",
                   help="save both captures as <output>_v20.csv / <output>_v19.csv")
    p.add_argument("--v19-first", action="store_true",
                   help="capture the v1.9 front end first (default order: "
                        "v20 then v19; rerun with this flag to rule out "
                        "order/drift effects)")
    p = sub.add_parser("ref", help="measure the reference sensor (emitter health)")
    p.add_argument("-s", "--seconds", type=float, default=DEFAULT_CAPTURE_S)
    p.add_argument("--freq", type=float,
                   help="drive frequency in Hz (firmware v1.9+; 405 M22 = 1, "
                        "default = board's 10 Hz)")
    p.add_argument("--set-baseline", action="store_true",
                   help="record this reading as the known-good emitter baseline")
    p.add_argument("--dc", action="store_true",
                   help="quick DC read of AIN1 only (no PWM / warm-up) - wiring checks")
    p.add_argument("--fe", choices=["v19", "v20"], help=fe_help)
    p = sub.add_parser("test", help="full battery -> offset -> ref -> capture sequence")
    p.add_argument("-s", "--seconds", type=float, default=DEFAULT_CAPTURE_S)
    p.add_argument("-o", "--output", help="save samples to this CSV file")
    p.add_argument("--freq", type=float,
                   help="drive frequency in Hz (firmware v1.9+; 405 M22 = 1, "
                        "default = board's 10 Hz)")
    p.add_argument("--skip-ref", "--no-ref", dest="skip_ref", action="store_true",
                   help="skip the optional AIN1 reference-sensor check")
    p.add_argument("--fe", choices=["v19", "v20"], help=fe_help)

    args = ap.parse_args()
    if args.command == "ports":
        cmd_ports(args)
        return

    rig = Esp32Rig(args.port or find_port())
    rig.connect()
    # For `pwm on` / `gate on` don't send PWM,OFF on close: with --no-hold the
    # user explicitly asked to leave (the reset will drop it anyway), and in
    # the default hold mode the drive was already turned off at the prompt.
    leave_on = args.command in ("pwm", "gate") and args.state == "on"
    try:
        {"bat": cmd_bat, "offset": cmd_offset, "pwm": cmd_pwm, "ref": cmd_ref,
         "gate": cmd_gate, "stream": cmd_stream, "test": cmd_test,
         "noisecmp": cmd_noisecmp}[args.command](rig, args)
    finally:
        rig.close(disable_pwm=not leave_on)


if __name__ == "__main__":
    main()
