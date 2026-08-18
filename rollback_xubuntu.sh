#!/usr/bin/env bash
# Roll a station back to its previous approved application commit.

set -Eeuo pipefail

SCRIPT_NAME=${0##*/}
TARGET_MODE=''
REQUESTED_REVISION=''
DRY_RUN=0
SKIP_TESTS=0
SETUP_ARGS=(--skip-apt --skip-tests)

usage() {
    cat <<EOF
Usage: $SCRIPT_NAME (--last | --revision FULL_SHA_OR_TAG) [options]

Return a station to an older, locally available approved commit without
touching results or fixture calibration. --last uses the pre-update commit
recorded by update_xubuntu.sh. The target must be an ancestor of the current
commit; mutable branch names are rejected.

Options:
  --last                Use the commit recorded immediately before the update
  --revision REV        Use an older full commit SHA or local release tag
  --dry-run             Verify and show the rollback without activating it
  --skip-tests          Emergency override: skip candidate software tests
  --with-experimental   Restore the optional legacy v6.1 launcher too
  --force-unsupported   Forward to setup_xubuntu.sh
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
        --last)
            [[ -z $TARGET_MODE ]] || die "Choose exactly one of --last or --revision."
            TARGET_MODE='last'
            ;;
        --revision)
            [[ -z $TARGET_MODE ]] || die "Choose exactly one of --last or --revision."
            shift
            (( $# )) || die "--revision requires a full commit SHA or release tag"
            TARGET_MODE='revision'
            REQUESTED_REVISION=$1
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        --skip-tests)
            SKIP_TESTS=1
            ;;
        --with-experimental|--force-unsupported)
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

[[ -n $TARGET_MODE ]] || {
    usage >&2
    exit 2
}

SOURCE_PATH=${BASH_SOURCE[0]}
if command -v readlink >/dev/null 2>&1; then
    RESOLVED_SOURCE=$(readlink -f -- "$SOURCE_PATH" 2>/dev/null || true)
    if [[ -n $RESOLVED_SOURCE ]]; then
        SOURCE_PATH=$RESOLVED_SOURCE
    fi
fi
REPO_ROOT=$(cd -- "$(dirname -- "$SOURCE_PATH")" && pwd -P)
STATE_DIR=${XDG_STATE_HOME:-"${HOME:?HOME is not set}/.local/state"}/eltec-rig
ROLLBACK_RECORD="$STATE_DIR/rollback-target.txt"

(( EUID != 0 )) || die "Do not run rollback with sudo; run as the technician account."
[[ -d $REPO_ROOT/.git ]] || die "Rollback requires the station's Git checkout."
[[ -f $REPO_ROOT/setup_xubuntu.sh ]] || die "Setup script is missing from this checkout."

if ! git -C "$REPO_ROOT" diff --quiet --ignore-submodules -- \
    || ! git -C "$REPO_ROOT" diff --cached --quiet --ignore-submodules --; then
    die "Tracked repository files have local changes. They were preserved; review them before rollback."
fi
if command -v pgrep >/dev/null 2>&1 \
    && pgrep -u "$(id -u)" -f '([e]ltec_rig_tester.py|[e]ltec_405m22_esp32_tester.py|[e]ltec_406mca_esp32_tester.py)' >/dev/null 2>&1; then
    die "The tester appears to be running. Close it before rollback."
fi

CURRENT_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)
if [[ $TARGET_MODE == last ]]; then
    [[ -r $ROLLBACK_RECORD ]] || die "No previous-update record exists: $ROLLBACK_RECORD"
    TARGET_COMMIT=$(sed -n 's/^previous_commit=//p' "$ROLLBACK_RECORD" | head -n 1)
    RECORDED_INCOMING=$(sed -n 's/^incoming_commit=//p' "$ROLLBACK_RECORD" | head -n 1)
    [[ $TARGET_COMMIT =~ ^[0-9a-fA-F]{40}$ ]] \
        || die "The previous-update record does not contain a valid full commit SHA."
    if [[ -n $RECORDED_INCOMING && $RECORDED_INCOMING != "$CURRENT_COMMIT" ]]; then
        die "The rollback record belongs to incoming commit $RECORDED_INCOMING, but this station is at $CURRENT_COMMIT. Specify a reviewed older target with --revision."
    fi
else
    if [[ $REQUESTED_REVISION =~ ^[0-9a-fA-F]{40}$ ]]; then
        TARGET_COMMIT=$REQUESTED_REVISION
    elif git -C "$REPO_ROOT" show-ref --verify --quiet "refs/tags/$REQUESTED_REVISION"; then
        TARGET_COMMIT=$(git -C "$REPO_ROOT" rev-parse "refs/tags/$REQUESTED_REVISION^{commit}")
    else
        die "--revision must be a full 40-character commit SHA or an existing local release tag; branch names are rejected."
    fi
fi

git -C "$REPO_ROOT" cat-file -e "$TARGET_COMMIT^{commit}" 2>/dev/null \
    || die "Rollback target is not available in the local Git repository: $TARGET_COMMIT"
TARGET_COMMIT=$(git -C "$REPO_ROOT" rev-parse "$TARGET_COMMIT^{commit}")
[[ $TARGET_COMMIT != "$CURRENT_COMMIT" ]] || die "The requested rollback target is already active."
git -C "$REPO_ROOT" merge-base --is-ancestor "$TARGET_COMMIT" "$CURRENT_COMMIT" \
    || die "Rollback target is not an ancestor of the active release; refusing an unrelated checkout."

for required_file in \
    setup_xubuntu.sh doctor_xubuntu.sh update_xubuntu.sh rollback_xubuntu.sh \
    tech_app/eltec_rig/eltec_rig_tester.py \
    tech_app/eltec_rig/m405m22/eltec_405m22_esp32_tester.py \
    tech_app/eltec_rig/m406mca/eltec_406mca_esp32_tester.py; do
    git -C "$REPO_ROOT" cat-file -e "$TARGET_COMMIT:$required_file" 2>/dev/null \
        || die "Rollback target is not a complete fleet release; missing $required_file"
done

note "Staging rollback target ${TARGET_COMMIT:0:12} for verification"
ROLLBACK_TMP=$(mktemp -d "${TMPDIR:-/tmp}/eltec-xubuntu-rollback.XXXXXX")
CANDIDATE_DIR="$ROLLBACK_TMP/candidate"
cleanup_candidate() {
    if [[ -n ${CANDIDATE_DIR:-} && -e $CANDIDATE_DIR/.git ]]; then
        git -C "$REPO_ROOT" worktree remove --force "$CANDIDATE_DIR" >/dev/null 2>&1 || true
    fi
    if [[ -n ${ROLLBACK_TMP:-} && -d $ROLLBACK_TMP ]]; then
        rm -rf -- "$ROLLBACK_TMP"
    fi
}
trap cleanup_candidate EXIT
git -C "$REPO_ROOT" worktree add --detach "$CANDIDATE_DIR" "$TARGET_COMMIT" >/dev/null

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

if (( ! SKIP_TESTS )); then
    note "Running rollback candidate software tests"
    VERIFY_TMP="$ROLLBACK_TMP/verify"
    mkdir -p -- "$VERIFY_TMP"
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
else
    note "Skipping rollback candidate software tests by explicit request"
fi

cleanup_candidate
trap - EXIT

if (( DRY_RUN )); then
    note "Rollback dry run passed; no checkout or launcher was changed"
    print_command git -C "$REPO_ROOT" checkout --detach "$TARGET_COMMIT"
    print_command "$REPO_ROOT/setup_xubuntu.sh" "${SETUP_ARGS[@]}"
    exit 0
fi

note "Activating verified rollback ${CURRENT_COMMIT:0:12} -> ${TARGET_COMMIT:0:12}"
git -C "$REPO_ROOT" checkout --detach "$TARGET_COMMIT"
if ! "$REPO_ROOT/setup_xubuntu.sh" "${SETUP_ARGS[@]}"; then
    die "Application code is at rollback commit $TARGET_COMMIT, but launcher/setup repair failed. Results were preserved. Correct the reported setup problem and rerun ./setup_xubuntu.sh --skip-apt."
fi

printf '\nRollback complete: %s -> %s\n' "${CURRENT_COMMIT:0:12}" "${TARGET_COMMIT:0:12}"
printf 'Results and reference calibration were not modified. Run ./doctor_xubuntu.sh --gui --hardware and repeat the known-good fixture check.\n'
