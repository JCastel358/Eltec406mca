#!/usr/bin/env bash
# Flash Arduino/Eltec/Eltec.ino onto the bench ESP32 (Xubuntu / any Linux).
# Xubuntu counterpart of run_flash_firmware.cmd. Arguments pass through, e.g.
#   ./run_flash_firmware.sh --sketch versions/Eltec_v2_2
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${ELTEC_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            break
        fi
    done
fi
if [ -z "$PYTHON_BIN" ]; then
    echo "Python 3 was not found. Install it, or set ELTEC_PYTHON." >&2
    exit 1
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/flash_firmware.py" "$@"
