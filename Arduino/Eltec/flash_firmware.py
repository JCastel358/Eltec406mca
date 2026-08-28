#!/usr/bin/env python3
"""
Flash Eltec.ino onto the bench ESP32 - one command, no Arduino IDE.

Why this exists: the rig board is a plain CP210x USB-UART, so the Arduino IDE
cannot identify it and shows no board next to the port. The IDE works fine once
Board and Port are picked by hand, but that is four menus every time and the
port silently disappears whenever another program (the rig app, a serial
monitor) is holding it open. This script does the whole job instead: find
arduino-cli, find the board, compile, upload, then prove over serial that the
firmware that is now running is the one that was just built.

Usage (from anywhere):
    python3 flash_firmware.py                     # live sketch -> the board
    python3 flash_firmware.py --list              # just show the serial ports
    python3 flash_firmware.py --port COM7         # skip autodetect
    python3 flash_firmware.py --sketch versions/Eltec_v2_2    # archived build
    python3 flash_firmware.py --check             # report IDN?/GATE?, flash nothing

Windows users can double-click run_flash_firmware.cmd next to this file;
Xubuntu users can run ./run_flash_firmware.sh.

Requires: the Arduino IDE 2.x (for its bundled arduino-cli and the installed
esp32 core) and pyserial. pyserial is only needed for port autodetect and the
post-flash check - with --port and --no-verify the script runs without it.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Board on every Eltec rig. Same string the READMEs and the version archive use.
FQBN = "esp32:esp32:esp32doit-devkit-v1"
BAUD_RATE = 500_000                      # matches Serial.begin() in Eltec.ino
EXPECTED_PREFIX = "ELTEC-ESP32-ADS1256,v"

# USB-serial bridges found on ESP32 dev boards, mirroring esp32_rig_readout.py.
KNOWN_USB_IDS = {
    (0x10C4, 0xEA60): "CP210x",
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x55D4): "CH9102",
    (0x0403, 0x6001): "FTDI",
    (0x303A, 0x1001): "ESP32 native USB",
}

# arduino-cli is not on PATH on a normal Arduino IDE install - it ships inside
# the IDE. These are the per-platform bundle locations, newest layout first.
CLI_BUNDLE_PATHS = {
    "Windows": [
        r"{LOCALAPPDATA}\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
        r"{PROGRAMFILES}\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
    ],
    "Linux": [
        "/opt/arduino-ide/resources/app/lib/backend/resources/arduino-cli",
        "{HOME}/.local/share/arduino-ide/resources/app/lib/backend/resources/arduino-cli",
        "{HOME}/arduino-ide/resources/app/lib/backend/resources/arduino-cli",
        "/usr/local/bin/arduino-cli",
    ],
    "Darwin": [
        "/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli",
    ],
}


class FlashError(Exception):
    """Anything that should stop the run with a readable message."""


# --------------------------------------------------------------- helpers ---
def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def find_arduino_cli(override: str | None) -> Path:
    """Locate arduino-cli: explicit path, then $ELTEC_ARDUINO_CLI, PATH, bundle."""
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise FlashError(f"--arduino-cli {path} does not exist.")
        return path

    env = os.environ.get("ELTEC_ARDUINO_CLI")
    if env:
        path = Path(env).expanduser()
        if not path.is_file():
            raise FlashError(f"ELTEC_ARDUINO_CLI points at {path}, which does not exist.")
        return path

    on_path = shutil.which("arduino-cli")
    if on_path:
        return Path(on_path)

    substitutions = {
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "PROGRAMFILES": os.environ.get("PROGRAMFILES", ""),
        "HOME": os.path.expanduser("~"),
    }
    for template in CLI_BUNDLE_PATHS.get(platform.system(), []):
        candidate = Path(template.format(**substitutions))
        if candidate.is_file():
            return candidate

    raise FlashError(
        "arduino-cli was not found.\n"
        "  It ships inside the Arduino IDE 2.x install, not on PATH. Install the\n"
        "  Arduino IDE (which also installs the esp32 core this board needs), or\n"
        "  point the script at an existing copy:\n"
        "      python3 flash_firmware.py --arduino-cli /path/to/arduino-cli\n"
        "      (or set the ELTEC_ARDUINO_CLI environment variable)"
    )


def load_pyserial(reason: str):
    try:
        import serial                       # noqa: F401
        from serial.tools import list_ports
    except ImportError as exc:
        raise FlashError(
            f"pyserial is needed to {reason}, and it is not installed.\n"
            "      pip install pyserial          (Ubuntu: sudo apt install python3-serial)\n"
            "  Or skip it: pass --port explicitly and add --no-verify."
        ) from exc
    return serial, list_ports


def describe_ports() -> list[tuple[str, str]]:
    _, list_ports = load_pyserial("list serial ports")
    found = []
    for port in list_ports.comports():
        chip = KNOWN_USB_IDS.get((port.vid, port.pid)) if port.vid is not None else None
        found.append((port.device, chip or (port.description or "unknown device")))
    return found


def find_port() -> str:
    """Pick the most ESP32-looking serial port. Same ranking as the CLI tools."""
    _, list_ports = load_pyserial("auto-detect the board's serial port")
    candidates = []
    for port in list_ports.comports():
        chip = KNOWN_USB_IDS.get((port.vid, port.pid)) if port.vid is not None else None
        if chip:
            candidates.append((0, port.device, chip))
        elif "USB" in (port.description or "").upper() or "usb" in port.device.lower():
            candidates.append((1, port.device, port.description or "?"))
    if not candidates:
        raise FlashError(
            "No ESP32 serial port found.\n"
            "  - Is the USB cable plugged into the ESP32 (and a data cable, not\n"
            "    charge-only)?\n"
            "  - Is another program holding the port - the rig app, an Arduino IDE\n"
            "    serial monitor, a stale python process? Close it and retry.\n"
            "  - Windows: check Device Manager for 'Silicon Labs CP210x'. If it is\n"
            "    missing or flagged, install the SiLabs CP210x VCP driver.\n"
            "  - Xubuntu: run 'ls /dev/ttyUSB*'; permission denied means\n"
            "    'sudo usermod -a -G dialout $USER' then log out and back in."
        )
    candidates.sort()
    _, device, chip = candidates[0]
    if len(candidates) > 1:
        others = ", ".join(dev for _, dev, _ in candidates[1:])
        print(f"    (other serial ports present: {others} - use --port to force one)")
    print(f"    Board: {device} ({chip})")
    return device


def run_cli(cli: Path, args: list[str], what: str) -> None:
    printable = " ".join(str(a) for a in args)
    print(f"    $ arduino-cli {printable}")
    result = subprocess.run([str(cli), *args], text=True)
    if result.returncode != 0:
        raise FlashError(
            f"{what} failed (arduino-cli exit code {result.returncode}). "
            "The compiler/uploader output above says why."
        )


def resolve_sketch(raw: str | None) -> Path:
    """Sketch dir to build: the live sketch by default, or an archived version."""
    if raw is None:
        return HERE
    candidate = Path(raw).expanduser()
    for path in (candidate, HERE / raw):
        path = path.resolve()
        if path.is_dir():
            if not list(path.glob("*.ino")):
                raise FlashError(f"{path} contains no .ino sketch.")
            return path
        if path.is_file() and path.suffix == ".ino":
            return path.parent
    raise FlashError(
        f"Sketch {raw!r} not found. Pass a folder holding an .ino - e.g.\n"
        "      --sketch versions/Eltec_v2_2"
    )


def sketch_version(sketch_dir: Path) -> str | None:
    """The version string the sketch will answer to IDN?, read from its source."""
    for ino in sorted(sketch_dir.glob("*.ino")):
        for line in ino.read_text(encoding="utf-8", errors="replace").splitlines():
            marker = f'Serial.println("{EXPECTED_PREFIX}'
            if marker in line:
                return line.split(marker, 1)[1].split('"', 1)[0]
    return None


def query_board(port: str, expected: str | None) -> bool:
    """Ask the freshly flashed board what it is. True when it matches."""
    serial, _ = load_pyserial("check the board after flashing")
    try:
        link = serial.Serial(port, BAUD_RATE, timeout=1.0)
    except Exception as exc:                       # pyserial raises several types
        raise FlashError(
            f"Could not open {port} to verify the flash: {exc}\n"
            "  The upload itself may still have succeeded - re-run with --check."
        ) from exc

    with link:
        time.sleep(2.0)                            # the ESP32 resets on port open
        link.reset_input_buffer()
        replies = {}
        for command in ("IDN?", "GATE?", "STATUS?"):
            link.write(command.encode() + b"\n")
            link.flush()
            deadline = time.time() + 2.0
            while time.time() < deadline:
                line = link.readline().decode(errors="replace").strip()
                if line:
                    replies[command] = line
                    print(f"    {command:9s} -> {line}")
                    break
            else:
                print(f"    {command:9s} -> (no reply)")

    identity = replies.get("IDN?", "")
    if not identity.startswith(EXPECTED_PREFIX):
        print("\n    The board did not answer IDN? with an Eltec firmware string.")
        return False
    if expected and identity != f"{EXPECTED_PREFIX}{expected}":
        print(f"\n    Expected {EXPECTED_PREFIX}{expected}, board reports {identity}.")
        print("    The upload may have gone to a different board.")
        return False
    return True


# ------------------------------------------------------------------ main ---
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile and upload the Eltec ESP32 firmware in one step.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Run with no arguments to flash the live Arduino/Eltec/Eltec.ino.",
    )
    parser.add_argument("--port", help="serial port (default: autodetect, e.g. COM3, /dev/ttyUSB0)")
    parser.add_argument("--sketch", help="sketch folder to build (default: the live Eltec.ino "
                                         "next to this script; e.g. versions/Eltec_v2_2)")
    parser.add_argument("--fqbn", default=FQBN, help=f"board FQBN (default: {FQBN})")
    parser.add_argument("--arduino-cli", help="path to arduino-cli if it is somewhere unusual")
    parser.add_argument("--list", action="store_true", help="list serial ports and exit")
    parser.add_argument("--check", action="store_true",
                        help="query the board's IDN?/GATE? and exit without flashing")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the post-flash serial check (no pyserial needed)")
    parser.add_argument("--pause", action="store_true",
                        help="wait for Enter before exiting (used by the double-click launchers)")
    args = parser.parse_args(argv)

    status = 0
    try:
        if args.list:
            ports = describe_ports()
            if not ports:
                print("No serial ports found.")
            for device, description in ports:
                print(f"  {device}  {description}")
            return 0

        if args.check:
            port = args.port or find_port()
            step(f"Reading the board on {port}")
            return 0 if query_board(port, None) else 1

        sketch = resolve_sketch(args.sketch)
        expected = sketch_version(sketch)

        step("Locating arduino-cli")
        cli = find_arduino_cli(args.arduino_cli)
        print(f"    {cli}")

        step("Finding the board")
        port = args.port or find_port()

        step(f"Compiling {sketch.name}" + (f" (firmware v{expected})" if expected else ""))
        run_cli(cli, ["compile", "--fqbn", args.fqbn, str(sketch)], "Compile")

        step(f"Uploading to {port}")
        run_cli(cli, ["upload", "-p", port, "--fqbn", args.fqbn, str(sketch)], "Upload")

        if args.no_verify:
            print("\nUploaded. Skipped the serial check (--no-verify).")
            return 0

        step("Checking what the board is running")
        if query_board(port, expected):
            print(f"\nDone - the board is running {EXPECTED_PREFIX}{expected}.")
            print("GATE? above shows which pin the emitter gate drives at boot;")
            print("the rig app retargets it with PIN,<n> after connect.")
        else:
            print("\nFlashed, but the board did not confirm the expected firmware.")
            status = 1

    except FlashError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        status = 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        status = 130
    finally:
        if args.pause:
            try:
                input("\nPress Enter to close...")
            except (EOFError, KeyboardInterrupt):
                pass
    return status


if __name__ == "__main__":
    sys.exit(main())
