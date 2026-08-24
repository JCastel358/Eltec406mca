#!/usr/bin/env bash
# Launch the Xubuntu/ESP32 tester from any working directory.

set -u

SOURCE_PATH=${BASH_SOURCE[0]}
if command -v readlink >/dev/null 2>&1; then
    RESOLVED_SOURCE=$(readlink -f -- "$SOURCE_PATH" 2>/dev/null || true)
    if [[ -n "$RESOLVED_SOURCE" ]]; then
        SOURCE_PATH=$RESOLVED_SOURCE
    fi
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "$SOURCE_PATH")" && pwd -P)
APP_PATH="$SCRIPT_DIR/eltec_406mca_esp32_tester.py"
PYTHON_BIN=${ELTEC_PYTHON:-python3}

STATE_ROOT=${XDG_STATE_HOME:-"$HOME/.local/state"}
LOG_DIR="$STATE_ROOT/eltec-406mca-esp32"
if ! mkdir -p -- "$LOG_DIR" 2>/dev/null; then
    LOG_DIR="${TMPDIR:-/tmp}/eltec-406mca-esp32-${UID}"
    mkdir -p -- "$LOG_DIR" || {
        printf 'Eltec 406MCA ESP32 Tester: could not create a log directory.\n' >&2
        exit 1
    }
fi

LOG_FILE="$LOG_DIR/launcher.log"
if [[ -f "$LOG_FILE" ]]; then
    LOG_SIZE=$(stat -c %s -- "$LOG_FILE" 2>/dev/null || printf '0')
    if [[ "$LOG_SIZE" =~ ^[0-9]+$ ]] && (( LOG_SIZE > 5242880 )); then
        mv -f -- "$LOG_FILE" "$LOG_FILE.previous" 2>/dev/null || true
    fi
fi
touch -- "$LOG_FILE"
chmod 600 -- "$LOG_FILE" 2>/dev/null || true

show_error() {
    local message=$1
    local details="${message}"$'\n\n'"Details: ${LOG_FILE}"

    printf '%s\n' "$(date '+%Y-%m-%d %H:%M:%S') ERROR: $message" >>"$LOG_FILE"
    printf 'Eltec 406MCA ESP32 Tester: %s\nDetails: %s\n' "$message" "$LOG_FILE" >&2

    if command -v notify-send >/dev/null 2>&1 \
        && notify-send --urgency=critical --app-name='Eltec 406MCA ESP32 Tester' \
            'Eltec tester could not start' "$details" >/dev/null 2>&1; then
        return
    fi
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --title='Eltec tester could not start' --text="$details" \
            >/dev/null 2>&1 || true
    elif command -v xmessage >/dev/null 2>&1; then
        xmessage -center "$details" >/dev/null 2>&1 || true
    fi
}

if [[ ! -f "$APP_PATH" ]]; then
    show_error "Application file not found: $APP_PATH"
    exit 1
fi

if [[ "$PYTHON_BIN" == */* ]]; then
    if [[ ! -x "$PYTHON_BIN" ]]; then
        show_error "Python is not executable: $PYTHON_BIN"
        exit 1
    fi
elif ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    show_error "Python 3 was not found. Install python3 and the dependencies listed in README.md."
    exit 1
fi

{
    printf '\n[%s] Starting Eltec 406MCA ESP32 Tester\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    printf 'Application: %s\n' "$APP_PATH"
    printf 'Python: %s\n' "$PYTHON_BIN"
    cd -- "$SCRIPT_DIR"
    PYTHONUNBUFFERED=1 "$PYTHON_BIN" "$APP_PATH" "$@"
} >>"$LOG_FILE" 2>&1
STATUS=$?

if (( STATUS != 0 )); then
    show_error "The application exited with status $STATUS."
fi

exit "$STATUS"
