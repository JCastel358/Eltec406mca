#!/usr/bin/env python3
"""Run every test suite of the Eltec test rig and print one summary table.

Each suite runs in its own interpreter - exactly the documented
``python -m unittest discover -s <suite dir>`` command, from the repository
root - because the model directories deliberately share module names
(``esp32_backend``, ``stability_analysis``, the vendored engine) and must
never be imported into one process together.

Exit status: 0 when every suite is clean, 1 otherwise. Two cases in the
406 MCA suite are known environment-only failures on Windows (they exercise
the bash installer and the POSIX exclusive-tty flag, and pass on Xubuntu);
on Windows they are reported as "known" and do not fail the run. Any other
failure or error does. See docs/ENGINEER_HANDOVER.md section 7.

Usage:
    python run_all_tests.py            # all suites
    python run_all_tests.py -v         # pass -v (or any unittest flag) through
    python run_all_tests.py --strict   # count the known Windows cases as failures
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# (label, suite directory relative to the repository root)
SUITES = (
    ("selector glue", "single_detector_rig/tests"),
    ("405 M22", "single_detector_rig/m405m22/tests"),
    ("406 MCA", "single_detector_rig/m406mca/tests"),
    ("449 M18", "single_detector_rig/m449m18/tests"),
)

# Exact test ids that fail on Windows for environment reasons only.
KNOWN_WINDOWS_ENVIRONMENT_CASES = {
    "406 MCA": {
        "test_launcher_installation_uses_only_v6_1_identities",
        "test_auto_connect_validates_candidates_and_is_idempotent",
    },
}

_RAN_RE = re.compile(r"^Ran (\d+) tests? in", re.MULTILINE)
_RESULT_RE = re.compile(r"^(OK|FAILED)(?: \((.*)\))?\s*$", re.MULTILINE)
_CASE_RE = re.compile(r"^(FAIL|ERROR): (\w+) \(", re.MULTILINE)


def run_suite(label: str, suite_dir: str, extra_args: list[str]) -> dict:
    command = [sys.executable, "-m", "unittest", "discover", "-s", suite_dir, *extra_args]
    completed = subprocess.run(
        command, cwd=REPO_ROOT, capture_output=True, text=True, errors="replace"
    )
    output = completed.stdout + completed.stderr
    ran = _RAN_RE.search(output)
    result = _RESULT_RE.search(output)
    counts = {"failures": 0, "errors": 0, "skipped": 0}
    if result and result.group(2):
        for item in result.group(2).split(","):
            key, _, value = item.strip().partition("=")
            if key in counts and value.isdigit():
                counts[key] = int(value)
    failing = [name for _kind, name in _CASE_RE.findall(output)]
    return {
        "label": label,
        "dir": suite_dir,
        "ran": int(ran.group(1)) if ran else 0,
        "parsed": bool(ran and result),
        "failing": failing,
        "output": output,
        **counts,
    }


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    extra_args = [arg for arg in argv if arg != "--strict"]
    on_windows = sys.platform.startswith("win")

    print(f"Eltec test rig - running {len(SUITES)} suites with {sys.executable}\n")
    results = []
    for label, suite_dir in SUITES:
        print(f"  {label:<14} {suite_dir} ...", end="", flush=True)
        outcome = run_suite(label, suite_dir, extra_args)
        results.append(outcome)
        print(f" ran {outcome['ran']}")

    print()
    print(f"  {'suite':<14} {'ran':>5} {'fail':>5} {'error':>6} {'skip':>5}  status")
    print(f"  {'-' * 14} {'-' * 5} {'-' * 5} {'-' * 6} {'-' * 5}  {'-' * 30}")
    overall_ok = True
    total = 0
    for outcome in results:
        total += outcome["ran"]
        known = KNOWN_WINDOWS_ENVIRONMENT_CASES.get(outcome["label"], set())
        unexpected = [name for name in outcome["failing"] if name not in known]
        accepted = [name for name in outcome["failing"] if name in known]
        if not outcome["parsed"]:
            status = "COULD NOT RUN"
            overall_ok = False
        elif unexpected or (accepted and (strict or not on_windows)):
            status = "FAILED"
            overall_ok = False
        elif accepted:
            status = f"OK (known Windows-only: {len(accepted)})"
        else:
            status = "OK"
        print(
            f"  {outcome['label']:<14} {outcome['ran']:>5} {outcome['failures']:>5} "
            f"{outcome['errors']:>6} {outcome['skipped']:>5}  {status}"
        )
        for name in unexpected:
            print(f"      unexpected: {name}")
    print(f"\n  total tests: {total}")

    if not overall_ok:
        print("\nRESULT: FAILED - details follow.\n")
        for outcome in results:
            known = KNOWN_WINDOWS_ENVIRONMENT_CASES.get(outcome["label"], set())
            if not outcome["parsed"] or any(n not in known for n in outcome["failing"]) or (
                outcome["failing"] and (strict or not on_windows)
            ):
                print(f"=== {outcome['label']} ({outcome['dir']}) ===")
                print(outcome["output"].rstrip())
                print()
        return 1
    print("\nRESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
