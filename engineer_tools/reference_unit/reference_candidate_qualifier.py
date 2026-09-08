#!/usr/bin/env python3
"""Pick the 406MCA detector to mount as the rig's AIN1 reference unit (engineering only).

Why (2026-09-04)
----------------
The single-detector rig's reference gate (AIN1, ``REFERENCE_GATE_ENABLED``)
has been off on every model since the shared dual op-amp buffer was shown
to couple the DUT into the reference channel (CALIBRATION_RECORD section
2.4). The buffer board now has one independent op-amp per channel, so the
gate can come back - which needs a permanently mounted reference detector.
Five candidate 406MCA parts are on the bench. The reference unit is read
at the start of every DUT test after a short emitter warm-up, so the part
that makes the best reference is the one that:

  * settles its DC offset quickly (many 406MCAs keep moving for tens of
    seconds after they are seated or after the emitter heats them - a slow
    settler makes every reference read a waiting game),
  * stabilizes its 10 Hz / 50 % response quickly and always inside the
    production 20 s deadline,
  * gives the SAME amplitude every time it is measured (the absolute
    sensitivity does not matter for a reference; its repeatability and its
    freedom from drift while the emitter is on are what the gate depends
    on), with a clean signal (production SNR gate) and a stable polarity.

What one run measures
---------------------
Every run walks a candidate through the production 406 MCA measurement
path, using the production code itself (``eltec_406mca_esp32_tester`` and
``stability_analysis`` imported headless; nothing is re-implemented):

  A. offset settling from insertion - the operator re-seats the part and
     presses Enter; ``OFFSET?`` is polled once a second (the production poll)
     until the level has been quiet for OFFSET_QUIET_SPAN_S or for at most
     ``--offset-max-s``. Reported: time into the 0.3-1.2 V band, time until
     the level stayed within +/-OFFSET_SETTLE_TOLERANCE_MV of its final
     value, the final value, and what the production settle rule
     (``wait_for_settled_offset``, replayed on the recorded reads) would
     have decided and when.
  B. the production driven capture - ``PIN,33`` / ``PWM,ON`` at the fixed
     10 Hz / 50 %, ``read_waveform_until_stable`` with the production DUT
     stability settings, stream retries and front-end re-checks exactly as
     the app does; then ``analyze_v6_stable_measurement`` for the
     production sensitivity (median of the 20 official cycle pk-pk values),
     SNR, polarity and the production PASS/FAIL. Reported in addition: the
     stabilization time from PWM-on and the spread (CV) of the 20 cycles.
  C. a drive hold - the emitter stays on and a further ``--hold-s`` of
     waveform is streamed; the per-cycle pk-pk trend over the hold gives
     the amplitude drift (%/min) a warm reference would show.
  D. the post-capture offset re-read - the production ``wait_for_settled_offset``
     is run live after ``PWM,OFF`` (this is the read that carries the
     production offset verdict), then polling continues until the level is
     quiet again: recovery time after emitter heating and the offset shift
     the heating caused.

``capture`` runs A-D ``--runs`` times per candidate (re-seat between runs so
every run has a real insertion event) and writes one ``.json`` (every
number, plus the offset and per-cycle series) and one ``.npz`` (the raw
waveforms, replayable) per run. ``compare`` reads the JSON files of every
candidate, aggregates per candidate (medians over runs, run-to-run CV of the
sensitivity), disqualifies anything that failed the production test, never
stabilized or never entered the offset band, ranks the rest on weighted
metric ranks (SCORE_WEIGHTS - printed with the table, so the trade-off is
visible) and writes ``comparison_<stamp>.csv`` + ``.png`` next to the runs.

Where the candidate sits
------------------------
Default ``--channel sensor``: the candidate is in the DUT socket (AIN0) and
is judged with the DUT rules - the strictest, most discriminating test, and
the socket makes swapping five parts quick. ``--channel ref`` measures a
part already mounted on AIN1 (``REF?`` / ``STREAM,START,REF``) with the same
threshold and cycle count; the production AIN1 path does not enforce the
measurement-attempt policy, which is noted in the output.

Outputs are written OUTSIDE the repository and outside every
``Eltec_*_Test_Results`` evidence folder (default
``~/Documents/Eltec_ReferenceCandidates/<label>/``). The tool issues no part
verdicts of its own and never touches the production apps or their data.

Examples (from the repository root):

  python engineer_tools/reference_unit/reference_candidate_qualifier.py capture --label refA --runs 3
  python engineer_tools/reference_unit/reference_candidate_qualifier.py capture --label refB --runs 3 --port COM3
  python engineer_tools/reference_unit/reference_candidate_qualifier.py compare            # every candidate under the root
  python engineer_tools/reference_unit/reference_candidate_qualifier.py compare refA refB refC --show
  python engineer_tools/reference_unit/reference_candidate_qualifier.py capture --simulate --label demo --runs 2 --out <scratch dir>

Permanent-reference retest (2026-09-08)
--------------------------------------
Add ``--precision`` to capture AND compare. Leave the candidate seated on
AIN0 for all 30 readings. Only the first reading waits for insertion settling;
later readings follow alternating 2/10/60 s minimum emitter-off intervals.
Each pulse still captures the production 20-cycle measurement plus the drive
hold, but also replays the actual five-cycle reference algorithm on its early
samples. Precision ranking emphasizes repeatability and worst deviations of
that early reading, then the 20-cycle repeatability, hold drift and speed.
These data live in ``<out>/precision`` and are never pooled with reseating runs.
The AIN0 replay tests the reference algorithm, not AIN1 wiring or crosstalk.

  python engineer_tools/reference_unit/reference_candidate_qualifier.py capture --precision --label refD
  python engineer_tools/reference_unit/reference_candidate_qualifier.py compare --precision refD refC refA --show
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO_ROOT / "single_detector_rig" / "m406mca"
if str(MODEL_DIR) not in sys.path:
    sys.path.insert(0, str(MODEL_DIR))

import eltec_406mca_esp32_tester as app  # noqa: E402  (the production 406 MCA tester, headless)
import stability_analysis as sa  # noqa: E402  (the production stability math)
from esp32_backend import Esp32BackendError, StreamStateError  # noqa: E402

TOOL_NAME = "reference_candidate_qualifier"
TOOL_VERSION = "1.1"
RUN_SCHEMA = 1

# NOT one of the Eltec_*_Test_Results evidence folders: this is engineering
# data about candidate parts, never production evidence.
DEFAULT_OUTPUT_ROOT = Path.home() / "Documents" / "Eltec_ReferenceCandidates"

# Offset polling. The poll is the production OFFSET_SETTLE_POLL_S (1 s) so
# the recorded reads can be replayed through the production settle rule
# one-for-one. The tool polls for longer than production's 20 s hold
# because the question here is "how long does THIS part really take", not
# "is it inside the band yet".
OFFSET_POLL_S = float(app.OFFSET_SETTLE_POLL_S)
OFFSET_MAX_WAIT_S = 60.0
# "Settled" for ranking purposes: every later read stays within this much
# of the final level. 10 mV is ~1.5 % of a typical 0.67 V offset and about
# what the reference read tolerates without moving the gate.
OFFSET_SETTLE_TOLERANCE_MV = 10.0
# Live early stop: once the newest OFFSET_QUIET_SPAN_S of reads span no more
# than the tolerance, the final level is known and polling stops.
OFFSET_QUIET_SPAN_S = 8.0
# Drive hold after the production capture (emitter still on). 20 s at 10 Hz
# is 200 cycles - enough for a slope in mV/min to mean something.
DEFAULT_HOLD_S = 20.0
DEFAULT_RUNS = 3
DEFAULT_PRECISION_RUNS = 30
DEFAULT_OFF_INTERVALS_S = (2.0, 10.0, 60.0)
PRECISION_PROTOCOL = "precision-v1"
# Production numbers used here (imported, never copied, so a change in the
# app is picked up automatically).
DRIVE_FREQUENCY_HZ = float(app.EMITTER_PWM_FREQUENCY_HZ)
DRIVE_DUTY_PERCENT = float(app.EMITTER_PWM_DUTY_CYCLE)
DRIVE_CHANNEL = app.EMITTER_PWM_CHANNEL
MEASUREMENT_CYCLES = int(app.SENSITIVITY_MEASUREMENT_CYCLES)
STABILITY_TIMEOUT_S = float(app.STABILITY_TIMEOUT_S)

# Ranking weights (lower metric = better for every one of these). Ranks are
# averaged across ties, multiplied by the weight and summed; the smallest
# total wins. Weights encode what a reference unit is FOR: it must be quick
# to settle and repeatable; its absolute sensitivity is irrelevant and is
# not a metric at all.
SCORE_WEIGHTS: dict[str, float] = {
    "offset_settle_s": 3.0,          # insertion settle (median over runs)
    "offset_settle_worst_s": 1.0,    # the slowest run
    "post_recovery_s": 2.0,          # settle after emitter heating
    "stabilization_s": 2.0,          # 10 Hz response stabilization from PWM-on
    "sensitivity_run_cv_percent": 3.0,  # run-to-run repeatability
    "hold_drift_abs_percent_per_min": 2.0,  # amplitude drift while driven
    "cycle_cv_percent": 1.0,         # spread of the 20 official cycles
    "snr_penalty": 1.0,              # 1 / SNR (higher SNR = smaller = better)
}
METRIC_LABELS = {
    "offset_settle_s": "offset settle s",
    "offset_settle_worst_s": "worst settle s",
    "post_recovery_s": "post-emitter recovery s",
    "stabilization_s": "stabilization s",
    "sensitivity_run_cv_percent": "run-to-run CV %",
    "hold_drift_abs_percent_per_min": "hold drift %/min",
    "cycle_cv_percent": "cycle CV %",
    "snr_penalty": "1/SNR",
}

# Engineering selection preferences, not production acceptance limits.
# Timing ranks share 0.1 s bins; precision ranks share 0.01 percentage-point
# bins so sub-millisecond differences cannot decide the recommendation.
PRECISION_WEIGHTS = {
    "reference_run_cv_percent": 5.0,
    "reference_max_deviation_percent": 3.0,
    "sensitivity_run_cv_percent": 2.0,
    "hold_drift_abs_percent_per_min": 2.0,
    "reference_ready_worst_s": 1.0,
    "reference_ready_s": 1.0,
}
METRIC_LABELS.update({
    "reference_run_cv_percent": "five-cycle reading CV %",
    "reference_max_deviation_percent": "five-cycle worst deviation %",
    "reference_ready_worst_s": "worst five-cycle ready s",
    "reference_ready_s": "median five-cycle ready s",
})


# ----------------------------------------------------------------------
# clocks (real, and virtual for --simulate and the tests)
# ----------------------------------------------------------------------
class RealClock:
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)


class VirtualClock:
    """A clock that only moves when something sleeps or advances it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))

    advance = sleep


# ----------------------------------------------------------------------
# analysis (pure functions - the tests exercise these directly)
# ----------------------------------------------------------------------
def _fmt(value, digits: int = 3, unit: str = "") -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "-"
    return f"{value:.{digits}f}{unit}"


def offset_settle_metrics(
    t_s, v, *, tolerance_mv: float = OFFSET_SETTLE_TOLERANCE_MV, tail_s: float = OFFSET_QUIET_SPAN_S
) -> dict:
    """Settling numbers for one recorded offset series (seconds from the first read).

    ``final_v`` is the mean of the last three reads. ``settle_s`` is the first
    time after which every read stays within ``tolerance_mv`` of that level -
    None when the series never went quiet (its last ``tail_s`` still spans
    more than the tolerance), which is how a part that was still moving at
    the maximum wait is told from one that settled at the very end.
    """

    t = np.asarray(t_s, dtype=float)
    volts = np.asarray(v, dtype=float)
    if t.size == 0:
        raise ValueError("offset series is empty")
    final_v = float(volts[-3:].mean())
    tol_v = tolerance_mv / 1000.0
    tail = volts[t >= t[-1] - tail_s]
    settled = bool(tail.max() - tail.min() <= tol_v) if tail.size else False
    settle_s = None
    if settled:
        outside = np.abs(volts - final_v) > tol_v
        last_outside = int(np.flatnonzero(outside)[-1]) if outside.any() else -1
        settle_s = float(t[last_outside + 1]) if last_outside + 1 < t.size else float(t[-1])
    in_band = (volts >= app.OFFSET_MIN_V) & (volts <= app.OFFSET_MAX_V)
    time_to_band_s = float(t[int(np.flatnonzero(in_band)[0])]) if in_band.any() else None
    production = replay_production_offset_wait(t, volts)
    return {
        "first_v": float(volts[0]),
        "final_v": final_v,
        "reads": int(volts.size),
        "duration_s": float(t[-1] - t[0]),
        "tolerance_mv": float(tolerance_mv),
        "settled": settled,
        "settle_s": settle_s,
        "time_to_band_s": time_to_band_s,
        "final_in_band": bool(app.OFFSET_MIN_V <= final_v <= app.OFFSET_MAX_V),
        "total_move_mv": float((volts[0] - final_v) * 1000.0),
        "tail_span_mv": float((tail.max() - tail.min()) * 1000.0) if tail.size else None,
        **production,
    }


def replay_production_offset_wait(t_s, v) -> dict:
    """Run the production settle rule over recorded reads (virtual clock).

    ``wait_for_settled_offset`` is given the recorded reads in order and a
    clock that returns each read's timestamp, so the decision and its timing
    are exactly what the app would have produced on this part. A series that
    ends before the rule exits is extended flat (the level had gone quiet).
    """

    t = [float(x) for x in t_s]
    volts = [float(x) for x in v]
    state = {"index": 0}

    def monotonic() -> float:
        i = state["index"]
        if i < len(t):
            return t[i]
        return t[-1] + (i - len(t) + 1) * OFFSET_POLL_S

    def read_offset() -> float:
        state["index"] += 1
        i = state["index"]
        return volts[i] if i < len(volts) else volts[-1]

    report = app.wait_for_settled_offset(
        read_offset, start_v=volts[0], monotonic=monotonic, sleep=lambda _s: None,
    )
    return {
        "production_settle_s": float(report.elapsed_s),
        "production_final_v": float(report.final_v),
        "production_settled": bool(report.settled),
        "production_in_band": bool(report.in_band),
        "production_timed_out": bool(report.timed_out),
        "production_drifting": bool(report.drifting),
    }


def cycle_peak_to_peak_series(waveform_v, sync_v, sample_rate_hz: float) -> tuple[np.ndarray, np.ndarray]:
    """Per complete PWM cycle: start time (s) and raw pk-pk (mV), production edges."""

    waveform = np.asarray(waveform_v, dtype=float)
    segments = sa.complete_cycle_segments(np.asarray(sync_v, dtype=float))
    if not segments:
        return np.zeros(0), np.zeros(0)
    starts = np.asarray([s for s, _e in segments], dtype=float) / float(sample_rate_hz)
    pp = np.asarray([waveform[s:e].max() - waveform[s:e].min() for s, e in segments], dtype=float) * 1000.0
    return starts, pp


def hold_trend(t_s, pp_mv) -> dict:
    """Amplitude trend over a driven hold: mean, CV and a least-squares slope."""

    t = np.asarray(t_s, dtype=float)
    pp = np.asarray(pp_mv, dtype=float)
    if pp.size < 3:
        return {"hold_cycles": int(pp.size), "hold_mean_mv": None, "hold_cv_percent": None,
                "hold_slope_mv_per_min": None, "hold_drift_percent_per_min": None}
    mean = float(pp.mean())
    slope_per_s = float(np.polyfit(t - t[0], pp, 1)[0])
    return {
        "hold_cycles": int(pp.size),
        "hold_mean_mv": mean,
        "hold_cv_percent": float(100.0 * pp.std(ddof=1) / mean) if mean else None,
        "hold_slope_mv_per_min": slope_per_s * 60.0,
        "hold_drift_percent_per_min": float(100.0 * slope_per_s * 60.0 / mean) if mean else None,
    }


def cv_percent(values) -> float | None:
    arr = np.asarray([x for x in values if x is not None], dtype=float)
    if arr.size < 2 or arr.mean() == 0:
        return 0.0 if arr.size == 1 else None
    return float(100.0 * arr.std(ddof=1) / arr.mean())


def _median(values) -> float | None:
    arr = [x for x in values if x is not None and math.isfinite(x)]
    return float(np.median(arr)) if arr else None


def aggregate_candidate(label: str, runs: list[dict]) -> dict:
    """Per-candidate summary over its runs, plus the reasons it is disqualified."""

    pre = [r["offset_pre"] for r in runs]
    post = [r["offset_post"] for r in runs]
    drive = [r["drive"] for r in runs]
    hold = [r["hold"] for r in runs]
    disqualified: list[str] = []
    never_settled = [r["run"] for r, p in zip(runs, pre) if not p["settled"]]
    never_in_band = [r["run"] for r, p in zip(runs, pre) if p["time_to_band_s"] is None]
    unstable = [r["run"] for r, d in zip(runs, drive) if not d["measurement_complete"]]
    failed = [r["run"] for r, d in zip(runs, drive) if d["production_passed"] is False]
    polarities = sorted({str(d["polarity"]) for d in drive if d["polarity"] is not None})
    if never_in_band:
        disqualified.append(f"offset never entered {app.OFFSET_MIN_V:.1f}-{app.OFFSET_MAX_V:.1f} V in run(s) {never_in_band}")
    if unstable:
        disqualified.append(f"10 Hz response never stabilized in run(s) {unstable}")
    if failed:
        # One entry per KIND of failure (the text before the colon), not one
        # per run - the numbers differ run to run, the reason does not.
        reasons = sorted({reason.split(":")[0].strip() for d in drive for reason in d["fail_reasons"]})
        disqualified.append(f"production test FAILED in run(s) {failed}: {'; '.join(reasons)}")
    if len(polarities) > 1:
        disqualified.append(f"polarity not consistent across runs: {polarities}")
    settle = [p["settle_s"] if p["settled"] else OFFSET_MAX_WAIT_S for p in pre]
    recovery = [p["settle_s"] if p["settled"] else OFFSET_MAX_WAIT_S for p in post]
    sensitivities = [d["sensitivity_mv"] for d in drive if d["sensitivity_mv"] is not None]
    snr = [d["signal_to_noise_ratio"] for d in drive if d["signal_to_noise_ratio"] is not None]
    drift = [abs(h["hold_drift_percent_per_min"]) for h in hold if h.get("hold_drift_percent_per_min") is not None]
    return {
        "label": label,
        "runs": len(runs),
        "run_numbers": [r["run"] for r in runs],
        "channel": sorted({r["channel"] for r in runs}),
        "disqualified": disqualified,
        "never_settled_runs": never_settled,
        "offset_first_v": _median([p["first_v"] for p in pre]),
        "offset_final_v": _median([p["final_v"] for p in pre]),
        "offset_final_spread_mv": (max(p["final_v"] for p in pre) - min(p["final_v"] for p in pre)) * 1000.0,
        "offset_settle_s": _median(settle),
        "offset_settle_worst_s": max(settle),
        "time_to_band_s": _median([p["time_to_band_s"] for p in pre]),
        "production_settle_s": _median([p["production_settle_s"] for p in pre]),
        "post_recovery_s": _median(recovery),
        "post_production_settle_s": _median([p["production_settle_s"] for p in post]),
        "offset_shift_mv": _median([(q["final_v"] - p["final_v"]) * 1000.0 for p, q in zip(pre, post)]),
        "stabilization_s": _median([d["stabilization_elapsed_s"] if d["stabilization_elapsed_s"] is not None else STABILITY_TIMEOUT_S for d in drive]),
        "stabilization_worst_s": max(d["stabilization_elapsed_s"] if d["stabilization_elapsed_s"] is not None else STABILITY_TIMEOUT_S for d in drive),
        "sensitivity_mean_mv": float(np.mean(sensitivities)) if sensitivities else None,
        "sensitivity_run_cv_percent": cv_percent(sensitivities),
        "cycle_cv_percent": _median([d["cycle_cv_percent"] for d in drive]),
        "snr_min": min(snr) if snr else None,
        "snr_penalty": (1.0 / min(snr)) if snr and min(snr) > 0 else None,
        "polarity": "/".join(polarities) if polarities else None,
        "hold_drift_abs_percent_per_min": _median(drift),
        "hold_cv_percent": _median([h.get("hold_cv_percent") for h in hold]),
        "production_passed_runs": sum(1 for d in drive if d["production_passed"]),
        "warnings": sorted({w for d in drive for w in d["warnings"]}),
    }


def reference_replay(waveform, sync, rate: float, pwm_offset_s: float) -> dict:
    """Select the same early five cycles as the reference gate, on AIN0 data.

    The stricter DUT acquisition keeps streaming beyond this window. Reuse
    the production reference settings and averaging; do not approximate the
    early result by taking five cycles from the later DUT measurement.
    """
    settings = app.reference_stability_settings(app.load_stability_settings())
    analysis = sa.analyze_stability(
        waveform, sync, rate, settings, pwm_elapsed_offset_s=pwm_offset_s,
        stability_deadline_s=STABILITY_TIMEOUT_S,
        measurement_cycles_required=app.REFERENCE_MEASUREMENT_CYCLES,
        enforce_measurement_stability=False, data_source="ain0_reference_replay",
    )
    complete = analysis.report.measurement_complete
    return {
        "measurement_complete": complete,
        "reading_mv": app.analyze_reference_stable_response_mv(analysis) if complete else None,
        "ready_s": analysis.measurement_cycles[-1].end_elapsed_s if complete else None,
        "stabilization_s": analysis.report.stabilization_elapsed_s,
        "threshold_mv": settings.peak_delta_threshold_mv,
        "confirmation_count": settings.consecutive_deltas_required,
        "measurement_cycles": int(app.REFERENCE_MEASUREMENT_CYCLES),
        "cycle_pp_mv": [float(c.peak_to_peak_v * 1000.0) for c in analysis.measurement_cycles],
        "cycle_numbers": [c.cycle_number for c in analysis.measurement_cycles],
    }


def aggregate_precision_candidate(label: str, runs: list[dict]) -> dict:
    candidate = aggregate_candidate(label, runs)
    sessions = {}
    for run in runs:
        session = sessions.setdefault(run["session_id"], {"planned": run["planned_runs"], "runs": set()})
        session["runs"].add(run["run"])
    for session_id, session in sessions.items():
        if session["runs"] != set(range(1, session["planned"] + 1)):
            candidate["disqualified"].append(
                f"incomplete session {session_id}: {len(session['runs'])}/{session['planned']} readings saved; "
                "repeat the retest using a fresh --out folder")
    refs = [r["reference"] for r in runs]
    readings = [r["reading_mv"] for r in refs if r["measurement_complete"]]
    ready = [r["ready_s"] for r in refs if r["measurement_complete"]]
    incomplete = [r["run"] for r in runs if not r["reference"]["measurement_complete"]]
    if incomplete:
        candidate["disqualified"].append(f"five-cycle reference reading incomplete in run(s) {incomplete}")
    if len(readings) < 2:
        candidate["disqualified"].append("at least two complete readings are needed to estimate precision")
    mean = float(np.mean(readings)) if readings else None
    deviations = [abs(v - mean) / mean * 100.0 for v in readings] if mean else []
    differences = [r["precision"]["early_vs_late_percent"] for r in runs
                   if r["precision"]["early_vs_late_percent"] is not None]
    conditions = {}
    for run in runs:
        if run["reference"]["measurement_complete"]:
            conditions.setdefault(run["precision"]["condition"], []).append(run["reference"]["reading_mv"])
    condition_stats = {key: {"n": len(values), "mean_mv": float(np.mean(values)),
                             "cv_percent": cv_percent(values) if len(values) > 1 else None}
                       for key, values in conditions.items()}
    # The first observation has a different insertion history. Show it, but
    # compare the repeated off-interval groups separately from that first read.
    means = [v["mean_mv"] for k, v in condition_stats.items() if k != "initial"]
    candidate.update({
        "reference_mean_mv": mean,
        "reference_run_cv_percent": cv_percent(readings) if len(readings) > 1 else None,
        "reference_max_deviation_percent": max(deviations) if deviations else None,
        "reference_ready_s": _median(ready),
        "reference_ready_worst_s": max(ready) if ready else None,
        "reference_complete_runs": len(readings),
        "reference_condition_span_percent": 100.0 * (max(means) - min(means)) / mean if len(means) > 1 and mean else None,
        "reference_condition_stats": condition_stats,
        "early_vs_late_median_percent": _median(differences),
        "early_vs_late_span_percent": max(differences) - min(differences) if differences else None,
        "stream_retries": sum(len(r["drive"].get("retries", [])) for r in runs),
    })
    if len(runs) < DEFAULT_PRECISION_RUNS:
        candidate["warnings"].append(f"Only {len(runs)} readings; recommended retest is {DEFAULT_PRECISION_RUNS} per candidate")
    if candidate["hold_drift_abs_percent_per_min"] is None:
        candidate["warnings"].append("No usable drive hold: thermal drift was not assessed")
    if any(r["drive"].get("retries") for r in runs):
        candidate["warnings"].append("Stream retries changed the heating history; inspect the per-run retries before choosing")
    return candidate


def rank_candidates(candidates: list[dict], weights: dict[str, float] | None = None,
                    *, precision: bool = False) -> list[dict]:
    """Weighted rank sum (lower is better); disqualified candidates sort last.

    A metric that is None for a candidate takes the worst rank for that
    metric, so a missing number can never help.
    """

    weights = SCORE_WEIGHTS if weights is None else weights
    eligible = [c for c in candidates if not c["disqualified"]]
    scores = {c["label"]: 0.0 for c in candidates}
    ranks = {c["label"]: {} for c in candidates}
    for metric, weight in weights.items():
        values = [(c["label"], c.get(metric)) for c in eligible]
        worst = max((v for _l, v in values if v is not None), default=0.0)
        digits = 1 if metric.endswith("_s") else 2
        keyed = [(l, worst + 1.0 if v is None else (round(v, digits) if precision else v)) for l, v in values]
        order = sorted(range(len(keyed)), key=lambda i: keyed[i][1])
        # average rank across ties
        position = 0
        while position < len(order):
            end = position
            while end + 1 < len(order) and keyed[order[end + 1]][1] == keyed[order[position]][1]:
                end += 1
            mean_rank = (position + end) / 2.0 + 1.0
            for i in order[position:end + 1]:
                ranks[keyed[i][0]][metric] = mean_rank
                scores[keyed[i][0]] += weight * mean_rank
            position = end + 1
    ordered = sorted(candidates, key=lambda c: (bool(c["disqualified"]), scores[c["label"]], c["label"]))
    result = []
    for place, c in enumerate(ordered, start=1):
        result.append({**c, "score": scores[c["label"]] if not c["disqualified"] else None,
                       "metric_ranks": ranks[c["label"]], "place": place if not c["disqualified"] else None})
    return result


def recommendation_text(ranked: list[dict]) -> str:
    eligible = [c for c in ranked if not c["disqualified"]]
    if not eligible:
        return "No candidate is eligible - every one was disqualified (see the reasons above)."
    best = eligible[0]
    strengths = [METRIC_LABELS[m] for m, r in sorted(best["metric_ranks"].items(), key=lambda kv: kv[1]) if r <= 1.5][:4]
    weak = [METRIC_LABELS[m] for m, r in best["metric_ranks"].items() if r >= max(2.0, len(eligible) - 0.5)]
    text = f"Recommended reference unit: {best['label']} (score {best['score']:.1f}, {best['runs']} runs)."
    tied = [c["label"] for c in eligible[1:] if c["score"] == best["score"]]
    if tied:
        text = f"Tied leading candidates: {best['label']}, {', '.join(tied)} (score {best['score']:.1f}); no unique winner."
    if strengths:
        text += f" Best on: {', '.join(strengths)}."
    if weak:
        text += f" Not the best on: {', '.join(weak)} - check those rows before deciding."
    if len(eligible) > 1:
        runner = eligible[1]
        text += f" Runner-up: {runner['label']} (score {runner['score']:.1f})."
    if best["never_settled_runs"]:
        text += f" Note: its offset had not gone quiet at the maximum wait in run(s) {best['never_settled_runs']}."
    return text


# ----------------------------------------------------------------------
# capture
# ----------------------------------------------------------------------
@dataclass
class CaptureConfig:
    label: str
    runs: int = DEFAULT_RUNS
    channel: str = "sensor"
    offset_max_s: float = OFFSET_MAX_WAIT_S
    hold_s: float = DEFAULT_HOLD_S
    tolerance_mv: float = OFFSET_SETTLE_TOLERANCE_MV
    auto: bool = False
    out_root: Path = DEFAULT_OUTPUT_ROOT
    precision: bool = False
    off_intervals_s: tuple[float, ...] = DEFAULT_OFF_INTERVALS_S


def guard_output_root(path: Path) -> Path:
    """Refuse the production evidence folders - this is not test evidence."""

    resolved = Path(path).expanduser().resolve()
    for part in resolved.parts:
        if part.startswith("Eltec_") and part.endswith("_Test_Results"):
            raise SystemExit(f"refusing to write under a production results folder: {resolved}")
    return resolved


def make_offset_reader(rig, channel: str):
    """OFFSET? on the DUT channel, REF? on AIN1 (no backend wrapper exists for REF?)."""

    if channel == "sensor":
        return lambda: float(rig.read_offset_voltage())
    if hasattr(rig, "read_reference_voltage"):
        return lambda: float(rig.read_reference_voltage())

    def read_reference() -> float:
        if rig.is_streaming:
            raise StreamStateError("Stop the waveform stream before reading the reference offset.")
        return rig._parse_scalar_response(rig._command("REF?", "REF,"), "REF")

    return read_reference


def poll_offset_until_quiet(read_offset, clock, *, max_wait_s: float, tolerance_mv: float,
                            quiet_span_s: float = OFFSET_QUIET_SPAN_S, poll_s: float = OFFSET_POLL_S,
                            seed: tuple[list[float], list[float]] | None = None, status=None) -> tuple[list[float], list[float]]:
    """Poll the offset every ``poll_s`` until quiet (or ``max_wait_s``); returns (t_s, v)."""

    t_list, v_list = ([], []) if seed is None else (list(seed[0]), list(seed[1]))
    started = clock.monotonic() - (t_list[-1] if t_list else 0.0)
    if not t_list:
        v_list.append(float(read_offset()))
        t_list.append(0.0)
        if status:
            status(t_list[-1], v_list[-1])
    tol_v = tolerance_mv / 1000.0
    while True:
        elapsed = clock.monotonic() - started
        recent = [v for t, v in zip(t_list, v_list) if t >= elapsed - quiet_span_s]
        if elapsed >= quiet_span_s and recent and (max(recent) - min(recent)) <= tol_v:
            break
        if elapsed >= max_wait_s:
            break
        clock.sleep(poll_s)
        v_list.append(float(read_offset()))
        t_list.append(clock.monotonic() - started)
        if status:
            status(t_list[-1], v_list[-1])
    return t_list, v_list


def _progress_printer(label: str):
    state = {"cycles": -1}

    def progress(analysis) -> None:
        report = analysis.report
        if report.capture_cycles == state["cycles"] or report.capture_cycles % 10:
            return
        state["cycles"] = report.capture_cycles
        last = "-" if report.last_delta_mv is None else f"{report.last_delta_mv:.3f} mV"
        print(f"    [{label}] {report.total_pwm_on_seconds:5.1f} s PWM-on, cycle {report.capture_cycles}, "
              f"last delta {last}, run {report.active_confirmation_run_length}/{report.active_confirmation_count}"
              + (" - stabilized" if report.stabilized else ""), flush=True)

    return progress


def run_candidate(rig, cfg: CaptureConfig, run_index: int, *, clock=None, prompt=input, quiet: bool = False,
                  previous_pwm_off_s: float | None = None) -> dict:
    """Phases A-D on one seated part. Returns the run record (JSON-ready + arrays)."""

    clock = RealClock() if clock is None else clock
    say = (lambda *_a, **_k: None) if quiet else (lambda *a, **k: print(*a, **k, flush=True))
    settings = app.dut_stability_settings(app.load_stability_settings())
    rig.connect()
    reverted = rig.ensure_qualified_front_end()
    if reverted:
        say("  board had reverted its front end - re-programmed FE,V19")
    identity = getattr(getattr(rig, "identity", None), "text", None) or "unknown"
    read_offset = make_offset_reader(rig, cfg.channel)
    notes: list[str] = []
    if cfg.channel != "sensor":
        notes.append("AIN1 path: production reference capture does not enforce the measurement-attempt policy")

    if not cfg.auto and (not cfg.precision or run_index == 1):
        instruction = "seat the part and LEAVE IT SEATED for all readings" if cfg.precision else "remove and RE-SEAT the part"
        prompt(f"\n  {cfg.label} run {run_index}: {instruction}, then press Enter... ")
    interval_s = cfg.off_intervals_s[(run_index - 2) % len(cfg.off_intervals_s)] if cfg.precision and run_index > 1 else None
    if interval_s is not None and previous_pwm_off_s is not None:
        say(f"\n  {cfg.label} reading {run_index}/{cfg.runs}: emitter off for at least {interval_s:g} s; leave part seated")
        while clock.monotonic() - previous_pwm_off_s < interval_s:
            clock.sleep(min(1.0, interval_s - (clock.monotonic() - previous_pwm_off_s)))
    # ---- A. insertion offset settle -------------------------------------
    say("  A. offset snapshot (part remains seated)" if cfg.precision and run_index > 1 else
        f"  A. offset settling (poll {OFFSET_POLL_S:g} s, up to {cfg.offset_max_s:g} s)")
    status = (lambda t, v: say(f"     {t:5.1f} s  {v:.4f} V")) if not quiet else None
    seated_at = clock.monotonic()
    if cfg.precision and run_index > 1:
        pre_t, pre_v = [0.0], [float(read_offset())]
    else:
        pre_t, pre_v = poll_offset_until_quiet(read_offset, clock, max_wait_s=cfg.offset_max_s,
                                               tolerance_mv=cfg.tolerance_mv, status=status)
    pre = offset_settle_metrics(pre_t, pre_v, tolerance_mv=cfg.tolerance_mv)
    if cfg.precision and run_index > 1:
        pre["snapshot_only"] = True
        say(f"     -> snapshot {pre['final_v']:.4f} V; insertion settling is not re-tested")
    else:
        say(f"     -> final {pre['final_v']:.4f} V, settled {'at %.0f s' % pre['settle_s'] if pre['settled'] else 'NO (still moving)'}, "
            f"in band {'at %.0f s' % pre['time_to_band_s'] if pre['time_to_band_s'] is not None else 'NEVER'}, "
            f"production rule: {pre['production_settle_s']:.0f} s "
            f"{'in band' if pre['production_in_band'] else 'OUT OF BAND'}{' (timed out)' if pre['production_timed_out'] else ''}")

    # ---- B. production driven capture + C. hold --------------------------
    say(f"  B. production capture at {DRIVE_FREQUENCY_HZ:g} Hz / {DRIVE_DUTY_PERCENT:g} % on {DRIVE_CHANNEL} ({cfg.channel})")
    pwm_on: dict = {}
    hold_waveform = np.zeros(0)
    hold_sync = np.zeros(0)
    hold_rate = float(app.DEFAULT_SAMPLE_RATE_HZ)
    hold_offset_s = None
    retries: list[str] = []

    def driven(attempt: int):
        if attempt and app.verify_qualified_front_end(rig):
            notes.append(f"attempt {attempt + 1}: {app.FRONT_END_REVERTED_TEXT}")
        activation = rig.configure_emitter_pwm(channel=DRIVE_CHANNEL, frequency_hz=DRIVE_FREQUENCY_HZ,
                                               duty_cycle_percent=DRIVE_DUTY_PERCENT)
        pwm_on["t"] = float(activation) if isinstance(activation, (int, float)) else clock.monotonic()
        return rig.read_waveform_until_stable(
            waveform_range_v=app.WAVEFORM_INPUT_RANGE_V, settings=settings,
            pwm_started_monotonic=pwm_on["t"], sample_rate_hz=app.DEFAULT_SAMPLE_RATE_HZ,
            expected_frequency_hz=DRIVE_FREQUENCY_HZ, stability_timeout_s=STABILITY_TIMEOUT_S,
            measurement_cycles=MEASUREMENT_CYCLES, progress=None if quiet else _progress_printer(cfg.label),
            channel=cfg.channel,
        )

    def on_retry(attempt: int, exc: Exception) -> None:
        retries.append(f"capture restart {attempt}: {exc}")
        say(f"     stream problem, restarting the capture ({attempt}/{app.REFERENCE_READING_STREAM_RETRIES}): {exc}")

    try:
        waveform, sync, rate, analysis = app.call_with_stream_retries(driven, on_retry=on_retry)
        if cfg.hold_s > 0:
            say(f"  C. drive hold {cfg.hold_s:g} s (emitter stays on)")

            def hold_capture(attempt: int):
                nonlocal hold_offset_s
                hold_offset_s = clock.monotonic() - pwm_on["t"]
                return rig.read_waveform_frame(
                    cycles=int(round(cfg.hold_s * DRIVE_FREQUENCY_HZ)), waveform_range_v=app.WAVEFORM_INPUT_RANGE_V,
                    sample_rate_hz=app.DEFAULT_SAMPLE_RATE_HZ, expected_frequency_hz=DRIVE_FREQUENCY_HZ, channel=cfg.channel,
                )

            hold_waveform, hold_sync, hold_rate = app.call_with_stream_retries(
                hold_capture, on_retry=lambda a, e: retries.append(f"hold restart {a}: {e}"))
    finally:
        deactivation = rig.disable_emitter_pwm(DRIVE_CHANNEL)
        pwm_off_t = float(deactivation) if isinstance(deactivation, (int, float)) else clock.monotonic()

    report = analysis.report
    capture_t, capture_pp = cycle_peak_to_peak_series(waveform, sync, rate)
    capture_t = capture_t + float(report.pwm_elapsed_offset_s)
    drive: dict = {
        "stabilized": bool(report.stabilized),
        "stabilization_cycle": report.stabilization_cycle,
        "stabilization_elapsed_s": report.stabilization_elapsed_s,
        "measurement_complete": bool(report.measurement_complete),
        "unstable": bool(report.unstable),
        "unstable_reason": report.unstable_reason,
        "measurement_attempt": int(report.measurement_attempt),
        "total_pwm_on_s": float(report.total_pwm_on_seconds),
        "capture_cycles": int(report.capture_cycles),
        "pwm_elapsed_offset_s": float(report.pwm_elapsed_offset_s),
        "threshold_mv": float(report.configured_threshold_mv),
        "confirmation_count": int(report.configured_confirmation_count),
        "sensitivity_mv": None, "cycle_pp_mv": [], "cycle_cv_percent": None,
        "signal_to_noise_ratio": None, "signal_to_noise_db": None, "polarity": None,
        "polarity_confidence": None, "measured_frequency_hz": None,
        "production_passed": None, "fail_reasons": [], "warnings": [],
        "cycle_t_s": [float(x) for x in capture_t], "cycle_pp_series_mv": [float(x) for x in capture_pp],
        "cycle_robust_peak_mv": [float(c.robust_peak_v * 1000.0) for c in analysis.cycles],
        "stream_note": getattr(rig, "last_stream_tolerance_note", None),
        "retries": retries,
    }
    # ---- D. post-capture offset (the production verdict read) ------------
    say("  D. post-capture offset re-read" + (" (no extra cooling wait)" if cfg.precision else " (production rule live, then until quiet)"))
    post_t: list[float] = []
    post_v: list[float] = []
    post_started = clock.monotonic()
    start_v = float(read_offset())
    post_t.append(0.0)
    post_v.append(start_v)
    settle = app.wait_for_settled_offset(
        read_offset, start_v=start_v, monotonic=clock.monotonic, sleep=clock.sleep,
        on_read=lambda value, elapsed: (post_t.append(float(elapsed)), post_v.append(float(value))),
    )
    offset_v = float(settle.final_v)
    if not cfg.precision:
        post_t, post_v = poll_offset_until_quiet(read_offset, clock, max_wait_s=cfg.offset_max_s, tolerance_mv=cfg.tolerance_mv,
                                                 seed=(post_t, post_v), status=status)
    post = offset_settle_metrics(post_t, post_v, tolerance_mv=cfg.tolerance_mv)
    post["production_live"] = {**settle.csv_fields(), "final_v": offset_v, "in_band": bool(settle.in_band),
                               "timed_out": bool(settle.timed_out)}

    if report.measurement_complete:
        metrics = app.analyze_v6_stable_measurement(waveform, sync, rate, analysis, offset_v=offset_v,
                                                    input_range_v=app.WAVEFORM_INPUT_RANGE_V)
        final = app.apply_signal_quality_gate(app.evaluate_result(offset_v, metrics, app.DEFAULT_FILTER_SETUP), metrics)
        drive.update({
            "sensitivity_mv": float(metrics.sensitivity_mv),
            "cycle_pp_mv": [float(x) for x in metrics.cycle_pp_mv],
            "cycle_cv_percent": cv_percent(metrics.cycle_pp_mv),
            "signal_to_noise_ratio": None if metrics.signal_to_noise_ratio is None else float(metrics.signal_to_noise_ratio),
            "signal_to_noise_db": None if metrics.signal_to_noise_db is None else float(metrics.signal_to_noise_db),
            "polarity": metrics.polarity,
            "polarity_confidence": None if metrics.polarity_confidence is None else float(metrics.polarity_confidence),
            "measured_frequency_hz": None if metrics.measured_frequency_hz is None else float(metrics.measured_frequency_hz),
            "production_passed": bool(final.passed),
            "fail_reasons": list(final.fail_reasons),
            "warnings": list(final.warnings) + list(metrics.warnings),
        })
        say(f"     -> sensitivity {metrics.sensitivity_mv:.3f} mV (cycle CV {drive['cycle_cv_percent']:.1f} %), "
            f"stabilized at {report.stabilization_elapsed_s:.1f} s, SNR {_fmt(metrics.signal_to_noise_ratio, 1)}, "
            f"polarity {metrics.polarity}, production {'PASS' if final.passed else 'FAIL: ' + '; '.join(final.fail_reasons)}")
    else:
        drive["production_passed"] = False
        drive["fail_reasons"] = [f"response did not stabilize: {report.unstable_reason or 'deadline'}"]
        say(f"     -> did NOT stabilize ({report.unstable_reason or 'deadline'})")

    hold_t, hold_pp = cycle_peak_to_peak_series(hold_waveform, hold_sync, hold_rate) if hold_waveform.size else (np.zeros(0), np.zeros(0))
    hold = hold_trend(hold_t, hold_pp)
    hold.update({"hold_s": float(cfg.hold_s), "hold_pwm_elapsed_offset_s": hold_offset_s,
                 "cycle_t_s": [float(x) for x in hold_t], "cycle_pp_series_mv": [float(x) for x in hold_pp]})
    if hold["hold_mean_mv"] is not None:
        say(f"     hold: mean {hold['hold_mean_mv']:.3f} mV, CV {hold['hold_cv_percent']:.1f} %, drift {hold['hold_drift_percent_per_min']:+.2f} %/min")
    recovery_text = "" if cfg.precision else f"recovered {'at %.0f s' % post['settle_s'] if post['settled'] else 'NO'}, "
    say(f"     post offset: final {post['final_v']:.4f} V (shift {(post['final_v'] - pre['final_v']) * 1000:+.1f} mV), "
        f"{recovery_text}production read {offset_v:.4f} V after {settle.elapsed_s:.0f} s")

    record = {
        "tool": TOOL_NAME, "tool_version": TOOL_VERSION, "schema": RUN_SCHEMA,
        "label": cfg.label, "run": run_index, "recorded_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "port": getattr(rig, "port_name", None), "firmware": identity, "channel": cfg.channel,
        "drive_setup": {"frequency_hz": DRIVE_FREQUENCY_HZ, "duty_percent": DRIVE_DUTY_PERCENT, "pin": DRIVE_CHANNEL,
                        "threshold_mv": settings.peak_delta_threshold_mv, "confirmation_count": settings.consecutive_deltas_required,
                        "measurement_cycles": MEASUREMENT_CYCLES, "stability_timeout_s": STABILITY_TIMEOUT_S,
                        "filter_setup": app.DEFAULT_FILTER_SETUP},
        "timing": {"seated_to_pwm_on_s": pwm_on.get("t", seated_at) - seated_at, "pwm_on_s": pwm_off_t - pwm_on.get("t", pwm_off_t),
                   "post_started_after_pwm_off_s": post_started - pwm_off_t},
        "offset_pre": {**pre, "t_s": [float(x) for x in pre_t], "v": [float(x) for x in pre_v]},
        "drive": drive,
        "hold": hold,
        "offset_post": {**post, "t_s": [float(x) for x in post_t], "v": [float(x) for x in post_v]},
        "notes": notes,
        "_arrays": {"waveform_v": np.asarray(waveform, dtype=float), "sync": np.asarray(sync, dtype=np.uint8),
                    "sample_rate_hz": float(rate), "hold_waveform_v": np.asarray(hold_waveform, dtype=float),
                    "hold_sync": np.asarray(hold_sync, dtype=np.uint8), "hold_sample_rate_hz": float(hold_rate)},
    }
    if cfg.precision:
        record["timing"]["read_start_to_pwm_on_s"] = record["timing"]["seated_to_pwm_on_s"]
        if run_index > 1:
            record["timing"]["seated_to_pwm_on_s"] = None
        reference = reference_replay(waveform, sync, rate, float(report.pwm_elapsed_offset_s))
        late = drive["sensitivity_mv"]
        early = reference["reading_mv"]
        record.update({
            "protocol": PRECISION_PROTOCOL,
            "reference": reference,
            "precision": {
                "off_intervals_s": list(cfg.off_intervals_s),
                "condition": "initial" if interval_s is None else f"off_{interval_s:g}s",
                "requested_off_s": interval_s,
                "offset_max_s": cfg.offset_max_s,
                "offset_tolerance_mv": cfg.tolerance_mv,
                "actual_off_s": pwm_on["t"] - previous_pwm_off_s if previous_pwm_off_s is not None else None,
                "early_vs_late_percent": 100.0 * (early - late) / late if early is not None and late else None,
            },
            "_pwm_off_monotonic": pwm_off_t,
        })
        say(f"     REFERENCE replay: {_fmt(early, 4)} mV, ready {_fmt(reference['ready_s'], 2)} s; "
            f"early vs later 20-cycle result {_fmt(record['precision']['early_vs_late_percent'], 2)} %")
    return record


def save_run(record: dict, out_root: Path) -> tuple[Path, Path]:
    folder = out_root / record["label"]
    folder.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    stem = f"{record['label']}_run{record['run']}_{stamp}"
    arrays = record.pop("_arrays")
    record.pop("_pwm_off_monotonic", None)
    npz_path = folder / f"{stem}.npz"
    np.savez_compressed(
        npz_path, waveform_v=arrays["waveform_v"], sync=arrays["sync"], sample_rate_hz=arrays["sample_rate_hz"],
        hold_waveform_v=arrays["hold_waveform_v"], hold_sync=arrays["hold_sync"], hold_sample_rate_hz=arrays["hold_sample_rate_hz"],
        offset_pre_t_s=np.asarray(record["offset_pre"]["t_s"]), offset_pre_v=np.asarray(record["offset_pre"]["v"]),
        offset_post_t_s=np.asarray(record["offset_post"]["t_s"]), offset_post_v=np.asarray(record["offset_post"]["v"]),
        label=record["label"], run=record["run"], recorded_at=record["recorded_at"], channel=record["channel"],
        drive_frequency_hz=DRIVE_FREQUENCY_HZ, drive_duty_percent=DRIVE_DUTY_PERCENT,
    )
    record["files"] = {"npz": npz_path.name}
    json_path = folder / f"{stem}.json"
    json_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return json_path, npz_path


def capture(args: argparse.Namespace, *, rig_factory=None, clock=None, prompt=input) -> list[Path]:
    clock = (VirtualClock() if args.simulate else RealClock()) if clock is None else clock
    root = guard_output_root(args.out)
    cfg = CaptureConfig(label=args.label, runs=args.runs or (DEFAULT_PRECISION_RUNS if args.precision else DEFAULT_RUNS),
                        channel=args.channel, offset_max_s=args.offset_max_s,
                        hold_s=args.hold_s, tolerance_mv=args.tolerance_mv, auto=args.auto or args.simulate,
                        out_root=root / "precision" if args.precision else root,
                        precision=args.precision, off_intervals_s=args.off_intervals_s)
    if (not cfg.label or cfg.label in {".", ".."} or any(c in cfg.label for c in '/\\:<>"|?*')
            or cfg.label.endswith((".", " "))):
        raise SystemExit("--label must be a single folder-safe candidate name")
    if cfg.precision and cfg.channel != "sensor":
        raise SystemExit("--precision uses the swappable AIN0 DUT socket; omit --channel ref")
    if args.simulate:
        rig = SimulatedRig(profile_for_label(args.label, args.sim_profile), clock)
    else:
        rig = (rig_factory or app.EmitterEsp32Rig)(args.port)
    written: list[Path] = []
    session_id = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    try:
        rig.connect()
        rig.disable_emitter_pwm(DRIVE_CHANNEL)
        print(f"connected: {getattr(getattr(rig, 'identity', None), 'text', 'simulated')} on {getattr(rig, 'port_name', '-')}")
        print(f"candidate {cfg.label}: {cfg.runs} run(s), channel {cfg.channel}, output {cfg.out_root / cfg.label}")
        if cfg.precision:
            print(f"PRECISION: leave seated; 20 production cycles plus early {app.REFERENCE_MEASUREMENT_CYCLES}-cycle reference replay. "
                  f"Alternating off intervals {cfg.off_intervals_s} s; hold {cfg.hold_s:g} s. Ctrl+C stops with completed readings saved.")
        previous_pwm_off_s = None
        for run_index in range(1, cfg.runs + 1):
            if args.simulate:
                if not cfg.precision or run_index == 1:
                    rig.reseat()
                else:
                    rig.run_gain = 1.0 + rig.rng.normal(0.0, rig.profile["run_jitter_pct"] / 100.0)
            record = run_candidate(rig, cfg, run_index, clock=clock, prompt=prompt,
                                   previous_pwm_off_s=previous_pwm_off_s)
            previous_pwm_off_s = record.get("_pwm_off_monotonic")
            record["simulated"] = bool(args.simulate)
            if cfg.precision:
                record["session_id"] = session_id
                record["planned_runs"] = cfg.runs
            json_path, npz_path = save_run(record, cfg.out_root)
            written.append(json_path)
            print(f"  saved {json_path.name} + {npz_path.name}")
    finally:
        try:
            rig.close()
        except Exception:
            pass
    return written


# ----------------------------------------------------------------------
# compare
# ----------------------------------------------------------------------
def load_runs(root: Path, labels: list[str] | None = None, *, precision: bool = False) -> dict[str, list[dict]]:
    runs: dict[str, list[dict]] = {}
    for path in sorted(root.rglob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if record.get("tool") != TOOL_NAME:
            continue
        if record.get("protocol", "screening") != (PRECISION_PROTOCOL if precision else "screening"):
            continue
        if labels and record.get("label") not in labels:
            continue
        record["_path"] = str(path)
        runs.setdefault(record["label"], []).append(record)
    for label in runs:
        runs[label].sort(key=lambda r: (r["recorded_at"], r["run"]) if precision else (r["run"], r["recorded_at"]))
    return runs


def compare_table(ranked: list[dict]) -> str:
    columns = [
        ("candidate", lambda c: c["label"]),
        ("place", lambda c: "-" if c["place"] is None else str(c["place"])),
        ("score", lambda c: _fmt(c["score"], 1)),
        ("runs", lambda c: str(c["runs"])),
        ("offset V", lambda c: _fmt(c["offset_final_v"], 3)),
        ("settle s", lambda c: _fmt(c["offset_settle_s"], 0)),
        ("worst s", lambda c: _fmt(c["offset_settle_worst_s"], 0)),
        ("band s", lambda c: _fmt(c["time_to_band_s"], 0)),
        ("prod s", lambda c: _fmt(c["production_settle_s"], 0)),
        ("recov s", lambda c: _fmt(c["post_recovery_s"], 0)),
        ("shift mV", lambda c: _fmt(c["offset_shift_mv"], 1)),
        ("stab s", lambda c: _fmt(c["stabilization_s"], 1)),
        ("sens mV", lambda c: _fmt(c["sensitivity_mean_mv"], 3)),
        ("run CV %", lambda c: _fmt(c["sensitivity_run_cv_percent"], 2)),
        ("cyc CV %", lambda c: _fmt(c["cycle_cv_percent"], 1)),
        ("drift %/min", lambda c: _fmt(c["hold_drift_abs_percent_per_min"], 2)),
        ("SNR", lambda c: _fmt(c["snr_min"], 1)),
        ("pol", lambda c: c["polarity"] or "-"),
        ("pass", lambda c: f"{c['production_passed_runs']}/{c['runs']}"),
    ]
    rows = [[fn(c) for _h, fn in columns] for c in ranked]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, (h, _fn) in enumerate(columns)]
    lines = ["  ".join(h.rjust(w) for (h, _fn), w in zip(columns, widths)), "  ".join("-" * w for w in widths)]
    lines += ["  ".join(cell.rjust(w) for cell, w in zip(row, widths)) for row in rows]
    return "\n".join(lines)


def precision_table(ranked: list[dict]) -> str:
    columns = [("candidate", "label", None), ("place", "place", 0), ("score", "score", 1),
               ("reads", "runs", 0), ("ref mV", "reference_mean_mv", 4),
               ("ref CV %", "reference_run_cv_percent", 3),
               ("max dev %", "reference_max_deviation_percent", 3),
               ("ready s", "reference_ready_s", 2), ("worst s", "reference_ready_worst_s", 2),
               ("20cy CV %", "sensitivity_run_cv_percent", 3),
               ("pause span %", "reference_condition_span_percent", 3),
               ("early/late %", "early_vs_late_median_percent", 2),
               ("drift %/min", "hold_drift_abs_percent_per_min", 2), ("retries", "stream_retries", 0)]
    rows = [[str(c[k]) if d is None else _fmt(c[k], d) for _, k, d in columns]
            + [f"{c['production_passed_runs']}/{c['runs']}"] for c in ranked]
    headers = [h for h, _, _ in columns] + ["prod pass"]
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]
    return "\n".join("  ".join(value.rjust(w) for value, w in zip(row, widths))
                     for row in [headers, ["-" * w for w in widths], *rows])


def write_comparison_csv(path: Path, ranked: list[dict], *, precision: bool = False) -> None:
    keys = ["label", "place", "score", "runs", "channel", "disqualified", "offset_first_v", "offset_final_v", "offset_final_spread_mv",
            "offset_settle_s", "offset_settle_worst_s", "time_to_band_s", "production_settle_s", "post_recovery_s",
            "post_production_settle_s", "offset_shift_mv", "stabilization_s", "stabilization_worst_s", "sensitivity_mean_mv",
            "sensitivity_run_cv_percent", "cycle_cv_percent", "snr_min", "polarity", "hold_drift_abs_percent_per_min",
            "hold_cv_percent", "production_passed_runs", "warnings"]
    weights = PRECISION_WEIGHTS if precision else SCORE_WEIGHTS
    if precision:
        keys += ["reference_mean_mv", "reference_run_cv_percent", "reference_max_deviation_percent",
                 "reference_ready_s", "reference_ready_worst_s", "reference_complete_runs",
                 "reference_condition_span_percent", "reference_condition_stats", "early_vs_late_median_percent",
                 "early_vs_late_span_percent", "stream_retries"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(keys + [f"rank_{m}" for m in weights])
        for c in ranked:
            row = []
            for key in keys:
                value = c.get(key)
                if isinstance(value, (list, tuple)):
                    value = " | ".join(str(v) for v in value)
                elif isinstance(value, dict):
                    value = json.dumps(value, sort_keys=True)
                elif isinstance(value, float):
                    value = f"{value:.6g}"
                row.append("" if value is None else value)
            row += [c["metric_ranks"].get(m, "") for m in weights]
            writer.writerow(row)
        writer.writerow([])
        writer.writerow(["weights"] + [f"{m}={w:g}" for m, w in weights.items()])


def plot_comparison(path: Path, runs: dict[str, list[dict]], ranked: list[dict], *, show: bool = False) -> Path | None:
    try:
        import matplotlib

        if not show:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None
    order = [c["label"] for c in ranked]
    colors = {label: plt.get_cmap("tab10")(i % 10) for i, label in enumerate(order)}
    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    ax_pre, ax_post, ax_cycles, ax_sens = axes.ravel()
    for axis in (ax_pre, ax_post):
        axis.axhspan(app.OFFSET_MIN_V, app.OFFSET_MAX_V, color="tab:green", alpha=0.07, label="production band")
    for label in order:
        for k, record in enumerate(runs[label]):
            kw = {"color": colors[label], "alpha": 0.9 if k == 0 else 0.45, "lw": 1.2}
            ax_pre.plot(record["offset_pre"]["t_s"], record["offset_pre"]["v"], label=label if k == 0 else None, **kw)
            ax_post.plot(record["offset_post"]["t_s"], record["offset_post"]["v"], label=label if k == 0 else None, **kw)
            d, h = record["drive"], record["hold"]
            t = list(d["cycle_t_s"])
            pp = list(d["cycle_pp_series_mv"])
            if h.get("hold_pwm_elapsed_offset_s") is not None and h["cycle_t_s"]:
                # NaN breaks the line between the capture and the hold (two streams)
                t += [float("nan")] + [h["hold_pwm_elapsed_offset_s"] + x for x in h["cycle_t_s"]]
                pp += [float("nan")] + h["cycle_pp_series_mv"]
            ax_cycles.plot(t, pp, label=label if k == 0 else None, **kw)
            if d["sensitivity_mv"] is not None:
                ax_sens.scatter([order.index(label) + 1], [d["sensitivity_mv"]], color=colors[label], s=30)
    ax_pre.set_title("A. offset after re-seating")
    ax_post.set_title("D. offset after PWM off")
    for axis in (ax_pre, ax_post):
        axis.set_xlabel("s")
        axis.set_ylabel("V")
        axis.legend(fontsize=8)
    ax_cycles.axvline(STABILITY_TIMEOUT_S, color="grey", ls="--", lw=0.8)
    ax_cycles.set_title("B+C. per-cycle pk-pp vs PWM-on time (capture, then hold)")
    ax_cycles.set_xlabel("s from PWM on")
    ax_cycles.set_ylabel("mV")
    ax_cycles.legend(fontsize=8)
    ax_sens.set_xticks(range(1, len(order) + 1))
    ax_sens.set_xticklabels(order, rotation=20)
    ax_sens.set_title("production sensitivity per run")
    ax_sens.set_ylabel("mV")
    fig.suptitle("Reference-unit candidates - " + ", ".join(f"{c['place'] or '-'}:{c['label']}" for c in ranked))
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    if show:
        plt.show()
    plt.close(fig)
    return path


def plot_precision(path: Path, runs: dict[str, list[dict]], ranked: list[dict], *, show: bool = False) -> Path | None:
    try:
        import matplotlib
        if not show:
            matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    fig, axes = plt.subplots(2, 2, figsize=(14, 9))
    panels = [("Five-cycle deviation from candidate mean", "deviation (%)"),
              ("Five-cycle reading complete", "seconds from PWM on"),
              ("Early five cycles vs later production 20 cycles", "difference (%)"),
              ("Drive-hold amplitude drift", "% / min")]
    for candidate in ranked:
        label = candidate["label"]
        records = runs[label]
        mean = candidate["reference_mean_mv"]
        series = [
            [100.0 * (r["reference"]["reading_mv"] / mean - 1.0)
             if mean and r["reference"]["reading_mv"] is not None else float("nan") for r in records],
            [r["reference"]["ready_s"] for r in records],
            [r["precision"]["early_vs_late_percent"] for r in records],
            [r["hold"]["hold_drift_percent_per_min"] for r in records],
        ]
        for ax, values in zip(axes.ravel(), series):
            ax.plot(range(1, len(records) + 1), values, ".-", label=label, lw=1)
    for ax, (title, ylabel) in zip(axes.ravel(), panels):
        ax.set(title=title, ylabel=ylabel, xlabel="reading (chronological order)")
        ax.grid(alpha=0.2)
        ax.legend(fontsize=8)
    simulated = all(r["simulated"] for records in runs.values() for r in records)
    fig.suptitle(("SIMULATED - " if simulated else "") +
                 "AIN0 reference precision retest - first reading after insertion; then repeated off intervals")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    if show:
        plt.show()
    plt.close(fig)
    return path


def validate_precision_protocols(runs: dict[str, list[dict]]) -> None:
    """Do not attribute different protocols, channels or simulation to parts."""
    signatures = set()
    for records in runs.values():
        for r in records:
            signatures.add(json.dumps({
                "drive_setup": r["drive_setup"], "hold_s": r["hold"]["hold_s"],
                "off_intervals_s": r["precision"]["off_intervals_s"], "channel": r["channel"],
                "offset_max_s": r["precision"]["offset_max_s"],
                "offset_tolerance_mv": r["precision"]["offset_tolerance_mv"],
                "simulated": r["simulated"], "firmware": None if r["simulated"] else r["firmware"],
                "reference": {k: r["reference"][k] for k in ("threshold_mv", "confirmation_count", "measurement_cycles")},
            }, sort_keys=True))
    if len(signatures) != 1:
        raise ValueError("Precision runs use different settings, firmware, channels or simulation. "
                         "Capture all candidates with the same options in a separate --out folder.")


def compare(args: argparse.Namespace) -> int:
    root = guard_output_root(args.out)
    if args.precision:
        root = root / "precision"
    runs = load_runs(root, args.labels or None, precision=args.precision)
    if not runs:
        print(f"no {TOOL_NAME} runs found under {root}" + (f" for {args.labels}" if args.labels else ""))
        return 1
    missing = sorted(set(args.labels or []) - runs.keys())
    if missing:
        raise ValueError(f"No matching runs for requested candidate(s): {', '.join(missing)}")
    if args.precision:
        validate_precision_protocols(runs)
    aggregate = aggregate_precision_candidate if args.precision else aggregate_candidate
    candidates = [aggregate(label, records) for label, records in sorted(runs.items())]
    weights = PRECISION_WEIGHTS if args.precision else SCORE_WEIGHTS
    ranked = rank_candidates(candidates, weights, precision=args.precision)
    print(f"\n=== reference-unit candidates under {root} ===")
    if args.precision and all(r["simulated"] for records in runs.values() for r in records):
        print("SIMULATED DATA - software demonstration only; not measurements of physical candidates")
    print(precision_table(ranked) if args.precision else compare_table(ranked))
    print("\nweights (rank x weight, lower total wins): " + ", ".join(f"{METRIC_LABELS[m]} x{w:g}" for m, w in weights.items()))
    if args.precision:
        print("Precision ranks use 0.01 percentage-point / 0.1 s bins. Max deviation is from each candidate's own mean.")
        print("Ready = PWM-on through the fifth fresh reference cycle (excludes transport/UI overhead). "
              "Early/late is a timing-dependent difference, not an accuracy error.")
        print("AIN0 screening only: repeat on another day; verify the winner after mounting on AIN1.")
    for c in ranked:
        if c["disqualified"]:
            print(f"DISQUALIFIED {c['label']}: " + " / ".join(c["disqualified"]))
        elif c["never_settled_runs"]:
            print(f"note {c['label']}: offset still moving at the maximum wait in run(s) {c['never_settled_runs']} (counted as {OFFSET_MAX_WAIT_S:g} s)")
        if c["warnings"]:
            if args.precision:
                # Keep precision/retry caveats visible despite long legacy-factor warnings.
                for warning in c["warnings"]:
                    if not warning.startswith(app.SENSITIVITY_NEAR_LIMIT_WARNING_PREFIX):
                        print(f"warning {c['label']}: {warning}")
                if any(w.startswith(app.SENSITIVITY_NEAR_LIMIT_WARNING_PREFIX) for w in c["warnings"]):
                    print(f"note {c['label']}: production sensitivity near-limit warnings; full text in JSON/CSV")
            else:
                print(f"warnings {c['label']}: " + " | ".join(c["warnings"])[:300])
        if args.precision:
            print(f"conditions {c['label']}: " + "; ".join(
                f"{name} n={v['n']} mean={v['mean_mv']:.4f} mV CV={_fmt(v['cv_percent'], 3)}%"
                for name, v in c["reference_condition_stats"].items()))
    print("\n" + recommendation_text(ranked))
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = root / f"comparison_{stamp}.csv"
    write_comparison_csv(csv_path, ranked, precision=args.precision)
    print(f"table written to {csv_path}")
    if not args.no_plot:
        plot = plot_precision if args.precision else plot_comparison
        png = plot(root / f"comparison_{stamp}.png", runs, ranked, show=args.show)
        print(f"plot written to {png}" if png else "matplotlib not available - no plot")
    return 0


# ----------------------------------------------------------------------
# simulation (no hardware): candidate personalities for --simulate and the tests
# ----------------------------------------------------------------------
SIM_PROFILES: dict[str, dict] = {
    # offset: start -> final with time constant tau (s); emitter heating adds
    # `heat_shift_v` that decays with `tau_post`. Response: pk-pk amplitude
    # `amp_mv` reached with time constant `amp_tau_s` from PWM on; drift over
    # a hold in %/min; run-to-run amplitude jitter in %; `wobble_pct` at
    # `wobble_hz` is a slow amplitude oscillation (a part that never holds
    # 10 consecutive quiet cycle deltas).
    "fast":  {"offset_start_v": 0.95, "offset_final_v": 0.66, "tau_s": 3.0, "heat_shift_v": -0.012, "tau_post_s": 2.0,
              "amp_mv": 3.6, "amp_tau_s": 2.0, "noise_rms_mv": 0.08, "drift_pct_per_min": 0.3, "run_jitter_pct": 0.8},
    # "slow" settles its offset in ~25 s and recovers slowly after the
    # emitter, but is otherwise a working part: eligible, ranked below "fast".
    "slow":  {"offset_start_v": 1.40, "offset_final_v": 0.70, "tau_s": 6.0, "heat_shift_v": -0.025, "tau_post_s": 9.0,
              "amp_mv": 3.9, "amp_tau_s": 3.5, "noise_rms_mv": 0.08, "drift_pct_per_min": 1.5, "run_jitter_pct": 2.5},
    "jittery": {"offset_start_v": 0.90, "offset_final_v": 0.64, "tau_s": 4.0, "heat_shift_v": -0.015, "tau_post_s": 2.5,
                "amp_mv": 3.2, "amp_tau_s": 2.5, "noise_rms_mv": 0.11, "drift_pct_per_min": 0.5, "run_jitter_pct": 6.0},
    "high_offset": {"offset_start_v": 2.2, "offset_final_v": 1.45, "tau_s": 8.0, "heat_shift_v": -0.03, "tau_post_s": 4.0,
                    "amp_mv": 3.5, "amp_tau_s": 2.5, "noise_rms_mv": 0.08, "drift_pct_per_min": 0.4, "run_jitter_pct": 1.0},
    "never_stable": {"offset_start_v": 0.9, "offset_final_v": 0.68, "tau_s": 3.0, "heat_shift_v": -0.01, "tau_post_s": 2.0,
                     "amp_mv": 3.5, "amp_tau_s": 2.5, "noise_rms_mv": 0.08, "drift_pct_per_min": 2.0, "run_jitter_pct": 1.0,
                     "wobble_pct": 12.0, "wobble_hz": 0.7},
}


def profile_for_label(label: str, explicit: str | None = None) -> dict:
    if explicit:
        return {"name": explicit, **SIM_PROFILES[explicit]}
    names = list(SIM_PROFILES)
    name = names[sum(label.encode()) % len(names)]
    return {"name": name, **SIM_PROFILES[name]}


class SimulatedRig:
    """Stand-in for EmitterEsp32Rig with one candidate personality on a virtual clock."""

    port_name = "SIM"

    def __init__(self, profile: dict, clock: VirtualClock, seed: int = 7) -> None:
        self.profile = profile
        self.clock = clock
        self.rng = np.random.default_rng(seed)
        self.identity = type("Identity", (), {"text": f"SIMULATED-{profile['name']}"})()
        self.is_streaming = False
        self.last_stream_tolerance_note = None
        self.seated_at = clock.monotonic()
        self.pwm_on_at: float | None = None
        self.pwm_off_at: float | None = None
        self.heat = 0.0
        self.run_gain = 1.0

    def connect(self) -> None:
        pass

    def close(self, disable_pwm: bool = True) -> None:
        pass

    def ensure_qualified_front_end(self) -> bool:
        return False

    def reseat(self) -> None:
        self.seated_at = self.clock.monotonic()
        self.pwm_on_at = None
        self.pwm_off_at = None
        self.heat = 0.0
        self.run_gain = 1.0 + self.rng.normal(0.0, self.profile["run_jitter_pct"] / 100.0)

    def _offset_at(self, t: float) -> float:
        p = self.profile
        base = p["offset_final_v"] + (p["offset_start_v"] - p["offset_final_v"]) * math.exp(-(t - self.seated_at) / p["tau_s"])
        heat = 0.0
        if self.pwm_on_at is not None:
            on_until = self.pwm_off_at if self.pwm_off_at is not None else t
            heat = p["heat_shift_v"] * (1.0 - math.exp(-(on_until - self.pwm_on_at) / 6.0))
            if self.pwm_off_at is not None:
                heat *= math.exp(-(t - self.pwm_off_at) / p["tau_post_s"])
        return base + heat

    def read_offset_voltage(self, waveform_range_v=None, samples=None, delay_s=None) -> float:
        if self.is_streaming:
            raise StreamStateError("streaming")
        return self._offset_at(self.clock.monotonic()) + self.rng.normal(0.0, 0.0006)

    read_reference_voltage = read_offset_voltage

    def configure_emitter_pwm(self, channel=DRIVE_CHANNEL, frequency_hz=DRIVE_FREQUENCY_HZ, duty_cycle_percent=DRIVE_DUTY_PERCENT) -> float:
        self.pwm_on_at = self.clock.monotonic()
        self.pwm_off_at = None
        return self.pwm_on_at

    def disable_emitter_pwm(self, channel=DRIVE_CHANNEL) -> float:
        self.pwm_off_at = self.clock.monotonic()
        return self.pwm_off_at

    def _synthesize(self, seconds: float, rate: float) -> tuple[np.ndarray, np.ndarray]:
        p = self.profile
        n = int(round(seconds * rate)) + 1
        t0 = self.clock.monotonic() - (self.pwm_on_at or self.clock.monotonic())
        t = t0 + np.arange(n) / rate
        amp = p["amp_mv"] / 1000.0 * self.run_gain * (0.35 + 0.65 * (1.0 - np.exp(-t / p["amp_tau_s"])))
        amp *= 1.0 + p["drift_pct_per_min"] / 100.0 * t / 60.0
        if p.get("wobble_pct"):
            amp *= 1.0 + p["wobble_pct"] / 100.0 * np.sin(2.0 * np.pi * p.get("wobble_hz", 0.7) * t)
        phase = (t * DRIVE_FREQUENCY_HZ) % 1.0
        # Thermal response to a square drive: exponential rise while ON, fall while OFF.
        shape = np.where(phase < 0.5, 1.0 - np.exp(-phase / 0.12), np.exp(-(phase - 0.5) / 0.12))
        shape = shape - shape.mean()
        waveform = np.array([self._offset_at(self.pwm_on_at + x) for x in t]) + amp * shape / (shape.max() - shape.min())
        waveform += self.rng.normal(0.0, p["noise_rms_mv"] / 1000.0, n)
        sync = (phase < 0.5).astype(float)
        self.clock.advance(seconds)
        return waveform, sync

    def read_waveform_until_stable(self, *, waveform_range_v, settings, pwm_started_monotonic, sample_rate_hz=1000.0,
                                   expected_frequency_hz=10.0, stability_timeout_s=20.0, measurement_cycles=20,
                                   progress=None, preview=None, cancelled=None, channel="sensor"):
        offset_s = max(0.0, self.clock.monotonic() - pwm_started_monotonic)
        max_stream_s = max(0.0, stability_timeout_s - offset_s) + (measurement_cycles + 2) / expected_frequency_hz
        waveform, sync = self._synthesize(max_stream_s, sample_rate_hz)
        kwargs = dict(pwm_elapsed_offset_s=offset_s, stability_deadline_s=stability_timeout_s,
                      measurement_cycles_required=measurement_cycles, enforce_measurement_stability=channel == "sensor",
                      max_measurement_attempts=app.MAX_MEASUREMENT_ATTEMPTS)
        analysis = sa.analyze_stability(waveform, sync, sample_rate_hz, settings, **kwargs)
        if analysis.report.measurement_complete:
            end = min(len(waveform), analysis.measurement_segments[-1][1] + 1)
            waveform, sync = waveform[:end], sync[:end]
            analysis = sa.analyze_stability(waveform, sync, sample_rate_hz, settings, **kwargs)
            # _synthesize generated the full deadline buffer; the real
            # backend stops at the selected measurement window instead.
            self.clock.now -= max_stream_s - (end - 1) / sample_rate_hz
        if progress is not None:
            progress(analysis)
        return waveform, sync, float(sample_rate_hz), analysis

    def read_waveform_frame(self, cycles, waveform_range_v, sample_rate_hz=1000.0, expected_frequency_hz=10.0, channel="sensor"):
        waveform, sync = self._synthesize(cycles / expected_frequency_hz, sample_rate_hz)
        return waveform, sync, float(sample_rate_hz)


# ----------------------------------------------------------------------
def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def finite_nonnegative(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number") from None
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return parsed


def off_intervals(value: str) -> tuple[float, ...]:
    intervals = tuple(finite_nonnegative(v) for v in value.split(","))
    if not intervals or len(set(intervals)) != len(intervals):
        raise argparse.ArgumentTypeError("provide distinct emitter-off intervals, e.g. 2,10,60")
    return intervals


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="measure one candidate (phases A-D), --runs times")
    cap.add_argument("--label", required=True, help="candidate name, e.g. refA or the part's marking")
    cap.add_argument("--precision", action="store_true", help="leave seated on AIN0; rank early reference precision; writes to <out>/precision")
    cap.add_argument("--runs", type=positive_int, default=None, help=f"readings per candidate (default {DEFAULT_RUNS} screening; {DEFAULT_PRECISION_RUNS} with --precision)")
    cap.add_argument("--off-intervals-s", type=off_intervals, default=DEFAULT_OFF_INTERVALS_S,
                     help="--precision: alternating minimum PWM-off intervals after the first reading (default 2,10,60)")
    cap.add_argument("--port", default=None, help="serial port (default: auto-detect the CP210x)")
    cap.add_argument("--channel", choices=("sensor", "ref"), default="sensor", help="DUT socket AIN0 (default) or the AIN1 reference mount")
    cap.add_argument("--offset-max-s", type=finite_nonnegative, default=OFFSET_MAX_WAIT_S, help=f"longest offset poll per phase (default {OFFSET_MAX_WAIT_S:g} s)")
    cap.add_argument("--hold-s", type=finite_nonnegative, default=DEFAULT_HOLD_S, help=f"drive hold after the production capture, 0 to skip (default {DEFAULT_HOLD_S:g} s)")
    cap.add_argument("--tolerance-mv", type=finite_nonnegative, default=OFFSET_SETTLE_TOLERANCE_MV, help=f"'settled' tolerance (default {OFFSET_SETTLE_TOLERANCE_MV:g} mV)")
    cap.add_argument("--auto", action="store_true", help="do not wait for Enter before each run (part already seated)")
    cap.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_ROOT, help=f"output root (default {DEFAULT_OUTPUT_ROOT})")
    cap.add_argument("--simulate", action="store_true", help="no hardware: a simulated candidate on a virtual clock")
    cap.add_argument("--sim-profile", choices=sorted(SIM_PROFILES), default=None, help="simulated personality (default: chosen from the label)")
    cmp_ = sub.add_parser("compare", help="aggregate, rank and plot every candidate under --out")
    cmp_.add_argument("--precision", action="store_true", help="compare only permanent-reference precision retests in <out>/precision")
    cmp_.add_argument("labels", nargs="*", help="candidate labels to include (default: all)")
    cmp_.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_ROOT, help=f"root holding the run folders (default {DEFAULT_OUTPUT_ROOT})")
    cmp_.add_argument("--no-plot", action="store_true")
    cmp_.add_argument("--show", action="store_true", help="open the plot window as well as saving the PNG")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "capture":
        try:
            written = capture(args)
        except KeyboardInterrupt:
            print("\nCapture stopped. Completed readings are saved; interrupted reading was not scored.")
            return 130
        except (Esp32BackendError, app.HardwareNotReadyError) as exc:
            print(f"capture aborted: {exc}", file=sys.stderr)
            return 2
        return 0 if written else 1
    try:
        return compare(args)
    except ValueError as exc:
        print(f"comparison refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
