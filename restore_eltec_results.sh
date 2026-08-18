#!/usr/bin/env bash
# Verify and safely restore an Eltec results backup without overwriting files.

set -Eeuo pipefail

SCRIPT_NAME=${0##*/}
MODE='results-only'
DRY_RUN=0
ARCHIVE=''

usage() {
    cat <<EOF
Usage: $SCRIPT_NAME [--results-only | --same-fixture] [--dry-run] BACKUP.tar.gz

Verify a unified-rig backup's sibling .sha256 file and restore both models
without overwriting any existing file. The safe default, --results-only,
excludes every fixture-specific reference_sensor_calibration.json. Use
--same-fixture only when restoring to the same physical fixture, reference
sensor, and emitter assembly.
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

while (( $# )); do
    case $1 in
        --results-only)
            MODE='results-only'
            ;;
        --same-fixture)
            MODE='same-fixture'
            ;;
        --dry-run)
            DRY_RUN=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --*)
            usage >&2
            die "Unknown option: $1"
            ;;
        *)
            [[ -z $ARCHIVE ]] || die "Pass exactly one backup archive."
            ARCHIVE=$1
            ;;
    esac
    shift
done

[[ -n $ARCHIVE ]] || {
    usage >&2
    exit 2
}
[[ -r $ARCHIVE && -f $ARCHIVE ]] || die "Backup archive is not readable: $ARCHIVE"
ARCHIVE=$(cd -- "$(dirname -- "$ARCHIVE")" && pwd -P)/$(basename -- "$ARCHIVE")
CHECKSUM_FILE="$ARCHIVE.sha256"
[[ -r $CHECKSUM_FILE && -f $CHECKSUM_FILE ]] \
    || die "Required checksum file is missing: $CHECKSUM_FILE"

if command -v pgrep >/dev/null 2>&1 \
    && pgrep -u "$(id -u)" -f '([e]ltec_rig_tester.py|[e]ltec_405m22_esp32_tester.py|[e]ltec_406mca_esp32_tester.py|[s]tability_calibration.py)' >/dev/null 2>&1; then
    die "Close the Eltec tester and stability-calibration tools before restoring results."
fi

mapfile -t CHECKSUM_LINES <"$CHECKSUM_FILE"
(( ${#CHECKSUM_LINES[@]} == 1 )) \
    || die "Backup checksum must contain exactly one entry for the selected archive."
if [[ ! ${CHECKSUM_LINES[0]} =~ ^([0-9a-fA-F]{64})[[:space:]][[:space:]](.*)$ ]]; then
    die "Backup checksum entry is malformed."
fi
EXPECTED_SHA=${BASH_REMATCH[1],,}
CHECKSUM_ARCHIVE_NAME=${BASH_REMATCH[2]}
[[ $CHECKSUM_ARCHIVE_NAME == "$(basename -- "$ARCHIVE")" ]] \
    || die "Checksum is for $CHECKSUM_ARCHIVE_NAME, not the selected archive $(basename -- "$ARCHIVE")."
ACTUAL_SHA=$(sha256sum -- "$ARCHIVE" | cut -d' ' -f1)
[[ $ACTUAL_SHA == "$EXPECTED_SHA" ]] || die "Backup SHA-256 verification failed."
printf '%s: OK\n' "$(basename -- "$ARCHIVE")"

python3 -c '
import pathlib
import sys
import tarfile

archive_path = sys.argv[1]
with tarfile.open(archive_path, "r:gz") as archive:
    for member in archive.getmembers():
        name = member.name.rstrip("/")
        path = pathlib.PurePosixPath(name)
        if not name or path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe archive path: {member.name}")
        allowed_roots = {
            "Eltec_405M22_Test_Results",
            "Eltec_406MCA_Test_Results",
            "Eltec_TestRig_Backup_Metadata",
        }
        if path.parts[0] not in allowed_roots:
            raise SystemExit(f"unexpected archive path: {member.name}")
        if not (member.isfile() or member.isdir()):
            raise SystemExit(f"unsupported archive member type: {member.name}")
' "$ARCHIVE" || die "Backup archive failed safe-member validation."
tar -tzf "$ARCHIVE" >/dev/null || die "Backup archive failed tar integrity validation."

while IFS= read -r archive_path; do
    case $archive_path in
        Eltec_405M22_Test_Results|Eltec_405M22_Test_Results/*|\
        Eltec_406MCA_Test_Results|Eltec_406MCA_Test_Results/*|\
        Eltec_TestRig_Backup_Metadata|Eltec_TestRig_Backup_Metadata/*)
            ;;
        *)
            die "Archive contains an unexpected path: $archive_path"
            ;;
    esac
    case $archive_path in
        /*|../*|*/../*|*/..)
            die "Archive contains an unsafe path: $archive_path"
            ;;
    esac
done < <(tar -tzf "$ARCHIVE")

RESTORE_TMP=$(mktemp -d "${TMPDIR:-/tmp}/eltec-results-restore.XXXXXX")
cleanup_restore_tmp() {
    if [[ -n ${RESTORE_TMP:-} && -d $RESTORE_TMP ]]; then
        rm -rf -- "$RESTORE_TMP"
    fi
}
trap cleanup_restore_tmp EXIT

tar -xzf "$ARCHIVE" -C "$RESTORE_TMP" --no-same-owner --no-same-permissions
STAGED_405="$RESTORE_TMP/Eltec_405M22_Test_Results"
STAGED_406="$RESTORE_TMP/Eltec_406MCA_Test_Results"
[[ -d $STAGED_405 || -d $STAGED_406 ]] \
    || die "Backup does not contain either unified-rig results root."

UNSAFE_MEMBER=$(find "$RESTORE_TMP" ! -type f ! -type d -print -quit)
[[ -z $UNSAFE_MEMBER ]] || die "Backup contains an unsupported member and was not restored: $UNSAFE_MEMBER"

if [[ $MODE == results-only ]]; then
    while IFS= read -r -d '' calibration_file; do
        rm -f -- "$calibration_file"
    done < <(find "$RESTORE_TMP" -type f -name reference_sensor_calibration.json -print0)
fi

DESTINATION_PARENT="${HOME:?HOME is not set}/Documents"
CONFLICT=''
while IFS= read -r -d '' staged_file; do
    relative_path=${staged_file#"$RESTORE_TMP"/}
    if [[ -e $DESTINATION_PARENT/$relative_path ]]; then
        CONFLICT=$relative_path
        break
    fi
done < <(find "$RESTORE_TMP" -type f -print0)
[[ -z $CONFLICT ]] \
    || die "Restore would overwrite an existing file: $DESTINATION_PARENT/$CONFLICT"

FILE_COUNT=$(find "$RESTORE_TMP" -type f | wc -l)
if (( DRY_RUN )); then
    printf 'Restore dry run passed: %s file(s), mode %s, no conflicts.\n' "$FILE_COUNT" "$MODE"
    exit 0
fi

mkdir -p -- "$DESTINATION_PARENT"
for staged_root in \
    "$RESTORE_TMP/Eltec_405M22_Test_Results" \
    "$RESTORE_TMP/Eltec_406MCA_Test_Results" \
    "$RESTORE_TMP/Eltec_TestRig_Backup_Metadata"; do
    if [[ -d $staged_root ]]; then
        cp -a --no-clobber "$staged_root" "$DESTINATION_PARENT/"
    fi
done

printf 'Restore complete: %s file(s), mode %s, destination %s\n' \
    "$FILE_COUNT" "$MODE" "$DESTINATION_PARENT"
if [[ $MODE == results-only ]]; then
    printf 'Fixture reference calibration was excluded; complete each model-specific calibration/known-good check before DUT testing.\n'
else
    printf 'Fixture calibration was restored under the operator-confirmed same-fixture mode.\n'
fi
