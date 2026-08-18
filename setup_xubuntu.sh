#!/usr/bin/env bash
# Provision one Xubuntu workstation for the production Eltec 406MCA v6 tester.

set -Eeuo pipefail

SCRIPT_NAME=${0##*/}
DRY_RUN=0
SKIP_APT=0
SKIP_TESTS=0
WITH_EXPERIMENTAL=0
FORCE_UNSUPPORTED=0
RELOGIN_REQUIRED=0
PREPARE_ONLY=0
ALLOW_MUTABLE_CHECKOUT=0

usage() {
    cat <<EOF
Usage: $SCRIPT_NAME [options]

Install or repair the production Eltec 406MCA v6 workstation setup.
Run this as the signed-in technician account, not with sudo.

Options:
  --dry-run             Show changes without making them
  --skip-apt            Do not update/install Ubuntu packages
  --skip-tests          Skip compilation and the v6 software test suite
  --with-experimental   Also install the isolated v6.1 evaluation launcher
  --force-unsupported   Continue on an unqualified OS release/architecture
  --allow-mutable-checkout
                        Engineering override: permit setup from a Git branch
  --prepare-only        Install system prerequisites only (updater/internal)
  -h, --help            Show this help
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

note() {
    printf '\n==> %s\n' "$*"
}

warn() {
    printf 'WARNING: %s\n' "$*" >&2
}

print_command() {
    printf '  +'
    printf ' %q' "$@"
    printf '\n'
}

run() {
    if (( DRY_RUN )); then
        print_command "$@"
    else
        "$@"
    fi
}

while (( $# )); do
    case $1 in
        --dry-run)
            DRY_RUN=1
            ;;
        --skip-apt)
            SKIP_APT=1
            ;;
        --skip-tests)
            SKIP_TESTS=1
            ;;
        --with-experimental)
            WITH_EXPERIMENTAL=1
            ;;
        --force-unsupported)
            FORCE_UNSUPPORTED=1
            ;;
        --allow-mutable-checkout)
            ALLOW_MUTABLE_CHECKOUT=1
            ;;
        --prepare-only)
            PREPARE_ONLY=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            die "Unknown option: $1"
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
PRODUCTION_DIR="$REPO_ROOT/tech_app/v6_esp32"
EXPERIMENTAL_DIR="$REPO_ROOT/tech_app/v6_1_esp32"
FAILURE_CALIBRATION_DIR="$REPO_ROOT/tech_app/v6_1_failure_calibration"
V1_MATH="$REPO_ROOT/tech_app/v1_single_sensor/eltec_406mca_tester.py"
DOCTOR="$REPO_ROOT/doctor_xubuntu.sh"

REQUIRED_FILES=(
    "$PRODUCTION_DIR/eltec_406mca_esp32_tester.py"
    "$PRODUCTION_DIR/esp32_backend.py"
    "$PRODUCTION_DIR/stability_analysis.py"
    "$PRODUCTION_DIR/stability_settings.json"
    "$PRODUCTION_DIR/assets/eltec_desktop_icon.png"
    "$PRODUCTION_DIR/run_eltec_406mca_esp32_tester.sh"
    "$PRODUCTION_DIR/install_xubuntu_launcher.sh"
    "$V1_MATH"
    "$DOCTOR"
    "$REPO_ROOT/update_xubuntu.sh"
    "$REPO_ROOT/rollback_xubuntu.sh"
    "$REPO_ROOT/backup_eltec_results.sh"
    "$REPO_ROOT/restore_eltec_results.sh"
    "$REPO_ROOT/make_update_bundle.sh"
)

for required_file in "${REQUIRED_FILES[@]}"; do
    [[ -f $required_file ]] || die "Required repository file is missing: $required_file"
done

(( EUID != 0 )) || die "Do not run this script with sudo. Run it as the technician account; the script requests sudo only for system packages and dialout access."

TECHNICIAN_USER=$(id -un)
TECHNICIAN_HOME=${HOME:-}
[[ -n $TECHNICIAN_HOME && $TECHNICIAN_HOME == /* ]] || die "HOME must be an absolute path for the technician account."
[[ -d $TECHNICIAN_HOME ]] || die "Technician home directory does not exist: $TECHNICIAN_HOME"
if (( ! DRY_RUN )); then
    [[ -w $TECHNICIAN_HOME ]] || die "Technician home directory is not writable: $TECHNICIAN_HOME"
fi

OS_ID=''
OS_LIKE=''
OS_VERSION=''
OS_NAME='unknown Ubuntu release'
if [[ -r /etc/os-release ]]; then
    # /etc/os-release is owned by the operating system and contains shell-safe
    # distribution metadata.
    # shellcheck disable=SC1091
    source /etc/os-release
    OS_ID=${ID:-}
    OS_LIKE=${ID_LIKE:-}
    OS_VERSION=${VERSION_ID:-}
    OS_NAME=${PRETTY_NAME:-$OS_NAME}
fi

IS_UBUNTU=0
if [[ $OS_ID == ubuntu || " $OS_LIKE " == *' ubuntu '* ]]; then
    IS_UBUNTU=1
fi

ARCHITECTURE=$(dpkg --print-architecture 2>/dev/null || uname -m)
SUPPORTED_OS=0
case $OS_VERSION in
    24.04|26.04)
        SUPPORTED_OS=1
        ;;
esac

if (( ! IS_UBUNTU || ! SUPPORTED_OS )) && (( ! FORCE_UNSUPPORTED )); then
    die "This fleet installer supports Xubuntu/Ubuntu 24.04 or 26.04 LTS. Detected: $OS_NAME. Use --force-unsupported only for an engineering trial."
fi
if [[ $ARCHITECTURE != amd64 ]] && (( ! FORCE_UNSUPPORTED )); then
    die "This fleet installer is qualified for amd64 computers. Detected: $ARCHITECTURE. Use --force-unsupported only for an engineering trial."
fi
if (( ! IS_UBUNTU || ! SUPPORTED_OS )); then
    warn "Continuing on unqualified operating system: $OS_NAME"
fi
if [[ $ARCHITECTURE != amd64 ]]; then
    warn "Continuing on unqualified architecture: $ARCHITECTURE"
fi

note "Target: $OS_NAME ($ARCHITECTURE), user $TECHNICIAN_USER"
printf 'Repository: %s\n' "$REPO_ROOT"
if [[ $OS_VERSION == 26.04 ]]; then
    warn "Xubuntu 26.04 is the long-lived fleet candidate, but this repository was exercised locally on 24.04. Complete one GUI and real-fixture pilot before cloning 26.04 across production."
fi
XUBUNTU_DESKTOP_INSTALLED=0
for desktop_package in xubuntu-desktop xubuntu-desktop-minimal; do
    if [[ $(dpkg-query -W -f='${Status}' "$desktop_package" 2>/dev/null || true) == 'install ok installed' ]]; then
        XUBUNTU_DESKTOP_INSTALLED=1
        break
    fi
done
if (( ! XUBUNTU_DESKTOP_INSTALLED )); then
    if [[ ${XDG_CURRENT_DESKTOP:-} != *XFCE* && ${XDG_CURRENT_DESKTOP:-} != *Xfce* ]]; then
        warn "An XFCE/Xubuntu desktop was not detected. Package installation and headless tests can pass, but the operator GUI still needs a logged-in graphical desktop check."
    fi
fi
if git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    CURRENT_BRANCH=$(git -C "$REPO_ROOT" branch --show-current)
    if [[ -n $CURRENT_BRANCH ]]; then
        if (( ! ALLOW_MUTABLE_CHECKOUT && ! DRY_RUN )); then
            die "This checkout follows mutable branch '$CURRENT_BRANCH'. Check out an approved full commit or release tag first. Use --allow-mutable-checkout only for an engineering trial."
        fi
        warn "Engineering checkout follows mutable branch '$CURRENT_BRANCH'. Fleet stations must use the same approved detached commit or release tag."
    fi
fi

APT_PACKAGES=(
    ca-certificates
    git
    python3
    python3-tk
    python3-numpy
    python3-serial
    python3-matplotlib
    libnotify-bin
    desktop-file-utils
    xdg-user-dirs
    libglib2.0-bin
    usbutils
)

if (( ! SKIP_APT )); then
    command -v apt-get >/dev/null 2>&1 || die "apt-get was not found; this does not appear to be an Ubuntu workstation."
    MISSING_APT_PACKAGES=()
    for package_name in "${APT_PACKAGES[@]}"; do
        if [[ $(dpkg-query -W -f='${Status}' "$package_name" 2>/dev/null || true) != 'install ok installed' ]]; then
            MISSING_APT_PACKAGES+=("$package_name")
        fi
    done
    if (( ${#MISSING_APT_PACKAGES[@]} )); then
        command -v sudo >/dev/null 2>&1 || die "sudo is required to install system packages."
        note "Installing missing Ubuntu runtime and desktop packages"
        run sudo -v
        run sudo apt-get update
        run sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${MISSING_APT_PACKAGES[@]}"
    else
        note "All required Ubuntu packages are already installed; leaving their versions unchanged"
    fi
else
    note "Skipping Ubuntu package installation"
fi

command -v getent >/dev/null 2>&1 || die "getent is required to configure serial access."
getent group dialout >/dev/null 2>&1 || die "The standard Ubuntu dialout group does not exist."

CONFIGURED_GROUPS=$(id -nG "$TECHNICIAN_USER")
if [[ " $CONFIGURED_GROUPS " != *' dialout '* ]]; then
    note "Granting $TECHNICIAN_USER access to ESP32 serial ports"
    command -v sudo >/dev/null 2>&1 || die "sudo is required to add the technician to dialout."
    run sudo usermod -aG dialout "$TECHNICIAN_USER"
    RELOGIN_NOTE="Serial access will activate after $TECHNICIAN_USER signs out and back in (or reboots)."
    RELOGIN_REQUIRED=1
else
    note "$TECHNICIAN_USER is already configured for dialout serial access"
fi

if (( PREPARE_ONLY )); then
    if (( DRY_RUN )); then
        note "System-prerequisite preparation dry run complete (no changes made)"
    else
        note "System prerequisites are prepared"
    fi
    if (( RELOGIN_REQUIRED )); then
        printf '\nIMPORTANT: %s\n' "$RELOGIN_NOTE"
    fi
    exit 0
fi

note "Creating per-user data locations without touching existing results"
if command -v xdg-user-dirs-update >/dev/null 2>&1; then
    run xdg-user-dirs-update
fi
RESULTS_DIR="$TECHNICIAN_HOME/Documents/Eltec_406MCA_Test_Results/v6_esp32"
run mkdir -p -- "$RESULTS_DIR"

note "Repairing script permissions"
run chmod u+x -- \
    "$REPO_ROOT/setup_xubuntu.sh" \
    "$REPO_ROOT/update_xubuntu.sh" \
    "$REPO_ROOT/rollback_xubuntu.sh" \
    "$REPO_ROOT/doctor_xubuntu.sh" \
    "$REPO_ROOT/backup_eltec_results.sh" \
    "$REPO_ROOT/restore_eltec_results.sh" \
    "$REPO_ROOT/make_update_bundle.sh" \
    "$PRODUCTION_DIR/run_eltec_406mca_esp32_tester.sh" \
    "$PRODUCTION_DIR/install_xubuntu_launcher.sh"

if (( WITH_EXPERIMENTAL )); then
    [[ -x $EXPERIMENTAL_DIR/install_xubuntu_launcher.sh || -f $EXPERIMENTAL_DIR/install_xubuntu_launcher.sh ]] \
        || die "The requested v6.1 launcher installer is missing."
    run chmod u+x -- \
        "$EXPERIMENTAL_DIR/run_eltec_406mca_esp32_tester.sh" \
        "$EXPERIMENTAL_DIR/install_xubuntu_launcher.sh"
fi

if (( ! SKIP_TESTS )); then
    if (( DRY_RUN )); then
        note "Software verification (dry run)"
        print_command python3 -c 'import tkinter, numpy, serial, matplotlib'
        print_command env -u DISPLAY python3 -m unittest discover -s "$PRODUCTION_DIR/tests" -q
        print_command env -u DISPLAY python3 -m unittest discover -s "$REPO_ROOT/tests" -q
    else
        note "Verifying Python, Tk, NumPy, pyserial, and Matplotlib"
        python3 -c 'import tkinter, numpy, serial, matplotlib; print(f"Python dependencies OK: Tk {tkinter.TkVersion}, NumPy {numpy.__version__}, pyserial {serial.VERSION}, Matplotlib {matplotlib.__version__}")'

        VERIFY_TMP=$(mktemp -d "${TMPDIR:-/tmp}/eltec-xubuntu-verify.XXXXXX")
        cleanup_verify_tmp() {
            if [[ -n ${VERIFY_TMP:-} && -d $VERIFY_TMP ]]; then
                rm -rf -- "$VERIFY_TMP"
            fi
        }
        trap cleanup_verify_tmp EXIT

        PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$VERIFY_TMP/pycache" \
            python3 -m py_compile \
            "$PRODUCTION_DIR/eltec_406mca_esp32_tester.py" \
            "$PRODUCTION_DIR/esp32_backend.py" \
            "$PRODUCTION_DIR/stability_analysis.py" \
            "$PRODUCTION_DIR/stability_calibration.py" \
            "$V1_MATH"

        note "Running the production v6 software suite headlessly"
        env -u DISPLAY \
            PYTHONDONTWRITEBYTECODE=1 \
            PYTHONPYCACHEPREFIX="$VERIFY_TMP/pycache" \
            MPLCONFIGDIR="$VERIFY_TMP/matplotlib" \
            python3 -m unittest discover -s "$PRODUCTION_DIR/tests" -q

        note "Running the workstation provisioning safety tests"
        env -u DISPLAY \
            PYTHONDONTWRITEBYTECODE=1 \
            PYTHONPYCACHEPREFIX="$VERIFY_TMP/pycache" \
            MPLCONFIGDIR="$VERIFY_TMP/matplotlib" \
            python3 -m unittest discover -s "$REPO_ROOT/tests" -q

        cleanup_verify_tmp
        trap - EXIT
    fi
else
    note "Skipping software tests"
fi

note "Installing the verified production launcher"
run "$PRODUCTION_DIR/install_xubuntu_launcher.sh"
if (( WITH_EXPERIMENTAL )); then
    note "Installing the optional, isolated v6.1 evaluation launcher"
    run "$EXPERIMENTAL_DIR/install_xubuntu_launcher.sh"
else
    note "Ensuring experimental launchers are absent from this production station"
    if [[ -f $EXPERIMENTAL_DIR/install_xubuntu_launcher.sh ]]; then
        run bash "$EXPERIMENTAL_DIR/install_xubuntu_launcher.sh" --uninstall
    fi
    if [[ -f $FAILURE_CALIBRATION_DIR/install_xubuntu_launcher.sh ]]; then
        run bash "$FAILURE_CALIBRATION_DIR/install_xubuntu_launcher.sh" --uninstall
    fi
fi

if (( ! DRY_RUN )); then
    note "Recording this workstation installation"
    STATE_DIR=${XDG_STATE_HOME:-"$TECHNICIAN_HOME/.local/state"}/eltec-406mca-esp32-v6
    mkdir -p -- "$STATE_DIR"
    INSTALL_INFO="$STATE_DIR/install-info.txt"
    INFO_TMP=$(mktemp "$STATE_DIR/install-info.XXXXXX")
    GIT_COMMIT='not-a-git-checkout'
    RELEASE_ID='not-a-git-checkout'
    GIT_DIRTY='unknown'
    if git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        GIT_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)
        RELEASE_ID=$(git -C "$REPO_ROOT" describe --exact-match --tags HEAD 2>/dev/null || printf '%s' "$GIT_COMMIT")
        if git -C "$REPO_ROOT" diff --quiet --ignore-submodules -- \
            && git -C "$REPO_ROOT" diff --cached --quiet --ignore-submodules --; then
            GIT_DIRTY='no'
        else
            GIT_DIRTY='yes'
        fi
    fi
    SETTINGS_SHA256=$(sha256sum "$PRODUCTION_DIR/stability_settings.json" | cut -d' ' -f1)
    PACKAGE_VERSIONS=''
    for package_name in "${APT_PACKAGES[@]}"; do
        package_version=$(dpkg-query -W -f='${Version}' "$package_name" 2>/dev/null || printf 'missing')
        if [[ -n $PACKAGE_VERSIONS ]]; then
            PACKAGE_VERSIONS+=','
        fi
        PACKAGE_VERSIONS+="$package_name=$package_version"
    done
    {
        printf 'installed_at_utc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        printf 'technician_user=%s\n' "$TECHNICIAN_USER"
        printf 'os=%s\n' "$OS_NAME"
        printf 'architecture=%s\n' "$ARCHITECTURE"
        printf 'python=%s\n' "$(python3 --version 2>&1)"
        printf 'repository=%s\n' "$REPO_ROOT"
        printf 'git_commit=%s\n' "$GIT_COMMIT"
        printf 'release_id=%s\n' "$RELEASE_ID"
        printf 'tracked_changes=%s\n' "$GIT_DIRTY"
        printf 'stability_settings_sha256=%s\n' "$SETTINGS_SHA256"
        printf 'package_versions=%s\n' "$PACKAGE_VERSIONS"
        printf 'production_app=v6_esp32\n'
    } >"$INFO_TMP"
    chmod 600 -- "$INFO_TMP"
    mv -f -- "$INFO_TMP" "$INSTALL_INFO"
    HISTORY_FILE="$STATE_DIR/deployment-history.log"
    printf '%s\trelease=%s\tcommit=%s\tos=%s\tpython=%s\n' \
        "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$RELEASE_ID" "$GIT_COMMIT" "$OS_VERSION" "$(python3 --version 2>&1)" \
        >>"$HISTORY_FILE"
    chmod 600 -- "$HISTORY_FILE"

    note "Final software health check"
    "$DOCTOR"
fi

if (( DRY_RUN )); then
    note "Xubuntu workstation setup dry run complete (no changes made)"
else
    note "Xubuntu workstation setup complete"
fi
printf 'Production launcher: Eltec 406MCA ESP32 Tester v6\n'
printf 'Results (preserved across app updates): %s\n' "$RESULTS_DIR"
printf 'GUI/hardware check after login and fixture connection: %s --gui --hardware\n' "$DOCTOR"
if (( RELOGIN_REQUIRED )); then
    printf '\nIMPORTANT: %s\n' "$RELOGIN_NOTE"
fi
printf '\nA new computer still requires an in-app reference calibration with its own known-good/new emitter before DUT testing.\n'
