#!/usr/bin/env bash
# Install v6 XFCE desktop and per-user Applications-menu launchers.

set -euo pipefail

SOURCE_PATH=${BASH_SOURCE[0]}
if command -v readlink >/dev/null 2>&1; then
    RESOLVED_SOURCE=$(readlink -f -- "$SOURCE_PATH" 2>/dev/null || true)
    if [[ -n "$RESOLVED_SOURCE" ]]; then
        SOURCE_PATH=$RESOLVED_SOURCE
    fi
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "$SOURCE_PATH")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)
LAUNCHER="$SCRIPT_DIR/run_eltec_406mca_esp32_tester.sh"
APP_PATH="$SCRIPT_DIR/eltec_406mca_esp32_tester.py"
ICON_PATH="$SCRIPT_DIR/assets/eltec_desktop_icon.png"
ENTRY_NAME='com.eltec.406mca-esp32-tester-v6.desktop'
APPLICATIONS_DIR=${XDG_DATA_HOME:-"$HOME/.local/share"}/applications

if command -v xdg-user-dir >/dev/null 2>&1; then
    DESKTOP_DIR=$(xdg-user-dir DESKTOP 2>/dev/null || true)
else
    DESKTOP_DIR=''
fi
if [[ -z "$DESKTOP_DIR" || "$DESKTOP_DIR" != /* ]]; then
    DESKTOP_DIR="$HOME/Desktop"
fi

MENU_ENTRY="$APPLICATIONS_DIR/$ENTRY_NAME"
DESKTOP_ENTRY="$DESKTOP_DIR/Eltec 406MCA ESP32 Tester v6.desktop"

if [[ ${1:-} == '--uninstall' ]]; then
    rm -f -- "$MENU_ENTRY" "$DESKTOP_ENTRY"
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
    fi
    printf 'Removed:\n  %s\n  %s\n' "$MENU_ENTRY" "$DESKTOP_ENTRY"
    exit 0
elif [[ $# -ne 0 ]]; then
    printf 'Usage: %s [--uninstall]\n' "$0" >&2
    exit 2
fi

for required_file in "$LAUNCHER" "$APP_PATH" "$ICON_PATH"; do
    if [[ ! -f "$required_file" ]]; then
        printf 'Required file not found: %s\n' "$required_file" >&2
        exit 1
    fi
done

# Desktop Entry Exec arguments have their own quoting rules. Quote the absolute
# launcher path so a checkout whose directory contains spaces still works.
desktop_exec_quote() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//\"/\\\"}
    value=${value//\`/\\\`}
    value=${value//\$/\\\$}
    value=${value//%/%%}
    printf '"%s"' "$value"
}

# Escape characters with special meaning in ordinary Desktop Entry values.
desktop_value() {
    local value=$1
    value=${value//\\/\\\\}
    value=${value//$'\n'/\\n}
    value=${value//$'\r'/\\r}
    value=${value//$'\t'/\\t}
    printf '%s' "$value"
}

EXEC_VALUE=$(desktop_exec_quote "$LAUNCHER")
ICON_VALUE=$(desktop_value "$ICON_PATH")
TMP_ENTRY=$(mktemp "${TMPDIR:-/tmp}/eltec-406mca-v6-launcher.XXXXXX.desktop")
trap 'rm -f -- "$TMP_ENTRY"' EXIT

{
    printf '%s\n' '[Desktop Entry]'
    printf '%s\n' 'Version=1.0'
    printf '%s\n' 'Type=Application'
    printf '%s\n' 'Name=Eltec 406MCA ESP32 Tester v6'
    printf '%s\n' 'GenericName=406MCA Adaptive Emitter Tester'
    printf '%s\n' 'Comment=Test Eltec 406MCA emitters with adaptive peak stabilization'
    printf 'Exec=%s\n' "$EXEC_VALUE"
    printf 'Icon=%s\n' "$ICON_VALUE"
    printf '%s\n' 'Terminal=false'
    printf '%s\n' 'Categories=Utility;'
    printf '%s\n' 'StartupNotify=true'
    printf '%s\n' 'X-GNOME-UsesNotifications=true'
} >"$TMP_ENTRY"

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$TMP_ENTRY"
fi

mkdir -p -- "$APPLICATIONS_DIR" "$DESKTOP_DIR"
chmod 755 -- "$LAUNCHER"
install -m 755 -- "$TMP_ENTRY" "$MENU_ENTRY"
install -m 755 -- "$TMP_ENTRY" "$DESKTOP_ENTRY"

# XFCE checks both the executable bit and the GIO trusted metadata. The metadata
# operation can be unavailable over SSH, so keep the valid executable launcher
# and let the user choose "Allow Launching" once if GIO cannot set it here.
TRUST_NOTE=''
if command -v gio >/dev/null 2>&1; then
    if ! gio set "$DESKTOP_ENTRY" metadata::trusted true >/dev/null 2>&1; then
        TRUST_NOTE=' (right-click it and choose "Allow Launching" if XFCE asks)'
    fi
else
    TRUST_NOTE=' (right-click it and choose "Allow Launching" if XFCE asks)'
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
fi

printf 'Installed the Eltec 406MCA ESP32 Tester v6 launcher:\n'
printf '  Desktop:          %s%s\n' "$DESKTOP_ENTRY" "$TRUST_NOTE"
printf '  Applications menu: %s\n' "$MENU_ENTRY"
printf '  Icon:              %s\n' "$ICON_PATH"
printf '\nKeep this repository at %s, or rerun this installer after moving it.\n' "$REPO_ROOT"
