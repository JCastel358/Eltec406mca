#!/usr/bin/env bash
# Non-configuring software and optional unified-rig ESP32 health checks.

set -uo pipefail

SCRIPT_NAME=${0##*/}
CHECK_HARDWARE=0
CHECK_GUI=0
REQUESTED_PORT=''
FAILURES=0
WARNINGS=0

usage() {
    cat <<EOF
Usage: $SCRIPT_NAME [--gui] [--hardware] [--port /dev/ttyUSB0]

Check the Xubuntu workstation setup. By default this only enumerates serial
adapters. --gui opens and closes the unified selector without starting a test.
--hardware opens the ESP32 through both model backends, validates firmware and
the model-specific front-end switching, then closes it with PWM forced off.
Opening the port resets this ESP32 board.
EOF
}

pass() {
    printf '[PASS] %s\n' "$*"
}

warn() {
    printf '[WARN] %s\n' "$*"
    WARNINGS=$((WARNINGS + 1))
}

fail() {
    printf '[FAIL] %s\n' "$*"
    FAILURES=$((FAILURES + 1))
}

while (( $# )); do
    case $1 in
        --hardware)
            CHECK_HARDWARE=1
            ;;
        --gui)
            CHECK_GUI=1
            ;;
        --port)
            shift
            (( $# )) || {
                usage >&2
                exit 2
            }
            REQUESTED_PORT=$1
            CHECK_HARDWARE=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            printf 'Unknown option: %s\n' "$1" >&2
            exit 2
            ;;
    esac
    shift
done

SOURCE_PATH=${BASH_SOURCE[0]}
if command -v readlink >/dev/null 2>&1; then
    RESOLVED_SOURCE=$(readlink -f -- "$SOURCE_PATH" 2>/dev/null || true)
    if [[ -n $RESOLVED_SOURCE ]]; then
        SOURCE_PATH=$RESOLVED_SOURCE
    fi
fi
REPO_ROOT=$(cd -- "$(dirname -- "$SOURCE_PATH")" && pwd -P)
PRODUCTION_DIR="$REPO_ROOT/tech_app/eltec_rig"
MODEL_405_DIR="$PRODUCTION_DIR/m405m22"
MODEL_406_DIR="$PRODUCTION_DIR/m406mca"
RESULTS_405_DIR="${HOME:-}/Documents/Eltec_405M22_Test_Results/405m22_esp32"
RESULTS_406_DIR="${HOME:-}/Documents/Eltec_406MCA_Test_Results/v6_1_esp32"
DOCTOR_TMP=$(mktemp -d "${TMPDIR:-/tmp}/eltec-xubuntu-doctor.XXXXXX") || {
    printf 'Could not create a temporary health-check directory.\n' >&2
    exit 1
}
cleanup_doctor_tmp() {
    if [[ -n ${DOCTOR_TMP:-} && -d $DOCTOR_TMP ]]; then
        rm -rf -- "$DOCTOR_TMP"
    fi
}
trap cleanup_doctor_tmp EXIT

printf 'Unified Eltec Test Rig station health check\n'
printf 'Repository: %s\n\n' "$REPO_ROOT"

OS_NAME='unknown'
OS_VERSION=''
OS_ID=''
OS_LIKE=''
if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    OS_NAME=${PRETTY_NAME:-unknown}
    OS_VERSION=${VERSION_ID:-}
    OS_ID=${ID:-}
    OS_LIKE=${ID_LIKE:-}
fi
if [[ $OS_ID != ubuntu && " $OS_LIKE " != *' ubuntu '* ]]; then
    fail "$OS_NAME is not an Ubuntu/Xubuntu base"
elif [[ $OS_VERSION == 24.04 ]]; then
    pass "$OS_NAME matches the locally exercised 24.04 LTS software base"
elif [[ $OS_VERSION == 26.04 ]]; then
    warn "$OS_NAME is the supported long-lived candidate; complete a GUI and real-fixture pilot before production qualification"
else
    warn "$OS_NAME is not the supported Xubuntu/Ubuntu 24.04 or 26.04 LTS base"
fi

XUBUNTU_DESKTOP_INSTALLED=0
for desktop_package in xubuntu-desktop xubuntu-desktop-minimal; do
    if [[ $(dpkg-query -W -f='${Status}' "$desktop_package" 2>/dev/null || true) == 'install ok installed' ]]; then
        XUBUNTU_DESKTOP_INSTALLED=1
        break
    fi
done
if (( XUBUNTU_DESKTOP_INSTALLED )) \
    || [[ ${XDG_CURRENT_DESKTOP:-} == *XFCE* || ${XDG_CURRENT_DESKTOP:-} == *Xfce* ]]; then
    pass "XFCE/Xubuntu desktop is detected"
else
    warn "XFCE/Xubuntu desktop is not detected; headless package tests do not prove that the operator GUI can run"
fi

ARCHITECTURE=$(dpkg --print-architecture 2>/dev/null || uname -m)
if [[ $ARCHITECTURE == amd64 ]]; then
    pass "Architecture is amd64"
else
    warn "Architecture $ARCHITECTURE has not been fleet-qualified"
fi

REQUIRED_FILES=(
    "$PRODUCTION_DIR/eltec_rig_tester.py"
    "$PRODUCTION_DIR/sensor_versions.py"
    "$PRODUCTION_DIR/run_eltec_rig_tester.sh"
    "$MODEL_405_DIR/eltec_405m22_esp32_tester.py"
    "$MODEL_405_DIR/esp32_backend.py"
    "$MODEL_405_DIR/stability_settings.json"
    "$MODEL_406_DIR/eltec_406mca_esp32_tester.py"
    "$MODEL_406_DIR/esp32_backend.py"
    "$MODEL_406_DIR/stability_settings.json"
    "$REPO_ROOT/Arduino/Eltec/Eltec.ino"
    "$REPO_ROOT/Arduino/Eltec/ESP32_ADS1256_Wiring_v2_0.md"
)
MISSING_FILES=0
for required_file in "${REQUIRED_FILES[@]}"; do
    if [[ ! -f $required_file ]]; then
        fail "Required file is missing: $required_file"
        MISSING_FILES=1
    fi
done
if (( ! MISSING_FILES )); then
    pass "Unified selector, both model apps, firmware, and wiring files are present"
fi

REQUIRED_COMMANDS=(python3 git xdg-user-dir desktop-file-validate notify-send gio lsusb)
for required_command in "${REQUIRED_COMMANDS[@]}"; do
    if command -v "$required_command" >/dev/null 2>&1; then
        pass "Command available: $required_command"
    else
        fail "Command missing: $required_command (rerun ./setup_xubuntu.sh)"
    fi
done

for module in tkinter numpy serial matplotlib; do
    if PYTHONDONTWRITEBYTECODE=1 MPLCONFIGDIR="$DOCTOR_TMP/matplotlib" python3 -c "import $module" >/dev/null 2>&1; then
        pass "Python module available: $module"
    else
        fail "Python module missing or broken: $module"
    fi
done

if PYTHONDONTWRITEBYTECODE=1 python3 -c 'import tkinter; tkinter.Tcl()' >/dev/null 2>&1; then
    pass "Tk interpreter initializes"
else
    fail "Tk interpreter could not initialize"
fi

if (( CHECK_GUI )) || [[ -n ${DISPLAY:-} ]]; then
    if [[ -z ${DISPLAY:-} ]]; then
        fail "GUI check was requested but DISPLAY is not set; run it inside the logged-in XFCE session"
    elif (cd -- "$PRODUCTION_DIR" && PYTHONDONTWRITEBYTECODE=1 MPLCONFIGDIR="$DOCTOR_TMP/matplotlib" python3 -c 'import eltec_rig_tester as app; root = app.EltecRigSelector(); root.update_idletasks(); root.destroy()') >/dev/null 2>&1; then
        pass "The unified selector GUI opens in the current graphical session"
    elif (( CHECK_GUI )); then
        fail "The unified selector GUI could not open on DISPLAY=${DISPLAY:-unset}"
    else
        warn "DISPLAY is set, but the unified GUI could not open; rerun with --gui inside the technician's XFCE session"
    fi
else
    warn "Graphical-session smoke check not run; use --gui inside the logged-in XFCE desktop"
fi

for model_entry in "405 M22:$MODEL_405_DIR" "406 MCA:$MODEL_406_DIR"; do
    MODEL_NAME=${model_entry%%:*}
    MODEL_DIR=${model_entry#*:}
    SETTINGS_SUMMARY=$(cd -- "$MODEL_DIR" && PYTHONDONTWRITEBYTECODE=1 python3 -c 'from stability_analysis import load_stability_settings; value = load_stability_settings(); print(f"{value.peak_delta_threshold_mv:.3f} mV, {value.consecutive_deltas_required} deltas")' 2>/dev/null)
    SETTINGS_STATUS=$?
    if (( SETTINGS_STATUS == 0 )); then
        pass "$MODEL_NAME stability settings load: $SETTINGS_SUMMARY"
    else
        fail "$MODEL_NAME stability_settings.json is missing or invalid"
    fi
done

TECHNICIAN_USER=$(id -un)
DIALOUT_ENTRY=$(getent group dialout 2>/dev/null || true)
if [[ -z $DIALOUT_ENTRY ]]; then
    fail "Ubuntu dialout group is missing"
else
    DIALOUT_GID=$(printf '%s\n' "$DIALOUT_ENTRY" | cut -d: -f3)
    CONFIGURED_GROUPS=$(id -nG "$TECHNICIAN_USER" 2>/dev/null || true)
    ACTIVE_GIDS=" $(id -G 2>/dev/null || true) "
    if [[ " $CONFIGURED_GROUPS " != *' dialout '* ]]; then
        fail "$TECHNICIAN_USER is not configured in dialout"
    elif [[ $ACTIVE_GIDS != *" $DIALOUT_GID "* ]]; then
        warn "$TECHNICIAN_USER is configured in dialout, but this login has not activated it; sign out and back in or reboot"
    else
        pass "$TECHNICIAN_USER has active dialout serial access"
    fi
fi

APPLICATIONS_DIR=${XDG_DATA_HOME:-"${HOME:-}/.local/share"}/applications
MENU_ENTRY="$APPLICATIONS_DIR/com.eltec.test-rig.desktop"
EXPECTED_RUNNER="$PRODUCTION_DIR/run_eltec_rig_tester.sh"
EXPECTED_ICON="$PRODUCTION_DIR/assets/eltec_desktop_icon.png"
if [[ -f $MENU_ENTRY ]]; then
    if desktop-file-validate "$MENU_ENTRY" >/dev/null 2>&1; then
        pass "Applications-menu launcher is installed and valid"
    else
        fail "Applications-menu launcher is invalid: $MENU_ENTRY"
    fi
    if grep -Fq -- "$EXPECTED_RUNNER" "$MENU_ENTRY" \
        && grep -Fq -- "$EXPECTED_ICON" "$MENU_ENTRY"; then
        pass "Applications-menu launcher points to this checkout"
    else
        fail "Applications-menu launcher points to a stale checkout; rerun setup"
    fi
else
    fail "Applications-menu launcher is missing: $MENU_ENTRY"
fi

DESKTOP_DIR=$(xdg-user-dir DESKTOP 2>/dev/null || true)
if [[ -z $DESKTOP_DIR || $DESKTOP_DIR != /* ]]; then
    DESKTOP_DIR="${HOME:-}/Desktop"
fi
DESKTOP_ENTRY="$DESKTOP_DIR/Eltec Test Rig.desktop"
if [[ -x $DESKTOP_ENTRY ]]; then
    pass "Executable XFCE desktop launcher is installed"
elif [[ -f $DESKTOP_ENTRY ]]; then
    fail "Desktop launcher exists but is not executable: $DESKTOP_ENTRY"
else
    fail "Desktop launcher is missing: $DESKTOP_ENTRY"
fi
if [[ -f $DESKTOP_ENTRY ]]; then
    if grep -Fq -- "$EXPECTED_RUNNER" "$DESKTOP_ENTRY" \
        && grep -Fq -- "$EXPECTED_ICON" "$DESKTOP_ENTRY"; then
        pass "XFCE desktop launcher points to this checkout"
    else
        fail "XFCE desktop launcher points to a stale checkout; rerun setup"
    fi
    TRUST_INFO=$(gio info -a metadata::trusted "$DESKTOP_ENTRY" 2>/dev/null || true)
    if [[ $TRUST_INFO == *'metadata::trusted: true'* ]]; then
        pass "XFCE desktop launcher is marked trusted"
    else
        warn "XFCE may ask once to right-click the desktop launcher and choose Allow Launching"
    fi
fi

for results_entry in "405 M22:$RESULTS_405_DIR" "406 MCA:$RESULTS_406_DIR"; do
    MODEL_NAME=${results_entry%%:*}
    RESULTS_DIR=${results_entry#*:}
    if [[ -d $RESULTS_DIR && -w $RESULTS_DIR ]]; then
        pass "$MODEL_NAME results directory exists and is writable: $RESULTS_DIR"
    elif [[ -d $RESULTS_DIR ]]; then
        fail "$MODEL_NAME results directory is not writable: $RESULTS_DIR"
    else
        fail "$MODEL_NAME results directory is missing: $RESULTS_DIR"
    fi
done

STATE_DIR=${XDG_STATE_HOME:-"${HOME:-}/.local/state"}/eltec-rig
if [[ -r $STATE_DIR/install-info.txt ]]; then
    INSTALL_COMMIT=$(sed -n 's/^git_commit=//p' "$STATE_DIR/install-info.txt" | head -n 1)
    INSTALL_405_SETTINGS_SHA=$(sed -n 's/^m405m22_stability_settings_sha256=//p' "$STATE_DIR/install-info.txt" | head -n 1)
    INSTALL_406_SETTINGS_SHA=$(sed -n 's/^m406mca_stability_settings_sha256=//p' "$STATE_DIR/install-info.txt" | head -n 1)
    pass "Installation record is present (commit ${INSTALL_COMMIT:-unknown})"
else
    INSTALL_COMMIT=''
    INSTALL_405_SETTINGS_SHA=''
    INSTALL_406_SETTINGS_SHA=''
    warn "Installation record is missing; rerun ./setup_xubuntu.sh"
fi

if [[ -d $REPO_ROOT/.git ]]; then
    CURRENT_COMMIT=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || true)
    if [[ -n $CURRENT_COMMIT ]]; then
        pass "Git checkout is readable at commit $CURRENT_COMMIT"
        CURRENT_FULL_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)
        if [[ -n $INSTALL_COMMIT && $INSTALL_COMMIT != not-a-git-checkout && $INSTALL_COMMIT != "$CURRENT_FULL_COMMIT" ]]; then
            fail "Current Git commit does not match the installation record; rerun verified setup/update"
        fi
        if git -C "$REPO_ROOT" diff --quiet --ignore-submodules -- \
            && git -C "$REPO_ROOT" diff --cached --quiet --ignore-submodules --; then
            pass "Tracked application files have no local modifications"
        else
            fail "Tracked application files have local modifications"
        fi
    else
        fail "Git checkout metadata is not readable"
    fi
else
    warn "This is not a Git checkout; online/offline bundle updates are unavailable"
fi

for settings_entry in \
    "405 M22:$MODEL_405_DIR/stability_settings.json:$INSTALL_405_SETTINGS_SHA" \
    "406 MCA:$MODEL_406_DIR/stability_settings.json:$INSTALL_406_SETTINGS_SHA"; do
    MODEL_NAME=${settings_entry%%:*}
    SETTINGS_REST=${settings_entry#*:}
    SETTINGS_FILE=${SETTINGS_REST%%:*}
    INSTALL_SETTINGS_SHA=${SETTINGS_REST#*:}
    if [[ -n $INSTALL_SETTINGS_SHA && -f $SETTINGS_FILE ]]; then
        CURRENT_SETTINGS_SHA=$(sha256sum "$SETTINGS_FILE" | cut -d' ' -f1)
        if [[ $CURRENT_SETTINGS_SHA == "$INSTALL_SETTINGS_SHA" ]]; then
            pass "$MODEL_NAME stability settings match the installation record"
        else
            fail "$MODEL_NAME stability settings differ from the installation record"
        fi
    fi
done

if [[ -f $MODEL_405_DIR/esp32_backend.py ]]; then
    CANDIDATE_OUTPUT=$(cd -- "$MODEL_405_DIR" && PYTHONDONTWRITEBYTECODE=1 python3 -c '
from esp32_backend import discover_candidate_ports
ports = discover_candidate_ports()
if not ports:
    raise SystemExit(3)
for item in ports:
    print(f"{item.device} ({item.bridge}, {item.vid:04x}:{item.pid:04x})")
' 2>&1)
    CANDIDATE_STATUS=$?
    if (( CANDIDATE_STATUS == 0 )); then
        pass "Supported ESP32 serial adapter detected: $CANDIDATE_OUTPUT"
    elif (( CANDIDATE_STATUS == 3 )); then
        warn "No supported ESP32 serial adapter is currently detected"
    else
        fail "Serial adapter discovery failed: $CANDIDATE_OUTPUT"
    fi
fi

if (( CHECK_HARDWARE )); then
    printf '\nHardware serial/firmware/front-end check (the port resets twice; PWM is forced off on close):\n'
    HARDWARE_OUTPUT=$(cd -- "$PRODUCTION_DIR" && PYTHONDONTWRITEBYTECODE=1 python3 -c '
import sys
from m405m22.esp32_backend import Esp32Rig as Rig405
from m406mca.esp32_backend import Esp32Rig as Rig406
port = sys.argv[1] or None
for model_name, rig_class in (("405 M22", Rig405), ("406 MCA", Rig406)):
    rig = rig_class(port)
    try:
        rig.connect()
        identity = rig.identity.text if rig.identity is not None else "Eltec ESP32"
        print(f"{model_name}: {identity} connected on {rig.port_name}")
    finally:
        rig.close()
' "$REQUESTED_PORT" 2>&1)
    HARDWARE_STATUS=$?
    if (( HARDWARE_STATUS == 0 )); then
        pass "$HARDWARE_OUTPUT"
    else
        fail "$HARDWARE_OUTPUT"
    fi
else
    printf '\nHardware serial/firmware/front-end check was not requested; use --hardware after connecting the fixture.\n'
fi

printf '\nSummary: %d failure(s), %d warning(s)\n' "$FAILURES" "$WARNINGS"
if (( FAILURES )); then
    exit 1
fi
exit 0
