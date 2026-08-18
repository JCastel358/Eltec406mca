#!/usr/bin/env bash
# Create a non-destructive archive of all station results and calibration data.

set -Eeuo pipefail

SCRIPT_NAME=${0##*/}

usage() {
    cat <<EOF
Usage: $SCRIPT_NAME DESTINATION_DIRECTORY

Back up ~/Documents/Eltec_406MCA_Test_Results plus station/deployment metadata
to a timestamped .tar.gz and SHA-256 file. For protection from an OS reimage,
use a mounted USB/network destination, for example /media/\$USER/ELTEC_BACKUP.
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

warn() {
    printf 'WARNING: %s\n' "$*" >&2
}

case ${1:-} in
    -h|--help)
        usage
        exit 0
        ;;
esac
(( $# == 1 )) || {
    usage >&2
    exit 2
}

SOURCE_PARENT="${HOME:?HOME is not set}/Documents"
SOURCE_NAME='Eltec_406MCA_Test_Results'
SOURCE_DIR="$SOURCE_PARENT/$SOURCE_NAME"
DESTINATION=$1

[[ -d $SOURCE_DIR ]] || die "No Eltec results directory exists yet: $SOURCE_DIR"
SOURCE_DIR=$(cd -- "$SOURCE_DIR" && pwd -P)
if command -v realpath >/dev/null 2>&1; then
    PLANNED_DESTINATION=$(realpath -m -- "$DESTINATION")
else
    PLANNED_DESTINATION=$DESTINATION
fi

case "$PLANNED_DESTINATION/" in
    "$SOURCE_DIR/"*)
        die "The backup destination cannot be inside the results directory."
        ;;
esac

if command -v pgrep >/dev/null 2>&1 \
    && pgrep -u "$(id -u)" -f '([e]ltec_406mca_esp32_tester.py|[s]tability_calibration.py)' >/dev/null 2>&1; then
    die "Close the Eltec tester and stability-calibration tools before backing up results."
fi

mkdir -p -- "$DESTINATION"
DESTINATION=$(cd -- "$DESTINATION" && pwd -P)

STAMP=$(date -u '+%Y%m%dT%H%M%SZ')
FINAL_ARCHIVE="$DESTINATION/eltec-406mca-results-$STAMP.tar.gz"
[[ ! -e $FINAL_ARCHIVE && ! -e $FINAL_ARCHIVE.sha256 ]] \
    || die "A backup with this timestamp already exists: $FINAL_ARCHIVE"

PARTIAL_ARCHIVE=$(mktemp "$DESTINATION/.eltec-results-backup.XXXXXX.partial")
METADATA_TMP=$(mktemp -d "${TMPDIR:-/tmp}/eltec-results-metadata.XXXXXX")
cleanup_partial() {
    if [[ -n ${PARTIAL_ARCHIVE:-} && -f $PARTIAL_ARCHIVE ]]; then
        rm -f -- "$PARTIAL_ARCHIVE"
    fi
    if [[ -n ${METADATA_TMP:-} && -d $METADATA_TMP ]]; then
        rm -rf -- "$METADATA_TMP"
    fi
}
trap cleanup_partial EXIT

METADATA_DIR="$METADATA_TMP/$SOURCE_NAME/_station_metadata/$STAMP"
mkdir -p -- "$METADATA_DIR"
SCRIPT_PATH=${BASH_SOURCE[0]}
if command -v readlink >/dev/null 2>&1; then
    RESOLVED_SCRIPT=$(readlink -f -- "$SCRIPT_PATH" 2>/dev/null || true)
    if [[ -n $RESOLVED_SCRIPT ]]; then
        SCRIPT_PATH=$RESOLVED_SCRIPT
    fi
fi
REPO_ROOT=$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P)
GIT_COMMIT='not-a-git-checkout'
if git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    GIT_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)
fi
MACHINE_ID_SHA='unavailable'
if [[ -r /etc/machine-id ]]; then
    MACHINE_ID_SHA=$(sha256sum /etc/machine-id | cut -d' ' -f1)
fi
{
    printf 'backup_created_at_utc=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'station_hostname=%s\n' "$(hostname 2>/dev/null || printf unknown)"
    printf 'station_machine_id_sha256=%s\n' "$MACHINE_ID_SHA"
    printf 'technician_user=%s\n' "$(id -un)"
    printf 'application_git_commit=%s\n' "$GIT_COMMIT"
} >"$METADATA_DIR/backup-station.txt"
STATE_DIR=${XDG_STATE_HOME:-"${HOME:?HOME is not set}/.local/state"}/eltec-406mca-esp32-v6
for state_file in install-info.txt deployment-history.log rollback-target.txt; do
    if [[ -r $STATE_DIR/$state_file && -f $STATE_DIR/$state_file ]]; then
        cp -p -- "$STATE_DIR/$state_file" "$METADATA_DIR/$state_file"
    fi
done

tar -czf "$PARTIAL_ARCHIVE" \
    -C "$SOURCE_PARENT" "$SOURCE_NAME" \
    -C "$METADATA_TMP" "$SOURCE_NAME"
tar -tzf "$PARTIAL_ARCHIVE" >/dev/null \
    || die "The completed backup archive did not pass tar integrity validation."
if ! chmod 600 -- "$PARTIAL_ARCHIVE" 2>/dev/null; then
    warn "The destination filesystem does not support Linux file permissions; protect the backup media physically."
fi
mv -- "$PARTIAL_ARCHIVE" "$FINAL_ARCHIVE"
PARTIAL_ARCHIVE=''
rm -rf -- "$METADATA_TMP"
METADATA_TMP=''
(cd -- "$DESTINATION" && sha256sum -- "$(basename -- "$FINAL_ARCHIVE")" >"$(basename -- "$FINAL_ARCHIVE").sha256")
if ! chmod 600 -- "$FINAL_ARCHIVE.sha256" 2>/dev/null; then
    warn "The destination filesystem does not support Linux file permissions for the checksum file."
fi
trap - EXIT

printf 'Backup complete:\n  %s\n  %s\n' "$FINAL_ARCHIVE" "$FINAL_ARCHIVE.sha256"
printf 'Keep calibration backups with the same physical fixture/reference assembly only.\n'
