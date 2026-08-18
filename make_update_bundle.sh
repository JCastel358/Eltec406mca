#!/usr/bin/env bash
# Build a portable, checksummed Git update bundle for offline stations.

set -Eeuo pipefail

SCRIPT_NAME=${0##*/}

usage() {
    cat <<EOF
Usage: $SCRIPT_NAME [OUTPUT.bundle] [REVISION]

Create an offline update from committed code. Defaults to the currently
checked-out commit (HEAD) and ./eltec406mca-update-<short-commit>.bundle.
An optional full SHA or tag acts as an assertion and must resolve to HEAD.
The bundle is pinned to that full commit in a companion manifest. Copy the .bundle,
.manifest, and .sha256 files to USB, then run the exact printed command on a
station.

The printed station command includes the approved full commit SHA.
EOF
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

case ${1:-} in
    -h|--help)
        usage
        exit 0
        ;;
esac
(( $# <= 2 )) || die "Pass at most OUTPUT.bundle and REVISION."

SOURCE_PATH=${BASH_SOURCE[0]}
if command -v readlink >/dev/null 2>&1; then
    RESOLVED_SOURCE=$(readlink -f -- "$SOURCE_PATH" 2>/dev/null || true)
    if [[ -n $RESOLVED_SOURCE ]]; then
        SOURCE_PATH=$RESOLVED_SOURCE
    fi
fi
REPO_ROOT=$(cd -- "$(dirname -- "$SOURCE_PATH")" && pwd -P)
[[ -d $REPO_ROOT/.git ]] || die "A Git checkout is required to build an update bundle."

REQUESTED_REVISION=${2:-HEAD}
git -C "$REPO_ROOT" cat-file -e "$REQUESTED_REVISION^{commit}" 2>/dev/null \
    || die "Revision is not an available commit or tag: $REQUESTED_REVISION"
FULL_COMMIT=$(git -C "$REPO_ROOT" rev-parse "$REQUESTED_REVISION^{commit}")
HEAD_COMMIT=$(git -C "$REPO_ROOT" rev-parse 'HEAD^{commit}')
[[ $FULL_COMMIT == "$HEAD_COMMIT" ]] \
    || die "Requested revision is not checked out. Run git checkout --detach $FULL_COMMIT, review it, and retry."

for required_tracked_file in \
    setup_xubuntu.sh update_xubuntu.sh doctor_xubuntu.sh \
    rollback_xubuntu.sh backup_eltec_results.sh restore_eltec_results.sh \
    make_update_bundle.sh; do
    git -C "$REPO_ROOT" cat-file -e "$FULL_COMMIT:$required_tracked_file" 2>/dev/null \
        || die "Provisioning file is absent from approved commit $FULL_COMMIT: $required_tracked_file"
done

if ! git -C "$REPO_ROOT" diff --quiet --ignore-submodules -- \
    || ! git -C "$REPO_ROOT" diff --cached --quiet --ignore-submodules --; then
    die "Tracked changes are not included in Git bundles. Commit them before building an update."
fi

SHORT_COMMIT=${FULL_COMMIT:0:12}
OUTPUT=${1:-"$PWD/eltec406mca-update-$SHORT_COMMIT.bundle"}
if [[ $OUTPUT != /* ]]; then
    OUTPUT="$PWD/$OUTPUT"
fi
OUTPUT_DIR=$(dirname -- "$OUTPUT")
[[ -d $OUTPUT_DIR && -w $OUTPUT_DIR ]] || die "Output directory is not writable: $OUTPUT_DIR"
[[ $(basename -- "$OUTPUT") != *$'\n'* ]] || die "Output filename cannot contain a newline."
[[ ! -e $OUTPUT && ! -e $OUTPUT.manifest && ! -e $OUTPUT.sha256 ]] \
    || die "Output, manifest, or checksum already exists; choose a new filename: $OUTPUT"

git -C "$REPO_ROOT" bundle create "$OUTPUT" HEAD
git -C "$REPO_ROOT" bundle verify "$OUTPUT"
BUNDLE_REF='HEAD'
BUNDLED_COMMIT=$(git -C "$REPO_ROOT" bundle list-heads "$OUTPUT" "$BUNDLE_REF" | awk 'NR == 1 { print $1 }')
[[ $BUNDLED_COMMIT == "$FULL_COMMIT" ]] \
    || die "The branch moved while the bundle was being built; discard it and retry."

{
    printf 'ELTEC_UPDATE_FORMAT=1\n'
    printf 'APPROVED_COMMIT=%s\n' "$FULL_COMMIT"
    printf 'BUNDLE_REF=%s\n' "$BUNDLE_REF"
} >"$OUTPUT.manifest"

BUNDLE_BASENAME=$(basename -- "$OUTPUT")
MANIFEST_BASENAME=$(basename -- "$OUTPUT.manifest")
BUNDLE_SHA=$(sha256sum -- "$OUTPUT" | cut -d' ' -f1)
MANIFEST_SHA=$(sha256sum -- "$OUTPUT.manifest" | cut -d' ' -f1)
{
    printf '%s  %s\n' "$BUNDLE_SHA" "$BUNDLE_BASENAME"
    printf '%s  %s\n' "$MANIFEST_SHA" "$MANIFEST_BASENAME"
} >"$OUTPUT.sha256"

printf '\nOffline update created from approved commit %s:\n' "$FULL_COMMIT"
printf '  %s\n  %s\n  %s\n' "$OUTPUT" "$OUTPUT.manifest" "$OUTPUT.sha256"
printf 'Copy all three files to the station and run this exact command:\n'
printf '  ./update_xubuntu.sh --bundle %q --revision %q\n' "$BUNDLE_BASENAME" "$FULL_COMMIT"
