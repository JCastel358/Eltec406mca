#!/usr/bin/env bash
# Run every Eltec test-rig suite and print a summary (see run_all_tests.py).
# ELTEC_PYTHON overrides the interpreter, as for the app launchers.
exec "${ELTEC_PYTHON:-python3}" "$(dirname -- "$0")/run_all_tests.py" "$@"
