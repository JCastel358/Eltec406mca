#!/usr/bin/env bash
# Non-destructive, fast-forward-only application update for one station.

set -Eeuo pipefail

SCRIPT_NAME=${0##*/}
DRY_RUN=0
BUNDLE=''
APPROVED_REVISION=''
APT_MODE='auto'
SKIP_TESTS_REQUESTED=0
FORCE_UNSUPPORTED_REQUESTED=0
ALLOW_MUTABLE_CHECKOUT_REQUESTED=0
SETUP_ARGS=()

usage() {
    cat <<EOF
Usage: $SCRIPT_NAME [options]

Update this Git checkout without touching results or calibration, then rerun
the idempotent workstation setup and production test suite.

Options:
  --bundle FILE         Update from a bundle carried on USB instead of GitHub
  --revision REV        Approved full commit SHA (USB) or SHA/release tag (online)
  --dry-run             Show the update/setup actions without changing files
  --skip-apt            Do not refresh packages (automatic for USB bundles)
  --refresh-apt         Refresh/install packages even with a USB bundle
  --skip-tests          Forward to setup_xubuntu.sh
  --with-experimental   Forward to setup_xubuntu.sh
  --force-unsupported   Forward to setup_xubuntu.sh
  --allow-mutable-checkout
                        Engineering override: update a branch checkout
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

print_command() {
    printf '  +'
    printf ' %q' "$@"
    printf '\n'
}

while (( $# )); do
    case $1 in
        --bundle)
            shift
            (( $# )) || die "--bundle requires a file path"
            BUNDLE=$1
            ;;
        --revision)
            shift
            (( $# )) || die "--revision requires a full commit SHA or release tag"
            APPROVED_REVISION=$1
            ;;
        --dry-run)
            DRY_RUN=1
            SETUP_ARGS+=(--dry-run)
            ;;
        --skip-apt)
            APT_MODE='skip'
            SETUP_ARGS+=(--skip-apt)
            ;;
        --refresh-apt)
            APT_MODE='refresh'
            ;;
        --skip-tests)
            SKIP_TESTS_REQUESTED=1
            SETUP_ARGS+=("$1")
            ;;
        --force-unsupported)
            FORCE_UNSUPPORTED_REQUESTED=1
            SETUP_ARGS+=("$1")
            ;;
        --allow-mutable-checkout)
            ALLOW_MUTABLE_CHECKOUT_REQUESTED=1
            SETUP_ARGS+=("$1")
            ;;
        --with-experimental)
            SETUP_ARGS+=("$1")
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
SETUP_SCRIPT="$REPO_ROOT/setup_xubuntu.sh"

if [[ -n $BUNDLE && $APT_MODE == auto ]]; then
    # A USB update is expected to work while disconnected. Application-only
    # releases use the already-verified distro packages. A release that adds a
    # system dependency must be installed with network/package-mirror access
    # and an explicit --refresh-apt.
    APT_MODE='skip'
    SETUP_ARGS+=(--skip-apt)
fi

(( EUID != 0 )) || die "Do not run updates with sudo; run as the technician account."
[[ -d $REPO_ROOT/.git ]] || die "Updates require a Git clone. For a ZIP/USB copy, make a fresh Git clone first."
[[ -x $SETUP_SCRIPT || -f $SETUP_SCRIPT ]] || die "Setup script is missing: $SETUP_SCRIPT"

CURRENT_BRANCH=$(git -C "$REPO_ROOT" branch --show-current)
if [[ -n $CURRENT_BRANCH ]] && (( ! ALLOW_MUTABLE_CHECKOUT_REQUESTED && ! DRY_RUN )); then
    die "This station follows mutable branch '$CURRENT_BRANCH'. Production updates require an approved detached commit. Use --allow-mutable-checkout only for an engineering trial."
fi

if ! git -C "$REPO_ROOT" diff --quiet --ignore-submodules -- \
    || ! git -C "$REPO_ROOT" diff --cached --quiet --ignore-submodules --; then
    die "Tracked repository files have local changes. They were preserved; commit or review them before updating."
fi

if command -v pgrep >/dev/null 2>&1 \
    && pgrep -u "$(id -u)" -f '([e]ltec_rig_tester.py|[e]ltec_405m22_esp32_tester.py|[e]ltec_406mca_esp32_tester.py)' >/dev/null 2>&1; then
    die "The tester appears to be running. Close it before updating."
fi

OLD_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)
TARGET_REF=''

if [[ -n $BUNDLE ]]; then
    [[ $APPROVED_REVISION =~ ^[0-9a-fA-F]{40}$ ]] \
        || die "USB bundle updates require --revision with the separately approved full 40-character commit SHA."
    [[ -f $BUNDLE && -r $BUNDLE ]] || die "Update bundle is not readable: $BUNDLE"
    BUNDLE=$(cd -- "$(dirname -- "$BUNDLE")" && pwd -P)/$(basename -- "$BUNDLE")
    BUNDLE_MANIFEST="$BUNDLE.manifest"
    BUNDLE_CHECKSUM="$BUNDLE.sha256"

    note "Verifying offline update bundle"
    [[ -f $BUNDLE_MANIFEST && -r $BUNDLE_MANIFEST ]] \
        || die "Required bundle manifest is missing: $BUNDLE_MANIFEST"
    [[ -f $BUNDLE_CHECKSUM && -r $BUNDLE_CHECKSUM ]] \
        || die "Required bundle checksum is missing: $BUNDLE_CHECKSUM"

    mapfile -t CHECKSUM_LINES <"$BUNDLE_CHECKSUM"
    (( ${#CHECKSUM_LINES[@]} == 2 )) \
        || die "Bundle checksum must contain exactly the bundle and manifest entries."
    verify_checksum_entry() {
        local checksum_line=$1
        local expected_basename=$2
        local checked_file=$3
        local expected_sha actual_sha listed_basename
        if [[ ! $checksum_line =~ ^([0-9a-fA-F]{64})[[:space:]][[:space:]](.*)$ ]]; then
            die "Malformed SHA-256 entry for $expected_basename"
        fi
        expected_sha=${BASH_REMATCH[1],,}
        listed_basename=${BASH_REMATCH[2]}
        [[ $listed_basename == "$expected_basename" ]] \
            || die "Checksum entry names $listed_basename instead of $expected_basename"
        actual_sha=$(sha256sum -- "$checked_file" | cut -d' ' -f1)
        [[ $actual_sha == "$expected_sha" ]] \
            || die "SHA-256 verification failed for $expected_basename"
        printf '%s: OK\n' "$expected_basename"
    }
    verify_checksum_entry "${CHECKSUM_LINES[0]}" "$(basename -- "$BUNDLE")" "$BUNDLE"
    verify_checksum_entry "${CHECKSUM_LINES[1]}" "$(basename -- "$BUNDLE_MANIFEST")" "$BUNDLE_MANIFEST"

    MANIFEST_FORMAT=''
    MANIFEST_COMMIT=''
    MANIFEST_REF=''
    while IFS='=' read -r manifest_key manifest_value; do
        case $manifest_key in
            ELTEC_UPDATE_FORMAT)
                [[ -z $MANIFEST_FORMAT ]] || die "Duplicate ELTEC_UPDATE_FORMAT in bundle manifest."
                MANIFEST_FORMAT=$manifest_value
                ;;
            APPROVED_COMMIT)
                [[ -z $MANIFEST_COMMIT ]] || die "Duplicate APPROVED_COMMIT in bundle manifest."
                MANIFEST_COMMIT=$manifest_value
                ;;
            BUNDLE_REF)
                [[ -z $MANIFEST_REF ]] || die "Duplicate BUNDLE_REF in bundle manifest."
                MANIFEST_REF=$manifest_value
                ;;
            *)
                die "Unknown or empty field in bundle manifest: $manifest_key"
                ;;
        esac
    done <"$BUNDLE_MANIFEST"
    [[ $MANIFEST_FORMAT == 1 ]] || die "Unsupported bundle manifest format: ${MANIFEST_FORMAT:-missing}"
    [[ $MANIFEST_COMMIT =~ ^[0-9a-fA-F]{40}$ ]] || die "Bundle manifest does not contain a full commit SHA."
    git check-ref-format --allow-onelevel "$MANIFEST_REF" >/dev/null 2>&1 \
        || die "Bundle manifest contains an invalid Git ref."
    [[ ${APPROVED_REVISION,,} == "${MANIFEST_COMMIT,,}" ]] \
        || die "Bundle commit $MANIFEST_COMMIT does not match the separately approved commit $APPROVED_REVISION."

    git -C "$REPO_ROOT" bundle verify "$BUNDLE"
    LISTED_COMMIT=$(git -C "$REPO_ROOT" bundle list-heads "$BUNDLE" "$MANIFEST_REF" | awk 'NR == 1 { print $1 }')
    [[ ${LISTED_COMMIT,,} == "${MANIFEST_COMMIT,,}" ]] \
        || die "Bundle ref does not resolve to its declared approved commit."
    if (( DRY_RUN )); then
        print_command git -C "$REPO_ROOT" fetch "$BUNDLE" "$MANIFEST_REF"
    else
        git -C "$REPO_ROOT" fetch "$BUNDLE" "$MANIFEST_REF"
        FETCHED_COMMIT=$(git -C "$REPO_ROOT" rev-parse 'FETCH_HEAD^{commit}')
        [[ ${FETCHED_COMMIT,,} == "${MANIFEST_COMMIT,,}" ]] \
            || die "Fetched bundle commit does not match the manifest. Nothing was merged."
        TARGET_REF=$FETCHED_COMMIT
    fi
else
    [[ -n $APPROVED_REVISION ]] \
        || die "Online updates require an approved immutable revision. Rerun with --revision FULL_COMMIT_SHA_OR_TAG, or use a reviewed --bundle FILE."
    REMOTE='origin'
    if ! git -C "$REPO_ROOT" remote get-url "$REMOTE" >/dev/null 2>&1; then
        REMOTE=$(git -C "$REPO_ROOT" remote | head -n 1)
    fi
    [[ -n $REMOTE ]] || die "No Git remote is configured; use --bundle FILE."
    note "Fetching approved online revision from $REMOTE"
    if [[ $APPROVED_REVISION =~ ^[0-9a-fA-F]{40}$ ]]; then
        REMOTE_FETCH_REF=$APPROVED_REVISION
    elif git check-ref-format "refs/tags/$APPROVED_REVISION" >/dev/null 2>&1; then
        REMOTE_FETCH_REF="refs/tags/$APPROVED_REVISION"
    else
        die "--revision must be a full 40-character commit SHA or a valid release tag; mutable branch names are rejected."
    fi
    if (( DRY_RUN )); then
        print_command git -C "$REPO_ROOT" fetch --no-tags "$REMOTE" "$REMOTE_FETCH_REF"
    else
        git -C "$REPO_ROOT" fetch --no-tags "$REMOTE" "$REMOTE_FETCH_REF"
        FETCHED_COMMIT=$(git -C "$REPO_ROOT" rev-parse 'FETCH_HEAD^{commit}')
        if [[ $APPROVED_REVISION =~ ^[0-9a-fA-F]{40}$ ]]; then
            [[ ${FETCHED_COMMIT,,} == "${APPROVED_REVISION,,}" ]] \
                || die "The selected remote did not return the approved commit. Nothing was merged."
        fi
        TARGET_REF=$FETCHED_COMMIT
    fi
fi

if (( DRY_RUN )); then
    FINAL_SETUP_ARGS=("${SETUP_ARGS[@]}")
    if [[ $APT_MODE != skip ]]; then
        note "Preparing candidate system prerequisites before touching live code (dry run)"
        print_command '<candidate>/setup_xubuntu.sh' --prepare-only
        FINAL_SETUP_ARGS+=(--skip-apt)
    fi
    if (( ! SKIP_TESTS_REQUESTED )); then
        note "Testing the candidate checkout before touching live code (dry run)"
        print_command env -u DISPLAY python3 -m unittest discover -s '<candidate>/tech_app/eltec_rig/tests' -q
        print_command env -u DISPLAY python3 -m unittest discover -s '<candidate>/tech_app/eltec_rig/m405m22/tests' -q
        print_command env -u DISPLAY python3 -m unittest discover -s '<candidate>/tech_app/eltec_rig/m406mca/tests' -q
        print_command env -u DISPLAY python3 -m unittest discover -s '<candidate>/tests' -q
        FINAL_SETUP_ARGS+=(--skip-tests)
    fi
    note "Fast-forward update (dry run)"
    printf 'Current commit: %s\n' "$OLD_COMMIT"
    print_command git -C "$REPO_ROOT" merge --ff-only '<fetched-update>'
    note "Workstation repair/verification (dry run)"
    print_command "$SETUP_SCRIPT" "${FINAL_SETUP_ARGS[@]}"
    "$SETUP_SCRIPT" "${FINAL_SETUP_ARGS[@]}"
    exit 0
fi

git -C "$REPO_ROOT" merge-base --is-ancestor HEAD "$TARGET_REF" \
    || die "The incoming update is not a fast-forward from this station. Nothing was merged."

NEW_COMMIT=$(git -C "$REPO_ROOT" rev-parse "$TARGET_REF")
if [[ $NEW_COMMIT == "$OLD_COMMIT" ]]; then
    note "Application code is already up to date at ${OLD_COMMIT:0:12}"
    FINAL_SETUP_ARGS=("${SETUP_ARGS[@]}")
else
    note "Staging candidate ${NEW_COMMIT:0:12} for verification"
    UPDATE_TMP=$(mktemp -d "${TMPDIR:-/tmp}/eltec-xubuntu-update.XXXXXX")
    CANDIDATE_DIR="$UPDATE_TMP/candidate"
    cleanup_candidate() {
        if [[ -n ${CANDIDATE_DIR:-} && -e $CANDIDATE_DIR/.git ]]; then
            git -C "$REPO_ROOT" worktree remove --force "$CANDIDATE_DIR" >/dev/null 2>&1 || true
        fi
        if [[ -n ${UPDATE_TMP:-} && -d $UPDATE_TMP ]]; then
            rm -rf -- "$UPDATE_TMP"
        fi
    }
    trap cleanup_candidate EXIT
    git -C "$REPO_ROOT" worktree add --detach "$CANDIDATE_DIR" "$NEW_COMMIT" >/dev/null

    FINAL_SETUP_ARGS=("${SETUP_ARGS[@]}")
    if [[ $APT_MODE != skip ]]; then
        note "Preparing candidate system prerequisites before touching live code"
        PREPARE_ARGS=(--prepare-only)
        if (( FORCE_UNSUPPORTED_REQUESTED )); then
            PREPARE_ARGS+=(--force-unsupported)
        fi
        bash "$CANDIDATE_DIR/setup_xubuntu.sh" "${PREPARE_ARGS[@]}"
        FINAL_SETUP_ARGS+=(--skip-apt)
    fi

    if (( ! SKIP_TESTS_REQUESTED )); then
        note "Testing candidate software before touching live code"
        VERIFY_TMP="$UPDATE_TMP/verify"
        mkdir -p -- "$VERIFY_TMP"
        bash -n \
            "$CANDIDATE_DIR/setup_xubuntu.sh" \
            "$CANDIDATE_DIR/doctor_xubuntu.sh" \
            "$CANDIDATE_DIR/update_xubuntu.sh" \
            "$CANDIDATE_DIR/rollback_xubuntu.sh" \
            "$CANDIDATE_DIR/make_update_bundle.sh" \
            "$CANDIDATE_DIR/backup_eltec_results.sh" \
            "$CANDIDATE_DIR/restore_eltec_results.sh" \
            "$CANDIDATE_DIR/tech_app/eltec_rig/install_xubuntu_launcher.sh" \
            "$CANDIDATE_DIR/tech_app/eltec_rig/run_eltec_rig_tester.sh"
        env -u DISPLAY \
            PYTHONDONTWRITEBYTECODE=1 \
            PYTHONPYCACHEPREFIX="$VERIFY_TMP/pycache" \
            MPLCONFIGDIR="$VERIFY_TMP/matplotlib" \
            python3 -m unittest discover -s "$CANDIDATE_DIR/tech_app/eltec_rig/tests" -q
        env -u DISPLAY \
            PYTHONDONTWRITEBYTECODE=1 \
            PYTHONPYCACHEPREFIX="$VERIFY_TMP/pycache" \
            MPLCONFIGDIR="$VERIFY_TMP/matplotlib" \
            python3 -m unittest discover -s "$CANDIDATE_DIR/tech_app/eltec_rig/m405m22/tests" -q
        env -u DISPLAY \
            PYTHONDONTWRITEBYTECODE=1 \
            PYTHONPYCACHEPREFIX="$VERIFY_TMP/pycache" \
            MPLCONFIGDIR="$VERIFY_TMP/matplotlib" \
            python3 -m unittest discover -s "$CANDIDATE_DIR/tech_app/eltec_rig/m406mca/tests" -q
        env -u DISPLAY \
            PYTHONDONTWRITEBYTECODE=1 \
            PYTHONPYCACHEPREFIX="$VERIFY_TMP/pycache" \
            MPLCONFIGDIR="$VERIFY_TMP/matplotlib" \
            python3 -m unittest discover -s "$CANDIDATE_DIR/tests" -q
        FINAL_SETUP_ARGS+=(--skip-tests)
    fi

    cleanup_candidate
    trap - EXIT

    note "Fast-forwarding live application code to verified candidate ${NEW_COMMIT:0:12}"
    STATE_DIR=${XDG_STATE_HOME:-"${HOME:?HOME is not set}/.local/state"}/eltec-rig
    mkdir -p -- "$STATE_DIR"
    ROLLBACK_RECORD_TMP=$(mktemp "$STATE_DIR/rollback-target.XXXXXX")
    {
        printf 'recorded_at_utc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        printf 'previous_commit=%s\n' "$OLD_COMMIT"
        printf 'incoming_commit=%s\n' "$NEW_COMMIT"
    } >"$ROLLBACK_RECORD_TMP"
    chmod 600 -- "$ROLLBACK_RECORD_TMP"
    mv -f -- "$ROLLBACK_RECORD_TMP" "$STATE_DIR/rollback-target.txt"
    git -C "$REPO_ROOT" merge --ff-only "$TARGET_REF"
fi

note "Repairing launchers, permissions, and installation record"
"$SETUP_SCRIPT" "${FINAL_SETUP_ARGS[@]}"

printf '\nUpdate complete: %s -> %s\n' "${OLD_COMMIT:0:12}" "${NEW_COMMIT:0:12}"
printf 'Results and reference calibration were not modified.\n'
