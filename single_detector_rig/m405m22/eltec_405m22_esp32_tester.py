"""
Eltec 405 M22 ESP32 tester - TP412 evaluation build (based on v6.1).

This build adapts the 406MCA v6.1 ESP32 tester to the Model 405 M22 high-gain
thermally compensated pyroelectric detector, following document TP412. The
405 M22's responsivity is specified at 1 Hz, so the emitter chop is 1 Hz / 50%
(the backend programs PWM,FREQ before every PWM,ON) and every timing constant
is retimed for 1-second PWM cycles. The sensitivity gate is live with the
lot-500 pairwise fixture calibration (2026-08-17: ~50 sensors measured on the
legacy fixture and on this rig in the same order, factor 4.30 - see
docs/CALIBRATION_RECORD.md). A TP412-style
emitter-off NOISE test runs after the offset gate and BEFORE the driven
sensitivity capture, so a noisy part is rejected early.

The hardware backend is the same ESP32 + ADS1256 rig used on Xubuntu and
requires firmware v2.0 (ADS1256 at PGA gain 1 with the input buffer off, so
the full 0.8-3.0 V TP412 offset band reads linearly).

Wiring / power (batteries isolated 2026-08-12 to fix the emitter spike):
    ADS AIN0 = buffered DUT sensor (DC offset + AC waveform), +/-5 V range
    ADS AIN1 = permanently-mounted reference sensor (required emitter-health gate)
    ADS AIN7 = legacy battery divider tap - NOT MONITORED anymore (see below)
    GPIO33   = PWM output to the MOSFET module (D25 until 2026-08-25)
    sync     = the ESP32 PWM state included with every streamed sample
    6.5 V battery -> emitters ONLY (via the MOSFET module)
    9 V battery   -> sensors (DUT + AIN1 reference buffer supplies)

Model 405 M22 notes (Data-Sheet-Model-405 + TP412):
    - JFET source-follower with gain (~5x with a 100k source resistor); the
      TP412 offset band is 0.8-3.0 V and is gated in full (firmware v2.0's
      gain-1/unbuffered front end reads to ~3.5 V linearly).
    - Thermal breakpoint is ~0.2 Hz and responsivity is specified at 1 Hz,
      hence the 1 Hz chop. Cycles last a full second: reference readings and
      DUT captures take roughly ten times longer than on the 406MCA build.
    - TP412 noise: emitter off, an adaptive 3-20 s quiet wait, then a 20 s
      capture (60 s soak on request). This build cuts the capture into 1 s
      windows and fails the part when more than 15% of the windows exceed
      the pk-pk limit (deliberately looser than TP412's zero-tolerance
      largest-excursion rule; the sensors are extremely sensitive and a few
      excursions are acceptable). TP412's 300 mV was read behind the legacy
      bench amplifier whose EFFECTIVE gain measured ~700x (not the nominal
      x4000), so the pin-level limit is ~429 uV pk-pk, gated on a
      band-limited (anti-alias FIR, 1000 -> 50 SPS) trace - see the NOISE_*
      constants and docs/CALIBRATION_RECORD.md. There is no settle
      detection: with the emitter off there is no signal to stabilize, only
      noise.
    - The AIN1 reference sensor baseline must be calibrated by THIS build at
      1 Hz; 406MCA baselines (10 Hz chop) are deliberately not read, and any
      baseline recorded before firmware v2.0 is rejected by schema version.

Guided flow:
    1. Calibrate AIN1 with a known-good emitter, then enter the batch details
       (including the TP412 filter setup: -625, -628, or -629).
    2. Place the sensor in the rig and press Enter.
    3. The app checks the AIN1 reference against its calibration before it
       reads AIN0, then runs the per-part steps in TP412 order with a step
       progress bar: (a) DUT DC offset, PWM off - an out-of-band offset fails
       the part immediately; (b) emitter-off noise - adaptive 3-20 s quiet
       wait plus the 20 s windowed capture (60 s soak selectable), shown
       band-limited as range-around-mean with red ±214 µV cutoff lines
       (the ~429 µV pk-pk pin-level limit), and a noisy part fails without
       a sensitivity capture;
       (c) sensitivity - 1 Hz emitter drive, peak stability, then the
       10-cycle chopped-response measurement.

Run:
    python3 eltec_405m22_esp32_tester.py
"""

from __future__ import annotations

import csv
import json
import math
import struct
import threading
import time
import tkinter as tk
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import numpy as np

# Reuse the proven signal analysis and pass/fail engine from the v1 tester so
# there is still a single source of truth for the production math. Hardware I/O
# is provided locally by esp32_backend.py.
import sys

_RIG_PACKAGE_DIR = Path(__file__).resolve().parents[1]
if str(_RIG_PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(_RIG_PACKAGE_DIR))
import attempt_history  # noqa: E402 - v2.0 per-batch attempt / skip log

_V1_TESTER_DIR = Path(__file__).resolve().parents[1] / "v1_single_sensor"
if str(_V1_TESTER_DIR) not in sys.path:
    sys.path.insert(0, str(_V1_TESTER_DIR))

import eltec_406mca_tester as _shared_engine
from eltec_406mca_tester import (
    DEFAULT_SAMPLE_RATE_HZ,
    FILTER_SPECS_MV,
    POSITIVE_POLARITY,
    PROCEDURE_SYNC_EDGE,
    SIM_CASES,
    FinalResult,
    WaveformMetrics,
    analyze_waveform,
    evaluate_result as evaluate_result_with_sensitivity_limit,
    find_sync_edges,
    format_polarity_detail,
    simulate_offset_v,
)

# ---- Model 405 M22 overrides of the shared 406MCA engine ------------------- #
# The shared v1 engine carries 406MCA constants. The 405 M22 is chopped at
# 1 Hz (its responsivity spec frequency) and gates the full TP412 offset band
# of 0.8-3.0 V (firmware v2.0 runs the ADS1256 at gain 1 with the input buffer
# off, so the band reads linearly). The engine's gate functions read these as
# module globals at call time, so patching the module keeps every shared gate
# consistent.
MODEL_NAME = "405M22"
EXPECTED_FREQUENCY_HZ = 1.0
OFFSET_MIN_V = 0.8
OFFSET_MAX_V = 3.0
_shared_engine.MODEL_NAME = MODEL_NAME
_shared_engine.EXPECTED_FREQUENCY_HZ = EXPECTED_FREQUENCY_HZ
_shared_engine.OFFSET_MIN_V = OFFSET_MIN_V
_shared_engine.OFFSET_MAX_V = OFFSET_MAX_V

# TP412 sensitivity setups for the 405 M22 (legacy scope mV, measured with the
# blackened tube + extra -25B filter at 10 cm from the 500 K blackbody).
# FILTER_SPECS_MV is the SAME dict object inside the shared engine, this
# module, the setup-step combobox, and the simulator - mutate it IN PLACE so
# every reader stays synchronized. It keeps the engine's historical shape
# (filter -> legacy minimum); the full TP412 min-max ranges live in
# FILTER_RANGES_MV for the (future) over-max gate and the operator hint.
FILTER_SPECS_MV.clear()
FILTER_SPECS_MV.update(
    {
        "-625 filter": 5.99,
        "-628 filter": 4.22,
        "-629 filter": 4.92,
    }
)
FILTER_RANGES_MV = {
    "-625 filter": (5.99, 11.98),
    "-628 filter": (4.22, 8.44),
    "-629 filter": (4.92, 9.84),
}


def simulate_offset_v(case_name: str) -> float:  # noqa: F811 - 405 M22 override
    """Simulator offsets matched to the 405 M22's 0.8-3.0 V TP412 band."""
    import random as _random

    if case_name == "Low offset":
        return 0.55
    if case_name == "High offset":
        return 5.0    # railed at the ADS full scale - the real high-offset signature
    return _random.uniform(1.0, 1.8)


_shared_engine.simulate_offset_v = simulate_offset_v

from esp32_backend import (
    BATTERY_DIVIDER_FILTER_CAPACITANCE_F,
    BATTERY_DIVIDER_RATIO,
    BATTERY_DIVIDER_R_BOTTOM_OHMS,
    BATTERY_DIVIDER_R_TOP_OHMS,
    EXPECTED_FIRMWARE_PREFIX,
    Esp32BackendError,
    Esp32EmitterRig,
    StreamSample,
    _opt_out_of_windows_power_throttling,
    probe_esp32_status,
)

# Windows 11 on battery demotes a backgrounded GUI process to EcoQoS and
# coarsens its timers to ~15.6 ms, which overflows the CP210x driver's real
# 512-byte receive queue mid-capture (2026-08-17 root cause). Opt the whole
# process out as early as possible - before the first reference calibration
# capture, not merely at rig connect.
_opt_out_of_windows_power_throttling()
from stability_analysis import (
    CycleAnalysis,
    DEFAULT_MAX_MEASUREMENT_ATTEMPTS,
    DEFAULT_SETTINGS_PATH,
    NoiseAnalysis,
    StabilityAnalysis,
    StabilitySettings,
    StabilitySettingsError,
    SyncValidationError,
    analyze_noise_capture_band_limited,
    analyze_stability,
    antialias_edge_context_samples,
    load_stability_settings,
    rising_edge_indices,
    validate_rising_sync_cycles,
)

# This rig reads the sensor through a unity-gain buffer (no AM502), so the
# external gain is always 1.0 and the offset rides on the waveform channel.
RIG_GAIN = 1.0

# TP412 lists -625 first; the operator picks the actual filter setup per batch
# on the setup step.
DEFAULT_FILTER_SETUP = "-625 filter"

# Standard production failure taxonomy. This is the same set used by the
# scope-verification workflow, minus its non-failure GOOD entry.
FAILURE_MODE_CHOICES = (
    "SB - Sensor bad",
    "GO/D - Good offset/no signal",
    "O - No sensitivity",
    "LS - Low sensitivity",
    "N - Noisy",
    "FN - Fast noise",
    "OSC - Oscillation",
    "HO - High offset",
    "LO - Low offset",
    "D - No offset",
    "TO - Technician says offset bad",
    "TS - Technician says sensitivity bad",
    "HRV - High ref volt",
    "LRV - Low ref volt",
    "RP - Reversed polarity",
    "Unstable - Unstable",
    "SI - Wrong pattern: sinewave",
    "SW - Wrong pattern: sawtooth",
    "SQ - Wrong pattern: square",
    "RSQ - Wrong pattern: rounded square",
    "T - Wrong pattern: triangle",
    "HIG - High IGSS",
    "Drop - Dropped",
)
UNSTABLE_FAILURE_MODE = "Unstable - Unstable"

# A sensor can also leave the fixture without any verdict: the rig or the
# serial stream failed and nothing was recorded, so there is no offset,
# sensitivity or polarity to judge. Those sensors are written as NOT MEASURED
# rows - they keep the batch CSV complete (no silent hole in the sensor
# numbering) while staying out of the pass/fail statistics and the yield.
# They are NOT part of the production failure taxonomy above, so they never
# appear in the failure-mode picker on a measured sensor.
NOT_MEASURED_TAG = "NM"
NOT_MEASURED_REASON_PREFIX = "Not measured:"
NOT_MEASURED_REASON_CHOICES = (
    "NM - Not measured: ESP32 stream/rig fault",
    "NM - Not measured: sensor could not be loaded",
    "NM - Not measured: skipped by technician",
)
DEFAULT_NOT_MEASURED_REASON = NOT_MEASURED_REASON_CHOICES[0]

OUTCOME_PASS = "PASS"
OUTCOME_FAIL = "FAIL"
OUTCOME_NOT_MEASURED = "NOT MEASURED"

# Fixed ESP32 rig settings. Technicians never change these in production.
# 405 M22: 1 Hz chop (responsivity spec frequency) for the DUT; the backend
# programs the board with PWM,FREQ on every activation (firmware v2.0
# required overall). The permanently mounted AIN1 reference unit is a 406MCA
# sensor, so every reference phase (calibration and the per-test gate) drives
# the emitter at that model's qualified 10 Hz instead — its baseline is then
# directly comparable to the sensor's historical 10 Hz characterization, and
# the ~10x shorter reference captures also reduce serial-stream exposure.
EMITTER_PWM_CHANNEL = "GPIO33"  # D25 until 2026-08-25; the backend sends PIN,33
EMITTER_PWM_FREQUENCY_HZ = 1.0
REFERENCE_PWM_FREQUENCY_HZ = 10.0
EMITTER_PWM_DUTY_CYCLE = 50.0
WAVEFORM_INPUT_RANGE_V = 5.0  # ADS1256 PGA x1 (firmware v2.0), buffer off => +/-5 V

# Battery monitoring is DISABLED on this fixture (2026-08-12 rewiring): the
# 6.5 V battery drives the emitters only and the 9 V battery drives the
# sensors. Neither battery is measurable on AIN7 - the legacy ~2:1 divider
# would put a 9 V pack at ~4.5 V on the pin, and firmware v2.0 runs the
# ADS1256 unbuffered, which loads that resistive divider anyway.
# TODO(hardware): measure the sensor battery on AIN6 with a >=4:1 divider
# (e.g. 300k/100k) and re-enable this gate with thresholds for the actual
# supply (the plan is to step the sensor supply down to ~8 V, or use an 8 V
# battery, so the noise limits stay comparable to TP412's +8 V bench supply).
# All battery machinery below is kept intact for that re-enable.
BATTERY_MONITORING_ENABLED = False
BATTERY_MIN_V = 5.8               # hard block: recharge at or below this
BATTERY_WARN_V = 6.0              # early warning band (testing is still allowed)

# Reference gate is DISABLED (2026-08-17): the fixture's buffer/voltage
# follower is a dual op-amp with no channel isolation, so the sensor under
# test couples into the AIN1 reference signal (seen as the reference reading
# collapsing 4.94 -> 0.30 mV with a DUT loaded). A baseline captured with one
# sensor is invalid the moment another is loaded, so no recalibration can
# make the gate trustworthy on this hardware - it would randomly block good
# parts or pass a weak emitter. All reference machinery below is kept intact:
# when the reworked buffer board (per-channel isolated op-amps) is installed,
# set this back to True and run a fresh "Calibrate reference unit".
# Operator mitigation meanwhile: if several sensors in a row fail low
# sensitivity, suspect the emitter before condemning the parts.
REFERENCE_GATE_ENABLED = False
# Reuse the load-step battery check instead of re-reading before the capture
# when the reading is healthy and at most this old (the battery sags slowly).
BATTERY_REUSE_WINDOW_S = 30.0

# Hardware plausibility guards ("is everything actually plugged in?").
# A floating ADS1256 input reads arbitrary voltages, which can look like a
# healthy battery or a live sensor. These bands catch readings that cannot
# come from a correctly wired rig, so the app blocks the test and tells the
# technician what to plug in instead of recording bogus numbers.
BATTERY_FAULT_MIN_V = 3.0    # below this: battery missing / AIN7 divider not wired
BATTERY_FAULT_MAX_V = 7.5    # above this: not a plausible battery reading
# A connected 405 M22 presents its DC offset on AIN0 through the buffer; a
# missing/unwired sensor floats near 0 V, so only a near-zero reading is
# treated as "no sensor". There is deliberately NO high-side plausibility
# rail: a bad high-offset part rails AIN0 at the ADC full scale (~5 V), so
# any reading above the TP412 3.0 V limit is a real part that must be
# recorded as an immediate HO failure, never blocked as a wiring error.
SENSOR_OFFSET_MIN_PLAUSIBLE_V = 0.05
# A JUST-inserted 405 M22 can also read below the plausibility floor (or well
# below its settled offset) for several seconds while the pyroelectric
# element's DC level wakes up - seen twice on the 2026-08-17 comparison lot
# (500-44 first read as "no sensor", 500-5 first read 0.6 V then 1.3 V on the
# immediate re-measure). Poll this long before declaring the slot empty.
OFFSET_WAKE_TIMEOUT_S = 5.0
OFFSET_WAKE_POLL_S = 0.5
# Battery level as displayed by the header gauge: full at ~6.4 V.
BATTERY_GAUGE_FULL_V = 6.4
# Simulator battery levels so the low-battery lockout can be exercised without hardware.
SIM_BATTERY_OK_V = 6.2
SIM_BATTERY_LOW_V = 5.6

# Signal-quality gate. A real chopped-emitter capture has a strong coherent
# waveform standing above the cycle-to-cycle noise (high SNR). A capture that is
# mostly sensor noise - e.g. the emitter is not actually being driven - has a
# low signal-to-noise ratio, so we fail it instead of trusting the raw
# amplitude. Tune this up once real good-sensor SNRs are known (the SNR is now
# logged to the batch CSV to help calibrate it).
MIN_SIGNAL_TO_NOISE_RATIO = 1.5   # ~3.5 dB

# TP412 noise test (emitter OFF), run right after the offset gate and BEFORE
# the driven sensitivity capture (2026-08-12 rework: a noisy part is rejected
# early instead of first spending up to three 60 s stabilization attempts).
# There is no signal while the emitter is off - only noise - so the old
# stability-style "wait for settle, restart on a breached delta" rule was
# meaningless here and was removed: the app now simply streams a fixed quiet
# wait (discarded) and then keeps the next NOISE_CAPTURE_SECONDS. The capture
# is cut into 1 s windows and the part fails when more than 20% of them
# exceed the pk-pk limit. TP412 itself allows NO excursion over the limit;
# the 20% allowance is a deliberate engineering relaxation for these very
# sensitive parts.
#
# THE LIMIT (recalibrated 2026-08-13, evening): TP412's 300 mV pk-pk is
# read on a scope BEHIND the legacy bench amplifier (TL084-based, NOMINAL
# x4000 per its paperwork), so the pin-level limit is 300 mV divided by the
# chain's EFFECTIVE gain. That effective gain was measured by
# cross-measuring the SAME part on both fixtures on the same day (see
# noise_experiments/): pin span ~240-270 uV over a 50 s / 20 Sa/s
# scope-equivalent view vs ~150-200 mV displayed on the legacy scope
# (GDS-1054B, 100 mV/div, cursors +/-150 mV, roll 5 s/div, 20 Sa/s) =>
# effective factor ~620-830x, adopted as 700. A true x4000 chain would
# have painted that part at ~1 V (10 divisions) - it demonstrably did not,
# so the sticker figure is not the effective one (10:1 probe, an amp range
# switch, or midband-vs-passband gain are the usual suspects). The
# resulting ~429 uV pin limit is provisional: single-part derivation;
# refine with 2-3 more parts / the comparison batch, and correct
# NOISE_EFFECTIVE_CHAIN_FACTOR if the probe/amp question resolves it
# exactly.
#
# Resolving hundreds of uV on this ADS1256 front end (PGA 1, +/-5 V, LSB
# ~0.6 uV) is comfortable ONLY band-limited: at 1000 SPS the ADC's own
# input noise is ~30-50 uV pk-pk per 1 s window and the raw ~500 Hz
# bandwidth admits mains/EMI the legacy amplifier never saw. The capture is
# therefore decimated by NOISE_DECIMATION_FACTOR (1000 -> 50 SPS, passband
# ~22 Hz) before the windowed pk-pk rule. Since 2026-08-20 the decimator is
# a proper anti-alias FIR (stability_analysis.decimate_antialiased, Kaiser
# windowed-sinc, >= 60 dB stopband): the original 20:1 boxcar's ~-13 dB
# sidelobes let out-of-band interference FOLD into the judged band at the
# rate drop (60 Hz mains landed at 10 Hz at only -16 dB; measured at 41% of
# the in-band signal on an interference-heavy bench capture). Same passband
# and output timeline, so quiet parts read the same (the lot-500 anchors
# replay to identical verdicts) and the live-preview boxcar is display-only.
# Clipping is still detected on the RAW samples.
NOISE_TEST_ENABLED = True
NOISE_WINDOW_S = 1.0
NOISE_LEGACY_AMPLIFIER_GAIN = 4000.0  # NOMINAL sticker gain (not effective)
NOISE_LEGACY_PP_LIMIT_MV = 300.0      # TP412 scope limit through that chain
# Effective end-to-end display factor measured by the 2026-08-13 same-part
# cross-measurement (range 620-830; refine with more parts).
NOISE_EFFECTIVE_CHAIN_FACTOR = 700.0
NOISE_PP_LIMIT_MV = NOISE_LEGACY_PP_LIMIT_MV / NOISE_EFFECTIVE_CHAIN_FACTOR
NOISE_DECIMATION_FACTOR = 20          # 1000 SPS -> 50 SPS (anti-alias FIR)
# Real filter history captured on each side of the noise window
# (2026-08-31): the anti-alias FIR seats on the tail of the discarded
# quiet wait (left) and on extra streamed samples (right) instead of
# synthetic reflection padding, which only attenuated out-of-band
# interference ~11-21 dB inside the first/last judged window (>= 60 dB
# everywhere else). 310 raw samples = 0.31 s per side at 1000 SPS; both
# slices are archived with saved raw captures so replays reproduce the
# live verdict exactly.
NOISE_EDGE_CONTEXT_SAMPLES = antialias_edge_context_samples(
    NOISE_DECIMATION_FACTOR
)
# <= 15% of windows may exceed the limit (3 of 20). Tightened from 20% on
# 2026-08-17 using the lot-500 fixture comparison: the one part the legacy
# fixture failed for noise (500-44, 496 mV on the old scope) measured 4/20
# windows over here and was passing at 20%; every other part measured 0/20
# except 500-3's isolated 2-window environmental spike (old fixture read it
# mid-pack at 192 mV), which stays tolerated. 4 over now fails, 2 passes.
NOISE_MAX_OVER_FRACTION = 0.15
NOISE_CAPTURE_SECONDS = 20.0          # standard capture; see the soak below
# Per-part EXTENDED NOISE SOAK (2026-08-18, calibrated on the lot-500
# re-run): part 500-44's burst noise is INTERMITTENT - it measured 4/20
# windows over on 08-17 but 0/20 over (worst 38% below the limit) on 08-18,
# while scope verification on the legacy fixture confirms it is genuinely
# noisy. No threshold catches a capture with zero over-windows, and
# tightening would false-fail clean parts (500-1 took an environmental
# transient to 3/20 over at 1.03 mV on 08-18). The discriminator is
# OBSERVATION TIME: the soak triples the capture to 60 s and holds the
# allowed over-window count at the SAME ABSOLUTE 3 (i.e. 3 of 60, not 15%
# of 60 = 9) - one environmental bang still spans only 1-3 windows
# regardless of capture length, while genuine burst noise recurs and
# accumulates windows. Operator-selectable per part on the load step; use
# it on suspect or historically noisy parts.
NOISE_SOAK_CAPTURE_SECONDS = 60.0
NOISE_SOAK_MAX_OVER_FRACTION = (
    NOISE_MAX_OVER_FRACTION * NOISE_CAPTURE_SECONDS / NOISE_SOAK_CAPTURE_SECONDS
)
# Quiet wait before the capture window (streamed but discarded). Reworked
# again 2026-08-13 (user request, after a real part passed with a "worst
# 1.0 mV" window that was pure DC settling): the wait is now ADAPTIVE. The
# capture starts once NOISE_BASELINE_SETTLE_BLOCKS consecutive 1 s
# block-mean deltas stay at/below NOISE_BASELINE_SETTLE_DELTA_MV (earliest
# at NOISE_WAIT_BEFORE_CAPTURE_S); if the baseline is still moving at
# NOISE_WAIT_MAX_S the capture starts anyway and the report notes it —
# unlike the old settle rule, this can NEVER fail the part by itself.
# Combined with per-window detrending in the analysis (each 1 s window is
# judged against its own least-squares baseline = "a fresh offset every
# second"), residual settling cannot inflate the windowed pk-pk while real
# noise excursions around the moving baseline are fully retained.
NOISE_WAIT_BEFORE_CAPTURE_S = 3.0     # minimum quiet wait
NOISE_WAIT_MAX_S = 20.0               # settle deadline; then measure anyway
NOISE_BASELINE_SETTLE_DELTA_MV = NOISE_PP_LIMIT_MV / 4.0  # ~107 uV per second
NOISE_BASELINE_SETTLE_BLOCKS = 2      # consecutive in-threshold mean deltas
NOISE_CLIP_LIMIT_V = WAVEFORM_INPUT_RANGE_V * 0.98  # clipped window = over-limit
NOISE_FAIL_REASON_PREFIX = "Emitter-off noise"


def format_noise_pp(value_mv: float, *, decimals: int = 0) -> str:
    """Render a pk-pk noise level in uV below 1 mV, else in mV."""
    if abs(value_mv) < 1.0:
        return f"{value_mv * 1000.0:.{decimals}f} µV"
    return f"{value_mv:.{decimals}f} mV"

# Paired-fixture sensitivity calibration, measured 2026-08-17 on lot 500:
# 46 sensors measured on the legacy fixture and on this rig (batch CSVs in
# analysis/405M22_Data/). Per-part legacy/raw ratio: median 4.2973,
# regression-through-origin 4.2853, spread 4.4% (sd), range 3.67-4.66 - the
# two estimators agree within 0.3%, so the factor is set to 4.30.
# Validation against the same batch with the TP412 -625 limit (5.99 mV):
#   - part 500-10 -> 4.03 mV legacy-equivalent, FAIL (old fixture read 4.08
#     and failed it) - the only low-sensitivity part in the lot;
#   - every other part passes with the nearest at 6.52 mV (500-15), the SAME
#     value the old fixture recorded for it; none land in the guard band;
#   - highest part 500-41 -> 10.88 mV, inside the 11.98 mV TP412 maximum
#     (old fixture read 11.30).
SENSITIVITY_LEGACY_EQUIVALENT_FACTOR = 4.30
# +/-0.10 mV raw around the limit (~7% of the -625 raw center 1.393 mV,
# i.e. +/-0.43 mV legacy-equivalent). No lot-500 part fell inside it.
# A reading inside this band is within the margin of error of the conversion
# factor: the sensor still PASSES, but the technician is warned and advised
# to re-measure. It is never a failure mode and never a quarantine record.
SENSITIVITY_RAW_NEAR_LIMIT_HALF_WIDTH_MV = 0.10
SENSITIVITY_CALIBRATION_ID = "405m22_tp412_lot500_pairwise_v1"
SENSITIVITY_NEAR_LIMIT = "NEAR LIMIT"
SENSITIVITY_NEAR_LIMIT_WARNING_PREFIX = "Sensitivity near limit:"
LOW_SENSITIVITY_FAILURE_ENABLED = True
# 405 M22 capture policy: same structure as v6, retimed for 1-second cycles.
# Qualification (5 deltas -> 6+ cycles) plus 10 measurement cycles needs at
# least ~16 s of PWM-on time, so the deadline is 60 s instead of 20 s.
STABILITY_TIMEOUT_S = 60.0
DUT_STABILITY_CONFIRMATION_DELTAS = 5
SENSITIVITY_MEASUREMENT_CYCLES = 10
MAX_MEASUREMENT_ATTEMPTS = DEFAULT_MAX_MEASUREMENT_ATTEMPTS
SYNC_VALIDATION_CYCLES = 3
# Live preview window: 6000 samples = 6 s = six full 1 Hz cycles.
STREAM_PREVIEW_MAX_SAMPLES = 6000
# Simulator length: 70 one-second cycles covers stabilization (~17 s), the
# measurement window, and the 60 s "Never stabilizes" timeout.
SIM_CAPTURE_CYCLES = 70

# Permanently-mounted AIN1 reference sensor. Each reference reading starts at
# PWM-on, uses a dedicated robust-peak delta limit, and averages the
# next five complete cycle peak-to-peak values. Calibration averages five of
# those adaptive readings. Every DUT test performs a fresh reference reading
# first and requires it to remain inside the fixed +/-25 percent window.
REFERENCE_CALIBRATION_READINGS = 5
REFERENCE_MEASUREMENT_CYCLES = 5
# Independent of the DUT threshold in stability_settings.json: relaxing the
# part-under-test rule must never relax the fixture's own reference gate.
REFERENCE_PEAK_DELTA_THRESHOLD_MV = 0.250
REFERENCE_TOLERANCE_PERCENT = 25.0
# Windows USB serial can lose a handful of bytes in a rare transient hiccup;
# the integrity validator then rejects the capture with nothing recorded. A
# calibration streams five long readings back to back, so give each reading
# this many fresh attempts before failing the whole calibration. Only
# StreamIntegrityError is retried; real hardware faults still abort at once.
REFERENCE_READING_STREAM_RETRIES = 2
# Even with the 1 MiB receive buffer and the dedicated drain thread, the
# Windows CP210x driver still drops a few samples in rare USB-scheduling
# hiccups (bench: isolated ~3-6 sample gaps, i.e. milliseconds of a 17-23 s
# capture). Rejecting the whole capture for that made real tests fail
# repeatedly even with retries, so a capture now tolerates BOUNDED micro-gap
# loss: at most STREAM_MAX_MICRO_GAPS gaps and STREAM_MAX_MISSING_SAMPLES
# lost samples in total, with the firmware/host counters agreeing within the
# same budget. Duplicate/reordered/torn records, ADC overruns, or a >2% rate
# error are the driver-overflow corruption signature and still reject the
# capture with nothing recorded (and are then retried as before).
STREAM_MAX_MICRO_GAPS = 3
STREAM_MAX_MISSING_SAMPLES = 20   # 20 ms of data at 1000 S/s
# Schema v3 marked baselines taken on firmware v2.0 (PGA gain 1, buffer off).
# Schema v4 additionally marks baselines whose reference readings were driven
# at the 406MCA reference unit's qualified 10 Hz (the app's first bench runs
# drove it at the DUT's 1 Hz, where a pyroelectric response is several times
# larger and not comparable). Older schemas are deliberately rejected so a
# fresh "Calibrate reference unit" run is forced after each change.
REFERENCE_CALIBRATION_SCHEMA_VERSION = 4
for _sim_case in ("Borderline sensitivity", "Never stabilizes"):
    if _sim_case not in SIM_CASES:
        SIM_CASES = [*SIM_CASES, _sim_case]


def simulate_v6_startup_capture(
    filter_setup: str,
    case_name: str,
    *,
    cycles: int = SIM_CAPTURE_CYCLES,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
    frequency_hz: float = EXPECTED_FREQUENCY_HZ,
) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Generate a deterministic PWM-on drift followed by a stable waveform."""
    duration_s = cycles / frequency_hz
    sample_count = int(round(duration_s * sample_rate_hz))
    t = np.arange(sample_count, dtype=float) / sample_rate_hz
    phase = (t * frequency_hz) % 1.0
    min_mv = FILTER_SPECS_MV[filter_setup]
    if case_name == "Low sensitivity":
        legacy_equivalent_sensitivity_mv = min_mv * 0.62
    elif case_name == "Borderline sensitivity":
        legacy_equivalent_sensitivity_mv = min_mv
    elif case_name == "Random good sensor":
        legacy_equivalent_sensitivity_mv = min_mv * 1.40
    else:
        legacy_equivalent_sensitivity_mv = min_mv * 1.35
    sensitivity_mv = (
        legacy_equivalent_sensitivity_mv / SENSITIVITY_LEGACY_EQUIVALENT_FACTOR
    )

    triangle = np.where(phase < 0.5, -1.0 + 4.0 * phase, 3.0 - 4.0 * phase)
    if case_name == "Wrong polarity":
        triangle = -triangle
    stable_case = case_name if case_name in SIM_CASES and case_name != "Never stabilizes" else "Known good"
    offset_v = simulate_offset_v(stable_case)
    # A 100 mV exponential baseline transient crosses the provisional 0.5 mV
    # adjacent-cycle DUT threshold at roughly twelve seconds.
    startup_drift_v = 0.100 * np.exp(-t / 3.0)
    if case_name == "Never stabilizes":
        # +/-1.25 mV per cycle keeps every adjacent-peak delta at 2.5 mV, five
        # times the DUT threshold, so this case never qualifies no matter how
        # long the capture runs.
        cycle_number = np.floor(t * frequency_hz).astype(int)
        startup_drift_v += np.where(cycle_number % 2 == 0, 0.00125, -0.00125)
    seed = sum((index + 1) * ord(char) for index, char in enumerate(case_name))
    rng = np.random.default_rng(seed)
    noise_v = rng.normal(0.0, 0.00001, sample_count)
    waveform_v = (
        offset_v
        + startup_drift_v
        + triangle * ((sensitivity_mv / 1000.0) / 2.0)
        + noise_v
    )
    sync_v = np.where(phase < 0.5, 1.0, 0.0)
    return waveform_v, sync_v, sample_rate_hz, offset_v


def analyze_esp32_waveform(*args, **kwargs) -> WaveformMetrics:
    """Run the shared production analysis with ESP32-specific warning text."""
    # The shared engine's def-time default is the 406MCA's 10 Hz; force the
    # 405 M22 chop frequency for callers that do not pass it explicitly.
    kwargs.setdefault("expected_frequency_hz", EXPECTED_FREQUENCY_HZ)
    metrics = analyze_waveform(*args, **kwargs)
    rewritten: list[str] = []
    for warning in metrics.warnings:
        if warning.startswith("Waveform is near the LabJack"):
            warning = warning.replace("the LabJack", "the ADS1256", 1).replace(
                "choose a larger AIN0 range", "check the buffer/PGA range"
            )
        warning = warning.replace("blade sync", "ESP32 PWM sync")
        warning = warning.replace("Blade sync", "ESP32 PWM sync")
        rewritten.append(warning)
    metrics.warnings = rewritten
    return metrics


def evaluate_result(
    offset_v: float | None,
    waveform_metrics: WaveformMetrics | None,
    filter_setup: str,
) -> FinalResult:
    """Apply the v6.1 fixture sensitivity transfer plus every other gate."""
    final = evaluate_result_with_sensitivity_limit(
        offset_v,
        waveform_metrics,
        filter_setup,
    )
    # The shared engine compares the raw ESP32 amplitude directly with the
    # legacy limit. Remove that incompatible result and apply the calibrated
    # raw guard-band policy below.
    final.fail_reasons = [
        reason
        for reason in final.fail_reasons
        if not reason.startswith("Sensitivity too low:")
    ]
    if LOW_SENSITIVITY_FAILURE_ENABLED and waveform_metrics is not None:
        raw_sensitivity_mv = waveform_metrics.sensitivity_mv
        disposition = sensitivity_gate_outcome(raw_sensitivity_mv, filter_setup)
        fail_below_mv, pass_above_mv = sensitivity_raw_limits_mv(filter_setup)
        equivalent_mv = legacy_equivalent_sensitivity_mv(raw_sensitivity_mv)
        if disposition == OUTCOME_FAIL:
            final.fail_reasons.append(
                f"Sensitivity too low: raw {raw_sensitivity_mv:.3f} mV is below "
                f"{fail_below_mv:.2f} mV; legacy-equivalent {equivalent_mv:.3f} mV "
                f"using factor {SENSITIVITY_LEGACY_EQUIVALENT_FACTOR:.3f}."
            )
        elif disposition == SENSITIVITY_NEAR_LIMIT:
            # Still a PASS: the reading is within the conversion factor's
            # margin of error, so warn and suggest a re-measure only.
            final.warnings.append(
                f"{SENSITIVITY_NEAR_LIMIT_WARNING_PREFIX} raw {raw_sensitivity_mv:.3f} mV "
                f"is inside the inclusive {fail_below_mv:.2f}-{pass_above_mv:.2f} mV "
                f"band around the limit; legacy-equivalent {equivalent_mv:.3f} mV using "
                f"factor {SENSITIVITY_LEGACY_EQUIVALENT_FACTOR:.3f}. Within the margin "
                "of error of the conversion factor - re-measure advised, sensor passes."
            )
        else:
            # TP412 also sets an upper sensitivity limit per filter. A max-side
            # guard-band policy is deliberately not defined yet - decide it when
            # the comparison batch turns this gate on.
            legacy_max_mv = FILTER_RANGES_MV[filter_setup][1]
            if equivalent_mv > legacy_max_mv:
                final.fail_reasons.append(
                    f"Sensitivity too high: legacy-equivalent {equivalent_mv:.3f} mV "
                    f"exceeds the TP412 maximum {legacy_max_mv:.2f} mV for "
                    f"{filter_setup} (raw {raw_sensitivity_mv:.3f} mV, factor "
                    f"{SENSITIVITY_LEGACY_EQUIVALENT_FACTOR:.3f})."
                )
    final.passed = not final.fail_reasons
    return final


def legacy_equivalent_sensitivity_mv(raw_sensitivity_mv: float) -> float:
    """Translate a final raw ESP32 sensitivity to the old-fixture scale."""
    return float(raw_sensitivity_mv) * SENSITIVITY_LEGACY_EQUIVALENT_FACTOR


def sensitivity_raw_limits_mv(filter_setup: str) -> tuple[float, float]:
    """Return rounded raw FAIL / NEAR LIMIT / PASS boundaries for one filter.

    The established legacy filter minimum remains at the center of a +/-0.10
    mV raw near-limit band. For the production default (-625, 5.99 mV legacy)
    this is 1.29-1.49 mV raw around 5.99 / 4.30.
    """
    try:
        legacy_min_mv = FILTER_SPECS_MV[filter_setup]
    except KeyError as exc:
        raise ValueError(f"Unknown filter/setup for sensitivity policy: {filter_setup}") from exc
    raw_center_mv = legacy_min_mv / SENSITIVITY_LEGACY_EQUIVALENT_FACTOR
    return (
        round(raw_center_mv - SENSITIVITY_RAW_NEAR_LIMIT_HALF_WIDTH_MV, 2),
        round(raw_center_mv + SENSITIVITY_RAW_NEAR_LIMIT_HALF_WIDTH_MV, 2),
    )


def sensitivity_gate_outcome(raw_sensitivity_mv: float, filter_setup: str) -> str:
    """Classify raw sensitivity; the near-limit band is a PASS with a warning."""
    raw_mv = float(raw_sensitivity_mv)
    if not math.isfinite(raw_mv):
        return OUTCOME_FAIL
    fail_below_mv, pass_above_mv = sensitivity_raw_limits_mv(filter_setup)
    if raw_mv < fail_below_mv:
        return OUTCOME_FAIL
    if raw_mv > pass_above_mv:
        return OUTCOME_PASS
    return SENSITIVITY_NEAR_LIMIT


def is_sensitivity_near_limit(final_result: FinalResult | None) -> bool:
    """True when a passing sensor's sensitivity sits in the near-limit band."""
    if final_result is None:
        return False
    return any(
        str(warning).startswith(SENSITIVITY_NEAR_LIMIT_WARNING_PREFIX)
        for warning in final_result.warnings
    )


def is_not_measured(final_result: FinalResult | None) -> bool:
    """True for a placeholder record written when nothing could be measured."""
    if final_result is None or final_result.passed:
        return False
    return any(
        str(reason).startswith(NOT_MEASURED_REASON_PREFIX)
        for reason in final_result.fail_reasons
    )


def build_not_measured_result(detail: str) -> FinalResult:
    """Placeholder record for a sensor that was never actually read.

    Every measured quantity stays empty on purpose: a NOT MEASURED row must
    never look like a 0 V offset or a 0 mV sensitivity in the analysis.
    """
    reason = " ".join(str(detail).split())
    return FinalResult(
        passed=False,
        offset_v=None,
        sensitivity_mv=None,
        polarity="",
        fail_reasons=[
            f"{NOT_MEASURED_REASON_PREFIX} {reason}"
            if reason
            else f"{NOT_MEASURED_REASON_PREFIX} nothing was recorded"
        ],
        warnings=[],
        waveform_metrics=None,
    )


def summarize_batch_outcomes(outcomes: list[str]) -> dict[str, float]:
    """Count a batch's rows for the summary window.

    Skipped sensors were never read, so they are neither a pass nor a fail:
    they are counted separately and excluded from both the tested count and
    the yield, which would otherwise be dragged down by a rig fault.
    """
    recorded = len(outcomes)
    passed = sum(1 for outcome in outcomes if outcome == OUTCOME_PASS)
    not_measured = sum(1 for outcome in outcomes if outcome == OUTCOME_NOT_MEASURED)
    tested = recorded - not_measured
    return {
        "recorded": recorded,
        "tested": tested,
        "passed": passed,
        "failed": tested - passed,
        "not_measured": not_measured,
        "yield_pct": (100.0 * passed / tested) if tested else 0.0,
    }


def result_outcome(final_result: FinalResult | None) -> str:
    """Return PASS, FAIL, or NOT MEASURED without weakening .passed."""
    if final_result is None:
        return ""
    if final_result.passed:
        return OUTCOME_PASS
    if is_not_measured(final_result):
        return OUTCOME_NOT_MEASURED
    return OUTCOME_FAIL

# --------------------------------------------------------------------------- #
# v6 theme - palette carried forward from v5 / eltecinstruments.com
# --------------------------------------------------------------------------- #
ELTEC_BLUE = "#1e419c"          # site primary blue
ELTEC_BLUE_DEEP = "#0b3d91"     # hero gradient start
ELTEC_BLUE_DARK = "#16336f"
ELTEC_BLUE_BRIGHT = "#4d8dff"   # hero gradient end
ELTEC_BLUE_LIGHT = "#e1e7f6"    # site light blue-gray tint
ELTEC_RED = "#ed1b44"           # site signal red

NAVY = "#0a1020"                # site dark-section background (scope view)
NAVY_EDGE = "#1b2740"
NAVY_GRID_MINOR = "#131c31"
NAVY_GRID_MAJOR = "#1c2947"

PAGE_BG = "#f3f5fa"
CARD_BG = "#ffffff"
CARD_BORDER = "#dce3f1"
TEXT_DARK = "#141d33"
NEUTRAL_FG = TEXT_DARK
MUTED_FG = "#5c6a88"
HEADER_FG = "#ffffff"
HEADER_SUB_FG = "#bcd0f7"

PASS_BG = "#e4f6eb"
PASS_FG = "#14532d"
PASS_ACCENT = "#17a34a"
FAIL_BG = "#fde7e9"
FAIL_FG = "#991b1b"
FAIL_ACCENT = "#dc2626"
WARN_BG = "#fdf5dd"
WARN_FG = "#854d0e"
WARN_ACCENT = "#f59e0b"
NEUTRAL_BG = "#e8edf6"

STEP_IDLE = "#c7d0e2"
STEP_IDLE_FG = "#93a1bd"
PRIMARY_DISABLED = "#aab9dc"
GHOST_BG = "#e6ebf6"
GHOST_HOVER = "#d4ddf0"

WAVE_BG = NAVY
TRACE_CORE = "#6ab2ff"          # scope trace glow: halo -> mid -> core
TRACE_MID = "#2f6fce"
TRACE_HALO = "#1c3a68"
SYNC_CORE = "#f5b93c"
SYNC_HALO = "#5c4310"
SCOPE_LIMIT_RED = "#ff4757"     # solid noise-range cutoff lines on navy

# The site's "technical gradient" strip (blue -> indigo -> violet), used as the
# accent line across the top of cards.
TECH_GRADIENT = ["#3b82f6", "#6366f1", "#a855f7"]
HEADER_GRADIENT = [ELTEC_BLUE_DEEP, ELTEC_BLUE, "#2e5bc0", ELTEC_BLUE_BRIGHT]

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
# Look for the logo next to this script and in an assets/ folder at any ancestor
# directory (the shared repo-root assets/ lives a few levels up), so it is
# found from either location.
LOGO_CANDIDATES = [ASSETS_DIR / "eltec_logo.png"] + [
    parent / "assets" / "eltec_logo.png"
    for parent in Path(__file__).resolve().parents
]


def find_logo_path() -> Path | None:
    for candidate in LOGO_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# High-DPI support. Without this Windows renders the app at 96 DPI and
# stretches the bitmap to the monitor scale, which makes circles and rounded
# shapes look blocky. Declaring DPI awareness + scaling all canvas geometry by
# UI_SCALE keeps everything crisp at 125%/150% display scaling.
# --------------------------------------------------------------------------- #
UI_SCALE = 1.0


def S(value: float) -> int:
    """Scale a logical (96-DPI) pixel dimension to physical pixels."""
    return int(round(value * UI_SCALE))


def Sf(value: float) -> float:
    """Float variant of S() for line widths and sub-pixel geometry."""
    return value * UI_SCALE


def enable_windows_dpi_awareness() -> None:
    """Opt out of DPI virtualization (must run before the Tk window exists).

    System-DPI-aware (not per-monitor v2) on purpose: Tk 8.6 does not fully
    support per-monitor mode and it can corrupt child-window repaints. System
    awareness gives the same crisp rendering on the single-monitor tester PCs.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
    except Exception:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def load_private_fonts() -> None:
    """Register brand fonts dropped into an assets/fonts folder (Windows only).

    The UI prefers Poppins / Manrope / JetBrains Mono (the faces used on
    eltecinstruments.com). AddFontResourceExW with FR_PRIVATE makes them
    available to this process only - nothing is installed system-wide.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
    except Exception:
        return
    font_files: list[Path] = []
    seen: set[Path] = set()
    for base in [ASSETS_DIR] + [parent / "assets" for parent in Path(__file__).resolve().parents]:
        fonts_dir = base / "fonts"
        if fonts_dir in seen or not fonts_dir.is_dir():
            continue
        seen.add(fonts_dir)
        font_files.extend(sorted(fonts_dir.glob("*.ttf")) + sorted(fonts_dir.glob("*.otf")))
    FR_PRIVATE = 0x10
    for font_file in font_files:
        try:
            ctypes.windll.gdi32.AddFontResourceExW(str(font_file), FR_PRIVATE, 0)
        except Exception:
            pass


def pick_font_family(root: tk.Misc, candidates: list[str], fallback: str) -> str:
    try:
        available = {family.lower() for family in tkfont.families(root)}
    except tk.TclError:
        return fallback
    for name in candidates:
        if name.lower() in available:
            return name
    return fallback


CSV_FIELDS = [
    "timestamp",
    "batch_number",
    "sensor_number",
    "sensor_id",
    "tester_name",
    "model",
    "filter_setup",
    "pwm_channel",
    "pwm_hz",
    "pwm_duty",
    "offset_v",
    # The insertion-time offset read. ``offset_v`` is the settled re-read
    # taken after the sensitivity capture (2026-08-17: these parts' offsets
    # rise for tens of seconds after insertion); comparing the two columns
    # shows each part's settling behavior.
    "offset_initial_v",
    # ``sensitivity_mv`` remains the raw ESP32 value for compatibility with
    # existing v6.1 batch files. The explicit fields below make the calibrated
    # value and decision policy unambiguous for new batches.
    "sensitivity_mv",
    "sensitivity_raw_mv",
    "sensitivity_legacy_equivalent_mv",
    "sensitivity_correction_factor",
    "sensitivity_calibration_id",
    "sensitivity_gate_outcome",
    "sensitivity_raw_fail_below_mv",
    "sensitivity_raw_pass_above_mv",
    "polarity",
    "polarity_good_bad",
    "pass_fail",
    "fail_reasons",
    "failure_mode_tag",
    "failure_mode_reason",
    "operator_comments",
    "waveform_snapshot_paths",
    "battery_v",
    "noise_rms_mv",
    "snr_db",
    # AIN1 emitter-health gate audit trail.
    "reference_calibrated_at",
    "reference_calibration_mv",
    "reference_lower_mv",
    "reference_upper_mv",
    "reference_check_mv",
    "reference_drift_pct",
    # V6 peak-delta stabilization telemetry.
    "stabilized",
    "stability_timeout",
    "stability_threshold_mv",
    "stability_required_deltas",
    "stabilization_cycle",
    "stabilization_seconds",
    "stability_window_max_delta_mv",
    "last_peak_delta_mv",
    "capture_cycles",
    "measurement_cycles",
    "stability_phase",
    "measurement_attempt",
    "measurement_failures",
    "active_stability_required_deltas",
    "active_measurement_cycles_required",
    "pwm_on_seconds",
    "data_source",
    # TP412 emitter-off noise test (windowed pk-pk; distinct from the
    # driven-capture noise_rms_mv/snr_db SNR metrics above).
    "noise_test_outcome",
    "noise_windows_total",
    "noise_windows_over",
    "noise_over_percent",
    "noise_worst_pp_mv",
    "noise_median_pp_mv",
    "noise_settle_s",
    "noise_capture_s",
    "noise_pp_limit_mv",
    "noise_analysis_rate_hz",
    "noise_baseline_settled",
    # v2.0 attempt history: how many measurement attempts this verdict
    # took and how often the part was set aside (details per event in the
    # batch's *_attempts.csv, see attempt_history.py).
    "measure_attempts",
    "skip_count",
]

STABILITY_SAMPLE_DIAGNOSTIC_FIELDS = (
    "batch_number",
    "sensor_id",
    "sample_index",
    "pwm_elapsed_s",
    "voltage_v",
    "sync",
)
STABILITY_CYCLE_DIAGNOSTIC_FIELDS = (
    "batch_number",
    "sensor_id",
    "cycle_number",
    "start_index",
    "end_index",
    "start_elapsed_s",
    "end_elapsed_s",
    "robust_peak_v",
    "raw_max_v",
    "raw_min_v",
    "peak_to_peak_v",
    "signed_peak_delta_mv",
    "absolute_peak_delta_mv",
    "within_threshold",
    "confirmation_run_length",
)


# --------------------------------------------------------------------------- #
# Results location + batch helpers
# --------------------------------------------------------------------------- #
def results_root_dir() -> Path:
    # Each tester version keeps its data in its own subfolder so results can be
    # tracked and analyzed per version. Autosave and waveform-snapshot folders
    # derive from this path, so they follow automatically.
    return Path.home() / "Documents" / "Eltec_405M22_Test_Results" / "405m22_esp32"


def safe_filename_part(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())
    return cleaned.strip("_") or "UNLABELED"


def batch_results_path(batch_number: str) -> Path:
    return results_root_dir() / f"405m22_esp32_lot_{safe_filename_part(batch_number)}.csv"


def batch_autosave_path(batch_number: str) -> Path:
    safe = safe_filename_part(batch_number)
    return results_root_dir() / "autosave" / f"esp32_lot_{safe}_current_sensor.json"


def reference_calibration_path() -> Path:
    """Persistent AIN1 emitter/reference baseline for this 405 M22 build.

    The 406MCA baselines (v6/v6.1) were measured with a 10 Hz chop and a
    pyroelectric response is strongly frequency dependent, so they are NOT
    read as a fallback here: this build always requires its own calibration
    captured at 1 Hz.
    """
    return results_root_dir() / "reference_sensor_calibration.json"


def reference_stability_settings(settings: StabilitySettings) -> StabilitySettings:
    """Use the dedicated fixed delta for AIN1 reference captures.

    The permanently mounted reference unit keeps its own
    ``REFERENCE_PEAK_DELTA_THRESHOLD_MV`` and deliberately ignores the tracked
    DUT threshold: the DUT limit is a screening knob for the part under test,
    while the reference limit guards the fixture's own repeatability.
    """
    return StabilitySettings(
        peak_delta_threshold_mv=REFERENCE_PEAK_DELTA_THRESHOLD_MV,
        consecutive_deltas_required=settings.consecutive_deltas_required,
    )


def dut_stability_settings(settings: StabilitySettings) -> StabilitySettings:
    """Apply the fixed v6.1 qualification count to the tracked DUT delta."""
    return StabilitySettings(
        peak_delta_threshold_mv=settings.peak_delta_threshold_mv,
        consecutive_deltas_required=DUT_STABILITY_CONFIRMATION_DELTAS,
    )




class ReferenceCalibrationError(RuntimeError):
    """The reference-unit calibration is missing, malformed, or unrepeatable."""


class ReferenceCaptureError(RuntimeError):
    """A chopped-emitter AIN1 capture cannot be used as a reference reading."""


@dataclass(frozen=True)
class ReferenceCalibration:
    """Persisted average and acceptance window for the fixed AIN1 sensor."""

    readings_mv: tuple[float, ...]
    mean_mv: float
    recorded_at: str
    tolerance_percent: float = REFERENCE_TOLERANCE_PERCENT
    valid: bool = True
    invalidated_at: str | None = None
    invalidation_reason: str | None = None
    failed_reading_mv: float | None = None

    @property
    def lower_mv(self) -> float:
        return self.mean_mv * (1.0 - self.tolerance_percent / 100.0)

    @property
    def upper_mv(self) -> float:
        return self.mean_mv * (1.0 + self.tolerance_percent / 100.0)

    def drift_percent(self, reading_mv: float) -> float:
        return (float(reading_mv) - self.mean_mv) / self.mean_mv * 100.0

    def accepts(self, reading_mv: float) -> bool:
        reading_mv = float(reading_mv)
        return (
            self.valid
            and math.isfinite(reading_mv)
            and self.lower_mv <= reading_mv <= self.upper_mv
        )

    def invalidated(self, reason: str, failed_reading_mv: float | None = None) -> "ReferenceCalibration":
        return ReferenceCalibration(
            readings_mv=self.readings_mv,
            mean_mv=self.mean_mv,
            recorded_at=self.recorded_at,
            tolerance_percent=self.tolerance_percent,
            valid=False,
            invalidated_at=datetime.now().isoformat(timespec="seconds"),
            invalidation_reason=str(reason).strip(),
            failed_reading_mv=failed_reading_mv,
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": REFERENCE_CALIBRATION_SCHEMA_VERSION,
            "channel": "AIN1",
            "reference_pwm_hz": REFERENCE_PWM_FREQUENCY_HZ,
            "metric": "mean peak-to-peak response of five post-stability cycles (mV)",
            "reading_count": len(self.readings_mv),
            "readings_mv": [round(value, 6) for value in self.readings_mv],
            "mean_mv": round(self.mean_mv, 6),
            "tolerance_percent": self.tolerance_percent,
            "lower_mv": round(self.lower_mv, 6),
            "upper_mv": round(self.upper_mv, 6),
            "recorded_at": self.recorded_at,
            "valid": self.valid,
            "invalidated_at": self.invalidated_at,
            "invalidation_reason": self.invalidation_reason,
            "failed_reading_mv": self.failed_reading_mv,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "ReferenceCalibration":
        try:
            if int(payload.get("schema_version")) != REFERENCE_CALIBRATION_SCHEMA_VERSION:
                raise ValueError("unsupported schema version")
            readings = tuple(float(value) for value in payload["readings_mv"])
            mean_mv = float(payload["mean_mv"])
            tolerance = float(payload["tolerance_percent"])
            recorded_at = str(payload["recorded_at"]).strip()
            valid = bool(payload.get("valid", True))
            invalidated_at = payload.get("invalidated_at")
            invalidation_reason = payload.get("invalidation_reason")
            failed = payload.get("failed_reading_mv")
            failed_reading_mv = None if failed is None else float(failed)
        except (KeyError, TypeError, ValueError) as exc:
            raise ReferenceCalibrationError(
                "Reference calibration file is malformed; calibrate the reference unit again."
            ) from exc
        if (
            len(readings) < 2
            or not recorded_at
            or not math.isfinite(mean_mv)
            or mean_mv <= 0
            or not math.isfinite(tolerance)
            or tolerance <= 0
            or any(not math.isfinite(value) or value <= 0 for value in readings)
        ):
            raise ReferenceCalibrationError(
                "Reference calibration contains invalid readings; calibrate the reference unit again."
            )
        calculated_mean = float(np.mean(readings))
        if not math.isclose(mean_mv, calculated_mean, rel_tol=1e-5, abs_tol=1e-5):
            raise ReferenceCalibrationError(
                "Reference calibration average does not match its readings; run calibration again."
            )
        # A file invalidated only because its last reading missed the former
        # narrower window may be used again when that recorded reading is
        # inside the current policy. A fresh AIN1 check still runs before AIN0.
        if (
            not valid
            and tolerance < REFERENCE_TOLERANCE_PERCENT
            and failed_reading_mv is not None
            and math.isfinite(failed_reading_mv)
            and mean_mv * (1.0 - REFERENCE_TOLERANCE_PERCENT / 100.0)
            <= failed_reading_mv
            <= mean_mv * (1.0 + REFERENCE_TOLERANCE_PERCENT / 100.0)
        ):
            valid = True
            invalidated_at = None
            invalidation_reason = None
            failed_reading_mv = None
        return cls(
            readings_mv=readings,
            mean_mv=mean_mv,
            recorded_at=recorded_at,
            # The acceptance band is an application policy, not a per-file
            # setting. This also upgrades calibration files saved when v6.1
            # used the older +/-10% window.
            tolerance_percent=REFERENCE_TOLERANCE_PERCENT,
            valid=valid,
            invalidated_at=None if invalidated_at is None else str(invalidated_at),
            invalidation_reason=None if invalidation_reason is None else str(invalidation_reason),
            failed_reading_mv=failed_reading_mv,
        )


def build_reference_calibration(
    readings_mv: list[float] | tuple[float, ...],
    *,
    recorded_at: str | None = None,
    required_readings: int = REFERENCE_CALIBRATION_READINGS,
    tolerance_percent: float = REFERENCE_TOLERANCE_PERCENT,
) -> ReferenceCalibration:
    """Average repeatable AIN1 readings and produce the hard acceptance band."""
    readings = tuple(float(value) for value in readings_mv)
    if len(readings) != required_readings:
        raise ReferenceCalibrationError(
            f"Reference calibration requires {required_readings} readings; received {len(readings)}."
        )
    if any(not math.isfinite(value) or value <= 0 for value in readings):
        raise ReferenceCalibrationError("Every reference calibration reading must be finite and positive.")
    mean_mv = float(np.mean(readings))
    deviations = [abs(value - mean_mv) / mean_mv * 100.0 for value in readings]
    if max(deviations) > tolerance_percent:
        formatted = ", ".join(f"{value:.2f}" for value in readings)
        raise ReferenceCalibrationError(
            f"Reference calibration was not repeatable within +/-{tolerance_percent:g}% "
            f"(readings: {formatted} mV). Check the fixed sensor, wiring, battery, "
            "and emitter, then calibrate again."
        )
    return ReferenceCalibration(
        readings_mv=readings,
        mean_mv=mean_mv,
        recorded_at=recorded_at or datetime.now().isoformat(timespec="seconds"),
        tolerance_percent=tolerance_percent,
    )


def load_reference_calibration(path: Path | None = None) -> ReferenceCalibration | None:
    # No 406MCA fallback: those baselines were captured with a 10 Hz chop and
    # cannot gate a 1 Hz reference reading. Calibrate this build directly.
    path = reference_calibration_path() if path is None else Path(path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReferenceCalibrationError(
            f"Could not read reference calibration {path}; calibrate the reference unit again."
        ) from exc
    if not isinstance(payload, dict):
        raise ReferenceCalibrationError(
            f"Reference calibration {path} is not a JSON object; run calibration again."
        )
    return ReferenceCalibration.from_dict(payload)


def save_reference_calibration(
    calibration: ReferenceCalibration, path: Path | None = None
) -> Path:
    """Atomically persist the AIN1 baseline or its invalidated state."""
    path = reference_calibration_path() if path is None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(calibration.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise ReferenceCalibrationError(
            f"Could not save reference calibration to {path}: {exc}"
        ) from exc
    return path


def analyze_reference_stable_response_mv(analysis: StabilityAnalysis) -> float:
    """Average the five fresh cycle p-p values selected after peak stability."""
    if not analysis.report.measurement_complete:
        if analysis.report.timed_out:
            raise ReferenceCaptureError(
                f"Reference unit did not stabilize within {STABILITY_TIMEOUT_S:g} seconds."
            )
        raise ReferenceCaptureError("Reference-unit capture ended before a stable reading was complete.")
    cycles = analysis.measurement_cycles
    if len(cycles) != REFERENCE_MEASUREMENT_CYCLES:
        raise ReferenceCaptureError(
            f"Reference reading requires {REFERENCE_MEASUREMENT_CYCLES} fresh cycles; "
            f"received {len(cycles)}."
        )
    reading_mv = float(np.mean([cycle.peak_to_peak_v for cycle in cycles])) * 1000.0
    if not math.isfinite(reading_mv) or reading_mv <= 0:
        raise ReferenceCaptureError("Reference-unit response is not finite and positive.")
    return reading_mv


def count_existing_batch_rows(csv_path: Path) -> int:
    if not csv_path.exists():
        return 0
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
            return sum(1 for _row in csv.DictReader(csv_file))
    except Exception:
        return 0


def next_sensor_number_for_batch(csv_path: Path) -> int:
    if not csv_path.exists():
        return 1
    next_number = 1
    try:
        with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
            for row in csv.DictReader(csv_file):
                sensor_number_text = (row.get("sensor_number") or "").strip()
                if sensor_number_text:
                    try:
                        next_number = max(next_number, int(sensor_number_text) + 1)
                        continue
                    except ValueError:
                        pass
                sensor_id = (row.get("sensor_id") or "").strip()
                if "-" in sensor_id:
                    suffix = sensor_id.rsplit("-", 1)[-1]
                    try:
                        next_number = max(next_number, int(suffix) + 1)
                    except ValueError:
                        pass
    except Exception:
        return count_existing_batch_rows(csv_path) + 1
    return next_number


def polarity_good_bad(polarity: str) -> str:
    if not polarity or polarity in ("NOT MEASURED", "UNKNOWN"):
        return ""
    return "GOOD" if polarity == POSITIVE_POLARITY else "BAD"


def split_failure_mode(choice: str) -> tuple[str, str]:
    """Validate and split a displayed production failure-mode choice.

    The NOT MEASURED reasons are accepted here too so a skipped sensor can be
    written with a tag, but they are kept out of FAILURE_MODE_CHOICES so the
    result-step picker only ever offers real production failure modes.
    """
    choice = str(choice).strip()
    if choice not in FAILURE_MODE_CHOICES + NOT_MEASURED_REASON_CHOICES:
        raise ValueError("Choose a failure mode before saving this failed sensor.")
    tag, reason = choice.split(" - ", 1)
    return tag, reason


def suggest_failure_mode(final_result: FinalResult | None) -> str:
    """Choose the primary failure in measurement order."""
    if final_result is None or final_result.passed:
        return ""
    if is_not_measured(final_result):
        return DEFAULT_NOT_MEASURED_REASON
    reason_text = " ".join(final_result.fail_reasons).lower()

    # Offset is measured before waveform capture, so an electrical offset
    # failure remains primary even when the subsequent waveform is unstable,
    # weak, reversed, or noisy. Continue capturing to retain those diagnostics.
    offset_v = final_result.offset_v
    if offset_v is not None and math.isfinite(offset_v):
        if offset_v > OFFSET_MAX_V:
            return "HO - High offset"
        if offset_v < OFFSET_MIN_V:
            if offset_v <= SENSOR_OFFSET_MIN_PLAUSIBLE_V:
                return "D - No offset"
            return "LO - Low offset"

    if "unstable" in reason_text or "stabiliz" in reason_text:
        return UNSTABLE_FAILURE_MODE
    if "sensitivity too low" in reason_text:
        no_coherent_response = (
            "signal-to-noise too low" in reason_text
            or "snr unavailable" in reason_text
            or "sensor noise with no emitter response" in reason_text
        )
        if (
            no_coherent_response
            and offset_v is not None
            and math.isfinite(offset_v)
            and OFFSET_MIN_V <= offset_v <= OFFSET_MAX_V
        ):
            return "GO/D - Good offset/no signal"
        return "LS - Low sensitivity"
    if "polarity" in reason_text:
        return "RP - Reversed polarity"
    # The TP412 emitter-off noise test (windowed pk-pk) and the
    # driven-capture SNR gate both indicate a noisy part.
    if "emitter-off noise" in reason_text:
        return "N - Noisy"
    if "signal-to-noise" in reason_text or "snr" in reason_text:
        return "N - Noisy"
    return "SB - Sensor bad"


def _fmt_optional_float(value: float | None, decimals: int) -> str:
    """Format an optional metric for CSV, blank when missing or non-finite."""
    if value is None or not math.isfinite(value):
        return ""
    return f"{value:.{decimals}f}"


def append_result_csv(
    csv_path: Path,
    *,
    batch_number: str,
    sensor_number: int,
    sensor_id: str,
    tester_name: str,
    filter_setup: str,
    pwm_channel: str,
    pwm_hz: float,
    pwm_duty: float,
    final_result: FinalResult,
    comment: str,
    snapshot_paths: list[Path],
    battery_v: float | None = None,
    capture_report: "StabilityCaptureReport | None" = None,
    noise_report: "NoiseCaptureReport | None" = None,
    reference_calibration: "ReferenceCalibration | None" = None,
    reference_check_mv: float | None = None,
    failure_mode: str = "",
    offset_initial_v: float | None = None,
    measure_attempts: int = 0,
    skip_count: int = 0,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not csv_path.exists() or csv_path.stat().st_size == 0
    metrics = final_result.waveform_metrics
    outcome = result_outcome(final_result)
    raw_sensitivity_mv = final_result.sensitivity_mv
    equivalent_sensitivity_mv = (
        None
        if raw_sensitivity_mv is None or not math.isfinite(raw_sensitivity_mv)
        else legacy_equivalent_sensitivity_mv(raw_sensitivity_mv)
    )
    # With the 405 M22 gate disabled the guard-band classification would be
    # misleading next to a PASS verdict, so the column stays blank until real
    # 405 M22 limits are qualified and the gate is re-enabled.
    sensitivity_gate = (
        ""
        if raw_sensitivity_mv is None or not LOW_SENSITIVITY_FAILURE_ENABLED
        else sensitivity_gate_outcome(raw_sensitivity_mv, filter_setup)
    )
    sensitivity_fail_below_mv, sensitivity_pass_above_mv = sensitivity_raw_limits_mv(
        filter_setup
    )
    if final_result.passed:
        failure_mode_tag, failure_mode_reason = "", ""
    else:
        failure_mode_tag, failure_mode_reason = split_failure_mode(
            failure_mode or suggest_failure_mode(final_result)
        )
    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "batch_number": batch_number,
        "sensor_number": str(sensor_number),
        "sensor_id": sensor_id,
        "tester_name": tester_name,
        "model": MODEL_NAME,
        "filter_setup": filter_setup,
        "pwm_channel": pwm_channel,
        "pwm_hz": f"{pwm_hz:g}",
        "pwm_duty": f"{pwm_duty:g}",
        "offset_v": "" if final_result.offset_v is None else f"{final_result.offset_v:.6f}",
        "offset_initial_v": _fmt_optional_float(offset_initial_v, 5),
        "sensitivity_mv": "" if final_result.sensitivity_mv is None else f"{final_result.sensitivity_mv:.6f}",
        "sensitivity_raw_mv": "" if raw_sensitivity_mv is None else f"{raw_sensitivity_mv:.6f}",
        "sensitivity_legacy_equivalent_mv": _fmt_optional_float(
            equivalent_sensitivity_mv, 6
        ),
        "sensitivity_correction_factor": f"{SENSITIVITY_LEGACY_EQUIVALENT_FACTOR:.6f}",
        "sensitivity_calibration_id": SENSITIVITY_CALIBRATION_ID,
        "sensitivity_gate_outcome": sensitivity_gate,
        "sensitivity_raw_fail_below_mv": f"{sensitivity_fail_below_mv:.6f}",
        "sensitivity_raw_pass_above_mv": f"{sensitivity_pass_above_mv:.6f}",
        "polarity": final_result.polarity,
        "polarity_good_bad": polarity_good_bad(final_result.polarity),
        "pass_fail": outcome,
        "fail_reasons": "; ".join(final_result.fail_reasons),
        "failure_mode_tag": failure_mode_tag,
        "failure_mode_reason": failure_mode_reason,
        "operator_comments": comment.strip(),
        "waveform_snapshot_paths": "; ".join(str(path) for path in snapshot_paths),
        "measure_attempts": str(measure_attempts),
        "skip_count": str(skip_count),
        "battery_v": "" if battery_v is None else f"{battery_v:.3f}",
        "noise_rms_mv": _fmt_optional_float(metrics.noise_rms_mv if metrics else None, 4),
        "snr_db": _fmt_optional_float(metrics.signal_to_noise_db if metrics else None, 2),
        "reference_calibrated_at": (
            reference_calibration.recorded_at if reference_calibration else ""
        ),
        "reference_calibration_mv": _fmt_optional_float(
            reference_calibration.mean_mv if reference_calibration else None, 4
        ),
        "reference_lower_mv": _fmt_optional_float(
            reference_calibration.lower_mv if reference_calibration else None, 4
        ),
        "reference_upper_mv": _fmt_optional_float(
            reference_calibration.upper_mv if reference_calibration else None, 4
        ),
        "reference_check_mv": _fmt_optional_float(reference_check_mv, 4),
        "reference_drift_pct": _fmt_optional_float(
            reference_calibration.drift_percent(reference_check_mv)
            if reference_calibration and reference_check_mv is not None
            else None,
            3,
        ),
    }
    if capture_report is not None:
        row.update(capture_report.csv_fields())
    if noise_report is not None:
        row.update(noise_report.csv_fields())
    # Batch CSVs created before a column was added keep their original header;
    # write only the columns that file already has so rows stay aligned.
    fieldnames = CSV_FIELDS
    if not write_header:
        try:
            with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
                existing = next(csv.reader(csv_file), None)
            if existing:
                fieldnames = existing
        except Exception:
            pass
    row = {name: row.get(name, "") for name in fieldnames}
    with csv_path.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# --------------------------------------------------------------------------- #
# Waveform snapshot PNG (self-contained, with a matplotlib upgrade if present)
# --------------------------------------------------------------------------- #
def plot_text_line(text: str, max_chars: int = 180) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max(0, max_chars - 3)].rstrip() + "..."


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def write_rgb_png(path: Path, width: int, height: int, pixels: bytearray, text_chunks: dict[str, str] | None = None) -> None:
    rows = []
    stride = width * 3
    for y in range(height):
        rows.append(b"\x00" + bytes(pixels[y * stride : (y + 1) * stride]))
    with path.open("wb") as png_file:
        png_file.write(b"\x89PNG\r\n\x1a\n")
        png_file.write(png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        for key, value in (text_chunks or {}).items():
            safe_key = "".join(ch for ch in key if 32 <= ord(ch) <= 126).strip()[:79] or "Comment"
            safe_value = value.replace("\x00", " ")
            png_file.write(png_chunk(b"tEXt", safe_key.encode("latin-1", "replace") + b"\x00" + safe_value.encode("utf-8", "replace")))
        png_file.write(png_chunk(b"IDAT", zlib.compress(b"".join(rows), level=6)))
        png_file.write(png_chunk(b"IEND", b""))


def set_rgb_pixel(pixels: bytearray, width: int, height: int, x: int, y: int, color: tuple[int, int, int]) -> None:
    if x < 0 or x >= width or y < 0 or y >= height:
        return
    idx = (y * width + x) * 3
    pixels[idx : idx + 3] = bytes(color)


def draw_rgb_line(pixels: bytearray, width: int, height: int, x0: float, y0: float, x1: float, y1: float, color: tuple[int, int, int]) -> None:
    x0_i, y0_i, x1_i, y1_i = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
    dx = abs(x1_i - x0_i)
    dy = -abs(y1_i - y0_i)
    sx = 1 if x0_i < x1_i else -1
    sy = 1 if y0_i < y1_i else -1
    err = dx + dy
    while True:
        set_rgb_pixel(pixels, width, height, x0_i, y0_i, color)
        if x0_i == x1_i and y0_i == y1_i:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0_i += sx
        if e2 <= dx:
            err += dx
            y0_i += sy


def draw_rgb_rect_outline(pixels: bytearray, width: int, height: int, left: int, top: int, right: int, bottom: int, color: tuple[int, int, int]) -> None:
    draw_rgb_line(pixels, width, height, left, top, right, top, color)
    draw_rgb_line(pixels, width, height, right, top, right, bottom, color)
    draw_rgb_line(pixels, width, height, right, bottom, left, bottom, color)
    draw_rgb_line(pixels, width, height, left, bottom, left, top, color)


def draw_signal_trace(pixels: bytearray, width: int, height: int, signal: np.ndarray, left: int, top: int, right: int, bottom: int, color: tuple[int, int, int]) -> None:
    signal = np.asarray(signal, dtype=float)
    if signal.size < 2:
        return
    signal_min = float(np.min(signal))
    signal_max = float(np.max(signal))
    if abs(signal_max - signal_min) < 1e-9:
        signal_max = signal_min + 1.0
    plot_width = max(1, right - left)
    plot_height = max(1, bottom - top)
    previous_x = left
    previous_y = bottom - (float(signal[0]) - signal_min) / (signal_max - signal_min) * plot_height
    for idx in range(1, signal.size):
        x = left + idx / max(1, signal.size - 1) * plot_width
        y = bottom - (float(signal[idx]) - signal_min) / (signal_max - signal_min) * plot_height
        draw_rgb_line(pixels, width, height, previous_x, previous_y, x, y, color)
        previous_x = x
        previous_y = y


def save_waveform_snapshot_fallback_png(snapshot_path: Path, metrics: WaveformMetrics, title: str, detail_lines: list[str]) -> None:
    width, height = 1000, 620
    pixels = bytearray([255, 255, 255] * width * height)
    grid = (226, 232, 240)
    axis = (71, 85, 105)
    wave_color = (2, 132, 199)
    sync_color = (202, 138, 4)
    wave_box = (60, 45, width - 28, 385)
    sync_box = (60, 435, width - 28, height - 36)
    for left, top, right, bottom in (wave_box, sync_box):
        draw_rgb_rect_outline(pixels, width, height, left, top, right, bottom, axis)
        for step in range(1, 5):
            x = left + (right - left) * step // 5
            y = top + (bottom - top) * step // 5
            draw_rgb_line(pixels, width, height, x, top, x, bottom, grid)
            draw_rgb_line(pixels, width, height, left, y, right, y, grid)
    draw_signal_trace(pixels, width, height, metrics.waveform_v, *wave_box, wave_color)
    if metrics.sync_v.size:
        draw_signal_trace(pixels, width, height, metrics.sync_v, *sync_box, sync_color)
    metadata = {
        "Title": title,
        "Details": "\n".join(detail_lines),
        "Sample rate": f"{metrics.sample_rate_hz:.6g} Hz",
        "AIN0": "Top trace (buffered sensor)",
        "SYNC": "Bottom trace (ESP32 PWM state)",
    }
    write_rgb_png(snapshot_path, width, height, pixels, metadata)


def unused_snapshot_path(snapshot_dir: Path, sensor_id: str, filename_suffix: str) -> Path:
    """Return a new snapshot path without replacing an earlier capture."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    stem = f"{safe_filename_part(sensor_id)}_{timestamp}_{safe_filename_part(filename_suffix)}"
    candidate = snapshot_dir / f"{stem}.png"
    duplicate_number = 2
    while candidate.exists():
        candidate = snapshot_dir / f"{stem}_{duplicate_number}.png"
        duplicate_number += 1
    return candidate


def save_waveform_snapshot_image(batch_number: str, sensor_id: str, metrics: WaveformMetrics | None, title: str, detail_lines: list[str], filename_suffix: str) -> Path | None:
    if metrics is None or metrics.waveform_v.size == 0:
        return None
    snapshot_dir = results_root_dir() / "waveform_snapshots" / f"lot_{safe_filename_part(batch_number)}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = unused_snapshot_path(snapshot_dir, sensor_id, filename_suffix)
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        time_axis = np.arange(metrics.waveform_v.size, dtype=float) / max(metrics.sample_rate_hz, 1.0)
        fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
        fig.suptitle(title)
        axes[0].plot(time_axis, metrics.waveform_v, color="#0284c7", linewidth=1.0)
        axes[0].set_ylabel("AIN0 V (sensor)")
        axes[0].grid(True, alpha=0.25)
        axes[1].plot(time_axis[: metrics.sync_v.size], metrics.sync_v, color="#ca8a04", linewidth=1.0)
        axes[1].set_ylabel("PWM sync (0/1)")
        axes[1].set_xlabel("Seconds")
        axes[1].grid(True, alpha=0.25)
        if detail_lines:
            axes[0].text(
                0.01,
                0.98,
                "\n".join(detail_lines),
                transform=axes[0].transAxes,
                va="top",
                ha="left",
                fontsize=9,
                bbox={"facecolor": "white", "alpha": 0.85, "edgecolor": "#cbd5e1"},
            )
        fig.tight_layout()
        fig.savefig(snapshot_path, dpi=140)
        plt.close(fig)
    except Exception:
        save_waveform_snapshot_fallback_png(snapshot_path, metrics, title, detail_lines)
        snapshot_path.with_suffix(".txt").write_text(title + "\n" + "\n".join(detail_lines) + "\n", encoding="utf-8")
    return snapshot_path


def save_stability_diagnostic_csvs(
    snapshot_path: Path,
    *,
    batch_number: str,
    sensor_id: str,
    metrics: WaveformMetrics,
    report: "StabilityCaptureReport",
) -> list[Path]:
    """Persist the full production stream and every robust-peak decision.

    The files share the collision-safe PNG stem, making a timeout snapshot a
    self-contained troubleshooting bundle without widening the batch CSV with
    hundreds of cycle columns.
    """

    samples_path = snapshot_path.with_name(snapshot_path.stem + "_samples.csv")
    cycles_path = snapshot_path.with_name(snapshot_path.stem + "_cycles.csv")
    sample_rate_hz = max(float(metrics.sample_rate_hz), 1.0)
    waveform = np.asarray(metrics.waveform_v, dtype=float)
    sync = np.asarray(metrics.sync_v, dtype=float)
    with samples_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=STABILITY_SAMPLE_DIAGNOSTIC_FIELDS,
        )
        writer.writeheader()
        for index, voltage_v in enumerate(waveform):
            writer.writerow(
                {
                    "batch_number": batch_number,
                    "sensor_id": sensor_id,
                    "sample_index": index,
                    "pwm_elapsed_s": (
                        f"{report.pwm_elapsed_offset_s + index / sample_rate_hz:.9f}"
                    ),
                    "voltage_v": f"{float(voltage_v):.12g}",
                    "sync": "" if index >= len(sync) else f"{float(sync[index]):g}",
                }
            )

    with cycles_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=STABILITY_CYCLE_DIAGNOSTIC_FIELDS,
        )
        writer.writeheader()
        for cycle in report.cycle_diagnostics:
            writer.writerow(
                {
                    "batch_number": batch_number,
                    "sensor_id": sensor_id,
                    **cycle.as_dict(),
                }
            )
    return [samples_path, cycles_path]


def save_waveform_diagnostic_bundle(
    batch_number: str,
    sensor_id: str,
    metrics: WaveformMetrics,
    report: "StabilityCaptureReport | None",
    *,
    title: str,
    detail_lines: list[str],
    filename_suffix: str,
) -> list[Path]:
    snapshot_path = save_waveform_snapshot_image(
        batch_number,
        sensor_id,
        metrics,
        title,
        detail_lines,
        filename_suffix,
    )
    if snapshot_path is None:
        return []
    paths = [snapshot_path]
    if report is not None:
        paths.extend(
            save_stability_diagnostic_csvs(
                snapshot_path,
                batch_number=batch_number,
                sensor_id=sensor_id,
                metrics=metrics,
                report=report,
            )
        )
    return paths


def save_raw_noise_capture(
    batch_number: str,
    sensor_id: str,
    waveform_v: np.ndarray,
    sample_rate_hz: float,
    *,
    metadata: dict[str, object] | None = None,
    left_context_v: np.ndarray | None = None,
    right_context_v: np.ndarray | None = None,
) -> list[Path]:
    """Persist one part's RAW emitter-off noise capture for offline analysis.

    Saved on request per part (and automatically for noise failures) so spike
    morphology - rise time, width, amplitude distribution - can be studied
    from the full-bandwidth 1000 SPS record rather than the band-limited
    50 SPS trace the verdict uses (the band-limited trace is recomputable
    from this raw record, but not the other way around). Two files per
    capture with the same stem: a plain CSV (sample,t_s,volts with one
    leading '#' metadata line) and an NPZ carrying the same array plus
    metadata for numpy work. Since 2026-08-31 the NPZ also carries the
    edge-context slices (``left_context_v``/``right_context_v``) when the
    caller has them, so an offline replay can seat the anti-alias FIR
    exactly like the live verdict did; the CSV stays capture-only.
    """
    waveform = np.asarray(waveform_v, dtype=float)
    if waveform.size == 0:
        return []
    capture_dir = (
        results_root_dir() / "noise_captures" / f"lot_{safe_filename_part(batch_number)}"
    )
    capture_dir.mkdir(parents=True, exist_ok=True)
    base = f"{safe_filename_part(sensor_id)}_noise_raw"
    stem = base
    counter = 2
    while (capture_dir / f"{stem}.csv").exists() or (capture_dir / f"{stem}.npz").exists():
        stem = f"{base}_{counter}"
        counter += 1
    meta: dict[str, object] = {
        "batch_number": batch_number,
        "sensor_id": sensor_id,
        "sample_rate_hz": float(sample_rate_hz),
        "samples": int(waveform.size),
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "noise_pp_limit_mv": NOISE_PP_LIMIT_MV,
        "noise_decimation_factor": NOISE_DECIMATION_FACTOR,
    }
    if metadata:
        meta.update(metadata)
    csv_path = capture_dir / f"{stem}.csv"
    meta_text = "; ".join(f"{key}={value}" for key, value in meta.items())
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        handle.write(f"# {meta_text}\n")
        writer = csv.writer(handle)
        writer.writerow(["sample", "t_s", "volts"])
        rate = max(float(sample_rate_hz), 1.0)
        for index, volts in enumerate(waveform):
            writer.writerow([index, f"{index / rate:.6f}", f"{volts:.7f}"])
    npz_path = capture_dir / f"{stem}.npz"
    context_arrays: dict[str, np.ndarray] = {}
    for key, values in (
        ("left_context_v", left_context_v),
        ("right_context_v", right_context_v),
    ):
        if values is None:
            continue
        array = np.asarray(values, dtype=float)
        if array.size:
            context_arrays[key] = array
    np.savez_compressed(
        npz_path,
        waveform_v=waveform,
        sample_rate_hz=float(sample_rate_hz),
        **context_arrays,
        **{key: np.asarray(str(value)) for key, value in meta.items() if key != "sample_rate_hz"},
    )
    return [csv_path, npz_path]


def snapshot_detail_lines(
    batch_number: str,
    sensor_id: str,
    metrics: WaveformMetrics,
    comment: str = "",
    report: "StabilityCaptureReport | None" = None,
) -> list[str]:
    sensitivity_text = "Not measured (unstable)"
    raw_sensitivity_text = ""
    if metrics.stabilized:
        sensitivity_text = (
            f"{legacy_equivalent_sensitivity_mv(metrics.sensitivity_mv):.2f} mV "
            "legacy-equivalent"
        )
        raw_sensitivity_text = f"{metrics.sensitivity_mv:.3f} mV raw ESP32"
    polarity_text = (
        f"{metrics.polarity} ({polarity_good_bad(metrics.polarity)})"
        if metrics.stabilized
        else "Not measured"
    )
    lines = [
        f"Batch: {batch_number}",
        f"Sensor: {sensor_id}",
        f"Sensitivity: {sensitivity_text}",
        f"Polarity: {polarity_text}",
    ]
    if raw_sensitivity_text:
        lines.insert(3, f"Raw sensitivity: {raw_sensitivity_text}")
    if metrics.offset_v is not None:
        lines.insert(2, f"Offset: {metrics.offset_v:.3f} V")
    if metrics.measured_frequency_hz is not None:
        lines.append(f"PWM sync: {metrics.measured_frequency_hz:.3f} Hz")
    if report is not None:
        state = "stabilized" if report.stabilized else "unstable"
        lines.append(
            f"Stability: {state}; attempt {report.measurement_attempt}; "
            f"{report.active_required_deltas} deltas <= "
            f"{report.threshold_mv:.3f} mV"
        )
        if report.stabilization_seconds is not None:
            lines.append(
                f"Stable at cycle {report.stabilization_cycle}, "
                f"{report.stabilization_seconds:.3f} s after PWM on"
            )
        if report.last_peak_delta_mv is not None:
            lines.append(f"Last observed peak delta: {report.last_peak_delta_mv:.6f} mV")
    if comment.strip():
        lines.append("Comment: " + plot_text_line(comment, 220))
    return lines


# --------------------------------------------------------------------------- #
# ESP32 + ADS1256 device adapter for the v4-compatible measurement engine
# --------------------------------------------------------------------------- #
class StreamIntegrityError(Esp32BackendError):
    """A capture was rejected because the serial stream was unreliable.

    Nothing is recorded from such a capture, and on Windows the trigger is
    typically a transient USB/driver hiccup, so callers with time budget may
    retry the whole capture a bounded number of times (see
    ``call_with_stream_retries``).
    """


def call_with_stream_retries(
    capture,
    *,
    retries: int = REFERENCE_READING_STREAM_RETRIES,
    on_retry=None,
):
    """Run ``capture(attempt)``, retrying only transient stream failures.

    ``capture`` receives the zero-based attempt number. Only
    ``StreamIntegrityError`` is retried — hardware faults, cancellation, and
    every other error propagate immediately, as does the final integrity
    failure once ``retries`` extra attempts are exhausted.
    """

    attempt = 0
    while True:
        try:
            return capture(attempt)
        except StreamIntegrityError:
            attempt += 1
            if attempt > retries:
                raise
            if on_retry is not None:
                on_retry(attempt)


class StreamGapFiller:
    """Rebuild a contiguous sample timeline across tolerated serial micro-gaps.

    The Windows CP210x link occasionally drops a few samples even after the
    1 MiB-buffer/drain-thread fixes (USB scheduling level; noticeably more
    often with a laptop charger's EMI on the link). The integrity validator
    bounds that loss, but every downstream consumer — PWM sync cadence
    validation, cycle segmentation, noise windows, elapsed-time estimates —
    indexes the sample array assuming an unbroken 1 kS/s timeline, so a
    2-sample gap inside one 10 Hz reference cycle used to read as
    1000/98 = 10.204 Hz and fail the ±0.1 Hz sync check as a fake rig error.

    Each streamed sample carries the firmware's own ``timestamp_us``; when
    consecutive timestamps show a gap of 1..STREAM_MAX_MISSING_SAMPLES
    samples, the missing slots are refilled with linearly interpolated volts
    and a mid-gap sync transition (max cadence error ≈ gap/2 samples). Gaps
    beyond the budget are left unfilled and only counted — the integrity
    validator rejects those captures anyway. Samples without timestamps
    (tests, simulators) pass through untouched.
    """

    _UINT32_MASK = 0xFFFFFFFF

    def __init__(self, sample_rate_hz: float) -> None:
        self.period_us = 1_000_000.0 / float(sample_rate_hz)
        self.gap_count = 0
        self.filled_count = 0
        self.oversize_gap_count = 0
        self._last: StreamSample | None = None

    @property
    def saw_gaps(self) -> bool:
        return bool(self.gap_count or self.oversize_gap_count)

    def extend(self, chunk):
        """Return ``chunk`` with tolerated micro-gaps refilled in place."""
        out = []
        for sample in chunk:
            timestamp = getattr(sample, "timestamp_us", None)
            last = self._last
            last_timestamp = (
                None if last is None else getattr(last, "timestamp_us", None)
            )
            if timestamp is not None and last_timestamp is not None:
                delta_us = (int(timestamp) - int(last_timestamp)) & self._UINT32_MASK
                missing = int(round(delta_us / self.period_us)) - 1
                if missing >= 1:
                    if missing <= STREAM_MAX_MISSING_SAMPLES:
                        self.gap_count += 1
                        self.filled_count += missing
                        for index in range(1, missing + 1):
                            # Sync flips at the gap midpoint so a transition
                            # swallowed by the gap lands within gap/2 samples
                            # of its true position.
                            take_new = index > missing / 2.0
                            fraction = index / (missing + 1)
                            out.append(
                                StreamSample(
                                    timestamp_us=(
                                        int(last_timestamp)
                                        + int(round(index * self.period_us))
                                    )
                                    & self._UINT32_MASK,
                                    raw=0,
                                    volts=(
                                        last.volts
                                        + (sample.volts - last.volts) * fraction
                                    ),
                                    sync=int(sample.sync if take_new else last.sync),
                                )
                            )
                    else:
                        self.oversize_gap_count += 1
            out.append(sample)
            self._last = sample
        return out


class EmitterEsp32Rig(Esp32EmitterRig):
    """Add NumPy frame/adaptive-capture methods to the serial backend."""

    STREAM_CHUNK_SAMPLES = 100  # 0.1 s at 1000 SPS: responsive progress/early exit
    STREAM_TIMEOUT_S = 2.0

    @staticmethod
    def _sample_arrays(samples) -> tuple[np.ndarray, np.ndarray]:
        waveform = np.asarray([sample.volts for sample in samples], dtype=float)
        sync = np.asarray([sample.sync for sample in samples], dtype=float)
        return waveform, sync

    #: Human-readable note about micro-gap loss tolerated in the last
    #: validated capture, or None when that stream was perfectly clean.
    last_stream_tolerance_note: str | None = None

    def _validate_stream_diagnostics(self, diagnostics, *, minimum_samples: int) -> None:
        """Reject incomplete/corrupted streams instead of recording a verdict.

        Bounded micro-gap loss (see STREAM_MAX_MICRO_GAPS /
        STREAM_MAX_MISSING_SAMPLES) is tolerated and only noted on
        ``last_stream_tolerance_note`` — the Windows CP210x driver drops a few
        samples in rare USB-scheduling hiccups even with the enlarged receive
        buffer and the drain thread, and a handful of lost milliseconds does
        not change a 1 s-window noise verdict or a robust per-cycle peak.
        Anything beyond that budget, and every corruption signature
        (duplicates, reordering, torn lines, ADC overruns, rate error), still
        raises ``StreamIntegrityError`` with nothing recorded.
        """
        self.last_stream_tolerance_note = None
        problems: list[str] = []
        if diagnostics.received_samples < minimum_samples:
            problems.append(
                f"short capture ({diagnostics.received_samples}/{minimum_samples} samples)"
            )
        if diagnostics.torn_lines:
            problems.append(f"{diagnostics.torn_lines} malformed serial records")
        gaps = diagnostics.timestamp_gap_count
        missing = diagnostics.estimated_missing_samples
        if gaps and (
            gaps > STREAM_MAX_MICRO_GAPS or missing > STREAM_MAX_MISSING_SAMPLES
        ):
            problems.append(
                f"{gaps} timestamp gaps (~{missing} missing samples)"
            )
        if diagnostics.duplicate_timestamps:
            problems.append(f"{diagnostics.duplicate_timestamps} duplicate timestamps")
        if diagnostics.reordered_timestamps:
            problems.append(f"{diagnostics.reordered_timestamps} reordered timestamps")
        if diagnostics.firmware_adc_overruns:
            problems.append(
                f"{diagnostics.firmware_adc_overruns} ADC conversions overran the serial loop"
            )
        firmware_sent = getattr(diagnostics, "firmware_samples_sent", None)
        count_difference = (
            None
            if firmware_sent is None
            else firmware_sent - diagnostics.received_samples
        )
        if count_difference is not None and not (
            0 <= count_difference <= STREAM_MAX_MISSING_SAMPLES
        ):
            problems.append(
                "host/firmware sample counts differ "
                f"({diagnostics.received_samples}/{firmware_sent})"
            )
        rate_error = diagnostics.rate_error_percent
        if rate_error is not None and abs(rate_error) > 2.0:
            problems.append(
                f"sample rate is {diagnostics.measured_rate_hz:.1f} Hz "
                f"({rate_error:+.1f}% from expected)"
            )
        if problems:
            overflow_events = getattr(diagnostics, "driver_rx_overflow_events", 0)
            if overflow_events:
                # The Windows serial driver itself reported that IT dropped
                # receive data (CE_RXOVER/CE_OVERRUN): the computer stalled
                # reading, the cable is not the problem. Say so, with the two
                # operator remedies that address the real mechanism.
                advice = (
                    f". The Windows serial driver reported its receive queue "
                    f"overflowed {overflow_events} time(s) — the computer was "
                    "too slow to read the stream. Keep this window visible "
                    "during the capture, plug the laptop into power, then retry."
                )
            else:
                advice = (
                    ". Check the USB cable and close other serial programs, "
                    "then retry."
                )
            raise StreamIntegrityError(
                "ESP32 waveform stream was not reliable; nothing was recorded: "
                + "; ".join(problems)
                + advice
            )
        if gaps or count_difference:
            self.last_stream_tolerance_note = (
                f"Tolerated {gaps} serial micro-gap(s), ~{missing} of "
                f"{diagnostics.received_samples} samples lost (within the "
                f"{STREAM_MAX_MICRO_GAPS} gaps / {STREAM_MAX_MISSING_SAMPLES} "
                "samples budget)."
            )

    def read_waveform_frame(
        self,
        cycles: int,
        waveform_range_v: float,
        sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
        expected_frequency_hz: float = EXPECTED_FREQUENCY_HZ,
        channel: str = "sensor",
    ) -> tuple[np.ndarray, np.ndarray, float]:
        del waveform_range_v
        self.connect()
        target_scans = int(
            math.ceil((cycles / expected_frequency_hz) * sample_rate_hz)
        )
        samples = []
        header = self.start_stream(channel)
        if not math.isclose(header.sample_rate_hz, sample_rate_hz, rel_tol=0.01):
            try:
                self.stop_stream(timeout_s=self.STREAM_TIMEOUT_S)
            finally:
                raise Esp32BackendError(
                    f"ESP32 advertised {header.sample_rate_hz:g} samples/s; "
                    f"this tester requires {sample_rate_hz:g}. Re-flash "
                    f"{EXPECTED_FIRMWARE_PREFIX}2.0 or newer."
                )
        gap_filler = StreamGapFiller(float(header.sample_rate_hz))
        diagnostics = None
        try:
            while len(samples) < target_scans:
                chunk = gap_filler.extend(self.read_stream(
                    max_samples=min(self.STREAM_CHUNK_SAMPLES, target_scans - len(samples)),
                    timeout_s=self.STREAM_TIMEOUT_S,
                ))
                if not chunk:
                    raise Esp32BackendError(
                        f"ESP32 waveform stream stalled after {len(samples)}/{target_scans} samples."
                    )
                samples.extend(chunk)
        finally:
            if self.is_streaming:
                diagnostics = self.stop_stream(timeout_s=self.STREAM_TIMEOUT_S)
        if diagnostics is None:
            diagnostics = self.stream_diagnostics
        if diagnostics is None:
            raise Esp32BackendError("ESP32 stream diagnostics were unavailable.")
        self._validate_stream_diagnostics(
            diagnostics,
            minimum_samples=target_scans - gap_filler.filled_count,
        )
        waveform, sync = self._sample_arrays(samples[:target_scans])
        actual_scan_rate = diagnostics.measured_rate_hz or header.sample_rate_hz
        return waveform, sync, float(actual_scan_rate)

    def read_reference_until_stable(
        self,
        *,
        waveform_range_v: float,
        settings: StabilitySettings,
        pwm_started_monotonic: float,
        sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
        expected_frequency_hz: float = REFERENCE_PWM_FREQUENCY_HZ,
        progress=None,
        cancelled=None,
    ) -> tuple[np.ndarray, np.ndarray, float, StabilityAnalysis]:
        """Adaptively stabilize AIN1, then retain five fresh reference cycles.

        The reference unit is a 406MCA sensor, so the caller must have started
        the emitter at REFERENCE_PWM_FREQUENCY_HZ (10 Hz), not the DUT's 1 Hz.
        """
        return self.read_waveform_until_stable(
            waveform_range_v=waveform_range_v,
            settings=reference_stability_settings(settings),
            pwm_started_monotonic=pwm_started_monotonic,
            sample_rate_hz=sample_rate_hz,
            expected_frequency_hz=expected_frequency_hz,
            stability_timeout_s=STABILITY_TIMEOUT_S,
            measurement_cycles=REFERENCE_MEASUREMENT_CYCLES,
            progress=progress,
            cancelled=cancelled,
            channel="ref",
        )

    def read_waveform_until_stable(
        self,
        *,
        waveform_range_v: float,
        settings: StabilitySettings,
        pwm_started_monotonic: float,
        sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
        expected_frequency_hz: float = EXPECTED_FREQUENCY_HZ,
        stability_timeout_s: float = STABILITY_TIMEOUT_S,
        measurement_cycles: int = SENSITIVITY_MEASUREMENT_CYCLES,
        progress=None,
        preview=None,
        cancelled=None,
        channel: str = "sensor",
    ) -> tuple[np.ndarray, np.ndarray, float, StabilityAnalysis]:
        """Capture one uninterrupted PWM-on stream through stability/measurement.

        The same samples drive sync validation, peak-delta progress, optional
        live preview, and the final result. The stability deadline is measured
        from PWM activation; a started measurement window may finish afterward.
        """
        del waveform_range_v
        self.connect()
        samples = []
        header = self.start_stream(channel)
        if not math.isclose(header.sample_rate_hz, sample_rate_hz, rel_tol=0.01):
            try:
                self.stop_stream(timeout_s=self.STREAM_TIMEOUT_S)
            finally:
                raise Esp32BackendError(
                    f"ESP32 advertised {header.sample_rate_hz:g} samples/s; "
                    f"this tester requires {sample_rate_hz:g}. Re-flash {EXPECTED_FIRMWARE_PREFIX}2.0 or newer."
                )
        actual_scan_rate = float(header.sample_rate_hz)
        gap_filler = StreamGapFiller(actual_scan_rate)
        pwm_elapsed_offset_s = max(0.0, time.monotonic() - pwm_started_monotonic)
        is_reference = str(channel).lower() in {"ref", "reference", "ain1"}
        enforce_retry_policy = not is_reference
        analysis_settings = (
            dut_stability_settings(settings) if enforce_retry_policy else settings
        )
        maximum_measurement_cycles = measurement_cycles
        # Enough room for a final measurement window that begins at the
        # stability deadline, plus two edge-closing cycles. This is only a
        # safety ceiling; the state machine normally stops earlier.
        max_stream_s = max(0.0, stability_timeout_s - pwm_elapsed_offset_s) + (
            maximum_measurement_cycles + 2
        ) / expected_frequency_hz
        # N samples span N-1 intervals, so include the sample at the safety
        # ceiling rather than stopping one conversion before it.
        target_scans = int(math.ceil(max_stream_s * actual_scan_rate)) + 1
        diagnostics = None
        stream_data_source = "esp32_reference" if is_reference else "esp32_405m22"
        analysis = analyze_stability(
            [], [], actual_scan_rate, analysis_settings,
            pwm_elapsed_offset_s=pwm_elapsed_offset_s,
            stability_deadline_s=stability_timeout_s,
            measurement_cycles_required=measurement_cycles,
            enforce_measurement_stability=enforce_retry_policy,
            max_measurement_attempts=MAX_MEASUREMENT_ATTEMPTS,
            data_source=stream_data_source,
        )
        sync_validated = False
        samples_through_deadline = max(
            1,
            int(
                math.ceil(
                    max(0.0, stability_timeout_s - pwm_elapsed_offset_s)
                    * actual_scan_rate
                )
            )
            + 1,
        )
        # Re-analyzing the whole capture after every 0.1 s chunk walks an
        # ever-growing array while holding the GIL, which starves the serial
        # drain thread and (on Windows) risks receive-queue overflow. Analyze
        # every half PWM period instead — decisions happen on completed
        # cycles, so nothing is learned more often than that anyway.
        analysis_stride = max(
            self.STREAM_CHUNK_SAMPLES,
            int(round(0.5 * actual_scan_rate / expected_frequency_hz)),
        )
        volts_list: list[float] = []
        sync_list: list[float] = []
        samples_analyzed = 0
        try:
            while len(samples) < target_scans:
                if cancelled is not None and cancelled():
                    raise Esp32BackendError("Measurement was cancelled.")
                read_count = min(
                    self.STREAM_CHUNK_SAMPLES,
                    target_scans - len(samples),
                )
                if not analysis.report.stabilized:
                    # Approach the inclusive deadline exactly so the closing
                    # sample at 20.000 s is analyzed without a coarse serial
                    # chunk overshooting it.
                    read_count = min(
                        read_count,
                        max(1, samples_through_deadline - len(samples)),
                    )
                chunk = gap_filler.extend(self.read_stream(
                    max_samples=read_count,
                    timeout_s=self.STREAM_TIMEOUT_S,
                ))
                samples.extend(chunk)
                for sample in chunk:
                    volts_list.append(sample.volts)
                    sync_list.append(sample.sync)
                analysis_due = (
                    len(samples) - samples_analyzed >= analysis_stride
                    or len(samples) >= target_scans
                    or (
                        not analysis.report.stabilized
                        and len(samples) >= samples_through_deadline
                    )
                    # A stalled/ended stream gets one closing analysis pass:
                    # if the already-received samples complete the decision,
                    # use them rather than declaring a stall.
                    or (not chunk and len(samples) > samples_analyzed)
                )
                if not analysis_due:
                    if not chunk:
                        raise Esp32BackendError(
                            f"ESP32 waveform stream stalled after {len(samples)}/{target_scans} samples."
                        )
                    continue
                stream_ended = not chunk
                samples_analyzed = len(samples)
                waveform_np = np.asarray(volts_list, dtype=float)
                sync_np = np.asarray(sync_list, dtype=float)
                analysis = analyze_stability(
                    waveform_np,
                    sync_np,
                    actual_scan_rate,
                    analysis_settings,
                    pwm_elapsed_offset_s=pwm_elapsed_offset_s,
                    stability_deadline_s=stability_timeout_s,
                    measurement_cycles_required=measurement_cycles,
                    enforce_measurement_stability=enforce_retry_policy,
                    max_measurement_attempts=MAX_MEASUREMENT_ATTEMPTS,
                    data_source=stream_data_source,
                )
                captured_s = (
                    0.0 if not samples else (len(samples) - 1) / actual_scan_rate
                )
                if not sync_validated:
                    rising_edges = rising_edge_indices(sync_np)
                    validation_observation_limit_s = (
                        (SYNC_VALIDATION_CYCLES + 2) / expected_frequency_hz
                    )
                    if (
                        len(rising_edges) >= SYNC_VALIDATION_CYCLES + 1
                        or captured_s >= validation_observation_limit_s
                    ):
                        try:
                            validate_rising_sync_cycles(
                                sync_np,
                                actual_scan_rate,
                                expected_frequency_hz=expected_frequency_hz,
                                cycles_required=SYNC_VALIDATION_CYCLES,
                            )
                        except SyncValidationError as exc:
                            if gap_filler.saw_gaps:
                                # The validation window itself lost samples
                                # (micro-gap on/near a sync edge): that is a
                                # transient transport problem, not a PWM or
                                # wiring fault - reject the capture with
                                # nothing recorded so the caller's bounded
                                # retries take a fresh one.
                                raise StreamIntegrityError(
                                    "ESP32 waveform stream was not reliable; "
                                    "nothing was recorded: serial micro-gaps "
                                    "corrupted the PWM sync validation window "
                                    f"({exc})"
                                ) from exc
                            raise HardwareNotReadyError(
                                f"ESP32 {exc}. Check firmware and "
                                f"{EMITTER_PWM_CHANNEL}, then measure again."
                            ) from exc
                        sync_validated = True
                if progress is not None:
                    progress(analysis)
                if preview is not None:
                    preview(
                        waveform_np[-STREAM_PREVIEW_MAX_SAMPLES:].copy(),
                        sync_np[-STREAM_PREVIEW_MAX_SAMPLES:].copy(),
                    )
                if analysis.report.measurement_complete or analysis.report.unstable:
                    break
                if stream_ended:
                    raise Esp32BackendError(
                        f"ESP32 waveform stream stalled after {len(samples)}/{target_scans} samples."
                    )
        finally:
            if self.is_streaming:
                diagnostics = self.stop_stream(timeout_s=self.STREAM_TIMEOUT_S)

        if diagnostics is None:
            diagnostics = self.stream_diagnostics
        if diagnostics is None:
            raise Esp32BackendError("ESP32 stream diagnostics were unavailable.")
        # STREAM,STOP can drain a short tail that was sampled while PWM was
        # still on. Retain it so timeout troubleshooting gets the full stream
        # represented by the backend diagnostics.
        drained_samples = gap_filler.extend(list(self.drained_samples))
        if drained_samples:
            samples.extend(drained_samples)
        self._validate_stream_diagnostics(
            diagnostics,
            minimum_samples=len(samples) - gap_filler.filled_count,
        )
        waveform_np, sync_np = self._sample_arrays(samples)
        analysis = analyze_stability(
            waveform_np,
            sync_np,
            actual_scan_rate,
            analysis_settings,
            pwm_elapsed_offset_s=pwm_elapsed_offset_s,
            stability_deadline_s=stability_timeout_s,
            measurement_cycles_required=measurement_cycles,
            enforce_measurement_stability=enforce_retry_policy,
            max_measurement_attempts=MAX_MEASUREMENT_ATTEMPTS,
            data_source=stream_data_source,
        )
        if not sync_validated:
            raise HardwareNotReadyError(
                "ESP32 PWM sync could not be validated before the capture ended."
            )
        if not (analysis.report.measurement_complete or analysis.report.unstable):
            raise Esp32BackendError(
                "Adaptive capture reached its safety limit before producing a complete decision."
            )
        return waveform_np, sync_np, actual_scan_rate, analysis

    def read_noise_capture(
        self,
        *,
        sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
        min_wait_seconds: float = NOISE_WAIT_BEFORE_CAPTURE_S,
        max_wait_seconds: float = NOISE_WAIT_MAX_S,
        settle_delta_mv: float = NOISE_BASELINE_SETTLE_DELTA_MV,
        settle_blocks: int = NOISE_BASELINE_SETTLE_BLOCKS,
        capture_seconds: float = NOISE_CAPTURE_SECONDS,
        progress=None,
        preview=None,
        cancelled=None,
    ) -> tuple[np.ndarray, float, float, float, bool]:
        """Stream AIN0 with the emitter OFF: adaptive quiet wait, then capture.

        The PWM drive must already be off, so the sync bit stays 0 and none of
        the sync-cycle machinery applies. The quiet wait is adaptive
        (2026-08-13, user request): the capture starts once ``settle_blocks``
        consecutive 1 s block-mean deltas stay at/below ``settle_delta_mv``
        (earliest at ``min_wait_seconds``); if the DC level is still moving
        at ``max_wait_seconds`` the capture starts anyway — a slow baseline
        can NEVER fail the part from here, it only delays the start (the
        per-window detrend in the analysis absorbs what remains).
        ``progress`` receives ``(capturing, elapsed_s, baseline_delta_mv)``
        where ``capturing`` is False during the wait and ``baseline_delta_mv``
        is the latest per-second mean movement (None until two blocks exist).

        Returns ``(noise_waveform, left_context, right_context, actual_rate,
        wait_s, elapsed_s, baseline_settled)``. The contexts are the
        NOISE_EDGE_CONTEXT_SAMPLES raw samples immediately before/after the
        capture window (2026-08-31): the quiet wait always covers the left
        side (min 3 s >> 0.31 s) and the stream runs 0.31 s past the window
        for the right side. They seat the verdict's anti-alias FIR on real
        history instead of reflection padding; they are never part of the
        judged window itself.
        """
        self.connect()
        samples = []
        header = self.start_stream("sensor")
        if not math.isclose(header.sample_rate_hz, sample_rate_hz, rel_tol=0.01):
            try:
                self.stop_stream(timeout_s=self.STREAM_TIMEOUT_S)
            finally:
                raise Esp32BackendError(
                    f"ESP32 advertised {header.sample_rate_hz:g} samples/s; "
                    f"this tester requires {sample_rate_hz:g}. Re-flash "
                    f"{EXPECTED_FIRMWARE_PREFIX}2.0 or newer."
                )
        actual_scan_rate = float(header.sample_rate_hz)
        gap_filler = StreamGapFiller(actual_scan_rate)
        block_samples = max(5, int(round(NOISE_WINDOW_S * actual_scan_rate)))
        min_wait_samples = max(
            block_samples, int(math.ceil(min_wait_seconds * actual_scan_rate))
        )
        max_wait_samples = max(
            min_wait_samples, int(math.ceil(max_wait_seconds * actual_scan_rate))
        )
        capture_samples = max(5, int(math.ceil(capture_seconds * actual_scan_rate)))
        capture_start: int | None = None
        baseline_settled = False
        block_means: list[float] = []
        consecutive_quiet = 0
        last_delta_mv: float | None = None
        diagnostics = None
        volts_list: list[float] = []
        sync_list: list[float] = []
        samples_reported = 0
        # UI updates every half noise window; only the preview tail is
        # materialized per update (never the whole growing array), so the GIL
        # stays available to the serial drain thread on Windows.
        update_stride = max(
            self.STREAM_CHUNK_SAMPLES, int(round(0.5 * NOISE_WINDOW_S * actual_scan_rate))
        )
        try:
            while (
                capture_start is None
                or len(samples)
                < capture_start + capture_samples + NOISE_EDGE_CONTEXT_SAMPLES
            ):
                if cancelled is not None and cancelled():
                    raise Esp32BackendError("Measurement was cancelled.")
                if capture_start is None:
                    # Approach the next block boundary (or the wait deadline)
                    # exactly so the settle decision lands on whole blocks.
                    target = min(
                        max_wait_samples,
                        (len(block_means) + 1) * block_samples,
                    )
                else:
                    target = (
                        capture_start
                        + capture_samples
                        + NOISE_EDGE_CONTEXT_SAMPLES
                    )
                read_count = min(
                    self.STREAM_CHUNK_SAMPLES, max(1, target - len(samples))
                )
                chunk = gap_filler.extend(self.read_stream(
                    max_samples=read_count, timeout_s=self.STREAM_TIMEOUT_S
                ))
                if not chunk:
                    raise Esp32BackendError(
                        f"ESP32 noise stream stalled after {len(samples)} samples."
                    )
                samples.extend(chunk)
                for sample in chunk:
                    volts_list.append(sample.volts)
                    sync_list.append(sample.sync)
                while (
                    capture_start is None
                    and len(samples) // block_samples > len(block_means)
                ):
                    block_index = len(block_means)
                    block = volts_list[
                        block_index * block_samples : (block_index + 1) * block_samples
                    ]
                    block_mean = sum(block) / len(block)
                    if block_means:
                        last_delta_mv = abs(block_mean - block_means[-1]) * 1000.0
                        if last_delta_mv <= settle_delta_mv:
                            consecutive_quiet += 1
                        else:
                            consecutive_quiet = 0
                    block_means.append(block_mean)
                    boundary = (block_index + 1) * block_samples
                    if (
                        boundary >= min_wait_samples
                        and consecutive_quiet >= settle_blocks
                    ):
                        capture_start = boundary
                        baseline_settled = True
                    elif boundary >= max_wait_samples:
                        # Deadline: measure anyway. The report notes it and
                        # the per-window detrend absorbs the residual drift.
                        capture_start = boundary
                        baseline_settled = False
                if not (
                    len(samples) - samples_reported >= update_stride
                    or (
                        capture_start is not None
                        and len(samples)
                        >= capture_start
                        + capture_samples
                        + NOISE_EDGE_CONTEXT_SAMPLES
                    )
                ):
                    continue
                samples_reported = len(samples)
                elapsed_s = len(samples) / actual_scan_rate
                if progress is not None:
                    progress(capture_start is not None, elapsed_s, last_delta_mv)
                if preview is not None:
                    preview(
                        np.asarray(
                            volts_list[-STREAM_PREVIEW_MAX_SAMPLES:], dtype=float
                        ),
                        np.asarray(
                            sync_list[-STREAM_PREVIEW_MAX_SAMPLES:], dtype=float
                        ),
                    )
        finally:
            if self.is_streaming:
                diagnostics = self.stop_stream(timeout_s=self.STREAM_TIMEOUT_S)
        if diagnostics is None:
            diagnostics = self.stream_diagnostics
        if diagnostics is None:
            raise Esp32BackendError("ESP32 stream diagnostics were unavailable.")
        drained_samples = gap_filler.extend(list(self.drained_samples))
        if drained_samples:
            samples.extend(drained_samples)
        self._validate_stream_diagnostics(
            diagnostics,
            minimum_samples=len(samples) - gap_filler.filled_count,
        )
        waveform_np, _sync_np = self._sample_arrays(samples)
        elapsed_s = len(samples) / actual_scan_rate
        noise_waveform = waveform_np[capture_start : capture_start + capture_samples]
        capture_end = capture_start + capture_samples
        left_context = waveform_np[
            max(0, capture_start - NOISE_EDGE_CONTEXT_SAMPLES) : capture_start
        ]
        right_context = waveform_np[
            capture_end : capture_end + NOISE_EDGE_CONTEXT_SAMPLES
        ]
        return (
            noise_waveform,
            left_context,
            right_context,
            actual_scan_rate,
            capture_start / actual_scan_rate,
            elapsed_s,
            baseline_settled,
        )


# --------------------------------------------------------------------------- #
# Battery watcher helpers
# --------------------------------------------------------------------------- #
class BatteryTooLowError(RuntimeError):
    """Raised mid-measurement when the battery is at/below the block threshold."""

    def __init__(self, battery_v: float) -> None:
        super().__init__(f"Battery too low to test: {battery_v:.2f} V (minimum {BATTERY_MIN_V:.1f} V).")
        self.battery_v = battery_v


class HardwareNotReadyError(RuntimeError):
    """Raised before/at the start of a measurement when the rig is not wired
    up (missing sensor, unwired battery divider, no PWM sync). Nothing is
    measured or recorded; the message tells the technician what to plug in."""

class NoSensorDetectedError(HardwareNotReadyError):
    """AIN0 floats like an empty slot. A shorted/dead part looks identical, so
    the UI asks the technician whether a sensor is loaded before deciding."""

    def __init__(self, message: str, offset_v: float) -> None:
        super().__init__(message)
        self.offset_v = offset_v


class ReferenceGateError(HardwareNotReadyError):
    """AIN1 is uncalibrated, invalidated, or outside its calibrated window."""


class ReferenceCheckFailedError(ReferenceGateError):
    def __init__(
        self,
        reading_mv: float,
        calibration: ReferenceCalibration,
        *,
        dut_offset_v: float | None = None,
    ) -> None:
        drift = calibration.drift_percent(reading_mv)
        if dut_offset_v is None:
            dut_detail = "The sensor under test was not read."
        else:
            dut_detail = (
                f"AIN0 was checked at {dut_offset_v:.3f} V, which is not above the "
                f"{OFFSET_MAX_V:.1f} V high-offset limit, so a high-offset sensor does not "
                "explain the reference failure."
            )
        super().__init__(
            f"Reference unit measured {reading_mv:.2f} mV, {drift:+.1f}% from the "
            f"{calibration.mean_mv:.2f} mV calibration. The allowed range is "
            f"{calibration.lower_mv:.2f}-{calibration.upper_mv:.2f} mV "
            f"(+/-{calibration.tolerance_percent:g}%). {dut_detail} "
            "Replace/check the emitter, then recalibrate the reference unit before testing another sensor."
        )
        self.reading_mv = reading_mv
        self.calibration = calibration
        self.dut_offset_v = dut_offset_v


def high_offset_dut_explains_reference_failure(dut_offset_v: float) -> bool:
    """Return whether the sensor under test explains a failed reference gate.

    A high-offset part - AIN0 above the TP412 limit, typically railed at the
    ~5 V ADC full scale - couples into the AIN1 reference channel and can
    push the reference reading out of its window (either direction) or keep
    it from stabilizing. Swapping in a good part restores the reference, so
    such a failure condemns the part, not the fixture: the part records an
    immediate high-offset FAIL and the reference calibration stays intact.
    The check is one-sided on the DUT: in-band, low, or missing (near-0 V)
    sensors never suppress a reference failure.
    """
    return math.isfinite(dut_offset_v) and dut_offset_v > OFFSET_MAX_V


def battery_state_for(battery_v: float | None) -> str:
    """Classify a battery reading: 'fault', 'low', 'warn', 'ok', or 'unknown'.

    'fault' means the number cannot be a real battery through the divider
    (missing battery / divider not wired / floating input) - testing is blocked.
    """
    if battery_v is None:
        return "unknown"
    if battery_v < BATTERY_FAULT_MIN_V or battery_v > BATTERY_FAULT_MAX_V:
        return "fault"
    if battery_v <= BATTERY_MIN_V:
        return "low"
    if battery_v <= BATTERY_WARN_V:
        return "warn"
    return "ok"


def battery_gauge_fraction(battery_v: float | None) -> float:
    """Map the battery reading to a 0..1 fill for the header battery gauge."""
    if battery_v is None:
        return 0.0
    span = BATTERY_GAUGE_FULL_V - BATTERY_MIN_V
    return max(0.0, min(1.0, (battery_v - BATTERY_MIN_V) / span))


def apply_signal_quality_gate(final: FinalResult, metrics: WaveformMetrics | None) -> FinalResult:
    """Fail captures that are mostly noise (e.g. the emitter is not being driven).

    ``evaluate_result`` can pass a capture on amplitude + polarity alone, but a
    dead/undriven emitter produces sensor noise that can still look "big enough".
    Here we require a real coherent signal above the noise, mutating ``final``
    in place so the fail reason flows through to the CSV, autosave and UI.
    """
    if metrics is None:
        return final
    snr = metrics.signal_to_noise_ratio
    if snr is None:
        reason = (
            "Signal quality could not be verified (SNR unavailable) - confirm the "
            "emitter is powered and driving before retesting."
        )
    elif math.isfinite(snr) and snr < MIN_SIGNAL_TO_NOISE_RATIO:
        db = metrics.signal_to_noise_db
        db_text = "" if db is None or not math.isfinite(db) else f", {db:.1f} dB"
        reason = (
            f"Signal-to-noise too low: SNR {snr:.2f}{db_text} (minimum "
            f"{MIN_SIGNAL_TO_NOISE_RATIO:.1f}). This looks like sensor noise with no "
            "emitter response - check the emitter drive."
        )
    else:
        return final
    if reason not in final.fail_reasons:
        final.fail_reasons.append(reason)
    final.passed = False
    return final


# --------------------------------------------------------------------------- #
# TP412 emitter-off noise test telemetry + gate
# --------------------------------------------------------------------------- #
@dataclass
class NoiseCaptureReport:
    """Verdict and telemetry for one emitter-off noise capture.

    ``outcome`` is PASS/FAIL for a completed capture, or SKIPPED when the test
    did not run (unstable DUT, disabled, simulator shortcut). These fields are
    distinct from the driven-capture ``noise_rms_mv``/``snr_db`` SNR metrics.
    """

    outcome: str = "SKIPPED"
    windows_total: int | None = None
    windows_over: int | None = None
    over_percent: float | None = None
    worst_pp_mv: float | None = None
    median_pp_mv: float | None = None
    clipped_windows: int | None = None
    # Fixed quiet wait streamed (and discarded) before the capture window.
    # The CSV column keeps its historical noise_settle_s name.
    settle_s: float | None = None
    capture_s: float | None = None
    pp_limit_mv: float = NOISE_PP_LIMIT_MV
    # Allowance the verdict was actually judged with, as a percent of
    # windows (2026-08-31: the soak records its absolute-3 equivalent 5%
    # here instead of this default 15% - it was previously hard-wired).
    max_over_percent: float = NOISE_MAX_OVER_FRACTION * 100.0
    # Sample rate of the band-limited trace the pk-pk rule ran on; documents
    # the analysis bandwidth alongside the recorded numbers.
    analysis_rate_hz: float | None = None
    # Whether the DC level settled before the capture window started (a
    # False here never fails the part - the capture simply began at the
    # wait deadline and the per-window detrend absorbed the residue).
    baseline_settled: bool | None = None
    skip_reason: str | None = None

    @classmethod
    def skipped(cls, reason: str) -> "NoiseCaptureReport":
        return cls(outcome="SKIPPED", skip_reason=str(reason))

    @classmethod
    def from_analysis(
        cls,
        analysis: NoiseAnalysis,
        *,
        settle_s: float,
        capture_s: float,
        analysis_rate_hz: float | None = None,
        baseline_settled: bool | None = None,
        max_over_fraction: float | None = None,
    ) -> "NoiseCaptureReport":
        return cls(
            outcome=OUTCOME_PASS if analysis.passed else OUTCOME_FAIL,
            windows_total=analysis.windows_total,
            windows_over=analysis.windows_over,
            over_percent=analysis.over_fraction * 100.0,
            worst_pp_mv=analysis.worst_pp_mv,
            median_pp_mv=analysis.median_pp_mv,
            clipped_windows=analysis.clipped_windows,
            settle_s=max(0.0, float(settle_s)),
            capture_s=max(0.0, float(capture_s)),
            analysis_rate_hz=(
                None if analysis_rate_hz is None else float(analysis_rate_hz)
            ),
            baseline_settled=(
                None if baseline_settled is None else bool(baseline_settled)
            ),
            max_over_percent=(
                NOISE_MAX_OVER_FRACTION * 100.0
                if max_over_fraction is None
                else float(max_over_fraction) * 100.0
            ),
        )

    def csv_fields(self) -> dict[str, str]:
        return {
            "noise_test_outcome": self.outcome,
            "noise_windows_total": "" if self.windows_total is None else str(self.windows_total),
            "noise_windows_over": "" if self.windows_over is None else str(self.windows_over),
            "noise_over_percent": _fmt_optional_float(self.over_percent, 1),
            # 6 decimals: the pin-level limit is sub-mV, so worst/median
            # land in the microvolt range.
            "noise_worst_pp_mv": _fmt_optional_float(self.worst_pp_mv, 6),
            "noise_median_pp_mv": _fmt_optional_float(self.median_pp_mv, 6),
            "noise_settle_s": _fmt_optional_float(self.settle_s, 3),
            "noise_capture_s": _fmt_optional_float(self.capture_s, 3),
            "noise_pp_limit_mv": f"{self.pp_limit_mv:.6f}",
            "noise_analysis_rate_hz": _fmt_optional_float(self.analysis_rate_hz, 1),
            "noise_baseline_settled": (
                ""
                if self.baseline_settled is None
                else ("YES" if self.baseline_settled else "NO")
            ),
        }


def apply_noise_gate(
    final: FinalResult, noise_report: "NoiseCaptureReport | None"
) -> FinalResult:
    """Fold a failed emitter-off noise capture into the production verdict.

    Mutates ``final`` in place like ``apply_signal_quality_gate`` so the fail
    reason flows through to the CSV, autosave, failure-mode suggestion, and UI.
    PASS/SKIPPED reports leave the verdict untouched.
    """
    if noise_report is None or noise_report.outcome != OUTCOME_FAIL:
        return final
    total = noise_report.windows_total or 0
    # 2026-08-31 fix: use the allowance the capture was actually judged
    # with. The old fixed NOISE_MAX_OVER_FRACTION told a 60 s soak FAIL
    # "allowed 9" while the soak rule correctly allowed the absolute 3.
    allowed = int(total * noise_report.max_over_percent / 100.0 + 1e-9)
    clipped = noise_report.clipped_windows or 0
    clip_text = (
        f" ({clipped} window(s) clipped at the ADC rail)" if clipped else ""
    )
    # Deliberately no voltage magnitudes: this reason is shown on the result
    # screen, where a pin-level µV figure invites a false comparison with the
    # legacy station's mV reading. Every level (worst, median, limit, over
    # percent) is still recorded in the dedicated noise_* CSV columns and in
    # the auto-saved failure snapshot.
    reason = (
        f"{NOISE_FAIL_REASON_PREFIX} too high: {noise_report.windows_over} of "
        f"{total} one-second windows exceeded the noise limit "
        f"(allowed {allowed}){clip_text}."
    )
    if reason not in final.fail_reasons:
        final.fail_reasons.append(reason)
    final.passed = False
    return final


# --------------------------------------------------------------------------- #
# V6.1 adaptive-stability capture telemetry
# --------------------------------------------------------------------------- #
@dataclass
class StabilityCaptureReport:
    threshold_mv: float
    required_deltas: int
    stabilized: bool = False
    timed_out: bool = False
    unstable: bool = False
    unstable_reason: str | None = None
    phase: str = "stabilizing"
    measurement_attempt: int = 1
    measurement_failures: int = 0
    active_required_deltas: int = 5
    measurement_cycles_required: int = 10
    stabilization_cycle: int | None = None
    stabilization_seconds: float | None = None
    confirming_max_delta_mv: float | None = None
    last_peak_delta_mv: float | None = None
    capture_cycles: int = 0
    measurement_cycles: int = 0
    pwm_on_seconds: float = 0.0
    pwm_elapsed_offset_s: float = 0.0
    data_source: str = ""
    cycle_diagnostics: tuple[CycleAnalysis, ...] = ()

    @classmethod
    def from_analysis(
        cls,
        analysis: StabilityAnalysis,
        *,
        data_source: str,
        pwm_on_seconds: float | None = None,
    ) -> "StabilityCaptureReport":
        source = analysis.report
        return cls(
            threshold_mv=source.configured_threshold_mv,
            required_deltas=source.configured_confirmation_count,
            stabilized=source.stabilized,
            timed_out=source.timed_out,
            unstable=source.unstable,
            unstable_reason=source.unstable_reason,
            phase=source.phase,
            measurement_attempt=source.measurement_attempt,
            measurement_failures=source.measurement_failures,
            active_required_deltas=source.active_confirmation_count,
            measurement_cycles_required=source.measurement_cycles_required,
            stabilization_cycle=source.stabilization_cycle,
            stabilization_seconds=source.stabilization_elapsed_s,
            confirming_max_delta_mv=source.confirming_window_max_delta_mv,
            last_peak_delta_mv=source.last_delta_mv,
            capture_cycles=source.capture_cycles,
            measurement_cycles=source.measurement_cycle_count,
            pwm_on_seconds=(
                source.total_pwm_on_seconds
                if pwm_on_seconds is None
                else max(0.0, float(pwm_on_seconds))
            ),
            pwm_elapsed_offset_s=source.pwm_elapsed_offset_s,
            data_source=data_source,
            cycle_diagnostics=analysis.cycles,
        )

    def csv_fields(self) -> dict[str, str]:
        return {
            "stabilized": "YES" if self.stabilized else "NO",
            "stability_timeout": "YES" if self.timed_out else "NO",
            "stability_threshold_mv": f"{self.threshold_mv:.6f}",
            "stability_required_deltas": str(self.required_deltas),
            "stabilization_cycle": "" if self.stabilization_cycle is None else str(self.stabilization_cycle),
            "stabilization_seconds": _fmt_optional_float(self.stabilization_seconds, 3),
            "stability_window_max_delta_mv": _fmt_optional_float(self.confirming_max_delta_mv, 6),
            "last_peak_delta_mv": _fmt_optional_float(self.last_peak_delta_mv, 6),
            "capture_cycles": str(self.capture_cycles),
            "measurement_cycles": str(self.measurement_cycles),
            "stability_phase": self.phase,
            "measurement_attempt": str(self.measurement_attempt),
            "measurement_failures": str(self.measurement_failures),
            "active_stability_required_deltas": str(self.active_required_deltas),
            "active_measurement_cycles_required": str(self.measurement_cycles_required),
            "pwm_on_seconds": f"{self.pwm_on_seconds:.3f}",
            "data_source": self.data_source,
        }


def analyze_v6_stable_measurement(
    waveform_v: np.ndarray,
    sync_v: np.ndarray,
    sample_rate_hz: float,
    analysis: StabilityAnalysis,
    *,
    offset_v: float,
    input_range_v: float,
) -> WaveformMetrics:
    """Apply production signal math to the successful 20-cycle window."""
    segments = analysis.measurement_segments
    expected_cycles = analysis.report.measurement_cycles_required
    if len(segments) != expected_cycles:
        raise ValueError(
            f"Stable measurement requires {expected_cycles} complete cycles; "
            f"received {len(segments)}."
        )
    first_start = segments[0][0]
    last_end = segments[-1][1]
    # Include the low sample before the first rising edge and the closing edge
    # after the last selected cycle so the shared edge detector sees them all.
    slice_start = max(0, first_start - 1)
    slice_end = min(len(waveform_v), last_end + 1)
    measured_waveform = waveform_v[slice_start:slice_end]
    measured_sync = sync_v[slice_start:slice_end]
    metrics = analyze_esp32_waveform(
        waveform_v=measured_waveform,
        sync_v=measured_sync,
        sample_rate_hz=sample_rate_hz,
        am502_gain=RIG_GAIN,
        sync_edge=PROCEDURE_SYNC_EDGE,
        stability_window_cycles=expected_cycles,
        settle_cycles=0,
        input_range_v=input_range_v,
    )
    if metrics.cycles_used != expected_cycles:
        raise Esp32BackendError(
            f"Selected stability window contained {metrics.cycles_used} analyzable cycles; "
            f"expected {expected_cycles}. Nothing was recorded."
        )
    metrics.warnings = [
        warning for warning in metrics.warnings
        if not warning.startswith("Waveform did not stabilize")
    ]
    metrics.stabilized = True
    metrics.stability_change_pct = None
    metrics.stabilization_cycle = analysis.report.stabilization_cycle
    metrics.ignored_initial_cycles = analysis.report.stabilization_cycle or 0
    metrics.offset_v = offset_v
    # Keep the complete PWM-on transient available for the result scope and
    # troubleshooting snapshot while the numerical metrics remain restricted
    # to the selected post-stability cycles above.
    full_edges, full_frequency, full_sync_warnings = find_sync_edges(
        sync_v,
        sample_rate_hz,
        expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
        edge=PROCEDURE_SYNC_EDGE,
    )
    metrics.waveform_v = waveform_v
    metrics.sync_v = sync_v
    metrics.edges = full_edges
    metrics.measured_frequency_hz = full_frequency
    for warning in full_sync_warnings:
        rewritten = warning.replace("blade sync", "ESP32 PWM sync").replace("Blade sync", "ESP32 PWM sync")
        if rewritten not in metrics.warnings:
            metrics.warnings.append(rewritten)
    return metrics


def build_stability_timeout_result(
    waveform_v: np.ndarray,
    sync_v: np.ndarray,
    sample_rate_hz: float,
    analysis: StabilityAnalysis,
    *,
    offset_v: float,
    input_range_v: float,
) -> tuple[WaveformMetrics, FinalResult]:
    """Build a diagnostic waveform plus a FAIL with no official signal result."""
    edges, frequency, sync_warnings = find_sync_edges(
        sync_v,
        sample_rate_hz,
        expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
        edge=PROCEDURE_SYNC_EDGE,
    )
    detail = analysis.report.unstable_reason
    if not detail:
        detail = (
            f"Waveform peak did not stabilize within {analysis.report.stability_deadline_s:.1f} s: "
            f"required {analysis.report.active_confirmation_count} consecutive peak deltas "
            f"at or below {analysis.report.configured_threshold_mv:.3f} mV."
        )
    reason = "Unstable: " + detail
    warnings = [
        warning.replace("blade sync", "ESP32 PWM sync").replace("Blade sync", "ESP32 PWM sync")
        for warning in sync_warnings
    ]
    metrics = WaveformMetrics(
        sensitivity_mv=0.0,
        sensitivity_amplified_mv=0.0,
        polarity="NOT MEASURED",
        measured_frequency_hz=frequency,
        cycles_used=0,
        offset_v=offset_v,
        all_cycle_pp_mv=[cycle.peak_to_peak_v * 1000.0 for cycle in analysis.cycles],
        stabilized=False,
        stabilization_cycle=None,
        warnings=warnings + [reason],
        edges=edges,
        waveform_v=waveform_v,
        sync_v=sync_v,
        sample_rate_hz=sample_rate_hz,
        ignored_initial_cycles=0,
        input_range_v=input_range_v,
    )
    fail_reasons = [reason]
    if not (OFFSET_MIN_V <= offset_v <= OFFSET_MAX_V):
        fail_reasons.insert(
            0,
            f"Offset out of range: {offset_v:.3f} V, expected {OFFSET_MIN_V:.1f} to {OFFSET_MAX_V:.1f} V.",
        )
    final = FinalResult(
        passed=False,
        offset_v=offset_v,
        sensitivity_mv=None,
        polarity="",
        fail_reasons=fail_reasons,
        warnings=warnings,
        waveform_metrics=metrics,
    )
    return metrics, final


def build_offset_failure_result(
    offset_v: float,
    *,
    input_range_v: float,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
) -> tuple[WaveformMetrics, FinalResult]:
    """Immediate FAIL for a TP412 offset violation - nothing else is measured.

    The offset gate is the first per-part step, and a part outside the
    0.8-3.0 V band fails on the spot: the noise and sensitivity steps are
    skipped entirely, so there is no waveform to attach.
    """
    reason = (
        f"Offset out of range: {offset_v:.3f} V, expected "
        f"{OFFSET_MIN_V:.1f} to {OFFSET_MAX_V:.1f} V."
    )
    note = (
        "Noise and sensitivity were not measured: the offset gate fails the "
        "part immediately."
    )
    metrics = WaveformMetrics(
        sensitivity_mv=0.0,
        sensitivity_amplified_mv=0.0,
        polarity="NOT MEASURED",
        measured_frequency_hz=None,
        cycles_used=0,
        offset_v=offset_v,
        stabilized=False,
        stabilization_cycle=None,
        warnings=[reason, note],
        edges=[],
        waveform_v=np.asarray([], dtype=float),
        sync_v=np.asarray([], dtype=float),
        sample_rate_hz=sample_rate_hz,
        ignored_initial_cycles=0,
        input_range_v=input_range_v,
    )
    final = FinalResult(
        passed=False,
        offset_v=offset_v,
        sensitivity_mv=None,
        polarity="",
        fail_reasons=[reason],
        warnings=[note],
        waveform_metrics=metrics,
    )
    return metrics, final


BAD_SENSOR_FAILURE_MODE = "SB - Sensor bad"


def build_no_output_sensor_result(
    offset_v: float,
    *,
    input_range_v: float,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
) -> tuple[WaveformMetrics, FinalResult]:
    """FAIL for a loaded sensor whose AIN0 floats like an empty slot.

    A shorted or dead part and a missing part read the same (near 0 V); the
    technician confirmed a part is in the rig, so it fails with no offset
    instead of being reported as a wiring error. Nothing else is measured.
    """
    reason = (
        f"No offset: AIN0 reads {offset_v:.3f} V with a sensor loaded "
        f"(expected at least {SENSOR_OFFSET_MIN_PLAUSIBLE_V:.2f} V) - shorted or dead part."
    )
    note = "Noise and sensitivity were not measured: the part presents no output."
    metrics = WaveformMetrics(
        sensitivity_mv=0.0,
        sensitivity_amplified_mv=0.0,
        polarity="NOT MEASURED",
        measured_frequency_hz=None,
        cycles_used=0,
        offset_v=offset_v,
        stabilized=False,
        stabilization_cycle=None,
        warnings=[reason, note],
        edges=[],
        waveform_v=np.asarray([], dtype=float),
        sync_v=np.asarray([], dtype=float),
        sample_rate_hz=sample_rate_hz,
        ignored_initial_cycles=0,
        input_range_v=input_range_v,
    )
    final = FinalResult(
        passed=False,
        offset_v=offset_v,
        sensitivity_mv=None,
        polarity="",
        fail_reasons=[reason],
        warnings=[note],
        waveform_metrics=metrics,
    )
    return metrics, final


def build_noise_failure_result(
    offset_v: float,
    noise_report: "NoiseCaptureReport",
    *,
    input_range_v: float,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
) -> tuple[WaveformMetrics, FinalResult]:
    """Immediate FAIL for a failed emitter-off noise test (no driven capture).

    The noise test now runs before the sensitivity capture, and a noisy part
    is rejected on the spot rather than first spending up to three 60 s
    stabilization attempts. The noise stream itself is preserved separately
    via ``build_noise_waveform_metrics``.
    """
    note = (
        "Sensitivity was not measured: the emitter-off noise test fails the "
        "part before the driven capture."
    )
    metrics = WaveformMetrics(
        sensitivity_mv=0.0,
        sensitivity_amplified_mv=0.0,
        polarity="NOT MEASURED",
        measured_frequency_hz=None,
        cycles_used=0,
        offset_v=offset_v,
        stabilized=False,
        stabilization_cycle=None,
        warnings=[note],
        edges=[],
        waveform_v=np.asarray([], dtype=float),
        sync_v=np.asarray([], dtype=float),
        sample_rate_hz=sample_rate_hz,
        ignored_initial_cycles=0,
        input_range_v=input_range_v,
    )
    final = FinalResult(
        passed=False,
        offset_v=offset_v,
        sensitivity_mv=None,
        polarity="",
        fail_reasons=[],
        warnings=[note],
        waveform_metrics=metrics,
    )
    final = apply_noise_gate(final, noise_report)
    return metrics, final


def build_noise_waveform_metrics(
    noise_waveform_v: np.ndarray,
    sample_rate_hz: float,
    *,
    offset_v: float,
    input_range_v: float,
) -> WaveformMetrics:
    """Diagnostic-only metrics wrapping the emitter-off noise capture.

    The emitter is off, so sync is a constant 0 and no cycle analysis applies.
    This exists purely so the noise stream can reuse the waveform snapshot
    plumbing when a noisy sensor's capture is preserved.
    """
    return WaveformMetrics(
        sensitivity_mv=0.0,
        sensitivity_amplified_mv=0.0,
        polarity="NOT MEASURED",
        measured_frequency_hz=None,
        cycles_used=0,
        offset_v=offset_v,
        stabilized=False,
        stabilization_cycle=None,
        warnings=[],
        edges=[],
        waveform_v=np.asarray(noise_waveform_v, dtype=float),
        sync_v=np.zeros(len(noise_waveform_v), dtype=float),
        sample_rate_hz=sample_rate_hz,
        ignored_initial_cycles=0,
        input_range_v=input_range_v,
    )


# --------------------------------------------------------------------------- #
# v6 UI toolkit - colors, easing, animation engine
# --------------------------------------------------------------------------- #
def hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(channel)))) for channel in rgb)


def mix_color(color_a: str, color_b: str, t: float) -> str:
    """Linear blend from color_a (t=0) to color_b (t=1)."""
    t = max(0.0, min(1.0, t))
    a = hex_to_rgb(color_a)
    b = hex_to_rgb(color_b)
    return rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def gradient_color(stops: list[str], t: float) -> str:
    """Sample a multi-stop gradient at position t in [0, 1]."""
    if len(stops) == 1:
        return stops[0]
    t = max(0.0, min(1.0, t)) * (len(stops) - 1)
    idx = min(int(t), len(stops) - 2)
    return mix_color(stops[idx], stops[idx + 1], t - idx)


def ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def ease_in_out(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


def ease_out_back(t: float) -> float:
    """Ease-out with a small overshoot - used for 'pop' effects."""
    c1 = 1.70158
    c3 = c1 + 1.0
    return 1.0 + c3 * (t - 1.0) ** 3 + c1 * (t - 1.0) ** 2


class Animator:
    """Single-clock animation engine (~60 fps, named + cancellable).

    Every animation is registered under a name; starting a new animation with
    the same name replaces the old one, and cancel_prefix() lets the app drop
    a whole family (e.g. every "step:*" animation when a step is torn down).
    Frame callbacks that raise TclError (their widget was destroyed) silently
    stop the animation, so callers never have to guard widget lifetime.

    ONE after() timer drives every active animation. This matters: scheduling
    a separate 15 ms timer per animation keeps the Tk event queue permanently
    busy, which starves Tk's idle queue - and geometry propagation/redraws run
    as idle tasks, so layout would visibly lag while animations play.
    """

    FRAME_MS = 16

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self._anims: dict[str, dict] = {}
        self._job: str | None = None

    def animate(
        self,
        name: str,
        duration_ms: int,
        on_frame,
        easing=ease_out_cubic,
        on_done=None,
        loop: bool = False,
        delay_ms: int = 0,
    ) -> None:
        self._anims[name] = {
            "start": time.perf_counter() + delay_ms / 1000.0,
            "duration": max(1, duration_ms) / 1000.0,
            "on_frame": on_frame,
            "easing": easing,
            "on_done": on_done,
            "loop": loop,
        }
        if self._job is None:
            self._job = self.root.after(self.FRAME_MS, self._tick)

    def _tick(self) -> None:
        self._job = None
        now = time.perf_counter()
        for name in list(self._anims):
            anim = self._anims.get(name)
            if anim is None:
                continue
            elapsed = (now - anim["start"]) / anim["duration"]
            if elapsed < 0.0:  # still in its start delay
                continue
            if anim["loop"]:
                finished = False
                raw_t = elapsed % 1.0
            else:
                finished = elapsed >= 1.0
                raw_t = 1.0 if finished else elapsed
            easing = anim["easing"]
            try:
                anim["on_frame"](easing(raw_t) if easing is not None else raw_t)
            except tk.TclError:
                self._anims.pop(name, None)
                continue
            if finished:
                self._anims.pop(name, None)
                if anim["on_done"] is not None:
                    try:
                        anim["on_done"]()
                    except tk.TclError:
                        pass
        if self._anims and self._job is None:
            self._job = self.root.after(self.FRAME_MS, self._tick)

    def cancel(self, name: str) -> None:
        self._anims.pop(name, None)

    def cancel_prefix(self, prefix: str) -> None:
        for name in [key for key in self._anims if key.startswith(prefix)]:
            self._anims.pop(name, None)

    def cancel_all(self) -> None:
        self._anims.clear()
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except (tk.TclError, ValueError):
                pass
            self._job = None


def rounded_rect_points(x0: float, y0: float, x1: float, y1: float, r: float) -> list[float]:
    """Point list for a smooth=True polygon that renders as a rounded rect."""
    r = max(1.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    return [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]


def draw_round_rect(canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float, r: float, **kwargs) -> int:
    return canvas.create_polygon(rounded_rect_points(x0, y0, x1, y1, r), smooth=True, **kwargs)


def draw_horizontal_gradient(canvas: tk.Canvas, x0: int, y0: int, x1: int, y1: int, stops: list[str], tags: str, step: int = 4) -> None:
    """Paint a horizontal multi-stop gradient as thin vertical line segments."""
    span = max(1, x1 - x0)
    for x in range(x0, x1, step):
        color = gradient_color(stops, (x - x0) / span)
        canvas.create_line(x, y0, x, y1, fill=color, width=step, tags=tags)


# --------------------------------------------------------------------------- #
# v6 UI toolkit - custom widgets
# --------------------------------------------------------------------------- #
# How the action bar gives up width, in order: it drops the keyboard
# hints first (the shortcuts keep working), then switches to compact
# wording, and only then makes the buttons smaller. Keeping the buttons
# big matters more on the rig than spelling every label out in full.
FOOTER_KEY_HINTS = (" (Enter)", " (Esc)")
FOOTER_SHORT_LABELS = {
    "Save + Next Sensor": "Save + Next",
    "Save + Exit Batch": "Save + Exit",
    "Calibrate reference unit to test": "Calibrate reference first",
    "Recharge battery to test": "Recharge battery",
    "Check wiring to test": "Check wiring",
    "Fix stability settings": "Fix settings",
}


class RoundButton(tk.Canvas):
    """Rounded, hover-animated button (site-style primary / outline / ghost)."""

    PALETTES = {
        "primary": {
            "fill": ELTEC_BLUE, "hover": ELTEC_BLUE_DEEP, "press": "#092e6d",
            "fg": "#ffffff", "outline": "",
            "disabled_fill": PRIMARY_DISABLED, "disabled_fg": "#eef2f7", "disabled_outline": "",
        },
        "outline": {
            "fill": CARD_BG, "hover": "#eaf0fc", "press": "#dbe6fa",
            "fg": ELTEC_BLUE_DARK, "outline": "#b6c6e8",
            "disabled_fill": PAGE_BG, "disabled_fg": "#aeb9c5", "disabled_outline": "#d7deea",
        },
        "ghost": {
            "fill": GHOST_BG, "hover": GHOST_HOVER, "press": "#c3cfe8",
            "fg": ELTEC_BLUE_DARK, "outline": "",
            "disabled_fill": PAGE_BG, "disabled_fg": "#aeb9c5", "disabled_outline": "",
        },
        # v2.0 footer: green = save / move on, amber = set the part aside.
        "success": {
            "fill": "#1f8a4c", "hover": "#176d3c", "press": "#125630",
            "fg": "#ffffff", "outline": "",
            "disabled_fill": "#b9d8c5", "disabled_fg": "#f1f7f3", "disabled_outline": "",
        },
        "warn": {
            "fill": "#fff4d6", "hover": "#ffe9b3", "press": "#ffd98a",
            "fg": "#8a5a00", "outline": "#e8b94a",
            "disabled_fill": PAGE_BG, "disabled_fg": "#c2b7a3", "disabled_outline": "#e6dccb",
        },
    }
    SIZE_PADS = {"xl": (34, 18), "lg": (26, 14), "md": (20, 11), "sm": (16, 8)}

    def __init__(
        self,
        parent: tk.Widget,
        text: str = "",
        command=None,
        kind: str = "primary",
        size: str = "lg",
        font=("DejaVu Sans", 14, "bold"),
        parent_bg: str = PAGE_BG,
        radius: float | None = None,
    ) -> None:
        self._palette = self.PALETTES[kind]
        base_padx, base_pady = self.SIZE_PADS[size]
        self._padx, self._pady = S(base_padx), S(base_pady)
        self._text = text
        self._command = command
        self._state = "normal"
        self._hover_t = 0.0
        self._hover_target = 0.0
        self._hover_job: str | None = None
        self._pressed = False
        self._font = tkfont.Font(font=font)
        height = self._font.metrics("linespace") + 2 * self._pady
        self._radius = Sf(radius) if radius is not None else min(Sf(12.0), height / 2.0)
        super().__init__(
            parent,
            width=self._font.measure(text) + 2 * self._padx,
            height=height,
            bg=parent_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self._font_spec = font
        self._redraw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    # -- tk-compatible configure so app code can keep using configure(text=, state=) -- #
    def configure(self, cnf=None, **kwargs):  # noqa: D102 - tk API
        kwargs = dict(cnf or {}, **kwargs)
        dirty = False
        if "text" in kwargs:
            self._text = kwargs.pop("text")
            super().configure(width=self._font.measure(self._text) + 2 * self._padx)
            dirty = True
        if "command" in kwargs:
            self._command = kwargs.pop("command")
        if "state" in kwargs:
            self._state = str(kwargs.pop("state"))
            super().configure(cursor="hand2" if self._state == "normal" else "arrow")
            dirty = True
        if kwargs:
            super().configure(**kwargs)
        if dirty:
            self._redraw()
        return None

    config = configure

    def restyle(self, *, size: str, font, text: str | None = None) -> None:
        """Re-size and/or re-label in place.

        The navigation footer shrinks its buttons to fit narrow screens
        (see EmitterTesterApp._fit_footer), which means changing the pad,
        the font and the corner radius after construction.
        """
        base_padx, base_pady = self.SIZE_PADS[size]
        self._padx, self._pady = S(base_padx), S(base_pady)
        self._font = tkfont.Font(font=font)
        self._font_spec = font
        if text is not None:
            self._text = text
        height = self._font.metrics("linespace") + 2 * self._pady
        self._radius = min(Sf(12.0), height / 2.0)
        super().configure(
            width=self._font.measure(self._text) + 2 * self._padx, height=height
        )
        self._redraw()

    def _current_colors(self) -> tuple[str, str, str]:
        palette = self._palette
        if self._state != "normal":
            return palette["disabled_fill"], palette["disabled_fg"], palette["disabled_outline"]
        if self._pressed:
            return palette["press"], palette["fg"], palette["outline"]
        return mix_color(palette["fill"], palette["hover"], self._hover_t), palette["fg"], palette["outline"]

    def _redraw(self) -> None:
        self.delete("all")
        width = int(self["width"])
        height = int(self["height"])
        fill, fg, outline = self._current_colors()
        draw_round_rect(
            self, 1, 1, width - 2, height - 2, self._radius,
            fill=fill, outline=outline or fill, width=1,
        )
        offset = 1 if self._pressed and self._state == "normal" else 0
        self.create_text(width / 2, height / 2 + offset, text=self._text, fill=fg, font=self._font_spec)

    # -- hover animation (self-contained after() loop) -- #
    def _animate_hover(self, target: float) -> None:
        self._hover_target = target
        if self._hover_job is None:
            self._hover_tick()

    def _hover_tick(self) -> None:
        delta = self._hover_target - self._hover_t
        if abs(delta) < 0.04:
            self._hover_t = self._hover_target
            self._hover_job = None
        else:
            self._hover_t += delta * 0.28
            self._hover_job = self.after(15, self._hover_tick)
        try:
            self._redraw()
        except tk.TclError:
            self._hover_job = None

    def _on_enter(self, _event) -> None:
        if self._state == "normal":
            self._animate_hover(1.0)

    def _on_leave(self, _event) -> None:
        self._pressed = False
        self._animate_hover(0.0)

    def _on_press(self, _event) -> None:
        if self._state == "normal":
            self._pressed = True
            self._redraw()

    def _on_release(self, event) -> None:
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        if was_pressed and inside and self._state == "normal" and self._command is not None:
            self._command()


class ToggleSwitch(tk.Frame):
    """iOS-style animated toggle bound to a BooleanVar, with a text label."""

    TRACK_W, TRACK_H, KNOB_PAD = 46, 24, 3

    def __init__(self, parent: tk.Widget, text: str, variable: tk.BooleanVar, command=None, bg: str = PAGE_BG, font=("DejaVu Sans", 12)) -> None:
        super().__init__(parent, bg=bg)
        self._var = variable
        self._command = command
        self._t = 1.0 if variable.get() else 0.0
        self._job: str | None = None
        self._tw, self._th, self._knob_pad = S(self.TRACK_W), S(self.TRACK_H), Sf(self.KNOB_PAD)
        self._canvas = tk.Canvas(self, width=self._tw, height=self._th, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self._canvas.grid(row=0, column=0)
        self._label = tk.Label(self, text=text, bg=bg, fg=TEXT_DARK, font=font, cursor="hand2")
        self._label.grid(row=0, column=1, sticky="w", padx=(S(9), 0))
        self._canvas.bind("<Button-1>", self._on_click)
        self._label.bind("<Button-1>", self._on_click)
        self._redraw()

    def _on_click(self, _event) -> None:
        self._var.set(not self._var.get())
        self._animate_to(1.0 if self._var.get() else 0.0)
        if self._command is not None:
            self._command()

    def _animate_to(self, target: float) -> None:
        self._target = target
        if self._job is None:
            self._tick()

    def _tick(self) -> None:
        delta = self._target - self._t
        if abs(delta) < 0.05:
            self._t = self._target
            self._job = None
        else:
            self._t += delta * 0.3
            self._job = self.after(15, self._tick)
        try:
            self._redraw()
        except tk.TclError:
            self._job = None

    def _redraw(self) -> None:
        canvas = self._canvas
        canvas.delete("all")
        track = mix_color("#c3cde2", ELTEC_BLUE, self._t)
        draw_round_rect(canvas, 1, 1, self._tw - 2, self._th - 2, (self._th - 3) / 2, fill=track, outline=mix_color(track, self["bg"], 0.4))
        r = (self._th - 2 * self._knob_pad - 2) / 2
        min_x = self._knob_pad + 1 + r
        max_x = self._tw - self._knob_pad - 1 - r
        x = min_x + (max_x - min_x) * self._t
        y = self._th / 2
        canvas.create_oval(x - r, y - r, x + r, y + r, fill="#ffffff", outline=mix_color("#ffffff", track, 0.45))


class BatteryPill(tk.Canvas):
    """Header battery gauge: rounded pill + battery glyph, animated color."""

    W, H = 196, 36
    STATE_COLORS = {"ok": "#0f9d44", "warn": WARN_ACCENT, "low": FAIL_ACCENT, "fault": FAIL_ACCENT, "unknown": ELTEC_BLUE_DARK}

    def __init__(self, parent: tk.Widget, command=None, bg: str = ELTEC_BLUE) -> None:
        # Note: tkinter reserves self._w for the widget path, so use _pw/_ph.
        self._pw, self._ph = S(self.W), S(self.H)
        super().__init__(parent, width=self._pw, height=self._ph, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self._command = command
        self._text = "Battery: --"
        self._fraction = 0.0
        self._color = hex_to_rgb(self.STATE_COLORS["unknown"])
        self._target = self._color
        self._job: str | None = None
        self.bind("<Button-1>", lambda _e: self._command() if self._command else None)
        self._redraw()

    def set_state(self, state: str, text: str, fraction: float) -> None:
        self._text = text
        self._fraction = max(0.0, min(1.0, fraction))
        self._target = hex_to_rgb(self.STATE_COLORS.get(state, self.STATE_COLORS["unknown"]))
        if self._job is None:
            self._tick()

    def _tick(self) -> None:
        moved = False
        blended = []
        for current, target in zip(self._color, self._target):
            delta = target - current
            if abs(delta) > 1.5:
                moved = True
                blended.append(current + delta * 0.22)
            else:
                blended.append(float(target))
        self._color = tuple(blended)
        self._job = self.after(15, self._tick) if moved else None
        try:
            self._redraw()
        except tk.TclError:
            self._job = None

    @property
    def pill_width(self) -> int:
        return self._pw

    def _redraw(self) -> None:
        self.delete("all")
        fill = rgb_to_hex(self._color)
        draw_round_rect(self, 1, 1, self._pw - 2, self._ph - 2, (self._ph - 3) / 2, fill=fill, outline=mix_color(fill, "#ffffff", 0.22))
        # Battery glyph: body + tip + level fill.
        bx, by, bw, bh = S(16), self._ph / 2 - S(6), S(24), S(12)
        self.create_rectangle(bx, by, bx + bw, by + bh, outline="#ffffff", width=Sf(1.4))
        self.create_rectangle(bx + bw, by + Sf(3.5), bx + bw + S(3), by + bh - Sf(3.5), fill="#ffffff", outline="#ffffff")
        pad = Sf(2.5)
        level_w = (bw - 2 * pad) * self._fraction
        if level_w > 0.5:
            self.create_rectangle(bx + pad, by + pad, bx + pad + level_w, by + bh - pad, fill="#ffffff", outline="")
        self.create_text(bx + bw + S(13), self._ph / 2, anchor="w", text=self._text, fill="#ffffff", font=("DejaVu Sans", 11, "bold"))


class PulseDot(tk.Canvas):
    """Small pulsing status dot (measuring / live indicators)."""

    def __init__(self, parent: tk.Widget, animator: Animator, name: str, color: str = ELTEC_RED, bg: str = PAGE_BG, size: int = 16) -> None:
        size = S(size)
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self._size = size
        self._color = color
        self._bg = bg
        self._dot = self.create_oval(0, 0, 0, 0, fill=color, outline="")
        animator.animate(name, 1300, self._frame, easing=None, loop=True)
        self._frame(0.0)

    def _frame(self, t: float) -> None:
        pulse = 0.5 + 0.5 * math.sin(t * 2 * math.pi)
        center = self._size / 2
        radius = self._size * (0.22 + 0.14 * pulse)
        self.coords(self._dot, center - radius, center - radius, center + radius, center + radius)
        self.itemconfigure(self._dot, fill=mix_color(self._color, self._bg, 0.45 * (1.0 - pulse)))


class Card(tk.Canvas):
    """Rounded card with a soft drop shadow and optional gradient accent strip.

    Content goes into ``card.inner`` (a plain tk.Frame). The card stretches to
    its grid cell horizontally and sizes its height to the content.
    """

    MARGIN = 9          # room around the card for the shadow layers
    SHADOW_LAYERS = ((6, 0.05), (4, 0.08), (2, 0.11))

    def __init__(
        self,
        parent: tk.Widget,
        page_bg: str = PAGE_BG,
        card_bg: str = CARD_BG,
        radius: float = 14,
        accent_stops: list[str] | None = None,
        pad: tuple[int, int] = (26, 22),
        border: str = CARD_BORDER,
    ) -> None:
        # Start with a small height: the real height is set from the content
        # as soon as it lays out. (The Tk canvas default is ~7cm tall, which
        # would flash a giant empty card for a frame otherwise.)
        super().__init__(parent, bg=page_bg, highlightthickness=0, bd=0, height=S(64))
        self._page_bg = page_bg
        self._card_bg = card_bg
        self._radius = Sf(radius)
        self._accent_stops = accent_stops
        self._pad = (S(pad[0]), S(pad[1]))
        self._margin = S(self.MARGIN)
        self._border = border
        self._last_drawn = (0, 0)
        self.inner = tk.Frame(self, bg=card_bg)
        self._window = self.create_window(self._margin + self._pad[0], self._margin + self._pad[1], anchor="nw", window=self.inner)
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.bind("<Configure>", self._on_canvas_configure)

    def _on_inner_configure(self, _event=None) -> None:
        wanted = self.inner.winfo_reqheight() + 2 * (self._margin + self._pad[1])
        if int(float(self["height"])) != wanted:
            super().configure(height=wanted)
        self._redraw()

    def settle(self) -> None:
        """Adopt the final content size NOW instead of waiting for <Configure>
        events (which need extra event-loop round-trips and would paint the
        card at the wrong size first)."""
        inner_width = self.winfo_width() - 2 * (self._margin + self._pad[0])
        if inner_width > 1:
            self.itemconfigure(self._window, width=inner_width)
        self._on_inner_configure()

    def _on_canvas_configure(self, _event=None) -> None:
        inner_width = self.winfo_width() - 2 * (self._margin + self._pad[0])
        if inner_width > 1:
            self.itemconfigure(self._window, width=inner_width)
        self._redraw()

    def _redraw(self) -> None:
        width = self.winfo_width()
        height = int(float(self["height"]))
        # The decorations only depend on size; skip redundant redraws so
        # layout churn elsewhere doesn't trigger expensive gradient repaints.
        if (width, height) == self._last_drawn:
            return
        self._last_drawn = (width, height)
        self.delete("deco")
        if width <= 2 * self._margin or height <= 2 * self._margin:
            return
        x0, y0 = self._margin, self._margin
        x1, y1 = width - self._margin, height - self._margin
        for offset, strength in self.SHADOW_LAYERS:
            scaled = Sf(offset)
            draw_round_rect(
                self, x0 - scaled / 2, y0 + scaled / 2, x1 + scaled / 2, y1 + scaled,
                self._radius + scaled / 2,
                fill=mix_color(self._page_bg, "#1d2c55", strength),
                outline="", tags="deco",
            )
        draw_round_rect(self, x0, y0, x1, y1, self._radius, fill=self._card_bg, outline=self._border, width=1, tags="deco")
        if self._accent_stops:
            strip_x0 = int(x0 + self._radius * 0.8)
            strip_x1 = int(x1 - self._radius * 0.8)
            draw_horizontal_gradient(self, strip_x0, y0 + S(2), strip_x1, y0 + S(2), self._accent_stops, tags="deco", step=4)
        self.tag_lower("deco")


class StepRail(tk.Canvas):
    """Numbered vertical step rail ("01 / 02 / 03") with animated transitions."""

    WIDTH = 218
    TOP = 52
    GAP = 88
    CHIP_R = 17
    CHIP_X = 28

    def __init__(self, parent: tk.Widget, steps: list[str], animator: Animator, mono_family: str, body_family: str, bg: str = PAGE_BG) -> None:
        self._top, self._gap = S(self.TOP), S(self.GAP)
        self._chip_r, self._chip_x = S(self.CHIP_R), S(self.CHIP_X)
        super().__init__(parent, width=S(self.WIDTH), height=self._top + len(steps) * self._gap, bg=bg, highlightthickness=0, bd=0)
        self._animator = animator
        self._mono = mono_family
        self._body = body_family
        self._bg = bg
        self._steps = steps
        self._current = 0
        self._chip_colors = [STEP_IDLE] * len(steps)
        self._label_colors = [STEP_IDLE_FG] * len(steps)
        self._connector_fill = [0.0] * len(steps)
        self.create_text(S(6), S(14), anchor="w", text="TEST SEQUENCE", fill=STEP_IDLE_FG, font=(mono_family, 10, "bold"))
        self._items: list[dict] = []
        for index, label in enumerate(steps):
            cy = self._top + index * self._gap + self._chip_r
            item: dict = {"cy": cy}
            # Two pulse rings: a wide faint halo under a thinner core ring reads
            # as an anti-aliased circle instead of a hard jagged outline.
            item["ring_halo"] = self.create_oval(0, 0, 0, 0, outline="", width=Sf(4.5))
            item["ring"] = self.create_oval(0, 0, 0, 0, outline="", width=Sf(2.0))
            # The chip gets a soft blended outline for the same reason.
            item["chip"] = self.create_oval(
                self._chip_x - self._chip_r, cy - self._chip_r, self._chip_x + self._chip_r, cy + self._chip_r,
                fill=STEP_IDLE, outline=mix_color(STEP_IDLE, bg, 0.45), width=Sf(2.0),
            )
            item["num"] = self.create_text(self._chip_x, cy, text=f"{index + 1:02d}", fill="#ffffff", font=(mono_family, 11, "bold"))
            item["label"] = self.create_text(self._chip_x + self._chip_r + S(13), cy, anchor="w", text=label, fill=STEP_IDLE_FG, font=(body_family, 13, "bold"))
            if index < len(steps) - 1:
                y_from = cy + self._chip_r + S(6)
                y_to = cy + self._gap - self._chip_r - S(6)
                item["track"] = self.create_line(self._chip_x, y_from, self._chip_x, y_to, fill=STEP_IDLE, width=S(3), capstyle="round")
                item["fill_line"] = self.create_line(self._chip_x, y_from, self._chip_x, y_from, fill=PASS_ACCENT, width=S(3), state="hidden", capstyle="round")
                item["y_from"], item["y_to"] = y_from, y_to
            self._items.append(item)

    def set_current(self, current: int) -> None:
        self._current = current
        for index, item in enumerate(self._items):
            if index < current:
                chip_target, label_target = PASS_ACCENT, PASS_FG
                num_text, connector_target = "✓", 1.0
            elif index == current:
                chip_target, label_target = ELTEC_BLUE, ELTEC_BLUE_DARK
                num_text, connector_target = f"{index + 1:02d}", 0.0
            else:
                chip_target, label_target = STEP_IDLE, STEP_IDLE_FG
                num_text, connector_target = f"{index + 1:02d}", 0.0
            self._animate_chip(index, chip_target, label_target)
            if self.itemcget(item["num"], "text") != num_text:
                self.itemconfigure(item["num"], text=num_text)
                if num_text == "✓":
                    self._pop_number(index)
            if "fill_line" in item:
                self._animate_connector(index, connector_target)
        self._start_pulse()

    def _animate_chip(self, index: int, chip_target: str, label_target: str) -> None:
        chip_from = self._chip_colors[index]
        label_from = self._label_colors[index]
        self._chip_colors[index] = chip_target
        self._label_colors[index] = label_target
        item = self._items[index]

        def frame(t: float) -> None:
            chip = mix_color(chip_from, chip_target, t)
            self.itemconfigure(item["chip"], fill=chip, outline=mix_color(chip, self._bg, 0.45))
            self.itemconfigure(item["label"], fill=mix_color(label_from, label_target, t))

        self._animator.animate(f"rail:chip{index}", 380, frame, easing=ease_in_out)

    def _pop_number(self, index: int) -> None:
        item = self._items[index]

        def frame(t: float) -> None:
            size = max(1, int(round(4 + 9 * t)))
            self.itemconfigure(item["num"], font=(self._mono, size, "bold"))

        self._animator.animate(f"rail:pop{index}", 420, frame, easing=ease_out_back)

    def _animate_connector(self, index: int, target: float) -> None:
        item = self._items[index]
        start = self._connector_fill[index]
        if abs(start - target) < 0.001:
            return
        self._connector_fill[index] = target

        def frame(t: float) -> None:
            frac = start + (target - start) * t
            if frac <= 0.001:
                self.itemconfigure(item["fill_line"], state="hidden")
                return
            self.itemconfigure(item["fill_line"], state="normal")
            y_end = item["y_from"] + (item["y_to"] - item["y_from"]) * frac
            self.coords(item["fill_line"], self._chip_x, item["y_from"], self._chip_x, y_end)

        self._animator.animate(f"rail:conn{index}", 420, frame, easing=ease_in_out, delay_ms=120)

    def _start_pulse(self) -> None:
        item = self._items[self._current]
        for other in self._items:
            if other is not item:
                self.itemconfigure(other["ring"], outline="")
                self.itemconfigure(other["ring_halo"], outline="")

        def frame(t: float) -> None:
            pulse = 0.5 + 0.5 * math.sin(t * 2 * math.pi)
            radius = self._chip_r + Sf(3.5) + Sf(2.5) * pulse
            cy = item["cy"]
            strength = 0.18 + 0.30 * pulse
            self.coords(item["ring"], self._chip_x - radius, cy - radius, self._chip_x + radius, cy + radius)
            self.coords(item["ring_halo"], self._chip_x - radius, cy - radius, self._chip_x + radius, cy + radius)
            self.itemconfigure(item["ring"], outline=mix_color(self._bg, ELTEC_BLUE, strength))
            self.itemconfigure(item["ring_halo"], outline=mix_color(self._bg, ELTEC_BLUE, strength * 0.35))

        self._animator.animate("rail:pulse", 2100, frame, easing=None, loop=True)


class ScopeView(tk.Canvas):
    """Dark navy oscilloscope panel with real axes (site dark-section look).

    - The PWM sync square wave is overlaid ON the signal band (scaled to the
      same plot height) so polarity is inspectable directly: a POSITIVE part
      peaks while the overlay is HIGH (emitter on).
    - X (seconds) and Y (volts) carry numeric tick labels on a nice-step
      grid. ``min_span_v`` stops auto-zoom from magnifying tiny signals —
      the noise scope uses it so normal noise cannot look enormous — and
      ``limit_band_mv`` draws a dashed pk-pk acceptance band around the
      signal mean for visual verification.
    - ``relative_band_mv`` switches to the noise-range display: the trace is
      plotted as its deviation from its own mean, symmetric around 0 (in µV
      when the band is below 1 mV, else mV), with SOLID RED cutoff lines at
      ±band/2 — the pk-pk limit reads directly as "does the trace cross the
      red lines", instead of hunting absolute volt values. The readout shows
      the pk-pk range.
    - Traces render as per-pixel min/max envelopes, so downsampling a long
      capture can never hide a narrow spike.
    """

    PAD_LEFT = 64
    PAD_RIGHT = 16
    PAD_TOP = 32
    PAD_BOTTOM = 28

    def __init__(
        self,
        parent: tk.Widget,
        animator: Animator,
        name_prefix: str,
        height: int = 250,
        *,
        sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
        channel_label: str = "AIN0 · SENSOR",
        empty_text: str = "SIGNAL APPEARS HERE DURING MEASUREMENT",
        min_span_v: float | None = None,
        limit_band_mv: float | None = None,
        relative_band_mv: float | None = None,
    ) -> None:
        super().__init__(parent, height=S(height), bg=WAVE_BG, highlightthickness=1, highlightbackground=NAVY_EDGE, bd=0)
        self._animator = animator
        self._prefix = name_prefix
        self._pad_left = S(self.PAD_LEFT)
        self._pad_right = S(self.PAD_RIGHT)
        self._pad_top = S(self.PAD_TOP)
        self._pad_bottom = S(self.PAD_BOTTOM)
        self._sample_rate_hz = float(sample_rate_hz)
        self._channel_label = channel_label
        self._empty_text = empty_text
        self._min_span_v = min_span_v
        self._limit_band_mv = limit_band_mv
        self._relative_band_mv = relative_band_mv
        self.waveform: np.ndarray = np.array([], dtype=float)
        self.sync: np.ndarray = np.array([], dtype=float)
        self.bind("<Configure>", lambda _e: self.redraw())

    def set_data(self, waveform: np.ndarray, sync: np.ndarray) -> None:
        self.waveform = waveform
        self.sync = sync
        self.redraw()

    def set_display_mode(
        self,
        *,
        relative_band_mv: float | None,
        min_span_v: float | None,
        channel_label: str | None = None,
        sample_rate_hz: float | None = None,
    ) -> None:
        """Flip between absolute volts and the relative noise-range display.

        ``sample_rate_hz`` keeps the time axis honest when the displayed data
        changes rate (the noise view shows the band-limited 50 SPS trace).
        """
        self._relative_band_mv = relative_band_mv
        self._min_span_v = min_span_v
        if channel_label is not None:
            self._channel_label = channel_label
        if sample_rate_hz is not None:
            self._sample_rate_hz = float(sample_rate_hz)
        self.redraw()

    # ----- axis helpers ----- #
    @staticmethod
    def _nice_step(span: float, target_ticks: int = 4) -> float:
        """A 1/2/5 x 10^k step giving roughly ``target_ticks`` intervals."""
        raw = span / max(1, target_ticks)
        magnitude = 10.0 ** math.floor(math.log10(max(raw, 1e-12)))
        for multiple in (1.0, 2.0, 5.0):
            if raw <= multiple * magnitude * (1.0 + 1e-9):
                return multiple * magnitude
        return 10.0 * magnitude

    @staticmethod
    def _ticks(lo: float, hi: float, step: float) -> list[float]:
        first = math.ceil(lo / step - 1e-9) * step
        values = []
        value = first
        while value <= hi + step * 1e-6:
            values.append(0.0 if abs(value) < step * 1e-6 else value)
            value += step
        return values

    @staticmethod
    def _fmt(value: float, step: float) -> str:
        decimals = 0 if step >= 1 else min(6, max(0, -math.floor(math.log10(step))))
        return f"{value:.{decimals}f}"

    def _chip(self, x: int, y: int, text: str, core: str, tags: str = "") -> int:
        """Draw a labelled chip and return the x just after its right edge."""
        font_spec = ("DejaVu Sans Mono", 9, "bold")
        text_width = tkfont.Font(font=font_spec).measure(text)
        draw_round_rect(self, x, y, x + text_width + S(18), y + S(20), Sf(9), fill=mix_color(WAVE_BG, core, 0.16), outline=mix_color(WAVE_BG, core, 0.45), tags=tags)
        self.create_text(x + S(9) + text_width / 2, y + S(10), text=text, fill=core, font=font_spec, tags=tags)
        return x + text_width + S(18)

    def _y_of(self, value: float, lo: float, hi: float, top: float, bottom: float) -> float:
        return bottom - (value - lo) / (hi - lo) * (bottom - top)

    def _plot_envelope(self, signal: np.ndarray, left: float, right: float, lo: float, hi: float, top: float, bottom: float, halo: str, mid: str, core: str) -> None:
        """Per-pixel min/max envelope polyline: spikes survive downsampling."""
        n = len(signal)
        columns = max(2, int(right - left))
        points: list[float] = []
        if n <= columns:
            x_positions = np.linspace(left, right, n)
            for px, value in zip(x_positions, signal):
                points.extend([float(px), self._y_of(float(value), lo, hi, top, bottom)])
        else:
            edges = np.linspace(0, n, columns + 1).astype(int)
            for column in range(columns):
                begin, end = edges[column], max(edges[column] + 1, edges[column + 1])
                segment = signal[begin:end]
                seg_lo, seg_hi = float(np.min(segment)), float(np.max(segment))
                px = left + (right - left) * column / (columns - 1)
                # Alternate the min/max order so the polyline sweeps through
                # each column's full range without doubling back visibly.
                first, second = (seg_hi, seg_lo) if column % 2 == 0 else (seg_lo, seg_hi)
                points.extend([float(px), self._y_of(first, lo, hi, top, bottom)])
                if seg_hi - seg_lo > 1e-12:
                    points.extend([float(px), self._y_of(second, lo, hi, top, bottom)])
        self.create_line(points, fill=halo, width=Sf(5), joinstyle="round", capstyle="round")
        self.create_line(points, fill=mid, width=Sf(2.6), joinstyle="round", capstyle="round")
        self.create_line(points, fill=core, width=Sf(1.4), joinstyle="round", capstyle="round")

    def redraw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        waveform = self.waveform
        sync = self.sync
        left = self._pad_left
        right = width - self._pad_right
        top = self._pad_top
        bottom = height - self._pad_bottom
        if waveform.size < 2 or right - left < S(40) or bottom - top < S(30):
            if self._empty_text:
                self.create_text(
                    width / 2, height / 2 - S(6),
                    text=self._empty_text,
                    fill="#3d4f78", font=("DejaVu Sans Mono", 11, "bold"),
                )
                mid_y = height / 2 + S(18)
                self.create_line(left + S(20), mid_y, right - S(20), mid_y, fill="#22345c", width=Sf(1.4), dash=(6, 5))
            self._chip(S(10), S(8), self._channel_label, TRACE_CORE)
            return

        # --- relative (noise-range) mode: deviation from the mean --- #
        # Units auto-select: µV when the acceptance band is below 1 mV (the
        # ~429 µV pin-level TP412 limit), mV otherwise.
        relative = self._relative_band_mv is not None
        if relative:
            band_in_uv = self._relative_band_mv < 1.0
            display_scale = 1_000_000.0 if band_in_uv else 1000.0
            band_display = self._relative_band_mv * (display_scale / 1000.0)
            display = (waveform - float(np.mean(waveform))) * display_scale
            unit_label = "µV" if band_in_uv else "mV"
        else:
            display = waveform
            unit_label = "V"

        # --- y range: data + margin, but never zoomed past min_span_v --- #
        data_lo, data_hi = float(np.min(display)), float(np.max(display))
        span = data_hi - data_lo
        if relative:
            # Symmetric around 0 so the ±band/2 cutoffs sit mirrored, and
            # never zoomed past min_span_v (2x the pk-pk limit for the noise
            # scopes) so normal noise cannot be magnified into looking large.
            half_limit = band_display / 2.0
            min_half_span = (
                (self._min_span_v * display_scale) / 2.0
                if self._min_span_v is not None
                else band_display
            )
            hi = max(abs(data_lo), abs(data_hi), half_limit) * 1.10
            hi = max(hi, min_half_span)
            lo = -hi
        else:
            margin = max(span * 0.10, 1e-4)
            lo, hi = data_lo - margin, data_hi + margin
            if self._min_span_v is not None and (hi - lo) < self._min_span_v:
                center = 0.5 * (data_lo + data_hi)
                lo = center - self._min_span_v / 2.0
                hi = center + self._min_span_v / 2.0

        # --- grid + numeric ticks --- #
        tick_font = ("DejaVu Sans Mono", 8)
        y_step = self._nice_step(hi - lo)
        for value in self._ticks(lo, hi, y_step):
            y = self._y_of(value, lo, hi, top, bottom)
            self.create_line(left, y, right, y, fill=NAVY_GRID_MINOR)
            self.create_text(left - S(6), y, anchor="e", text=self._fmt(value, y_step), fill="#8ea6d4", font=tick_font)
        total_s = (len(display) - 1) / self._sample_rate_hz
        x_step = self._nice_step(max(total_s, 1e-6), target_ticks=6)
        for value in self._ticks(0.0, total_s, x_step):
            x = left + (right - left) * (value / total_s if total_s > 0 else 0.0)
            self.create_line(x, top, x, bottom, fill=NAVY_GRID_MINOR)
            self.create_text(x, bottom + S(4), anchor="n", text=self._fmt(value, x_step), fill="#8ea6d4", font=tick_font)
        self.create_line(left, bottom, right, bottom, fill=NAVY_GRID_MAJOR)
        self.create_line(left, top, left, bottom, fill=NAVY_GRID_MAJOR)
        self.create_text(left - S(6), top - S(12), anchor="e", text=unit_label, fill="#8ea6d4", font=tick_font)
        self.create_text(right, bottom + S(4), anchor="ne", text="s", fill="#8ea6d4", font=tick_font)

        if relative:
            # --- solid RED cutoff lines at ±band/2 (the pk-pk limit) --- #
            half_limit = band_display / 2.0
            zero_y = self._y_of(0.0, lo, hi, top, bottom)
            self.create_line(left, zero_y, right, zero_y, fill=NAVY_GRID_MAJOR)
            for edge in (-half_limit, half_limit):
                y = self._y_of(edge, lo, hi, top, bottom)
                self.create_line(left, y, right, y, fill=SCOPE_LIMIT_RED, width=Sf(1.8))
            self.create_text(
                right - S(6),
                self._y_of(half_limit, lo, hi, top, bottom) - S(9),
                anchor="e",
                text=(
                    f"±{half_limit:.1f} {unit_label} CUTOFF "
                    f"({band_display:.0f} {unit_label} PK-PK LIMIT)"
                ),
                fill=SCOPE_LIMIT_RED,
                font=tick_font,
            )
        elif self._limit_band_mv is not None:
            # --- optional pk-pk acceptance band around the signal mean --- #
            mean_v = float(np.mean(waveform))
            half_band = self._limit_band_mv / 2000.0  # mV pk-pk -> ±V
            for edge in (mean_v - half_band, mean_v + half_band):
                if lo <= edge <= hi:
                    y = self._y_of(edge, lo, hi, top, bottom)
                    self.create_line(left, y, right, y, fill="#d95f5f", width=Sf(1.2), dash=(7, 5))
            band_top = self._y_of(min(mean_v + half_band, hi), lo, hi, top, bottom)
            self.create_text(right - S(6), band_top + S(4), anchor="ne", text=f"{self._limit_band_mv:.0f} mV PK-PK LIMIT", fill="#d95f5f", font=tick_font)

        # --- sync square wave overlaid on the SAME band (polarity check) --- #
        if sync.size == waveform.size and float(np.max(sync)) > float(np.min(sync)):
            # HIGH rides just under the top edge, LOW just above the bottom,
            # so peak-vs-emitter-state (polarity) reads off directly.
            sync_scaled = np.where(sync > 0.5, hi - (hi - lo) * 0.06, lo + (hi - lo) * 0.06)
            self._plot_envelope(sync_scaled, left, right, lo, hi, top, bottom, SYNC_HALO, SYNC_HALO, SYNC_CORE)

        # --- signal trace on top --- #
        self._plot_envelope(display, left, right, lo, hi, top, bottom, TRACE_HALO, TRACE_MID, TRACE_CORE)
        if relative:
            readout = (
                f"range {span:.1f} {unit_label} pk-pk  ·  "
                f"{data_lo:+.1f} … {data_hi:+.1f} {unit_label}"
            )
        else:
            readout = f"{data_lo:+.4f} V  …  {data_hi:+.4f} V"
        self.create_text(right, S(12), anchor="e", text=readout, fill="#8ea6d4", font=("DejaVu Sans Mono", 10))

        chip_end = self._chip(S(10), S(8), self._channel_label, TRACE_CORE)
        if sync.size == waveform.size and float(np.max(sync)) > float(np.min(sync)):
            self._chip(chip_end + S(8), S(8), "PWM SYNC · HIGH = EMITTER ON", SYNC_CORE)


# --------------------------------------------------------------------------- #
# Guided ESP32 sensor tester UI (v6.1)
# --------------------------------------------------------------------------- #
class EmitterTesterApp(tk.Tk):
    SETUP_STEP = "setup"
    LOAD_STEP = "load"
    RESULT_STEP = "result"
    HEADER_H = 96

    def __init__(self) -> None:
        enable_windows_dpi_awareness()
        super().__init__()
        # With DPI awareness on, winfo_fpixels reports the true monitor DPI.
        # UI_SCALE drives all canvas geometry; "tk scaling" makes point-sized
        # fonts render at the same physical size, but crisp.
        global UI_SCALE
        try:
            UI_SCALE = max(1.0, self.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            UI_SCALE = 1.0
        try:
            self.tk.call("tk", "scaling", UI_SCALE * 96.0 / 72.0)
        except tk.TclError:
            pass
        self.title("Eltec 405 M22 ESP32 Tester (1 Hz)")
        self.minsize(S(1100), S(740))

        self.animator = Animator(self)
        load_private_fonts()
        self.FONT_DISPLAY = pick_font_family(
            self,
            ["Poppins SemiBold", "Poppins", "Manrope", "Noto Sans", "DejaVu Sans", "Segoe UI"],
            "DejaVu Sans",
        )
        self.FONT_BODY = pick_font_family(
            self, ["Manrope", "Roboto", "Noto Sans", "DejaVu Sans", "Segoe UI"], "DejaVu Sans"
        )
        self.FONT_MONO = pick_font_family(
            self,
            ["JetBrains Mono", "Cascadia Code", "DejaVu Sans Mono", "Liberation Mono", "Consolas"],
            "DejaVu Sans Mono",
        )

        self.device: EmitterEsp32Rig | None = None
        self.hardware_lock = threading.Lock()
        self.busy = False
        self.measuring = False
        self.step = self.SETUP_STEP
        self.measure_token = 0
        self.stability_settings: StabilitySettings | None = None
        self.stability_config_error: str | None = None
        try:
            self.stability_settings = load_stability_settings(DEFAULT_SETTINGS_PATH)
        except StabilitySettingsError as exc:
            self.stability_config_error = str(exc)

        # Battery watcher state (currently idle: the sensor battery is not
        # measurable on AIN7 - see BATTERY_MONITORING_ENABLED).
        self.battery_v: float | None = None
        self.battery_state = "unknown"  # "ok" | "warn" | "low" | "unknown"
        self.battery_checking = False
        self.battery_read_time: float | None = None  # time.monotonic() of last good read

        # Permanently-mounted AIN1 sensor calibration / emitter-health gate.
        self.reference_calibration: ReferenceCalibration | None = None
        self.reference_calibration_error: str | None = None
        self.reference_calibrating = False
        self.last_reference_check_mv: float | None = None
        try:
            self.reference_calibration = load_reference_calibration()
        except ReferenceCalibrationError as exc:
            self.reference_calibration_error = str(exc)

        # Batch / sensor state.
        self.batch_number = ""
        self.tester_name = ""
        self.filter_setup = DEFAULT_FILTER_SETUP
        self.current_sensor_number = 0
        self.current_sensor_id = ""
        self.result_saved = True
        # v2.0 skip / attempt history (attempt_history.py). While
        # ``resuming_skipped`` is set, "next" walks the skipped queue in
        # first-skipped-first-measured order instead of handing out fresh
        # sensor numbers.
        self.resuming_skipped = False
        self.measure_attempts = 0
        self.skip_count = 0
        self.skip_button: RoundButton | None = None
        self.remeasure_button: RoundButton | None = None
        self.skipped_button: RoundButton | None = None
        # Action-bar fitting (see _fit_footer): the chosen size/label
        # variant and a measuring-font cache.
        self.footer_nav_buttons: tuple = ()
        self.footer_action_buttons: tuple = ()
        self._footer_fit: tuple | None = None
        self._footer_fonts: dict = {}

        # Current-sensor measurement state.
        self.last_metrics: WaveformMetrics | None = None
        self.last_result: FinalResult | None = None
        self.last_capture_report: StabilityCaptureReport | None = None
        self.last_noise_report: NoiseCaptureReport | None = None
        self.last_noise_metrics: WaveformMetrics | None = None
        # RAW emitter-off noise capture for the current part (full 1000 SPS
        # record), kept so it can be saved on demand for offline spike
        # analysis; the band-limited verdict trace cannot recover it.
        self.last_noise_raw_waveform: np.ndarray | None = None
        self.last_noise_raw_rate_hz: float | None = None
        self.last_noise_raw_left_context: np.ndarray | None = None
        self.last_noise_raw_right_context: np.ndarray | None = None
        # True once this part's raw capture was auto-saved (any window over).
        self.noise_raw_auto_saved = False
        # Insertion-time offset read (the settled re-read carries the verdict).
        self.last_offset_initial_v: float | None = None
        # Text of the last attempt that recorded nothing (stream/rig fault).
        # While it is set, the result step offers "skip this sensor" next to
        # Measure so a batch is never stuck on one unreadable sensor.
        self.last_measure_error: str | None = None
        self.preview_waveform: np.ndarray = np.array([], dtype=float)
        self.preview_sync: np.ndarray = np.array([], dtype=float)
        # During the emitter-off noise step the live scope switches to the
        # relative noise-range display (deviation from mean + red cutoffs).
        self.preview_noise_display = False
        # Step-ladder progress state for the measuring screen's bar.
        self.measure_progress_step = 0
        self.measure_progress_total = 1
        self.measure_progress_fraction = 0.0
        self.measure_progress_canvas: tk.Canvas | None = None
        self.snapshot_paths: list[Path] = []
        self.stability_diagnostics_saved = False
        self.noise_diagnostics_saved = False

        self.logo_image: tk.PhotoImage | None = None
        self.wave_canvas: ScopeView | None = None
        self.noise_canvas: ScopeView | None = None
        self.default_focus_widget: tk.Widget | None = None
        self._advanced_dialog: tk.Toplevel | None = None
        self.step_frame: tk.Frame | None = None

        self._build_variables()
        if self.stability_config_error is not None:
            self.status_var.set(
                "V6.1 stability configuration error — measurement is disabled. "
                + self.stability_config_error
            )
        self._build_style()
        self._load_logo()
        self._build_layout()

        self.bind("<Return>", self.on_enter_key)
        self.bind("<KP_Enter>", self.on_enter_key)
        self.bind("<Escape>", self.on_escape_key)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.render_step()
        # Technicians run this full screen; start maximized so every control
        # (especially the footer buttons) is visible from the first launch.
        try:
            if self.tk.call("tk", "windowingsystem") == "x11":
                # XFCE/X11 uses the EWMH zoomed attribute instead of the
                # Windows-only ``state('zoomed')`` value. Delay until mapped so
                # xfwm sees and applies the request.
                self.after(0, lambda: self.attributes("-zoomed", True))
            else:
                self.state("zoomed")
        except tk.TclError:
            pass
        self.after(200, self.startup_probe)

    # ----- font shorthands ----- #
    def fd(self, size: int, weight: str = "bold") -> tuple:
        return (self.FONT_DISPLAY, size, weight)

    def fb(self, size: int, weight: str = "normal") -> tuple:
        return (self.FONT_BODY, size, weight)

    def fm(self, size: int, weight: str = "normal") -> tuple:
        return (self.FONT_MONO, size, weight)

    def _button_fonts(self) -> dict:
        return {"xl": self.fd(17), "lg": self.fd(15), "md": self.fd(13), "sm": self.fb(12, "bold")}

    def btn(self, parent: tk.Widget, text: str, command, kind: str = "primary", size: str = "lg", parent_bg: str = PAGE_BG) -> RoundButton:
        return RoundButton(
            parent, text=text, command=command, kind=kind, size=size,
            font=self._button_fonts()[size], parent_bg=parent_bg,
        )

    # ----- variables / style / logo ----- #
    def _build_variables(self) -> None:
        self.batch_var = tk.StringVar(value="")
        self.tester_var = tk.StringVar(value="")
        self.filter_var = tk.StringVar(value=DEFAULT_FILTER_SETUP)
        self.filter_hint_var = tk.StringVar(value="")
        self.simulator_var = tk.BooleanVar(value=False)
        self.sim_case_var = tk.StringVar(value="Random good sensor")
        self.sim_low_battery_var = tk.BooleanVar(value=False)
        self.show_live_var = tk.BooleanVar(value=False)
        self.show_details_var = tk.BooleanVar(value=False)
        self.notes_var = tk.StringVar(value="")
        self.failure_mode_var = tk.StringVar(value="")

        self.status_var = tk.StringVar(value="Checking ESP32 rig...")
        self.measure_status_var = tk.StringVar(value="")
        # "STEP 3/4 — NOISE (EMITTER OFF)" label above the progress bar.
        self.measure_step_var = tk.StringVar(value="")
        self.live_wave_header_var = tk.StringVar(
            value="LIVE SIGNAL  ·  ADS AIN0 SENSOR + PWM SYNC OVERLAY (HIGH = EMITTER ON)"
        )
        self.comment_status_var = tk.StringVar(value="")
        self.snapshot_status_var = tk.StringVar(value="")
        self.noise_capture_status_var = tk.StringVar(value="")
        # Per-part opt-in: 60 s noise soak for suspect/intermittent-burst
        # parts. Resets to off for every new sensor (deliberate - the soak is
        # a judgment call per part, not a sticky mode).
        self.noise_soak_var = tk.BooleanVar(value=False)
        self.reference_progress_var = tk.StringVar(value="")

        # One-line summary shown next to the "Advanced options" link.
        self.adv_summary_var = tk.StringVar()
        self.simulator_var.trace_add("write", lambda *_a: self._update_adv_summary())
        self._update_adv_summary()

    def _update_adv_summary(self) -> None:
        bits = ["capture: adaptive peak stability"]
        if self.simulator_var.get():
            bits.append("SIMULATOR ON")
        self.adv_summary_var.set("   ·   ".join(bits))

    def _build_style(self) -> None:
        self.configure(bg=PAGE_BG)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=PAGE_BG)
        style.configure("TLabel", background=PAGE_BG, foreground=TEXT_DARK, font=self.fb(14))
        style.configure("Muted.TLabel", background=PAGE_BG, foreground=MUTED_FG, font=self.fb(11))
        style.configure("TCheckbutton", background=PAGE_BG, foreground=TEXT_DARK, font=self.fb(12))
        style.map("TCheckbutton", background=[("active", PAGE_BG)])
        style.configure("TSeparator", background=CARD_BORDER)
        style.configure(
            "Card.TEntry", fieldbackground="#f7f9fd", foreground=TEXT_DARK,
            bordercolor=CARD_BORDER, lightcolor=CARD_BORDER, darkcolor=CARD_BORDER,
            insertcolor=ELTEC_BLUE, padding=(12, 8),
        )
        style.map("Card.TEntry", bordercolor=[("focus", ELTEC_BLUE)], lightcolor=[("focus", ELTEC_BLUE)], darkcolor=[("focus", ELTEC_BLUE)])
        style.configure("TCombobox", font=self.fb(16), padding=(10, 6))
        self.option_add("*TCombobox*Listbox.font", self.fb(15))

    def _load_logo(self) -> None:
        logo_path = find_logo_path()
        if logo_path is None:
            self.logo_image = None
            return
        try:
            self.logo_image = tk.PhotoImage(file=str(logo_path))
            # Shrink very large logos with a single integer factor chosen so the
            # image best fills the header badge (~140x52) without overflowing.
            factor = max(
                1,
                math.ceil(self.logo_image.height() / 52),
                math.ceil(self.logo_image.width() / 140),
            )
            if factor > 1:
                self.logo_image = self.logo_image.subsample(factor, factor)
            self.iconphoto(False, self.logo_image)
        except Exception:
            self.logo_image = None

    # ----- layout: header + step rail + content ----- #
    def _build_layout(self) -> None:
        self._hh = S(self.HEADER_H)
        self.header = tk.Canvas(self, height=self._hh, bg=ELTEC_BLUE, highlightthickness=0, bd=0)
        self.header.grid(row=0, column=0, sticky="ew")
        self.battery_pill = BatteryPill(self.header, command=self.refresh_battery)
        self._battery_window = self.header.create_window(0, self._hh / 2, anchor="e", window=self.battery_pill)
        self._header_status_item: int | None = None
        self._header_width = 0
        self.header.bind("<Configure>", self._redraw_header)
        self.status_var.trace_add("write", lambda *_args: self._update_header_status())
        self.animator.animate("header:wave", 5600, self._header_wave_frame, easing=None, loop=True)

        # Technical gradient accent strip under the app bar (site signature).
        self.accent_strip = tk.Canvas(self, height=S(3), bg=ELTEC_BLUE, highlightthickness=0, bd=0)
        self.accent_strip.grid(row=1, column=0, sticky="ew")
        self.accent_strip.bind(
            "<Configure>",
            lambda event: (
                self.accent_strip.delete("all"),
                draw_horizontal_gradient(self.accent_strip, 0, S(1), event.width, S(1), [ELTEC_BLUE_BRIGHT, "#6366f1", "#a855f7", ELTEC_RED], tags="grad", step=4),
            ),
        )

        body = tk.Frame(self, bg=PAGE_BG)
        body.grid(row=2, column=0, sticky="nsew", padx=(S(20), S(22)), pady=(S(16), S(14)))
        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)
        body.columnconfigure(2, weight=1)
        body.rowconfigure(0, weight=1)

        self.rail = StepRail(
            body,
            ["Batch info", "Load sensor", "Measure & result"],
            self.animator,
            mono_family=self.FONT_MONO,
            body_family=self.FONT_BODY,
        )
        self.rail.grid(row=0, column=0, sticky="nw", pady=(S(6), 0))

        divider = tk.Frame(body, bg=CARD_BORDER, width=1)
        divider.grid(row=0, column=1, sticky="ns", padx=(S(4), S(22)))

        self.content = tk.Frame(body, bg=PAGE_BG)
        self.content.grid(row=0, column=2, sticky="nsew")
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)
        # The step content lives inside a scroll canvas: when a step is taller
        # than the window (e.g. a FAIL with reasons + the waveform open), a
        # scrollbar appears and the mouse wheel brings the rest into view
        # instead of clipping it.
        self.step_scroll = tk.Canvas(self.content, bg=PAGE_BG, highlightthickness=0, bd=0)
        self.step_scroll.grid(row=0, column=0, sticky="nsew")
        self.step_vbar = ttk.Scrollbar(self.content, orient="vertical", command=self.step_scroll.yview)
        self.step_scroll.configure(yscrollcommand=self._on_step_scroll_set)
        self.step_scroll.bind("<Configure>", lambda _e: self._sync_step_scroll())
        self.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        # Tk/X11 reports wheel motion as buttons 4/5 rather than MouseWheel.
        self.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.bind_all("<Button-5>", self._on_mousewheel, add="+")
        self._step_window: int | None = None
        # The navigation footer lives OUTSIDE the scrolling area in its own
        # fixed row, so it is always fully visible no matter how tall the step
        # content gets.
        self.footer_bar = tk.Frame(self.content, bg=PAGE_BG)
        self.footer_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(S(12), 0))
        self.footer_bar.columnconfigure(0, weight=1)
        # Re-fit the action bar whenever the content column changes width:
        # the window is maximized after the first render, and the rig PCs
        # run different resolutions and UI scalings.
        self.footer_bar.bind("<Configure>", self._fit_footer)

    def _on_step_scroll_set(self, first: str, last: str) -> None:
        self.step_vbar.set(first, last)
        # Auto-hide: only show the scrollbar when there is something to scroll.
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.step_vbar.grid_remove()
        else:
            self.step_vbar.grid(row=0, column=1, sticky="ns", padx=(S(4), 0))

    def _sync_step_scroll(self) -> None:
        """Size the embedded step frame: full canvas width, and at least the
        canvas height (so weight rows keep absorbing surplus space) but taller
        when the content needs it - that overflow is what scrolls."""
        if self._step_window is None or self.step_frame is None or not self.step_frame.winfo_exists():
            return
        canvas_w = max(1, self.step_scroll.winfo_width())
        canvas_h = max(1, self.step_scroll.winfo_height())
        total_h = max(canvas_h, self.step_frame.winfo_reqheight())
        self.step_scroll.itemconfigure(self._step_window, width=canvas_w, height=total_h)
        self.step_scroll.configure(scrollregion=(0, 0, canvas_w, total_h))

    def _on_mousewheel(self, event: tk.Event) -> None:
        widget = event.widget
        # Only scroll the main window's step area; leave dialogs (comment box,
        # batch summary) and text widgets to their own wheel handling.
        try:
            if not isinstance(widget, tk.Widget) or widget.winfo_toplevel() is not self:
                return
            if isinstance(widget, tk.Text):
                return
            first, last = self.step_vbar.get()
            if first <= 0.0 and last >= 1.0:
                return
            if getattr(event, "num", None) == 4:
                direction = -1
            elif getattr(event, "num", None) == 5:
                direction = 1
            else:
                delta = int(getattr(event, "delta", 0) / 120)
                if delta == 0:
                    return
                direction = -delta
            self.step_scroll.yview_scroll(3 * direction, "units")
        except tk.TclError:
            pass

    def _redraw_header(self, _event=None) -> None:
        width = self.header.winfo_width()
        if width <= 2:
            return
        self._header_width = width
        height = self._hh
        self.header.delete("static")
        for x in range(0, width, 4):
            color = gradient_color(HEADER_GRADIENT, x / max(1, width))
            self.header.create_line(x, 0, x, height, fill=color, width=4, tags="static")
        # Faint vertical grid ticks (site dark-section texture).
        for x in range(S(56), width, S(128)):
            tick = mix_color(gradient_color(HEADER_GRADIENT, x / max(1, width)), "#ffffff", 0.05)
            self.header.create_line(x, 0, x, height, fill=tick, tags="static")

        # Logo badge (white rounded chip, like the site's logo-on-white).
        badge_x0, badge_y0, badge_x1, badge_y1 = S(18), S(14), S(176), height - S(14)
        draw_round_rect(self.header, badge_x0, badge_y0, badge_x1, badge_y1, Sf(12), fill="#ffffff", outline=mix_color(ELTEC_BLUE, "#ffffff", 0.75), tags="static")
        badge_cx = (badge_x0 + badge_x1) / 2
        badge_cy = (badge_y0 + badge_y1) / 2
        if self.logo_image is not None:
            self.header.create_image(badge_cx, badge_cy, image=self.logo_image, tags="static")
        else:
            self.header.create_text(badge_cx, badge_cy, text="ELTEC", fill=ELTEC_RED, font=(self.FONT_DISPLAY, 22, "bold italic"), tags="static")

        title_x = badge_x1 + S(26)
        self.header.create_text(title_x, S(26), anchor="w", text="405 M22 SENSOR TESTER", fill=HEADER_FG, font=self.fd(21), tags="static")
        title_width = tkfont.Font(font=self.fd(21)).measure("405 M22 SENSOR TESTER")
        chip_x = title_x + title_width + S(14)
        draw_round_rect(self.header, chip_x, S(15), chip_x + S(40), S(37), Sf(8), fill=ELTEC_RED, outline="", tags="static")
        self.header.create_text(chip_x + S(20), S(26), text="V6.1", fill="#ffffff", font=self.fm(10, "bold"), tags="static")
        if self.simulator_var.get():
            # Loud amber badge: everything on screen is synthetic.
            sim_text = "SIMULATOR"
            sim_font = self.fm(11, "bold")
            sim_w = tkfont.Font(font=sim_font).measure(sim_text) + S(22)
            sim_x = chip_x + S(40) + S(10)
            draw_round_rect(self.header, sim_x, S(15), sim_x + sim_w, S(37), Sf(8), fill=WARN_ACCENT, outline="", tags="static")
            self.header.create_text(sim_x + sim_w / 2, S(26), text=sim_text, fill="#3d2c00", font=sim_font, tags="static")
        self.header.create_text(title_x, S(47), anchor="w", text="PYROELECTRIC SENSOR QC  ·  EMITTER RIG", fill=mix_color(HEADER_SUB_FG, ELTEC_BLUE, 0.25), font=self.fm(9, "bold"), tags="static")

        if self._header_status_item is not None:
            self.header.delete(self._header_status_item)
        self._header_status_item = self.header.create_text(
            title_x, S(70), anchor="w", text=self.status_var.get(), fill=HEADER_SUB_FG, font=self.fb(11), tags="status",
        )
        # Blend the battery pill into the local gradient color and pin it right.
        pill_x = width - S(26)
        pill_center_t = (pill_x - self.battery_pill.pill_width / 2) / max(1, width)
        self.battery_pill.configure(bg=gradient_color(HEADER_GRADIENT, pill_center_t))
        self.header.coords(self._battery_window, pill_x, height / 2)
        self.header.tag_raise("wave")

    def _update_header_status(self) -> None:
        if self._header_status_item is not None:
            try:
                self.header.itemconfigure(self._header_status_item, text=self.status_var.get())
            except tk.TclError:
                pass

    def _header_wave_frame(self, t: float) -> None:
        width = self._header_width
        if width <= 2:
            return
        base_y = self._hh - S(12)
        step = S(8)
        points: list[float] = []
        for x in range(0, width + step, step):
            y = base_y + Sf(5.0) * math.sin(2 * math.pi * (2.5 * x / max(1, width) + t))
            points.extend([x, y])
        if not self.header.find_withtag("wave"):
            self.header.create_line(points, fill="#89b4f8", width=Sf(1.4), smooth=True, tags="wave")
        else:
            self.header.coords("wave", *points)
            self.header.tag_raise("wave")

    # ----- step rendering ----- #
    def clear_content(self) -> None:
        self.animator.cancel_prefix("step:")
        self.wave_canvas = None
        self.noise_canvas = None
        if self.step_frame is not None and self.step_frame.winfo_exists():
            self.step_frame.destroy()
        if self._step_window is not None:
            self.step_scroll.delete(self._step_window)
        self.step_frame = tk.Frame(self.step_scroll, bg=PAGE_BG)
        self.step_frame.columnconfigure(0, weight=1)
        self._step_window = self.step_scroll.create_window(0, 0, anchor="nw", window=self.step_frame)
        self.step_frame.bind("<Configure>", lambda _e: self._sync_step_scroll())
        self.step_scroll.yview_moveto(0.0)

    def update_progress_labels(self) -> None:
        order = [self.SETUP_STEP, self.LOAD_STEP, self.RESULT_STEP]
        self.rail.set_current(order.index(self.step))

    def render_step(self) -> None:
        self.clear_content()
        self.default_focus_widget = None
        self.update_progress_labels()
        if self.step == self.SETUP_STEP:
            self.render_setup_step()
        elif self.step == self.LOAD_STEP:
            self.render_load_step()
        else:
            self.render_result_step()
        self.render_navigation()
        self.update_navigation_state()
        self._settle_layout()
        self._slide_in_step()
        self.after_idle(self.focus_default_widget)
        # Insurance against Windows leaving stale pixels in child widgets
        # after the initial layout passes (moved windows keep their old bits).
        self.after(250, self._force_full_repaint)
        self.after(900, self._force_full_repaint)

    def _settle_layout(self) -> None:
        """Bring every Card in the new step to its final size synchronously,
        so the first paint of the step is already the final layout."""
        try:
            self.update_idletasks()  # give every widget its requested size
        except tk.TclError:
            return
        stack: list[tk.Widget] = [self.step_frame]
        while stack:
            widget = stack.pop()
            if isinstance(widget, Card):
                widget.settle()
            stack.extend(widget.winfo_children())
        try:
            self.update_idletasks()  # apply the new card heights to the grid
        except tk.TclError:
            pass

    def _force_full_repaint(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.GetAncestor.restype = ctypes.c_void_p
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.RedrawWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
            hwnd = user32.GetAncestor(self.winfo_id(), 2)  # GA_ROOT
            RDW_FLAGS = 0x1 | 0x4 | 0x80 | 0x100  # INVALIDATE|ERASE|ALLCHILDREN|UPDATENOW
            user32.RedrawWindow(hwnd, None, None, RDW_FLAGS)
        except Exception:
            pass

    def _slide_in_step(self) -> None:
        window = self._step_window

        def on_frame(t: float) -> None:
            self.step_scroll.coords(window, int(round(S(44) * (1.0 - t))), 0)

        self.animator.animate("step:slide", 340, on_frame, easing=ease_out_cubic)

    def _step_heading(self, row: int, number: str, title: str, subtitle: str) -> None:
        head = tk.Frame(self.step_frame, bg=PAGE_BG)
        head.grid(row=row, column=0, sticky="ew")
        tk.Label(head, text=f"{number} —", bg=PAGE_BG, fg=ELTEC_RED, font=self.fm(13, "bold")).pack(anchor="w")
        tk.Label(head, text=title, bg=PAGE_BG, fg=TEXT_DARK, font=self.fd(29)).pack(anchor="w", pady=(2, 0))
        if subtitle:
            tk.Label(head, text=subtitle, bg=PAGE_BG, fg=MUTED_FG, font=self.fb(13)).pack(anchor="w", pady=(4, 0))

    def _field_label(self, parent: tk.Widget, row: int, text: str, bg: str = CARD_BG, pady: tuple = (14, 4)) -> None:
        tk.Label(parent, text=text.upper(), bg=bg, fg=MUTED_FG, font=self.fb(11, "bold")).grid(row=row, column=0, sticky="w", pady=pady)

    def render_setup_step(self) -> None:
        self._step_heading(0, "01", "Batch information", "Enter the batch number and your name, choose the filter, then press Enter.")

        card = Card(self.step_frame, accent_stops=TECH_GRADIENT)
        card.grid(row=1, column=0, sticky="new", pady=(20, 0))
        inner = card.inner
        inner.columnconfigure(0, weight=1)

        self._field_label(inner, 0, "Batch number", pady=(0, 4))
        batch_entry = ttk.Entry(inner, textvariable=self.batch_var, font=self.fb(19), style="Card.TEntry")
        batch_entry.grid(row=1, column=0, sticky="ew")
        self.default_focus_widget = batch_entry

        self._field_label(inner, 2, "Tester name")
        ttk.Entry(inner, textvariable=self.tester_var, font=self.fb(19), style="Card.TEntry").grid(row=3, column=0, sticky="ew")

        self._field_label(inner, 4, "Filter / setup")
        filter_combo = ttk.Combobox(
            inner, textvariable=self.filter_var, values=list(FILTER_SPECS_MV.keys()), state="readonly",
            font=self.fb(15), height=min(max(len(FILTER_SPECS_MV), 5), 10),
        )
        filter_combo.grid(row=5, column=0, sticky="ew")
        filter_combo.bind("<<ComboboxSelected>>", lambda _e: self.update_filter_hint())
        tk.Label(inner, textvariable=self.filter_hint_var, bg=CARD_BG, fg=ELTEC_BLUE, font=self.fm(11, "bold")).grid(row=6, column=0, sticky="w", pady=(8, 0))
        self.update_filter_hint()

        self._build_reference_calibration_card(row=2)

        adv_container = tk.Frame(self.step_frame, bg=PAGE_BG)
        adv_container.grid(row=3, column=0, sticky="new", pady=(S(16), 0))
        link = tk.Label(adv_container, text="⚙  Advanced options…", bg=PAGE_BG, fg=ELTEC_BLUE,
                        font=self.fb(12, "bold"), cursor="hand2")
        link.grid(row=0, column=0, sticky="w")
        link.bind("<Button-1>", lambda _e: self.open_advanced_options())
        tk.Label(adv_container, textvariable=self.adv_summary_var, bg=PAGE_BG, fg=MUTED_FG,
                 font=self.fb(11)).grid(row=0, column=1, sticky="w", padx=(S(14), 0))

    def _build_advanced_panel(self, parent: tk.Widget) -> tk.Frame:
        panel = tk.Frame(parent, bg=PAGE_BG)
        panel.columnconfigure(1, weight=1)
        settings = self.stability_settings
        if settings is None:
            stability_rule_text = "The tracked v6.1 stability settings are invalid; measurement is disabled."
        else:
            stability_rule_text = (
                f"V6.1 DUT attempts 1, 2, and 3 each require "
                f"{DUT_STABILITY_CONFIRMATION_DELTAS} consecutive "
                f"cycle-to-cycle peak deltas at or below {settings.peak_delta_threshold_mv:.3f} mV, "
                f"then {SENSITIVITY_MEASUREMENT_CYCLES} measurement cycles that must stay within "
                f"the same limit. A kick discards that window and restarts the same 10/20 check. "
                f"A third measurement kick fails as unstable. Requalification must finish within "
                f"{STABILITY_TIMEOUT_S:g} seconds of PWM-on."
            )
        ttk.Checkbutton(panel, text="Simulator mode (training only - synthetic data, clearly badged)",
                        variable=self.simulator_var,
                        command=self.on_simulator_toggle).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(panel, text="Sim case", bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11)).grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        ttk.Combobox(panel, textvariable=self.sim_case_var, values=SIM_CASES, state="readonly",
                     width=24, font=self.fb(11)).grid(row=1, column=1, sticky="w", pady=(8, 0))
        if BATTERY_MONITORING_ENABLED:
            ttk.Checkbutton(panel, text="Simulate low battery (test the change-battery lockout)",
                            variable=self.sim_low_battery_var,
                            command=self.refresh_battery).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tk.Label(
            panel,
            text=stability_rule_text,
            bg=PAGE_BG, fg=MUTED_FG, font=self.fb(10), wraplength=S(640), justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))
        tk.Label(
            panel,
            text=(f"ESP32 rig (firmware v2.0): ADS AIN1 = fixed reference/emitter gate, "
                  f"ADS AIN0 = buffered DUT (offset + AC), "
                  f"streamed sync = PWM state, {EMITTER_PWM_CHANNEL} = MOSFET gate. "
                  f"Emitter driven at {EMITTER_PWM_FREQUENCY_HZ:g} Hz for the DUT and "
                  f"{REFERENCE_PWM_FREQUENCY_HZ:g} Hz for the 406MCA reference phases, "
                  f"{EMITTER_PWM_DUTY_CYCLE:g}% duty (fixed). "
                  f"ADS sensor range is ±{WAVEFORM_INPUT_RANGE_V:g} V (PGA ×1, input buffer off) "
                  f"through a unity-gain buffer. "
                  f"AIN1 reference checks are DISABLED by decision (op-amp "
                  f"crosstalk; docs/CALIBRATION_RECORD.md 2.4) - when enabled they must stay "
                  f"within +/-{REFERENCE_TOLERANCE_PERCENT:g}% of calibration. "
                  f"The emitter-off noise test runs BEFORE the driven sensitivity capture "
                  f"(fail-fast): {NOISE_CAPTURE_SECONDS:g} s once the level settles; at most "
                  f"{NOISE_MAX_OVER_FRACTION:.0%} of its {NOISE_WINDOW_S:g} s windows may exceed "
                  f"{NOISE_PP_LIMIT_MV:g} mV pk-pk (provisional TP412 limit). "
                  f"Power: the 6.5 V battery drives the emitters ONLY and the 9 V battery drives "
                  f"the sensors; neither is monitored - the legacy AIN7 divider "
                  f"({BATTERY_DIVIDER_R_TOP_OHMS / 1000:.1f}k/"
                  f"{BATTERY_DIVIDER_R_BOTTOM_OHMS / 1000:.1f}k) cannot read them. "
                  f"TODO: sensor-battery monitoring on AIN6 with a >=4:1 divider."),
            bg=PAGE_BG, fg=MUTED_FG, font=self.fb(10), wraplength=S(640), justify="left",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))
        return panel

    def open_advanced_options(self) -> None:
        """All advanced settings live in their own window: the inline panel
        used to expand below the setup card where it could not be scrolled
        into view on smaller screens."""
        if self._advanced_dialog is not None and self._advanced_dialog.winfo_exists():
            self._advanced_dialog.lift()
            self._advanced_dialog.focus_set()
            return
        dialog = tk.Toplevel(self)
        self._advanced_dialog = dialog
        dialog.title("Advanced options")
        dialog.configure(bg=PAGE_BG)
        dialog.transient(self)
        dialog.minsize(S(760), S(420))
        dialog.geometry(f"+{self.winfo_rootx() + S(240)}+{self.winfo_rooty() + S(130)}")

        frame = tk.Frame(dialog, bg=PAGE_BG)
        frame.grid(row=0, column=0, sticky="nsew", padx=S(22), pady=S(18))
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tk.Label(frame, text="ADVANCED —", bg=PAGE_BG, fg=ELTEC_RED, font=self.fm(11, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(frame, text="Tester configuration", bg=PAGE_BG, fg=TEXT_DARK, font=self.fd(19)).grid(row=1, column=0, sticky="w", pady=(2, S(14)))
        self._build_advanced_panel(frame).grid(row=2, column=0, sticky="new")
        buttons = tk.Frame(frame, bg=PAGE_BG)
        buttons.grid(row=3, column=0, sticky="e", pady=(S(16), 0))
        self.btn(buttons, "Close (Enter)", dialog.destroy, kind="primary", size="sm").grid(row=0, column=0)

        def close(_event: tk.Event | None = None) -> str:
            dialog.destroy()
            return "break"

        dialog.bind("<Return>", close)
        dialog.bind("<KP_Enter>", close)
        dialog.bind("<Escape>", close)
        dialog.focus_set()

    def update_filter_hint(self) -> None:
        if LOW_SENSITIVITY_FAILURE_ENABLED:
            try:
                fail_below_mv, pass_above_mv = sensitivity_raw_limits_mv(
                    self.filter_var.get()
                )
            except ValueError:
                hint = "SENSITIVITY POLICY UNAVAILABLE FOR THIS FILTER"
            else:
                hint = (
                    f"RAW SENSITIVITY: < {fail_below_mv:.2f} mV FAIL  ·  "
                    f"{fail_below_mv:.2f}-{pass_above_mv:.2f} mV PASS + RE-TEST ADVISED  ·  "
                    f"> {pass_above_mv:.2f} mV PASS  ·  DISPLAY ×"
                    f"{SENSITIVITY_LEGACY_EQUIVALENT_FACTOR:.3f}"
                )
        else:
            range_text = ""
            tp412_range = FILTER_RANGES_MV.get(self.filter_var.get())
            if tp412_range is not None:
                range_text = (
                    f"TP412 {tp412_range[0]:.2f}-{tp412_range[1]:.2f} mV "
                    "(LEGACY SCOPE, W/ BLACKENED TUBE + EXTRA -25B)  ·  "
                )
            hint = (
                range_text
                + "SENSITIVITY GATE DISABLED UNTIL THE COMPARISON-BATCH "
                "CALIBRATION FACTOR IS DERIVED"
            )
        self.filter_hint_var.set(hint)

    def reference_gate_ready(self) -> bool:
        if not REFERENCE_GATE_ENABLED:
            return True
        if self.simulator_var.get():
            return True
        calibration = self.reference_calibration
        return calibration is not None and calibration.valid

    def _build_reference_calibration_card(self, row: int) -> None:
        calibration = self.reference_calibration
        simulator = self.simulator_var.get()
        ready = simulator or (calibration is not None and calibration.valid)
        if not REFERENCE_GATE_ENABLED:
            title = "Reference gate disabled (op-amp crosstalk)"
            detail = (
                "The shared dual op-amp buffer lets the sensor under test couple "
                "into the AIN1 reference, so the emitter-health check is off until "
                "the channel-isolated buffer board is installed. If several sensors "
                "in a row fail low sensitivity, suspect the emitter first."
            )
            accent = WARN_ACCENT
        elif simulator:
            title = "Reference unit simulated"
            detail = "Training mode uses a synthetic reference reading; hardware calibration is unchanged."
            accent = WARN_ACCENT
        elif self.reference_calibrating:
            title = "Calibrating reference unit…"
            detail = self.reference_progress_var.get() or (
                f"Collecting {REFERENCE_CALIBRATION_READINGS} stable readings."
            )
            accent = ELTEC_BLUE_BRIGHT
        elif calibration is not None and calibration.valid:
            title = "Reference unit calibrated"
            detail = "Ready. The reference unit will be checked automatically before every sensor."
            accent = PASS_ACCENT
        elif calibration is not None:
            title = "Reference unit lockout — recalibration required"
            detail = calibration.invalidation_reason or (
                "The previous calibration is invalid. Replace/check the emitter and calibrate again."
            )
            accent = FAIL_ACCENT
        else:
            title = "Reference unit calibration required"
            detail = self.reference_calibration_error or (
                f"Install a known-good/new emitter, then collect {REFERENCE_CALIBRATION_READINGS} "
                "stable reference readings. Sensor testing stays locked until this is complete."
            )
            accent = FAIL_ACCENT

        bg = CARD_BG
        card = Card(
            self.step_frame,
            card_bg=bg,
            border=mix_color(accent, bg, 0.60),
            accent_stops=[accent, accent],
            pad=(18, 14),
        )
        card.grid(row=row, column=0, sticky="ew", pady=(S(16), 0))
        inner = card.inner
        inner.columnconfigure(0, weight=1)
        tk.Label(inner, text=title, bg=bg, fg=TEXT_DARK, font=self.fb(14, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        detail_options = (
            {"textvariable": self.reference_progress_var}
            if self.reference_calibrating
            else {"text": detail}
        )
        tk.Label(
            inner,
            bg=bg,
            fg=MUTED_FG,
            font=self.fb(11),
            wraplength=S(650),
            justify="left",
            **detail_options,
        ).grid(row=1, column=0, sticky="w", pady=(S(4), 0))
        if not simulator and REFERENCE_GATE_ENABLED:
            button_text = "Recalibrate reference unit" if ready else "Calibrate reference unit"
            button = self.btn(
                inner,
                button_text,
                self.run_reference_calibration,
                kind="outline" if ready else "primary",
                size="sm",
                parent_bg=bg,
            )
            button.grid(row=0, column=1, rowspan=2, padx=(S(16), 0))
            if self.busy or self.measuring or self.reference_calibrating:
                button.configure(state="disabled")

    def render_load_step(self) -> None:
        self._step_heading(
            0,
            "02",
            f"Load sensor {self.current_sensor_id}"
            + ("  (skipped part)" if self.resuming_skipped else ""),
            f"Batch {self.batch_number}    ·    Filter: {self.filter_setup}",
        )

        self._build_reference_calibration_card(row=1)

        card = Card(self.step_frame, accent_stops=TECH_GRADIENT)
        card.grid(row=2, column=0, sticky="ew", pady=(22, 0))
        inner = card.inner
        inner.columnconfigure(1, weight=1)
        rig = tk.Canvas(inner, width=S(190), height=S(128), bg=CARD_BG, highlightthickness=0, bd=0)
        rig.grid(row=0, column=0, padx=(0, S(24)))
        self._draw_rig_illustration(rig)
        text_col = tk.Frame(inner, bg=CARD_BG)
        text_col.grid(row=0, column=1, sticky="w")
        tk.Label(text_col, text="Place the sensor in the testing rig", bg=CARD_BG, fg=TEXT_DARK, font=self.fd(24)).pack(anchor="w")
        tk.Label(text_col, text="Then press Enter to read the offset and run the emitter test.", bg=CARD_BG, fg=MUTED_FG, font=self.fb(13)).pack(anchor="w", pady=(6, 0))
        chips = tk.Frame(text_col, bg=CARD_BG)
        chips.pack(anchor="w", pady=(14, 0))
        for chip_text in (f"SENSOR {self.current_sensor_id}", f"{EMITTER_PWM_FREQUENCY_HZ:g} Hz · 50% DUTY", "GAIN ×1 BUFFER"):
            chip = tk.Label(chips, text=chip_text, bg=ELTEC_BLUE_LIGHT, fg=ELTEC_BLUE_DARK, font=self.fm(9, "bold"), padx=10, pady=4)
            chip.pack(side="left", padx=(0, 8))
        ToggleSwitch(
            text_col,
            f"Extended noise soak ({NOISE_SOAK_CAPTURE_SECONDS:.0f} s) for this sensor",
            self.noise_soak_var,
            font=self.fb(12),
            bg=CARD_BG,
        ).pack(anchor="w", pady=(S(12), 0))
        tk.Label(
            text_col,
            text=(
                "For suspect parts with come-and-go burst noise: 3× the noise "
                "observation with the same 3-window allowance. Resets after "
                "each sensor."
            ),
            bg=CARD_BG,
            fg=MUTED_FG,
            font=self.fb(10),
            wraplength=S(520),
            justify="left",
        ).pack(anchor="w", pady=(2, 0))
        self._build_battery_banner()

    def _draw_rig_illustration(self, rig: tk.Canvas) -> None:
        """Draw the rig glyph with a pulsing emitter glow."""
        draw_round_rect(rig, S(24), S(18), S(166), S(96), Sf(10), fill=NAVY, outline=NAVY_EDGE)
        draw_round_rect(rig, S(50), S(38), S(140), S(76), Sf(7), fill="#111b31", outline="#2a3a5f")
        rig.create_line(S(18), S(102), S(172), S(102), fill=ELTEC_BLUE_DARK, width=Sf(5), capstyle="round")
        rig.create_text(S(95), S(114), text="EMITTER RIG", fill=MUTED_FG, font=self.fm(9, "bold"))
        glow = rig.create_oval(0, 0, 0, 0, fill="", outline="")
        core = rig.create_oval(0, 0, 0, 0, fill=ELTEC_RED, outline="")

        def frame(t: float) -> None:
            pulse = 0.5 + 0.5 * math.sin(t * 2 * math.pi)
            cx, cy = Sf(95), Sf(57)
            core_rx = Sf(min(24.0, (7.0 + 1.5 * pulse) * 1.7))
            core_ry = Sf(7.0 + 1.5 * pulse)
            glow_rx = Sf(min(40.0, (7.0 + 1.5 * pulse) * 1.7 + 8 + 5.0 * pulse))
            glow_ry = Sf(min(14.0, 7.0 + 1.5 * pulse + 4 + 2.5 * pulse))
            rig.coords(core, cx - core_rx, cy - core_ry, cx + core_rx, cy + core_ry)
            rig.coords(glow, cx - glow_rx, cy - glow_ry, cx + glow_rx, cy + glow_ry)
            rig.itemconfigure(glow, fill=mix_color("#111b31", ELTEC_RED, 0.22 + 0.18 * pulse))
            rig.itemconfigure(core, fill=mix_color(ELTEC_RED, "#ff7a92", 0.5 * pulse))

        self.animator.animate("step:rig", 2000, frame, easing=None, loop=True)

    def render_result_step(self) -> None:
        if self.measuring:
            self.render_measuring_view()
        elif self.last_result is not None:
            self.render_result_view()
        elif self.last_measure_error is not None:
            self.render_measure_fault_view()
        else:
            self._step_heading(0, "03", f"{self.current_sensor_id}: ready to measure", "Press Enter (or Measure) to run the emitter test.")
            self.btn(self.step_frame, "Measure", self.run_measurement, kind="primary", size="lg").grid(row=2, column=0, sticky="w", pady=(22, 0))
        if not self.measuring:
            self._build_battery_banner()

    def render_measure_fault_view(self) -> None:
        """Nothing was recorded: offer a retry AND a way past this sensor.

        A rig or serial fault used to leave the technician with Measure as the
        only option, which blocks the whole batch on one sensor that may not
        even be at fault. Skipping writes a NOT MEASURED row with a reason, so
        the sensor is accounted for without inventing a verdict for it.
        """
        self._step_heading(
            0,
            "03",
            f"{self.current_sensor_id}: nothing was recorded",
            "Re-measure or Skip part below, or record this sensor as NOT MEASURED.",
        )
        card = Card(
            self.step_frame,
            card_bg=FAIL_BG,
            border=mix_color(FAIL_ACCENT, FAIL_BG, 0.45),
            accent_stops=[FAIL_ACCENT, FAIL_ACCENT],
            pad=(18, 14),
        )
        card.grid(row=1, column=0, sticky="ew", pady=(16, 0))
        inner = card.inner
        inner.columnconfigure(1, weight=1)
        tk.Label(inner, text="⚠", bg=FAIL_BG, fg=FAIL_ACCENT, font=self.fd(22)).grid(
            row=0, column=0, padx=(0, S(12)), sticky="n"
        )
        tk.Label(
            inner,
            text="LAST ATTEMPT FAILED",
            bg=FAIL_BG,
            fg=FAIL_FG,
            font=self.fm(10, "bold"),
        ).grid(row=0, column=1, sticky="w")
        tk.Label(
            inner,
            text=self.last_measure_error or "",
            bg=FAIL_BG,
            fg=FAIL_FG,
            font=self.fb(12),
            wraplength=S(700),
            justify="left",
        ).grid(row=1, column=1, sticky="w", pady=(6, 0))

        buttons = tk.Frame(self.step_frame, bg=PAGE_BG)
        buttons.grid(row=2, column=0, sticky="w", pady=(22, 0))
        self.btn(
            buttons,
            "Record as NOT MEASURED",
            self.open_skip_sensor_window,
            kind="outline",
            size="lg",
        ).grid(row=0, column=0)
        tk.Label(
            self.step_frame,
            text=(
                f"{OUTCOME_NOT_MEASURED} writes a verdict row with no offset, sensitivity "
                "or polarity and leaves the sensor out of the yield. Skip part keeps it "
                "open to measure later instead."
            ),
            bg=PAGE_BG,
            fg=MUTED_FG,
            font=self.fb(11),
            wraplength=S(700),
            justify="left",
        ).grid(row=3, column=0, sticky="w", pady=(10, 0))

    def _build_battery_banner(self) -> None:
        """Show a yellow low-warning strip or a red block, with a re-check button."""
        if not BATTERY_MONITORING_ENABLED:
            return
        if self.battery_state not in ("warn", "low", "fault"):
            return
        blocked = self.battery_state in ("low", "fault")
        bg = FAIL_BG if blocked else WARN_BG
        fg = FAIL_FG if blocked else WARN_FG
        accent = FAIL_ACCENT if blocked else WARN_ACCENT
        volts = "" if self.battery_v is None else f" ({self.battery_v:.2f} V)"
        if self.battery_state == "fault":
            message = (f"Battery reads{volts}, which is not a valid battery level — the battery or the "
                       "divider is probably not connected. Check the battery clip and rig wiring, then re-check.")
        elif self.battery_state == "low":
            message = f"Recharge the sensor battery{volts}. Testing is blocked until it is charged and re-checked."
        else:
            message = f"Battery is getting low{volts}. Swap it soon — testing is still allowed."
        card = Card(self.step_frame, card_bg=bg, border=mix_color(accent, bg, 0.45), accent_stops=[accent, accent], pad=(18, 12))
        card.grid(row=8, column=0, sticky="ew", pady=(16, 0))
        inner = card.inner
        inner.columnconfigure(1, weight=1)
        tk.Label(inner, text="⚠", bg=bg, fg=accent, font=self.fd(22)).grid(row=0, column=0, padx=(0, S(12)))
        tk.Label(inner, text=message, bg=bg, fg=fg, font=self.fb(13, "bold"), wraplength=S(620), justify="left").grid(row=0, column=1, sticky="w")
        self.btn(inner, "Re-check battery", self.refresh_battery, kind="outline", size="sm", parent_bg=bg).grid(row=0, column=2, padx=(S(12), 0))

    def render_measuring_view(self) -> None:
        head = tk.Frame(self.step_frame, bg=PAGE_BG)
        head.grid(row=0, column=0, sticky="ew")
        PulseDot(head, self.animator, "step:pulse", color=ELTEC_RED, bg=PAGE_BG, size=18).pack(side="left", padx=(0, 10), pady=(6, 0))
        tk.Label(head, text=f"{self.current_sensor_id}: measuring…", bg=PAGE_BG, fg=ELTEC_BLUE_DARK, font=self.fd(28)).pack(side="left")
        tk.Label(self.step_frame, textvariable=self.measure_status_var, bg=PAGE_BG, fg=MUTED_FG, font=self.fb(15)).grid(row=1, column=0, sticky="w", pady=(10, 0))
        self._build_progress_bar(row=2)
        ToggleSwitch(self.step_frame, "Show live waveform while reading", self.show_live_var, command=self.toggle_live_view, font=self.fb(12)).grid(row=3, column=0, sticky="w", pady=(S(14), 0))
        if self.show_live_var.get():
            self._build_wave_canvas(row=4, live=True)

    def _build_progress_bar(self, row: int) -> None:
        """Step-ladder progress bar: which test step is running and how far.

        The label reads e.g. "STEP 3/4 — NOISE (EMITTER OFF)"; the bar fills
        continuously as the sequence proceeds, with tick marks at the step
        boundaries so the position reads off directly.
        """
        holder = tk.Frame(self.step_frame, bg=PAGE_BG)
        holder.grid(row=row, column=0, sticky="ew", pady=(S(16), 0))
        holder.columnconfigure(0, weight=1)
        tk.Label(
            holder,
            textvariable=self.measure_step_var,
            bg=PAGE_BG,
            fg=ELTEC_BLUE_DARK,
            font=self.fm(11, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        bar = tk.Canvas(holder, height=S(14), bg=PAGE_BG, highlightthickness=0, bd=0)
        bar.grid(row=1, column=0, sticky="ew", pady=(S(6), 0))
        self.measure_progress_canvas = bar
        bar.bind("<Configure>", lambda _e: self._redraw_measure_progress())
        self._redraw_measure_progress()

    def _redraw_measure_progress(self) -> None:
        bar = self.measure_progress_canvas
        if bar is None or not bar.winfo_exists():
            return
        bar.delete("all")
        width = max(1, bar.winfo_width())
        height = max(1, bar.winfo_height())
        total = max(1, self.measure_progress_total)
        step = self.measure_progress_step
        overall = 0.0
        if step > 0:
            overall = min(
                1.0, (step - 1 + self.measure_progress_fraction) / total
            )
        top = max(0, (height - S(8)) // 2)
        bottom = top + S(8)
        draw_round_rect(bar, 0, top, width, bottom, Sf(4), fill=GHOST_BG, outline="")
        if overall > 0.0:
            fill_width = max(S(8), int(round(width * overall)))
            draw_round_rect(
                bar, 0, top, fill_width, bottom, Sf(4),
                fill=mix_color(ELTEC_BLUE, ELTEC_BLUE_BRIGHT, 0.25), outline="",
            )
        # Step boundaries as ticks so "step 2 of 4" reads off the bar.
        for boundary in range(1, total):
            x = int(round(width * boundary / total))
            bar.create_line(x, top - S(2), x, bottom + S(2), fill=PAGE_BG, width=Sf(3))
            bar.create_line(x, top - S(2), x, bottom + S(2), fill=STEP_IDLE, width=Sf(1.2))

    def render_result_view(self) -> None:
        result = self.last_result
        outcome = result_outcome(result)
        passed = outcome == OUTCOME_PASS
        near_limit = passed and is_sensitivity_near_limit(result)
        self._build_result_banner(row=0, outcome=outcome, near_limit=near_limit)
        next_row = 1
        if near_limit:
            # The sensor passed. Its sensitivity is inside the margin of error
            # of the conversion factor, so tell the technician and suggest a
            # re-measure - saving it as-is records a normal PASS.
            warn_card = Card(
                self.step_frame,
                card_bg=WARN_BG,
                border=mix_color(WARN_ACCENT, WARN_BG, 0.45),
                accent_stops=[WARN_ACCENT, WARN_ACCENT],
                pad=(18, 14),
            )
            warn_card.grid(row=next_row, column=0, sticky="ew", pady=(14, 0))
            warn_inner = warn_card.inner
            warn_inner.columnconfigure(0, weight=1)
            tk.Label(
                warn_inner,
                text="PASSED - SENSITIVITY NEAR THE LIMIT",
                bg=WARN_BG,
                fg=WARN_FG,
                font=self.fm(10, "bold"),
            ).grid(row=0, column=0, sticky="w", pady=(0, 5))
            tk.Label(
                warn_inner,
                text=self._near_limit_message(result),
                bg=WARN_BG,
                fg=WARN_FG,
                font=self.fb(11),
                justify="left",
                anchor="w",
                wraplength=S(900),
            ).grid(row=1, column=0, sticky="ew")
            next_row += 1
        if not passed:
            card_bg = FAIL_BG
            card_fg = FAIL_FG
            card_accent = FAIL_ACCENT
            failure_card = Card(
                self.step_frame,
                card_bg=card_bg,
                border=mix_color(card_accent, card_bg, 0.45),
                accent_stops=[card_accent, card_accent],
                pad=(18, 14),
            )
            failure_card.grid(row=next_row, column=0, sticky="ew", pady=(14, 0))
            failure_inner = failure_card.inner
            failure_inner.columnconfigure(0, weight=1)
            tk.Label(
                failure_inner,
                text="FAILURE MODE",
                bg=card_bg,
                fg=card_fg,
                font=self.fm(10, "bold"),
            ).grid(row=0, column=0, sticky="w", pady=(0, 5))
            failure_combo = ttk.Combobox(
                failure_inner,
                textvariable=self.failure_mode_var,
                values=FAILURE_MODE_CHOICES,
                state="readonly",
                font=self.fb(14),
                height=10,
            )
            failure_combo.grid(row=1, column=0, sticky="ew")
            failure_combo.bind(
                "<<ComboboxSelected>>",
                lambda _event: self.write_autosave("failure_mode_selected"),
            )
            tk.Label(
                failure_inner,
                text="Confirm or change the failure mode, then save the sensor.",
                bg=card_bg,
                fg=card_fg,
                font=self.fb(10),
            ).grid(row=2, column=0, sticky="w", pady=(6, 0))
            next_row += 1
        if self.show_details_var.get():
            tiles = tk.Frame(self.step_frame, bg=PAGE_BG)
            tiles.grid(row=next_row, column=0, sticky="ew", pady=(14, 0))
            for column in range(4):
                tiles.columnconfigure(column, weight=1, uniform="tiles")
            offset_ok = result.offset_v is not None and OFFSET_MIN_V <= result.offset_v <= OFFSET_MAX_V
            fail_below_mv, pass_above_mv = sensitivity_raw_limits_mv(self.filter_setup)
            sensitivity_outcome = (
                ""
                if result.sensitivity_mv is None
                else sensitivity_gate_outcome(result.sensitivity_mv, self.filter_setup)
            )
            sensitivity_equivalent_mv = (
                None
                if result.sensitivity_mv is None
                else legacy_equivalent_sensitivity_mv(result.sensitivity_mv)
            )
            pol_verdict = polarity_good_bad(result.polarity)
            self._result_tile(tiles, 0, "Offset", result.offset_v, offset_ok, unit=" V", decimals=3)
            self._result_tile(
                tiles,
                1,
                "Sensitivity (equiv.)",
                sensitivity_equivalent_mv,
                sensitivity_outcome == OUTCOME_PASS,
                unit=" mV",
                decimals=2,
                accent_override=(
                    WARN_ACCENT if sensitivity_outcome == SENSITIVITY_NEAR_LIMIT else None
                ),
            )
            self._result_tile(tiles, 2, "Polarity", pol_verdict or None, pol_verdict == "GOOD")
            noise_report = self.last_noise_report
            # Verdict only, deliberately no magnitude: this rig reads the
            # sensor pin in µV while the legacy station reads mV behind its
            # amplifier chain, so a number here invites a false comparison.
            # The full telemetry stays in the batch CSV and the snapshot.
            if noise_report is None or noise_report.outcome == "SKIPPED":
                self._result_tile(
                    tiles, 3, "Noise", "Skipped", False,
                    accent_override=WARN_ACCENT,
                )
            else:
                self._result_tile(
                    tiles,
                    3,
                    "Noise",
                    noise_report.outcome,
                    noise_report.outcome == OUTCOME_PASS,
                )
            next_row += 1

            detail_bits = [f"Filter: {self.filter_setup}"]
            if LOW_SENSITIVITY_FAILURE_ENABLED:
                detail_bits.append(
                    f"raw sensitivity {result.sensitivity_mv:.3f} mV"
                    if result.sensitivity_mv is not None
                    else "raw sensitivity not measured"
                )
                detail_bits.append(
                    f"raw gate <{fail_below_mv:.2f} fail / "
                    f"{fail_below_mv:.2f}-{pass_above_mv:.2f} pass, re-test advised / "
                    f">{pass_above_mv:.2f} pass"
                )
                detail_bits.append(
                    f"legacy-equivalent factor ×{SENSITIVITY_LEGACY_EQUIVALENT_FACTOR:.3f}"
                )
            else:
                detail_bits.append("sensitivity failure check disabled")
            if result.polarity and polarity_good_bad(result.polarity):
                detail_bits.append(f"polarity {result.polarity}")
            pol_detail = format_polarity_detail(self.last_metrics)
            if pol_detail:
                detail_bits.append(pol_detail)
            if self.last_metrics is not None and self.last_metrics.signal_to_noise_db is not None \
                    and math.isfinite(self.last_metrics.signal_to_noise_db):
                detail_bits.append(f"SNR {self.last_metrics.signal_to_noise_db:.1f} dB")
            if noise_report is not None:
                if noise_report.outcome == "SKIPPED":
                    detail_bits.append(
                        "noise test SKIPPED"
                        + (f" ({noise_report.skip_reason})" if noise_report.skip_reason else "")
                    )
                else:
                    # Same reasoning as the noise tile: verdict, not levels.
                    noise_bit = f"noise {noise_report.outcome}"
                    if noise_report.baseline_settled is False:
                        noise_bit += " (level still moving at the wait deadline)"
                    detail_bits.append(noise_bit)
            report = self.last_capture_report
            if report is not None and report.data_source == "simulator":
                detail_bits.append("SIMULATED DATA")
            if self.last_reference_check_mv is not None:
                calibration = self.reference_calibration
                if calibration is not None and calibration.valid:
                    detail_bits.append(
                        f"reference {self.last_reference_check_mv:.2f} mV "
                        f"({calibration.drift_percent(self.last_reference_check_mv):+.1f}%)"
                    )
                else:
                    detail_bits.append(f"reference {self.last_reference_check_mv:.2f} mV")
            if report is not None and report.capture_cycles:
                detail_bits.append(f"PWM on {report.pwm_on_seconds:.1f} s / {report.capture_cycles} cycles")
            if report is not None and report.stabilized:
                detail_bits.append(
                    f"stable at {report.stabilization_seconds:.1f} s / cycle {report.stabilization_cycle}"
                )
                detail_bits.append(
                    f"attempt {report.measurement_attempt} / sensitivity window "
                    f"{report.measurement_cycles} cycles"
                )
            elif report is not None and report.unstable:
                detail_bits.append(
                    f"unstable on attempt {report.measurement_attempt}"
                )
            tk.Label(
                self.step_frame,
                text="   ·   ".join(detail_bits),
                bg=PAGE_BG,
                fg=MUTED_FG,
                font=self.fb(11),
                wraplength=S(880),
                justify="left",
                anchor="w",
            ).grid(row=next_row, column=0, sticky="w", pady=(S(12), 0))
            next_row += 1

            if result.fail_reasons:
                reasons = tk.Text(
                    self.step_frame,
                    height=min(len(result.fail_reasons) + 1, 4),
                    wrap="word",
                    font=self.fb(12),
                    relief="flat",
                    bd=0,
                    bg="#fff5f5",
                    fg=FAIL_FG,
                    padx=12,
                    pady=10,
                    highlightbackground="#f3c2c2",
                    highlightcolor="#f3c2c2",
                    highlightthickness=1,
                )
                reasons.grid(row=next_row, column=0, sticky="ew", pady=(10, 0))
                reasons.insert("1.0", "\n".join(f"•  {reason}" for reason in result.fail_reasons))
                reasons.configure(state="disabled")
                next_row += 1

        tools = tk.Frame(self.step_frame, bg=PAGE_BG)
        tools.grid(row=next_row, column=0, sticky="w", pady=(16, 0))
        self.btn(tools, "Comment", self.open_comment_window, kind="ghost", size="sm").grid(row=0, column=0, padx=(0, 10))
        self.btn(tools, "Capture waveform", self.capture_waveform_snapshot, kind="ghost", size="sm").grid(row=0, column=1, padx=(0, 10))
        noise_capture_button = self.btn(
            tools,
            "Save noise capture",
            self.save_noise_capture_for_analysis,
            kind="ghost",
            size="sm",
        )
        noise_capture_button.grid(row=0, column=2, padx=(0, 10))
        if self.last_noise_raw_waveform is None:
            noise_capture_button.configure(state="disabled")
        ToggleSwitch(
            tools,
            "Show test details",
            self.show_details_var,
            command=self.toggle_result_details,
            font=self.fb(12),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(S(12), 0))
        ToggleSwitch(
            tools,
            "Show waveform",
            self.show_live_var,
            command=self.toggle_live_view,
            font=self.fb(12),
        ).grid(row=1, column=2, sticky="w", pady=(S(12), 0))
        tk.Label(tools, textvariable=self.comment_status_var, bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11)).grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        tk.Label(tools, textvariable=self.snapshot_status_var, bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11)).grid(row=3, column=0, columnspan=4, sticky="w")
        tk.Label(tools, textvariable=self.noise_capture_status_var, bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11)).grid(row=4, column=0, columnspan=4, sticky="w")

        if self.show_live_var.get():
            self._build_wave_canvas(row=next_row + 1, live=False)
            self.redraw_waveform()

    def _near_limit_message(self, result: FinalResult) -> str:
        fail_below_mv, pass_above_mv = sensitivity_raw_limits_mv(self.filter_setup)
        raw_mv = result.sensitivity_mv
        reading = (
            f"Raw {raw_mv:.3f} mV (≈{legacy_equivalent_sensitivity_mv(raw_mv):.2f} mV "
            f"legacy-equivalent) is inside the {fail_below_mv:.2f}-{pass_above_mv:.2f} mV "
            "band around the limit"
            if raw_mv is not None and math.isfinite(raw_mv)
            else "The sensitivity reading is inside the band around the limit"
        )
        return (
            f"{reading}, within the margin of error of the ×"
            f"{SENSITIVITY_LEGACY_EQUIVALENT_FACTOR:.2f} conversion factor. "
            "Suggestion: Re-measure to confirm. No quarantine is needed - "
            "if you move on, this sensor is saved as a PASS."
        )

    def _build_result_banner(self, row: int, outcome: str, near_limit: bool = False) -> None:
        if outcome == OUTCOME_PASS and near_limit:
            accent, banner_bg, banner_fg = PASS_ACCENT, PASS_BG, PASS_FG
            glyph, verdict = "✓", "PASS · NEAR LIMIT"
        elif outcome == OUTCOME_PASS:
            accent, banner_bg, banner_fg = PASS_ACCENT, PASS_BG, PASS_FG
            glyph, verdict = "✓", OUTCOME_PASS
        else:
            accent, banner_bg, banner_fg = FAIL_ACCENT, FAIL_BG, FAIL_FG
            glyph, verdict = "✕", OUTCOME_FAIL
        banner = tk.Canvas(self.step_frame, height=S(112), bg=PAGE_BG, highlightthickness=0, bd=0)
        banner.grid(row=row, column=0, sticky="ew")
        vals = {"bar": 0.0, "glyph": 0.0, "text": 0.0}

        def redraw() -> None:
            banner.delete("all")
            width = max(1, banner.winfo_width())
            height = S(112)
            draw_round_rect(banner, S(2), S(2), width - S(2), height - S(2), Sf(14), fill=banner_bg, outline=mix_color(accent, banner_bg, 0.55))
            bar_h = (height - S(16)) * vals["bar"]
            if bar_h > 2:
                draw_round_rect(banner, S(10), S(8) + (height - S(16) - bar_h) / 2, S(18), S(8) + (height - S(16) + bar_h) / 2, Sf(4), fill=accent, outline="")
            glyph_size = max(1, int(round(6 + 38 * vals["glyph"])))
            center_x = width / 2
            banner.create_text(center_x - S(78), height / 2, text=glyph, fill=accent, font=self.fd(glyph_size))
            text_x = center_x - S(30) + S(26) * (1.0 - vals["text"])
            text_color = mix_color(banner_bg, banner_fg, vals["text"])
            banner.create_text(text_x, height / 2, anchor="w", text=verdict, fill=text_color, font=self.fd(38))

        def animate(key: str, name: str, duration: int, easing, delay: int) -> None:
            def frame(t: float) -> None:
                vals[key] = t
                redraw()
            self.animator.animate(name, duration, frame, easing=easing, delay_ms=delay)

        banner.bind("<Configure>", lambda _e: redraw())
        animate("bar", "step:banner_bar", 420, ease_out_cubic, 0)
        animate("glyph", "step:banner_glyph", 520, ease_out_back, 120)
        animate("text", "step:banner_text", 420, ease_out_cubic, 220)

    def _result_tile(
        self,
        parent: tk.Frame,
        column: int,
        label: str,
        value,
        ok: bool,
        unit: str = "",
        decimals: int = 2,
        accent_override: str | None = None,
    ) -> None:
        accent = accent_override or (PASS_ACCENT if ok else FAIL_ACCENT)
        card = Card(parent, accent_stops=[accent, accent], pad=(18, 14))
        card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else S(12), 0))
        inner = card.inner
        tk.Label(inner, text=label.upper(), bg=CARD_BG, fg=MUTED_FG, font=self.fm(10, "bold")).pack(anchor="w")
        # Fixed character width so the count-up animation never changes the
        # label's requested size (a growing label would relayout the whole
        # step frame on every animation frame).
        value_label = tk.Label(inner, bg=CARD_BG, fg=accent, font=self.fd(25), width=12, anchor="w")
        value_label.pack(anchor="w", pady=(S(4), 0))
        if isinstance(value, (int, float)):
            target = float(value)

            def frame(t: float) -> None:
                value_label.configure(text=f"{target * t:.{decimals}f}{unit}")

            frame(0.0)
            self.animator.animate(f"step:tile{column}", 700, frame, easing=ease_out_cubic, delay_ms=140 + column * 110)
        elif value is None:
            value_label.configure(text="Not measured", font=self.fd(17))
        else:
            # Fade the color in rather than animating the font size: a size
            # animation changes the label's requested size every frame, which
            # would relayout the whole step frame at 60 fps.
            value_label.configure(text=str(value))

            def fade(t: float) -> None:
                value_label.configure(fg=mix_color(CARD_BG, accent, t))

            fade(0.0)
            self.animator.animate(f"step:tile{column}", 520, fade, easing=ease_in_out, delay_ms=140 + column * 110)

    def _build_wave_canvas(self, row: int, live: bool) -> None:
        # minsize keeps the scope readable even when the step content above it
        # (e.g. a FAIL view with reasons) is tall.
        self.step_frame.rowconfigure(row, weight=1, minsize=S(170))
        wrapper = tk.Frame(self.step_frame, bg=PAGE_BG)
        wrapper.grid(row=row, column=0, sticky="nsew", pady=(S(14), 0))
        wrapper.columnconfigure(1, weight=1)
        wrapper.rowconfigure(1, weight=1)
        if live:
            PulseDot(wrapper, self.animator, "step:live", color=ELTEC_RED, bg=PAGE_BG, size=13).grid(row=0, column=0, padx=(0, S(6)))
            tk.Label(wrapper, textvariable=self.live_wave_header_var, bg=PAGE_BG, fg=MUTED_FG, font=self.fm(10, "bold")).grid(row=0, column=1, sticky="w")
        else:
            tk.Label(
                wrapper,
                text="DRIVEN CAPTURE  ·  ADS AIN0 SENSOR + PWM SYNC OVERLAY (HIGH = EMITTER ON)",
                bg=PAGE_BG, fg=MUTED_FG, font=self.fm(10, "bold"),
            ).grid(row=0, column=1, sticky="w")
        scope = ScopeView(
            wrapper,
            self.animator,
            "step:scope",
            height=240,
            empty_text=(
                "SIGNAL APPEARS HERE DURING MEASUREMENT"
                if live
                else "NO DRIVEN CAPTURE — THE TEST ENDED BEFORE THE SENSITIVITY STEP"
            ),
        )
        scope.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(S(6), 0))
        self.wave_canvas = scope
        if live:
            # A measurement may already be in its noise step when the live
            # scope is toggled on - apply the current display mode.
            self._apply_preview_display_mode()
        scope.set_data(self.preview_waveform, self.preview_sync)

        # Result view: also show the emitter-off noise capture so the noise
        # verdict can be verified visually. The noise scope uses the RELATIVE
        # range display: deviation from the capture mean (µV at the current
        # ~429 µV pin-level limit) with solid red cutoff lines at ±limit/2, so
        # "does it cross the lines" is the whole reading. min_span_v (2x the
        # limit) keeps the y-axis from zooming into the noise, so a passing
        # part's noise looks small. The stored trace is the band-limited one
        # the verdict was computed from (see NOISE_DECIMATION_FACTOR).
        self.noise_canvas = None
        noise_metrics = self.last_noise_metrics
        if not live and noise_metrics is not None and noise_metrics.waveform_v.size >= 2:
            wrapper.rowconfigure(3, weight=1)
            noise_caption = (
                f"EMITTER-OFF NOISE  ·  {NOISE_CAPTURE_SECONDS:.0f} s BAND-LIMITED CAPTURE, EACH 1 s WINDOW AROUND ITS OWN BASELINE  ·  "
                f"RED = ±{format_noise_pp(NOISE_PP_LIMIT_MV / 2, decimals=1)} CUTOFF, PASS IF ≤ "
                f"{NOISE_MAX_OVER_FRACTION * 100:.0f}% OF 1 s WINDOWS EXCEED "
                f"{format_noise_pp(NOISE_PP_LIMIT_MV)} PK-PK "
                f"(= {NOISE_LEGACY_PP_LIMIT_MV:.0f} mV ON THE LEGACY SCOPE, ×{NOISE_EFFECTIVE_CHAIN_FACTOR:.0f} EFFECTIVE CHAIN)"
            )
            tk.Label(wrapper, text=noise_caption, bg=PAGE_BG, fg=MUTED_FG, font=self.fm(10, "bold")).grid(row=2, column=0, columnspan=2, sticky="w", pady=(S(12), 0))
            noise_scope = ScopeView(
                wrapper,
                self.animator,
                "step:noise",
                height=190,
                sample_rate_hz=noise_metrics.sample_rate_hz,
                channel_label="AIN0 · NOISE RANGE (EMITTER OFF)",
                empty_text="",
                min_span_v=2.0 * NOISE_PP_LIMIT_MV / 1000.0,
                relative_band_mv=NOISE_PP_LIMIT_MV,
            )
            noise_scope.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(S(6), 0))
            self.noise_canvas = noise_scope
            noise_scope.set_data(
                noise_metrics.waveform_v, np.array([], dtype=float)
            )

    # ----- navigation ----- #
    #
    # v2.0 action bar, left -> right:
    #   Back · Measure skipped (N)   ...   Skip part · Re-measure ·
    #   Save + Exit Batch · Save + Next Sensor
    # Colour carries the meaning: green = save and move on, amber = set the
    # part aside for later, blue outline = run the test again.
    #
    # The buttons sit in two groups (navigation left, actions right) so
    # _fit_footer can shrink or wrap the bar on a narrow screen instead of
    # letting the rightmost button run off the edge.
    FOOTER_GAP = 10
    # Tried in order: (action size, nav size, label tier, rows). Label
    # tiers: "full" -> "nohint" (no "(Enter)"/"(Esc)") -> "short"
    # (compact wording). Full-size buttons on two rows beat small
    # buttons on one, so wrapping comes before shrinking.
    FOOTER_VARIANTS = (
        ("xl", "lg", "full", 1),
        ("xl", "lg", "nohint", 1),
        ("xl", "lg", "short", 1),
        ("xl", "lg", "short", 2),
        ("lg", "md", "short", 2),
        ("md", "sm", "short", 2),
    )

    def render_navigation(self) -> None:
        # The footer bar is a fixed row below the step frame (built once in
        # _build_layout), so the buttons can never be pushed off-screen by
        # tall step content. Rebuild its buttons for the current step.
        for child in self.footer_bar.winfo_children():
            child.destroy()
        self.footer_bar.columnconfigure(0, weight=1)
        self.footer_bar.columnconfigure(1, weight=0)
        self.footer_left = tk.Frame(self.footer_bar, bg=PAGE_BG)
        self.footer_right = tk.Frame(self.footer_bar, bg=PAGE_BG)
        result_step = self.step == self.RESULT_STEP
        gap = S(self.FOOTER_GAP)

        self.back_button = self.btn(self.footer_left, "Back", self.go_back, kind="ghost", size="lg")
        self.back_button.grid(row=0, column=0, sticky="w")
        self.skipped_button = self.btn(self.footer_left, "Measure skipped", self.measure_skipped, kind="outline", size="lg")
        self.skipped_button.grid(row=0, column=1, sticky="w", padx=(gap, 0))

        self.skip_button = self.btn(self.footer_right, "Skip part", self.open_skip_window, kind="warn", size="xl")
        self.skip_button.grid(row=0, column=0, sticky="e", padx=(0, gap))
        self.remeasure_button = self.btn(self.footer_right, "Re-measure", self.run_measurement, kind="outline", size="xl")
        self.remeasure_button.grid(row=0, column=1, sticky="e", padx=(0, gap))
        self.secondary_button = self.btn(self.footer_right, "Save + Exit Batch", self.save_and_end_batch, kind="outline", size="xl")
        self.secondary_button.grid(row=0, column=2, sticky="e", padx=(0, gap))
        self.primary_button = self.btn(
            self.footer_right, "Next", self.go_next, kind="success" if result_step else "primary", size="xl"
        )
        self.primary_button.grid(row=0, column=3, sticky="e")

        self.footer_nav_buttons = (self.back_button, self.skipped_button)
        self.footer_action_buttons = (
            self.skip_button,
            self.remeasure_button,
            self.secondary_button,
            self.primary_button,
        )
        for button in self.footer_nav_buttons + self.footer_action_buttons:
            button._footer_full_text = button._text
        self._footer_fit = None  # brand-new widgets: force a fresh fit
        self._apply_footer_rows(1)

    def _apply_footer_rows(self, rows: int) -> None:
        """One row (nav left, actions right) or actions wrapped onto row 2."""
        self.footer_left.grid(row=0, column=0, sticky="w")
        if rows == 1:
            self.footer_right.grid(row=0, column=1, sticky="e", pady=0)
        else:
            self.footer_right.grid(row=1, column=0, columnspan=2, sticky="e", pady=(S(8), 0))

    def _set_footer_text(self, button: RoundButton, text: str) -> None:
        """Set a footer label, keeping the full wording for _fit_footer."""
        button._footer_full_text = text
        fit = getattr(self, "_footer_fit", None)
        button.configure(text=self._footer_label(button, "full" if fit is None else fit[2]))

    @staticmethod
    def _footer_label(button: RoundButton, tier: str) -> str:
        """The button's wording at one of the tiers above."""
        text = getattr(button, "_footer_full_text", button._text)
        if tier == "full":
            return text
        for hint in FOOTER_KEY_HINTS:
            if text.endswith(hint):
                text = text[: -len(hint)]
                break
        if tier == "short":
            # "Measure skipped (3)" carries a count, so match its prefix.
            if text.startswith("Measure skipped"):
                text = "Skipped" + text[len("Measure skipped"):]
            else:
                text = FOOTER_SHORT_LABELS.get(text, text)
        return text

    def _footer_font(self, size: str) -> tkfont.Font:
        cache = self._footer_fonts
        if size not in cache:
            cache[size] = tkfont.Font(font=self._button_fonts()[size])
        return cache[size]

    def _footer_group_width(self, buttons: list, size: str, tier: str) -> int:
        if not buttons:
            return 0
        font = self._footer_font(size)
        pad = 2 * S(RoundButton.SIZE_PADS[size][0])
        total = S(self.FOOTER_GAP) * (len(buttons) - 1)
        for button in buttons:
            total += font.measure(self._footer_label(button, tier)) + pad
        return total

    def _fit_footer(self, _event: tk.Event | None = None) -> None:
        """Keep every footer button inside the window.

        The action bar carries up to six buttons, and at full size they need
        more width than the content column has on a 1366-wide rig screen or
        at 150% Windows scaling - which clipped the rightmost button, Save +
        Next Sensor, off the edge. Measure what the visible buttons actually
        need and pick the first FOOTER_VARIANTS entry that fits: it drops the
        key hints, then switches to compact wording, then wraps the actions
        onto their own row, and only shrinks the buttons as a last resort.
        Bound to the footer's <Configure>, so it also re-fits when the window
        is maximized or resized.
        """
        bar = getattr(self, "footer_bar", None)
        if bar is None or not bar.winfo_exists() or not self.footer_action_buttons:
            return
        available = bar.winfo_width()
        if available <= 1:
            return  # not laid out yet; the <Configure> binding calls back
        # grid_remove()d buttons report no manager and must not be measured.
        nav = [button for button in self.footer_nav_buttons if button.winfo_manager()]
        actions = [button for button in self.footer_action_buttons if button.winfo_manager()]
        chosen = self.FOOTER_VARIANTS[-1]
        for variant in self.FOOTER_VARIANTS:
            action_size, nav_size, tier, rows = variant
            nav_width = self._footer_group_width(nav, nav_size, tier)
            action_width = self._footer_group_width(actions, action_size, tier)
            if rows == 1:
                needed = nav_width + action_width + (S(self.FOOTER_GAP) if nav and actions else 0)
            else:
                needed = max(nav_width, action_width)
            if needed <= available:
                chosen = variant
                break
        if chosen == self._footer_fit:
            return
        self._footer_fit = chosen
        action_size, nav_size, tier, rows = chosen
        fonts = self._button_fonts()
        for button in self.footer_nav_buttons:
            button.restyle(size=nav_size, font=fonts[nav_size], text=self._footer_label(button, tier))
        for button in self.footer_action_buttons:
            button.restyle(size=action_size, font=fonts[action_size], text=self._footer_label(button, tier))
        self._apply_footer_rows(rows)

    def update_navigation_state(self) -> None:
        idle = not self.busy and not self.measuring
        for button in (self.skipped_button, self.skip_button, self.remeasure_button, self.secondary_button):
            if button is not None:
                button.grid_remove()
        if self.step == self.SETUP_STEP:
            self.back_button.configure(state="disabled")
            self._set_footer_text(self.primary_button, "Start (Enter)")
            self.primary_button.configure(state="disabled" if self.busy else "normal")
            self._fit_footer()
            return
        # Skipped parts waiting to be measured (never the one on the bench).
        waiting = [
            item for item in self.skipped_parts_queue() if item[1] != self.current_sensor_id
        ]
        if waiting and not self.resuming_skipped:
            self.skipped_button.grid()
            self._set_footer_text(self.skipped_button, f"Measure skipped ({len(waiting)})")
            self.skipped_button.configure(state="normal" if idle else "disabled")
        if self.step == self.LOAD_STEP:
            self.back_button.configure(state="disabled" if self.busy else "normal")
            self.skip_button.grid()
            self.skip_button.configure(state="normal" if self.can_skip_part() else "disabled")
            # Hard block: no measurement on a low battery or a wiring fault
            # (battery branches are inert while monitoring is disabled).
            battery_blocking = (
                BATTERY_MONITORING_ENABLED and self.battery_state in ("low", "fault")
            )
            blocked = (
                self.busy
                or battery_blocking
                or self.stability_config_error is not None
                or not self.reference_gate_ready()
            )
            if self.stability_config_error is not None:
                measure_text = "Fix stability settings"
            elif battery_blocking and self.battery_state == "fault":
                measure_text = "Check wiring to test"
            elif battery_blocking:
                measure_text = "Recharge battery to test"
            elif not self.reference_gate_ready():
                measure_text = "Calibrate reference unit to test"
            else:
                measure_text = "Measure (Enter)"
            self._set_footer_text(self.primary_button, measure_text)
            self.primary_button.configure(state="disabled" if blocked else "normal")
        else:
            self.skip_button.grid()
            self.remeasure_button.grid()
            self.secondary_button.grid()
            self.back_button.configure(state="disabled" if self.busy or self.result_saved else "normal")
            ready = idle and self.last_result is not None and not self.result_saved
            self.skip_button.configure(state="normal" if self.can_skip_part() else "disabled")
            self._set_footer_text(
                self.remeasure_button,
                "Re-measure" if self.last_result is not None or self.last_measure_error else "Measure",
            )
            self.remeasure_button.configure(state="normal" if idle and not self.result_saved else "disabled")
            self._set_footer_text(
                self.primary_button,
                "Save + Next Sensor (Enter)",
            )
            self.primary_button.configure(state="normal" if ready else "disabled")
            self._set_footer_text(
                self.secondary_button,
                "Save + Exit Batch (Esc)",
            )
            self.secondary_button.configure(state="normal" if ready else "disabled")
        self._fit_footer()

    def go_next(self) -> None:
        if self.busy:
            return
        if self.step == self.SETUP_STEP:
            self.start_batch()
        elif self.step == self.LOAD_STEP:
            if self.stability_config_error is not None:
                self.run_measurement()
                return
            self.show_step(self.RESULT_STEP)
            self.run_measurement()
        elif self.step == self.RESULT_STEP:
            self.save_and_continue()

    def go_back(self) -> None:
        if self.busy or self.measuring:
            return
        if self.step == self.LOAD_STEP:
            self.show_step(self.SETUP_STEP)
        elif self.step == self.RESULT_STEP and not self.result_saved:
            self.show_step(self.LOAD_STEP)

    def show_step(self, step: str) -> None:
        self.step = step
        self.render_step()
        # Re-check the battery whenever we arrive at the load step (a sensor is
        # about to be tested) so the watcher reflects the current supply.
        if step == self.LOAD_STEP:
            self.refresh_battery()

    def on_enter_key(self, event: tk.Event) -> str | None:
        if isinstance(event.widget, (ttk.Button, RoundButton)):
            return None
        if self.busy or self.measuring:
            return "break"
        if self.step == self.RESULT_STEP and (self.last_result is None or self.result_saved):
            return "break"
        self.go_next()
        return "break"

    def on_escape_key(self, _event: tk.Event) -> str | None:
        if self.step == self.RESULT_STEP and not self.busy and not self.measuring and self.last_result is not None and not self.result_saved:
            self.save_and_end_batch()
            return "break"
        return None

    def focus_default_widget(self) -> None:
        widget = self.default_focus_widget
        if self.busy or widget is None or not widget.winfo_exists():
            if widget is None:
                self.focus_set()
            return
        widget.focus_set()
        try:
            widget.selection_range(0, tk.END)
            widget.icursor(tk.END)
        except (AttributeError, tk.TclError):
            pass

    # ----- batch lifecycle ----- #
    def start_batch(self) -> None:
        batch_number = self.batch_var.get().strip()
        if not batch_number:
            messagebox.showerror("Batch number needed", "Please enter a batch number.")
            return
        tester_name = self.tester_var.get().strip()
        if not tester_name:
            messagebox.showerror("Tester name needed", "Please enter the tester name.")
            return

        self.batch_number = batch_number
        self.tester_name = tester_name
        self.filter_setup = self.filter_var.get()
        csv_path = batch_results_path(batch_number)
        self.resuming_skipped = False
        self.current_sensor_number = self._next_fresh_sensor_number()
        existing = count_existing_batch_rows(csv_path)
        position = "next" if existing else "first"
        waiting = self.skipped_parts_queue()
        status = f"Batch {batch_number}: {position} sensor is {batch_number}-{self.current_sensor_number}."
        if waiting:
            status += f"  {len(waiting)} skipped part(s) waiting: {attempt_history.format_queue(waiting)}."
        self.status_var.set(status)
        self.prepare_current_sensor()
        self.show_step(self.LOAD_STEP)

    def prepare_current_sensor(self) -> None:
        self.current_sensor_id = f"{self.batch_number}-{self.current_sensor_number}"
        self.result_saved = False
        # Earlier attempts on this id (a part coming back from the skipped
        # pile keeps its history) so the verdict row reports the true count.
        self.measure_attempts, self.skip_count = attempt_history.attempt_counts(
            self._attempts_path(), self.current_sensor_id
        )
        self.last_metrics = None
        self.last_result = None
        self.last_capture_report = None
        self.last_noise_report = None
        self.last_noise_metrics = None
        self.last_noise_raw_waveform = None
        self.last_noise_raw_rate_hz = None
        self.last_noise_raw_left_context = None
        self.last_noise_raw_right_context = None
        self.noise_raw_auto_saved = False
        self.noise_soak_var.set(False)
        self.last_reference_check_mv = None
        self.last_offset_initial_v = None
        self.last_measure_error = None
        self.show_details_var.set(False)
        self.preview_waveform = np.array([], dtype=float)
        self.preview_sync = np.array([], dtype=float)
        self.snapshot_paths = []
        self.stability_diagnostics_saved = False
        self.noise_diagnostics_saved = False
        self.notes_var.set("")
        self.failure_mode_var.set("")
        self.comment_status_var.set("")
        self.snapshot_status_var.set("")
        self.noise_capture_status_var.set("")
        self.measure_status_var.set("")
        self._reset_measure_progress()

    def save_and_continue(self) -> None:
        if self.save_current_sensor():
            self._advance_to_next_sensor()

    def save_and_end_batch(self) -> None:
        if self.save_current_sensor():
            self._end_batch()

    def _advance_to_next_sensor(self) -> None:
        if self.resuming_skipped:
            # Walk the skipped pile in the order it was built; once it is
            # empty fall back to fresh numbers.
            waiting = [
                item for item in self.skipped_parts_queue() if item[1] != self.current_sensor_id
            ]
            if waiting:
                self._load_skipped_part(*waiting[0])
                return
            self.resuming_skipped = False
        self.current_sensor_number = self._next_fresh_sensor_number()
        self.prepare_current_sensor()
        self.show_step(self.LOAD_STEP)

    def _end_batch(self) -> None:
        saved_batch = self.batch_number
        saved_csv = batch_results_path(saved_batch)
        self.status_var.set(f"Batch {saved_batch} ended.")
        self.step = self.SETUP_STEP
        self.result_saved = True
        self.show_batch_summary_window(saved_batch, saved_csv)
        self.render_step()

    # ----- v2.0: set a part aside and come back to it in skip order ----- #
    def _attempts_path(self) -> Path:
        return attempt_history.attempts_path_for(batch_results_path(self.batch_number))

    def skipped_parts_queue(self) -> list[tuple[int, str]]:
        """Skipped parts without a verdict yet, first skipped first."""
        if not self.batch_number:
            return []
        return attempt_history.skipped_queue(
            self._attempts_path(), batch_results_path(self.batch_number)
        )

    def _next_fresh_sensor_number(self) -> int:
        """Next never-used number: above every saved AND every skipped id."""
        csv_path = batch_results_path(self.batch_number)
        return max(
            next_sensor_number_for_batch(csv_path),
            attempt_history.highest_sensor_number(self._attempts_path()) + 1,
        )

    def can_skip_part(self) -> bool:
        return (
            self.step in (self.LOAD_STEP, self.RESULT_STEP)
            and not self.busy
            and not self.measuring
            and not self.result_saved
            and bool(self.current_sensor_id)
        )

    def _log_attempt(
        self,
        event: str,
        *,
        result: FinalResult | None = None,
        reason: str = "",
        note: str = "",
    ) -> None:
        """Append one event to the batch's attempt log (never blocks a test)."""
        if not self.batch_number or not self.current_sensor_id:
            return
        if event in (attempt_history.EVENT_MEASURED, attempt_history.EVENT_MEASURE_ERROR):
            self.measure_attempts += 1
        elif event == attempt_history.EVENT_SKIPPED:
            self.skip_count += 1
        noise_report = getattr(self, "last_noise_report", None)
        try:
            attempt_history.append_attempt(
                self._attempts_path(),
                batch_number=self.batch_number,
                sensor_number=self.current_sensor_number,
                sensor_id=self.current_sensor_id,
                event=event,
                attempt=self.measure_attempts,
                outcome=result_outcome(result) if result is not None else "",
                reason=reason,
                note=note or self.notes_var.get(),
                tester_name=self.tester_name,
                offset_v=None if result is None else result.offset_v,
                sensitivity_mv=None if result is None else result.sensitivity_mv,
                polarity="" if result is None else result.polarity,
                noise_worst_pp_mv=None if noise_report is None else noise_report.worst_pp_mv,
                fail_reasons=None if result is None else result.fail_reasons,
            )
        except Exception:
            if event == attempt_history.EVENT_SKIPPED:
                raise

    def open_skip_window(self) -> None:
        if not self.can_skip_part():
            return
        dialog = tk.Toplevel(self)
        dialog.title(f"Skip {self.current_sensor_id}")
        dialog.minsize(S(560), S(320))
        dialog.configure(bg=PAGE_BG)
        dialog.transient(self)

        frame = tk.Frame(dialog, bg=PAGE_BG)
        frame.grid(row=0, column=0, sticky="nsew", padx=18, pady=16)
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=1)
        frame.columnconfigure(0, weight=1)
        tk.Label(frame, text="SKIP PART —", bg=PAGE_BG, fg="#8a5a00", font=self.fm(11, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(
            frame,
            text=f"Set {self.current_sensor_id} aside",
            bg=PAGE_BG,
            fg=TEXT_DARK,
            font=self.fd(19),
        ).grid(row=1, column=0, sticky="w", pady=(2, 8))

        # Just a comment box - no reason list to click through (2026-08-24:
        # keep the skip to two clicks; the attempt log carries the numbers).
        note_holder = tk.Frame(frame, bg=PAGE_BG)
        note_holder.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        note_holder.rowconfigure(1, weight=1)
        note_holder.columnconfigure(0, weight=1)
        tk.Label(note_holder, text="COMMENT (OPTIONAL)", bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 4))
        note = tk.Text(
            note_holder, wrap="word", font=self.fb(13), undo=True, relief="flat", bd=0,
            bg=CARD_BG, fg=TEXT_DARK, padx=12, pady=10, insertbackground=ELTEC_BLUE,
            highlightbackground=CARD_BORDER, highlightcolor=ELTEC_BLUE, highlightthickness=1,
            height=4,
        )
        note.grid(row=1, column=0, sticky="nsew")

        tk.Label(
            frame,
            text=(
                f"Put it on the skipped pile, in order. It comes back as "
                f"{self.current_sensor_id} under “Measure skipped”."
            ),
            bg=PAGE_BG,
            fg=MUTED_FG,
            font=self.fb(11),
            wraplength=S(520),
            justify="left",
        ).grid(row=5, column=0, sticky="w", pady=(10, 0))

        def commit(_event: tk.Event | None = None) -> str:
            if self.skip_current_part("", note.get("1.0", "end-1c")):
                dialog.destroy()
            return "break"

        buttons = tk.Frame(frame, bg=PAGE_BG)
        buttons.grid(row=6, column=0, sticky="e", pady=(16, 0))
        self.btn(buttons, "Cancel", dialog.destroy, kind="ghost", size="md").grid(row=0, column=0, padx=(0, 10))
        self.btn(buttons, "Skip part", commit, kind="warn", size="md").grid(row=0, column=1)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        note.focus_set()

    def skip_current_part(self, reason: str, note: str = "") -> bool:
        """Record the skip and move on WITHOUT spending a sensor number."""
        if not self.can_skip_part():
            return False
        skipped_id = self.current_sensor_id
        try:
            self._log_attempt(
                attempt_history.EVENT_SKIPPED,
                result=self.last_result,
                reason=reason,
                note=note,
            )
        except Exception as exc:
            messagebox.showerror("Could not record the skip", str(exc))
            return False
        self.result_saved = True  # nothing left to save for this part now
        self.delete_autosave()
        self._advance_to_next_sensor()
        waiting = len(self.skipped_parts_queue())
        self.status_var.set(
            f"{skipped_id} set aside ({waiting} skipped, waiting). Now loading {self.current_sensor_id}."
        )
        return True

    def measure_skipped(self) -> None:
        """Bring the skipped pile back, first skipped first."""
        if self.busy or self.measuring:
            return
        if self.step == self.RESULT_STEP and self.last_result is not None and not self.result_saved:
            messagebox.showinfo(
                "Finish this part first",
                f"Save, skip or re-measure {self.current_sensor_id} before loading a skipped part.",
            )
            return
        waiting = [
            item for item in self.skipped_parts_queue() if item[1] != self.current_sensor_id
        ]
        if not waiting:
            return
        first_number, first_id = waiting[0]
        dialog = tk.Toplevel(self)
        dialog.title("Skipped parts")
        dialog.minsize(S(520), S(260))
        dialog.configure(bg=PAGE_BG)
        dialog.transient(self)
        frame = tk.Frame(dialog, bg=PAGE_BG)
        frame.grid(row=0, column=0, sticky="nsew", padx=18, pady=16)
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tk.Label(frame, text="SKIPPED PARTS —", bg=PAGE_BG, fg="#8a5a00", font=self.fm(11, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(
            frame,
            text=f"{len(waiting)} waiting, in skip order",
            bg=PAGE_BG,
            fg=TEXT_DARK,
            font=self.fd(19),
        ).grid(row=1, column=0, sticky="w", pady=(2, 8))
        tk.Label(
            frame,
            text=attempt_history.format_queue(waiting),
            bg=CARD_BG,
            fg=ELTEC_BLUE_DARK,
            font=self.fm(13, "bold"),
            padx=12,
            pady=10,
            wraplength=S(480),
            justify="left",
            anchor="w",
        ).grid(row=2, column=0, sticky="ew")
        tk.Label(
            frame,
            text=f"Take the first part off the pile — it is {first_id}. The rest follow in this order.",
            bg=PAGE_BG,
            fg=MUTED_FG,
            font=self.fb(12),
            wraplength=S(480),
            justify="left",
        ).grid(row=3, column=0, sticky="w", pady=(12, 0))

        def load(_event: tk.Event | None = None) -> str:
            dialog.destroy()
            self._load_skipped_part(first_number, first_id)
            return "break"

        buttons = tk.Frame(frame, bg=PAGE_BG)
        buttons.grid(row=4, column=0, sticky="e", pady=(18, 0))
        self.btn(buttons, "Cancel", dialog.destroy, kind="ghost", size="md").grid(row=0, column=0, padx=(0, 10))
        self.btn(buttons, f"Load {first_id} (Enter)", load, kind="primary", size="md").grid(row=0, column=1)
        dialog.bind("<Return>", load)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.focus_set()

    def _load_skipped_part(self, sensor_number: int, sensor_id: str) -> None:
        self.resuming_skipped = True
        self.current_sensor_number = sensor_number
        self.prepare_current_sensor()
        self._log_attempt(attempt_history.EVENT_RESUMED)
        remaining = len(self.skipped_parts_queue()) - 1
        self.status_var.set(
            f"Skipped part {sensor_id} loaded — measure it now."
            + (f"  {remaining} more waiting." if remaining > 0 else "  Last skipped part.")
        )
        self.show_step(self.LOAD_STEP)

    # ----- skip a sensor that could not be measured ----- #
    def can_skip_sensor(self) -> bool:
        """Skipping is only offered when an attempt recorded nothing."""
        return (
            self.step == self.RESULT_STEP
            and not self.busy
            and not self.measuring
            and not self.result_saved
            and self.last_result is None
            and self.last_measure_error is not None
        )

    def open_skip_sensor_window(self) -> None:
        if not self.can_skip_sensor():
            return
        dialog = tk.Toplevel(self)
        dialog.title(f"Skip {self.current_sensor_id}")
        dialog.minsize(S(640), S(430))
        dialog.configure(bg=PAGE_BG)
        dialog.transient(self)

        frame = tk.Frame(dialog, bg=PAGE_BG)
        frame.grid(row=0, column=0, sticky="nsew", padx=18, pady=16)
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        frame.rowconfigure(5, weight=1)
        frame.columnconfigure(0, weight=1)
        tk.Label(frame, text="SKIP SENSOR —", bg=PAGE_BG, fg=ELTEC_RED, font=self.fm(11, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(
            frame,
            text=f"Sensor {self.current_sensor_id} was not measured",
            bg=PAGE_BG,
            fg=TEXT_DARK,
            font=self.fd(19),
        ).grid(row=1, column=0, sticky="w", pady=(2, 8))
        tk.Label(
            frame,
            text=(
                f"It is saved as {OUTCOME_NOT_MEASURED}: no offset, sensitivity or "
                "polarity is written, and it does not count as a pass or a fail."
            ),
            bg=PAGE_BG,
            fg=MUTED_FG,
            font=self.fb(12),
            wraplength=S(600),
            justify="left",
        ).grid(row=2, column=0, sticky="w")

        reason_var = tk.StringVar(value=DEFAULT_NOT_MEASURED_REASON)
        self._field_label(frame, 3, "Reason", bg=PAGE_BG, pady=(16, 4))
        reason_combo = ttk.Combobox(
            frame,
            textvariable=reason_var,
            values=list(NOT_MEASURED_REASON_CHOICES),
            state="readonly",
            font=self.fb(14),
            height=6,
        )
        reason_combo.grid(row=4, column=0, sticky="ew")

        note_holder = tk.Frame(frame, bg=PAGE_BG)
        note_holder.grid(row=5, column=0, sticky="nsew", pady=(14, 0))
        note_holder.rowconfigure(1, weight=1)
        note_holder.columnconfigure(0, weight=1)
        tk.Label(note_holder, text="NOTE (OPTIONAL)", bg=PAGE_BG, fg=MUTED_FG, font=self.fb(11, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 4))
        note = tk.Text(
            note_holder, wrap="word", font=self.fb(13), undo=True, relief="flat", bd=0,
            bg=CARD_BG, fg=TEXT_DARK, padx=12, pady=10, insertbackground=ELTEC_BLUE,
            highlightbackground=CARD_BORDER, highlightcolor=ELTEC_BLUE, highlightthickness=1,
            height=4,
        )
        note.grid(row=1, column=0, sticky="nsew")

        # The rig error itself is recorded automatically, so the technician
        # only has to add what the message cannot say.
        tk.Label(
            frame,
            text=f"Recorded automatically: {self.last_measure_error}",
            bg=PAGE_BG,
            fg=MUTED_FG,
            font=self.fb(10),
            wraplength=S(600),
            justify="left",
        ).grid(row=6, column=0, sticky="w", pady=(10, 0))

        def commit(end_batch: bool) -> None:
            if self.save_skipped_sensor(reason_var.get(), note.get("1.0", "end-1c")):
                dialog.destroy()
                if end_batch:
                    self._end_batch()
                else:
                    self._advance_to_next_sensor()

        buttons = tk.Frame(frame, bg=PAGE_BG)
        buttons.grid(row=7, column=0, sticky="e", pady=(16, 0))
        self.btn(buttons, "Cancel", dialog.destroy, kind="ghost", size="sm").grid(row=0, column=0, padx=(0, 10))
        self.btn(buttons, "Skip + Exit Batch", lambda: commit(True), kind="outline", size="sm").grid(row=0, column=1, padx=(0, 10))
        self.btn(buttons, "Skip + Next Sensor", lambda: commit(False), kind="primary", size="sm").grid(row=0, column=2)
        reason_combo.focus_set()

    def save_skipped_sensor(self, reason: str, note: str) -> bool:
        """Write the NOT MEASURED row, restoring state if the write fails."""
        if not self.can_skip_sensor():
            return False
        try:
            split_failure_mode(reason)
        except ValueError:
            messagebox.showerror(
                "Reason needed", "Choose why this sensor could not be measured."
            )
            return False
        previous_mode = self.failure_mode_var.get()
        previous_notes = self.notes_var.get()
        detail = self.last_measure_error or "nothing was recorded"
        self.last_result = build_not_measured_result(detail)
        self.failure_mode_var.set(reason)
        self.notes_var.set(
            "; ".join(part for part in (previous_notes.strip(), note.strip()) if part)
        )
        if not self.save_current_sensor():
            self.last_result = None
            self.failure_mode_var.set(previous_mode)
            self.notes_var.set(previous_notes)
            self.render_step()
            return False
        self.status_var.set(
            f"{self.current_sensor_id}: saved as {OUTCOME_NOT_MEASURED}."
        )
        return True

    def save_current_sensor(self) -> bool:
        if self.last_result is None:
            messagebox.showinfo("Nothing to save", "Run the measurement before saving.")
            return False
        pwm_hz, pwm_duty = EMITTER_PWM_FREQUENCY_HZ, EMITTER_PWM_DUTY_CYCLE
        failure_mode = ""
        if not self.last_result.passed:
            try:
                split_failure_mode(self.failure_mode_var.get())
            except ValueError as exc:
                messagebox.showerror("Failure mode needed", str(exc))
                return False
            failure_mode = self.failure_mode_var.get()
        try:
            if (
                self.last_capture_report is not None
                and self.last_capture_report.unstable
                and not self.stability_diagnostics_saved
            ):
                if self.last_metrics is None:
                    raise RuntimeError(
                        "The unstable waveform is unavailable; the result was not saved."
                    )
                diagnostic_paths = save_waveform_diagnostic_bundle(
                    self.batch_number,
                    self.current_sensor_id,
                    self.last_metrics,
                    self.last_capture_report,
                    title=(
                        f"{MODEL_NAME} {self.current_sensor_id} unstable"
                    ),
                    detail_lines=snapshot_detail_lines(
                        self.batch_number,
                        self.current_sensor_id,
                        self.last_metrics,
                        self.notes_var.get(),
                        self.last_capture_report,
                    ),
                    filename_suffix="unstable",
                )
                if not diagnostic_paths:
                    raise RuntimeError(
                        "The unstable diagnostic bundle could not be created."
                    )
                self.snapshot_paths.extend(diagnostic_paths)
                self.stability_diagnostics_saved = True
            if (
                self.last_noise_report is not None
                and self.last_noise_report.outcome == OUTCOME_FAIL
                and not self.noise_diagnostics_saved
                and self.last_noise_metrics is not None
            ):
                # Preserve the emitter-off noise stream for a noisy sensor the
                # same way an unstable capture is preserved. The cycle-CSV
                # sidecars are sync-based and do not apply (report=None).
                noise_paths = save_waveform_diagnostic_bundle(
                    self.batch_number,
                    self.current_sensor_id,
                    self.last_noise_metrics,
                    None,
                    title=(
                        f"{MODEL_NAME} {self.current_sensor_id} emitter-off noise"
                    ),
                    detail_lines=self.noise_snapshot_detail_lines(),
                    filename_suffix="noisy",
                )
                self.snapshot_paths.extend(noise_paths)
                # A noise failure is exactly the part whose spikes are worth
                # studying: keep its RAW 1000 SPS capture automatically
                # alongside the band-limited snapshot (skipped when the
                # any-window-over auto-save already wrote it mid-measurement).
                if (
                    not self.noise_raw_auto_saved
                    and self.last_noise_raw_waveform is not None
                    and self.last_noise_raw_rate_hz is not None
                ):
                    self.snapshot_paths.extend(
                        save_raw_noise_capture(
                            self.batch_number,
                            self.current_sensor_id,
                            self.last_noise_raw_waveform,
                            self.last_noise_raw_rate_hz,
                            left_context_v=getattr(
                                self, "last_noise_raw_left_context", None
                            ),
                            right_context_v=getattr(
                                self, "last_noise_raw_right_context", None
                            ),
                            metadata={
                                "operator_requested": "no (automatic on noise FAIL)",
                                "noise_outcome": self.last_noise_report.outcome,
                                "noise_windows_over": self.last_noise_report.windows_over,
                                "noise_windows_total": self.last_noise_report.windows_total,
                                "noise_worst_pp_mv": self.last_noise_report.worst_pp_mv,
                            },
                        )
                    )
                self.noise_diagnostics_saved = True
            append_result_csv(
                batch_results_path(self.batch_number),
                batch_number=self.batch_number,
                sensor_number=self.current_sensor_number,
                sensor_id=self.current_sensor_id,
                tester_name=self.tester_name,
                filter_setup=self.filter_setup,
                pwm_channel=EMITTER_PWM_CHANNEL,
                pwm_hz=pwm_hz,
                pwm_duty=pwm_duty,
                final_result=self.last_result,
                comment=self.notes_var.get(),
                snapshot_paths=self.snapshot_paths,
                battery_v=self.battery_v,
                capture_report=self.last_capture_report,
                noise_report=self.last_noise_report,
                reference_calibration=self.reference_calibration,
                reference_check_mv=self.last_reference_check_mv,
                failure_mode=failure_mode,
                offset_initial_v=self.last_offset_initial_v,
                measure_attempts=self.measure_attempts,
                skip_count=self.skip_count,
            )
        except Exception as exc:
            messagebox.showerror("Could not save result", str(exc))
            return False
        self.result_saved = True
        self.delete_autosave()
        self._log_attempt(attempt_history.EVENT_SAVED, result=self.last_result)
        self.status_var.set(f"Saved {self.current_sensor_id}.")
        self.update_navigation_state()
        return True

    def noise_snapshot_detail_lines(self) -> list[str]:
        """PNG footer lines for a preserved emitter-off noise capture."""
        report = self.last_noise_report
        lines = [
            f"Batch: {self.batch_number}",
            f"Sensor: {self.current_sensor_id}",
        ]
        metrics = self.last_noise_metrics
        if metrics is not None and metrics.offset_v is not None:
            lines.append(f"Offset: {metrics.offset_v:.3f} V")
        if report is not None:
            allowed = int((report.windows_total or 0) * NOISE_MAX_OVER_FRACTION + 1e-9)
            lines.append(
                f"Noise: {report.outcome} - {report.windows_over}/{report.windows_total} "
                f"windows over {format_noise_pp(report.pp_limit_mv)} pk-pk (allowed {allowed})"
            )
            if report.worst_pp_mv is not None:
                lines.append(
                    "Worst window: "
                    f"{format_noise_pp(report.worst_pp_mv, decimals=1)} pk-pk"
                )
            if report.analysis_rate_hz is not None:
                lines.append(
                    f"Band-limited to {report.analysis_rate_hz:.0f} SPS, per-window "
                    "baseline removed, before the pk-pk rule"
                )
            if report.settle_s is not None:
                lines.append(f"Quiet wait before capture: {report.settle_s:.1f} s")
            if report.baseline_settled is False:
                lines.append("DC level was still moving at the wait deadline (measured anyway)")
        comment = self.notes_var.get().strip()
        if comment:
            lines.append(f"Comment: {comment}")
        return lines

    def _post(self, callback) -> None:
        """Schedule a callback on the UI thread, ignoring app-shutdown races."""
        try:
            self.after(0, callback)
        except (RuntimeError, tk.TclError):
            pass

    # ----- battery watcher ----- #
    def _refresh_battery_pill(self) -> None:
        if not BATTERY_MONITORING_ENABLED:
            self.battery_pill.set_state("unknown", "Battery: not monitored", 0.0)
            return
        if self.battery_v is None:
            text = "Battery: checking…" if self.battery_checking else "Battery: --"
        elif self.battery_state == "ok":
            text = f"Battery {self.battery_v:.1f} V  ✓"
        elif self.battery_state == "warn":
            text = f"Battery low  {self.battery_v:.1f} V"
        elif self.battery_state == "fault":
            text = f"CHECK WIRING  {self.battery_v:.1f} V"
        else:
            text = f"RECHARGE BATTERY  {self.battery_v:.1f} V"
        self.battery_pill.set_state(self.battery_state, text, battery_gauge_fraction(self.battery_v))

    def refresh_battery(self) -> None:
        """Read the sensor battery in the background and update the watcher state.

        Idle while battery monitoring is disabled (nothing measurable on AIN7
        since the 2026-08-12 battery isolation; see BATTERY_MONITORING_ENABLED)
        - the pill just shows "not monitored" and no BAT? read is issued.
        """
        if not BATTERY_MONITORING_ENABLED:
            self._refresh_battery_pill()
            return
        if self.battery_checking or self.busy or self.measuring:
            return
        self.battery_checking = True
        self._refresh_battery_pill()
        simulator = self.simulator_var.get()
        sim_low = self.sim_low_battery_var.get()

        def worker() -> None:
            error: Exception | None = None
            battery_v: float | None = None
            try:
                if simulator:
                    time.sleep(0.15)
                    battery_v = SIM_BATTERY_LOW_V if sim_low else SIM_BATTERY_OK_V
                else:
                    with self.hardware_lock:
                        self.ensure_connected()
                        battery_v = self.device.read_battery_voltage()
            except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not fatal
                error = exc
            self._post(lambda: self.on_battery_update(battery_v, error))

        threading.Thread(target=worker, daemon=True).start()

    def on_battery_update(self, battery_v: float | None, error: Exception | None = None) -> None:
        self.battery_checking = False
        if error is None and battery_v is not None:
            self.battery_v = battery_v
            self.battery_state = battery_state_for(battery_v)
            self.battery_read_time = time.monotonic()
        elif self.battery_v is None:
            # Never got a reading (device missing/claimed): leave the pill
            # neutral and tell the technician what is wrong in the status bar.
            self.battery_state = "unknown"
            if error is not None:
                self.status_var.set(self._friendly_hardware_error(str(error)))
        self._refresh_battery_pill()
        if self.busy or self.measuring:
            return
        # Refresh on-screen banners + the Measure button lock when idle.
        if self.step in (self.LOAD_STEP, self.RESULT_STEP):
            self.render_step()
        else:
            self.update_navigation_state()

    # ----- AIN1 reference calibration / emitter-health gate ----- #
    def _capture_reference_reading(
        self,
        device: EmitterEsp32Rig,
        *,
        pwm_started_monotonic: float,
        token: int,
        push,
        status_prefix: str,
        calibration_ui: bool = False,
        step_progress=None,
    ) -> float:
        dut_settings = self.stability_settings
        if dut_settings is None:
            raise StabilitySettingsError("V6.1 stability settings are unavailable.")
        settings = reference_stability_settings(dut_settings)

        def progress(current: StabilityAnalysis) -> None:
            report = current.report
            if report.stabilized:
                text = (
                    f"{status_prefix}: stable. Averaging cycle "
                    f"{report.measurement_cycle_count}/{REFERENCE_MEASUREMENT_CYCLES}…"
                )
                fraction = 0.5 + 0.5 * min(
                    1.0,
                    report.measurement_cycle_count
                    / max(1, REFERENCE_MEASUREMENT_CYCLES),
                )
            else:
                latest = current.cycles[-1] if current.cycles else None
                delta_text = (
                    "waiting for two peaks"
                    if latest is None or latest.absolute_peak_delta_mv is None
                    else f"peak Δ {latest.absolute_peak_delta_mv:.3f} mV"
                )
                confirmation = 0 if latest is None else latest.confirmation_run_length
                text = (
                    f"{status_prefix}: {delta_text} · "
                    f"{confirmation}/{settings.consecutive_deltas_required} stable"
                )
                fraction = 0.5 * min(
                    1.0,
                    confirmation / max(1, settings.consecutive_deltas_required),
                )
            push(lambda value=text: self.set_measure_status(token, value))
            if step_progress is not None:
                step_progress(fraction)
            if calibration_ui:
                push(lambda value=text: self.reference_progress_var.set(value))

        _waveform, _sync, _sample_rate_hz, analysis = device.read_reference_until_stable(
            waveform_range_v=WAVEFORM_INPUT_RANGE_V,
            settings=settings,
            pwm_started_monotonic=pwm_started_monotonic,
            sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
            expected_frequency_hz=REFERENCE_PWM_FREQUENCY_HZ,
            progress=progress,
            cancelled=lambda: token != self.measure_token,
        )
        return analyze_reference_stable_response_mv(analysis)

    def run_reference_calibration(self) -> None:
        """Collect and persist five adaptive readings with a known-good emitter."""
        if self.busy or self.measuring or self.reference_calibrating or self.simulator_var.get():
            return

        # Starting a recalibration means the old emitter baseline must no longer
        # authorize tests. Keep its audit values, but invalidate it immediately.
        if self.reference_calibration is not None and self.reference_calibration.valid:
            self.reference_calibration = self.reference_calibration.invalidated(
                "Reference-unit recalibration was started; testing remains locked until it completes."
            )
            try:
                save_reference_calibration(self.reference_calibration)
            except ReferenceCalibrationError as exc:
                self.reference_calibration_error = str(exc)

        self.busy = True
        self.reference_calibrating = True
        self.reference_progress_var.set(
            "Starting the emitter and watching reference peak stability…"
        )
        self.status_var.set("Reference-unit calibration: watching peak stability…")
        self.measure_token += 1
        token = self.measure_token
        self.render_step()

        def push(callback) -> None:
            self._post(callback)

        def worker() -> None:
            try:
                calibration = self._hardware_reference_calibration(token, push)
            except Exception as exc:  # noqa: BLE001 - shown in the calibration card/dialog
                push(lambda exc=exc: self.on_reference_calibration_error(token, exc))
            else:
                push(lambda: self.on_reference_calibration_done(token, calibration))

        threading.Thread(target=worker, daemon=True).start()

    def _hardware_reference_calibration(self, token: int, push) -> ReferenceCalibration:
        readings_mv: list[float] = []
        with self.hardware_lock:
            self.ensure_connected()
            device = self.device
            device.disable_emitter_pwm(EMITTER_PWM_CHANNEL)
            if BATTERY_MONITORING_ENABLED:
                battery_v = device.read_battery_voltage()
                push(lambda v=battery_v: self.on_battery_update(v))
                if battery_state_for(battery_v) == "fault":
                    raise HardwareNotReadyError(
                        f"The battery input reads {battery_v:.2f} V, which is not a valid battery level. "
                        "Check the battery clip and divider wiring before calibrating the reference unit."
                    )
                if battery_v <= BATTERY_MIN_V:
                    raise BatteryTooLowError(battery_v)
            try:
                # The AIN1 reference unit is a 406MCA sensor: calibrate it at
                # its qualified 10 Hz drive, not the DUT's 1 Hz.
                activation_time = device.configure_emitter_pwm(
                    channel=EMITTER_PWM_CHANNEL,
                    frequency_hz=REFERENCE_PWM_FREQUENCY_HZ,
                    duty_cycle_percent=EMITTER_PWM_DUTY_CYCLE,
                )
                first_reading_start = (
                    float(activation_time)
                    if isinstance(activation_time, (int, float))
                    else time.monotonic()
                )
                for reading_number in range(1, REFERENCE_CALIBRATION_READINGS + 1):
                    def capture(attempt: int, reading_number: int = reading_number) -> float:
                        # Only the very first attempt of reading 1 anchors its
                        # stability deadline to the PWM,ON moment; retries and
                        # later readings start their own timing window.
                        reading_start = (
                            first_reading_start
                            if reading_number == 1 and attempt == 0
                            else time.monotonic()
                        )
                        return self._capture_reference_reading(
                            device,
                            pwm_started_monotonic=reading_start,
                            token=token,
                            push=push,
                            status_prefix=(
                                f"Reference calibration {reading_number}/"
                                f"{REFERENCE_CALIBRATION_READINGS}"
                            ),
                            calibration_ui=True,
                        )

                    def on_retry(attempt: int, reading_number: int = reading_number) -> None:
                        text = (
                            f"Serial stream glitch in reading {reading_number}; "
                            f"nothing was recorded — retrying "
                            f"({attempt}/{REFERENCE_READING_STREAM_RETRIES})…"
                        )
                        push(lambda value=text: self.reference_progress_var.set(value))
                        push(lambda value=text: self.status_var.set(value))

                    reading_mv = call_with_stream_retries(capture, on_retry=on_retry)
                    readings_mv.append(reading_mv)
                    progress_text = (
                        f"Reference reading {reading_number}/{REFERENCE_CALIBRATION_READINGS} complete"
                    )
                    push(
                        lambda value=progress_text: self.reference_progress_var.set(value)
                    )
                    push(lambda value=progress_text: self.status_var.set(value))
            finally:
                device.disable_emitter_pwm(EMITTER_PWM_CHANNEL)

        calibration = build_reference_calibration(readings_mv)
        save_reference_calibration(calibration)
        return calibration

    def on_reference_calibration_done(
        self, token: int, calibration: ReferenceCalibration
    ) -> None:
        if token != self.measure_token:
            return
        self.busy = False
        self.reference_calibrating = False
        self.reference_calibration = calibration
        self.reference_calibration_error = None
        self.reference_progress_var.set("")
        self.status_var.set("Reference unit calibrated and ready.")
        self.render_step()
        messagebox.showinfo(
            "Reference calibration complete",
            f"Saved {len(calibration.readings_mv)} stable reference readings.\n\n"
            "The reference unit will be checked automatically before every sensor test.",
        )

    def on_reference_calibration_error(self, token: int, exc: Exception) -> None:
        if token != self.measure_token:
            return
        self.busy = False
        self.reference_calibrating = False
        self.reference_progress_var.set("")
        text = self._friendly_hardware_error(str(exc))
        self.reference_calibration_error = text
        if self.reference_calibration is not None and not self.reference_calibration.valid:
            self.reference_calibration = self.reference_calibration.invalidated(
                "Reference-unit recalibration failed: " + text
            )
            try:
                save_reference_calibration(self.reference_calibration)
            except ReferenceCalibrationError:
                pass
        self.status_var.set("Reference calibration failed — sensor testing remains locked. " + text)
        self.render_step()
        messagebox.showerror("Reference calibration failed", text)

    # ----- measurement ----- #
    def run_measurement(self, _event: tk.Event | None = None) -> None:
        if self.busy or self.measuring:
            return
        if self.last_result is not None and not self.result_saved:
            # The shown verdict is being discarded: keep it in the attempt
            # log so a later review can see WHY the part was re-measured.
            self._log_attempt(attempt_history.EVENT_REMEASURE, result=self.last_result)
        simulator = self.simulator_var.get()
        if self.stability_config_error is not None or self.stability_settings is None:
            text = (
                "Measurement is disabled because the tracked v6.1 stability settings could not be loaded.\n\n"
                + (self.stability_config_error or "Unknown stability configuration error.")
            )
            self.status_var.set(text.replace("\n", " "))
            messagebox.showerror("Fix stability settings", text)
            return
        if not simulator and not self.reference_gate_ready():
            calibration = self.reference_calibration
            if calibration is not None and calibration.invalidation_reason:
                detail = calibration.invalidation_reason
            else:
                detail = self.reference_calibration_error or "No valid reference calibration is saved."
            self.step = self.LOAD_STEP
            self.status_var.set("Reference calibration required — the sensor was not read.")
            self.render_step()
            messagebox.showwarning(
                "Calibrate the reference unit before testing",
                detail + "\n\nReplace/check the emitter, then press “Calibrate reference unit” before testing a sensor.",
            )
            return
        # Hard block: refuse to start a test on a known-low battery or a
        # wiring fault. Inert while battery monitoring is disabled (the sensor
        # battery is not measurable on AIN7 - see BATTERY_MONITORING_ENABLED).
        if BATTERY_MONITORING_ENABLED and self.battery_state in ("low", "fault"):
            volts = "" if self.battery_v is None else f" ({self.battery_v:.2f} V)"
            if self.battery_state == "fault":
                self.status_var.set(f"Battery reading is not valid{volts}. Check the battery clip and divider wiring, then press “Re-check battery”.")
            else:
                self.status_var.set(f"Battery too low{volts}. Recharge the sensor battery and press “Re-check battery”.")
            self.refresh_battery()
            return

        waveform_range_v = WAVEFORM_INPUT_RANGE_V
        pwm_channel = EMITTER_PWM_CHANNEL
        pwm_hz = EMITTER_PWM_FREQUENCY_HZ
        pwm_duty = EMITTER_PWM_DUTY_CYCLE
        sim_case = self.sim_case_var.get()
        sim_low_battery = self.sim_low_battery_var.get()
        filter_setup = self.filter_setup
        show_live = self.show_live_var.get()
        noise_soak = self.noise_soak_var.get()

        self.measuring = True
        self.busy = True
        self.last_metrics = None
        self.last_result = None
        self.last_capture_report = None
        self.last_noise_report = None
        self.last_noise_metrics = None
        self.last_noise_raw_waveform = None
        self.last_noise_raw_rate_hz = None
        self.last_noise_raw_left_context = None
        self.last_noise_raw_right_context = None
        self.noise_raw_auto_saved = False
        self.last_reference_check_mv = None
        self.last_offset_initial_v = None
        self.last_measure_error = None
        self.show_details_var.set(False)
        self.stability_diagnostics_saved = False
        self.noise_diagnostics_saved = False
        self.measure_status_var.set("Reading the sensor offset first…")
        self._reset_measure_progress()
        self.measure_progress_total = 4 if NOISE_TEST_ENABLED else 3
        self._apply_preview_display_mode()
        self.measure_token += 1
        token = self.measure_token
        self.render_step()

        def push(callback) -> None:
            self._post(callback)

        def worker() -> None:
            try:
                if simulator:
                    metrics, final, offset_v = self._simulate_measurement(
                        filter_setup, sim_case, sim_low_battery, waveform_range_v,
                        show_live, token, push, noise_soak=noise_soak,
                    )
                else:
                    metrics, final, offset_v = self._hardware_measurement(
                        filter_setup, waveform_range_v, pwm_channel, pwm_hz, pwm_duty,
                        show_live, token, push, noise_soak=noise_soak,
                    )
            except BatteryTooLowError as exc:
                push(lambda exc=exc: self.on_battery_block(token, exc.battery_v))
            except ReferenceGateError as exc:
                push(lambda exc=exc: self.on_reference_block(token, exc))
            except HardwareNotReadyError as exc:
                push(lambda exc=exc: self.on_hardware_not_ready(token, exc))
            except Exception as exc:
                push(lambda exc=exc: self.on_measure_error(token, exc))
            else:
                push(lambda: self.on_measure_done(token, metrics, final))

        threading.Thread(target=worker, daemon=True).start()

    def _fresh_battery_reading(self) -> float | None:
        """Reuse the load-step battery check when it is healthy and recent,
        so the measurement does not spend ~0.3 s re-reading a slow-moving DC
        value. Warn/low readings are never reused - those always re-check."""
        if (
            self.battery_v is not None
            and self.battery_state == "ok"
            and self.battery_read_time is not None
            and (time.monotonic() - self.battery_read_time) <= BATTERY_REUSE_WINDOW_S
        ):
            return self.battery_v
        return None

    def _hardware_measurement(self, filter_setup, waveform_range_v, pwm_channel, pwm_hz, pwm_duty, show_live, token, push, *, noise_soak=False):
        # Preview samples always flow from the production stream; this initial
        # UI state must never select a different acquisition path.
        del show_live
        settings = self.stability_settings
        if settings is None:
            raise StabilitySettingsError("V6.1 stability settings are unavailable.")
        calibration = self.reference_calibration
        if REFERENCE_GATE_ENABLED and (calibration is None or not calibration.valid):
            raise ReferenceGateError(
                "The reference unit has no valid calibration. The sensor was not read. "
                "Replace/check the emitter and recalibrate the reference unit before testing."
            )
        # TP412 per-part sequence (2026-08-13 order): offset fail-fast ->
        # reference gate (while REFERENCE_GATE_ENABLED) -> emitter-off noise
        # -> driven sensitivity capture.
        # Offset runs FIRST (a plain DC read with the emitter off): a part
        # outside 0.8-3.0 V - including one railed at the ~5 V ADC full
        # scale - fails on the spot and never reaches the reference gate,
        # so its AIN1 interference can never invalidate the reference
        # calibration. The step ladder below drives the measuring screen's
        # progress bar.
        reference_steps = 1 if REFERENCE_GATE_ENABLED else 0
        noise_step = 2 + reference_steps
        total_steps = reference_steps + (3 if NOISE_TEST_ENABLED else 2)
        sensitivity_step = total_steps

        def step_progress(step: int, label: str, fraction: float) -> None:
            push(
                lambda: self.set_measure_progress(
                    token, step, total_steps, label, fraction
                )
            )

        def offset_fail_fast(offset_value: float, skip_reason: str):
            """Record an immediate TP412 offset FAIL - nothing else is measured."""
            push(lambda v=offset_value: self.set_measure_status(
                token,
                f"Offset {v:.3f} V is outside the {OFFSET_MIN_V:.1f}-"
                f"{OFFSET_MAX_V:.1f} V band — recording the failure…",
            ))
            metrics, final = build_offset_failure_result(
                offset_value, input_range_v=waveform_range_v
            )
            self.last_capture_report = None
            self.last_noise_report = NoiseCaptureReport.skipped(skip_reason)
            return metrics, final, offset_value

        with self.hardware_lock:
            self.ensure_connected()
            device = self.device
            device.disable_emitter_pwm(pwm_channel)
            # Battery gate (currently disabled - the sensor battery is not
            # measurable on AIN7 since the 2026-08-12 battery isolation; see
            # BATTERY_MONITORING_ENABLED). When re-enabled: bail out before
            # measuring so no unreliable reading is recorded.
            if BATTERY_MONITORING_ENABLED:
                battery_v = self._fresh_battery_reading()
                if battery_v is None:
                    battery_v = device.read_battery_voltage()
                    push(lambda v=battery_v: self.on_battery_update(v))
                if battery_state_for(battery_v) == "fault":
                    raise HardwareNotReadyError(
                        f"The battery input reads {battery_v:.2f} V, which is not a valid battery level. "
                        "The battery or the ADS battery divider is probably not connected - "
                        "check the battery clip and the rig wiring, then press Re-check battery."
                    )
                if battery_v <= BATTERY_MIN_V:
                    raise BatteryTooLowError(battery_v)

            # STEP 1: the quick offset check, deliberately BEFORE the
            # reference gate. The emitter is off and this is a plain DC
            # read, so nothing about the fixture needs to be trusted yet -
            # and a bad part is rejected before it can disturb AIN1.
            push(lambda: self.set_measure_status(
                token,
                "Reading sensor offset first (emitter off)…",
            ))
            step_progress(1, "Offset", 0.0)
            offset_v = device.read_offset_voltage(waveform_range_v=waveform_range_v)
            # Pre-flight: through the buffer a missing/unseated sensor floats
            # near 0 V. A high reading is never "no sensor" - a high-offset
            # part rails AIN0 at the ~5 V ADC full scale and must record the
            # HO failure below, not a wiring error. A just-inserted part can
            # also sit below the floor for a few seconds while its offset
            # wakes up (lot 500: 500-44's first attempt read as "no sensor"),
            # so poll briefly before declaring the slot empty.
            wake_deadline = time.monotonic() + OFFSET_WAKE_TIMEOUT_S
            while (
                offset_v < SENSOR_OFFSET_MIN_PLAUSIBLE_V
                and time.monotonic() < wake_deadline
            ):
                push(lambda: self.set_measure_status(
                    token,
                    "AIN0 reads near 0 V — waiting for the sensor offset to wake up…",
                ))
                time.sleep(OFFSET_WAKE_POLL_S)
                offset_v = device.read_offset_voltage(waveform_range_v=waveform_range_v)
            if offset_v < SENSOR_OFFSET_MIN_PLAUSIBLE_V:
                raise NoSensorDetectedError(
                    f"No sensor detected: AIN0 reads {offset_v:.3f} V DC, but a connected 405 M22 "
                    f"presents at least {SENSOR_OFFSET_MIN_PLAUSIBLE_V:.2f} V. "
                    "Seat the sensor in the rig and check the buffer wiring, then press Measure again.",
                    offset_v,
                )
            push(lambda v=offset_v: self.on_initial_offset(token, v))
            push(lambda v=offset_v: self.on_offset_update(token, v))
            step_progress(1, "Offset", 1.0)

            # The early gate is HIGH-SIDE ONLY (2026-08-17). These sensors'
            # offsets settle UPWARD for tens of seconds after insertion: on
            # the lot-500 fixture comparison, 35 of 48 parts read 0.15-1.1 V
            # lower on this first read than their settled legacy-fixture
            # value, while the two parts that got an immediate second attempt
            # matched it almost exactly. A low first read is therefore not a
            # verdict - the part continues, and the offset is re-verified
            # after the sensitivity capture once it has had ~25 s to settle.
            # A HIGH first read is real (HO parts read high or railed at
            # once and never settle back into band), so it still fails on
            # the spot without wasting the noise + sensitivity time.
            if offset_v > OFFSET_MAX_V:
                return offset_fail_fast(
                    offset_v,
                    "offset out of range - reference, noise and sensitivity skipped",
                )
            if offset_v < OFFSET_MIN_V:
                push(lambda v=offset_v: self.set_measure_status(
                    token,
                    f"Offset {v:.3f} V is below the {OFFSET_MIN_V:.1f} V minimum but "
                    "may still be settling — continuing; it is re-verified after "
                    "the sensitivity capture.",
                ))

            # STEP 2 (only while REFERENCE_GATE_ENABLED): the reference gate.
            # Start the emitter and immediately stream the fixed reference
            # unit. The same consecutive-peak stability rule as AIN0 selects
            # five fresh cycles — but against the reference unit's own
            # dedicated delta limit, not the DUT's — and there is no fixed
            # warm-up delay. Disabled 2026-08-17: the shared dual op-amp
            # buffer couples the DUT into AIN1, so this reading tracks the
            # loaded sensor instead of the emitter (see REFERENCE_GATE_ENABLED).
            if REFERENCE_GATE_ENABLED:
                push(lambda: self.set_measure_status(
                    token,
                    "Offset OK. Checking reference unit — watching peak stability…",
                ))
                step_progress(2, "Reference check", 0.0)
                reference_capture_error: ReferenceCaptureError | None = None
                reference_check_mv: float | None = None
                try:
                    def reference_gate_capture(attempt: int) -> float:
                        # The AIN1 reference unit is a 406MCA sensor: gate it at
                        # its qualified 10 Hz drive, then switch to 1 Hz for the
                        # DUT capture below. Re-activating on a retry restarts the
                        # PWM phase and gives a fresh stability anchor.
                        reference_activation_time = device.configure_emitter_pwm(
                            channel=pwm_channel,
                            frequency_hz=REFERENCE_PWM_FREQUENCY_HZ,
                            duty_cycle_percent=pwm_duty,
                        )
                        reference_started_monotonic = (
                            float(reference_activation_time)
                            if isinstance(reference_activation_time, (int, float))
                            else time.monotonic()
                        )
                        return self._capture_reference_reading(
                            device,
                            pwm_started_monotonic=reference_started_monotonic,
                            token=token,
                            push=push,
                            status_prefix="Reference unit",
                            step_progress=lambda fraction: step_progress(
                                2, "Reference check", fraction
                            ),
                        )

                    def reference_gate_retry(attempt: int) -> None:
                        push(lambda: self.set_measure_status(
                            token,
                            "Serial stream glitch during the reference check; "
                            f"nothing was recorded — retrying ({attempt}/"
                            f"{REFERENCE_READING_STREAM_RETRIES})…",
                        ))

                    try:
                        reference_check_mv = call_with_stream_retries(
                            reference_gate_capture, on_retry=reference_gate_retry
                        )
                    except ReferenceCaptureError as exc:
                        # Judged below, after the guaranteed PWM-off: a part whose
                        # offset has drifted out of band mid-test can keep AIN1
                        # from stabilizing, and that condemns the part, not the
                        # fixture.
                        reference_capture_error = exc
                finally:
                    device.disable_emitter_pwm(pwm_channel)

                def high_offset_recheck():
                    """Re-read AIN0 after a failed reference gate.

                    The offset passed its own gate moments ago, but a part that
                    has since drifted above the TP412 limit (or railed) explains
                    the AIN1 failure: record the part as an immediate high-offset
                    FAIL and leave the reference calibration intact - swapping in
                    a good part restores the reference. Returns the fresh offset
                    and the fail-fast result (None when the reference failure
                    stands on its own).
                    """
                    recheck_v = device.read_offset_voltage(waveform_range_v=waveform_range_v)
                    if not high_offset_dut_explains_reference_failure(recheck_v):
                        return recheck_v, None
                    push(lambda v=recheck_v: self.on_offset_update(token, v))
                    push(lambda: self.set_measure_status(
                        token,
                        "High-offset sensor confirmed. Ignoring its AIN1 interference and recording the failure…",
                    ))
                    return recheck_v, offset_fail_fast(
                        recheck_v,
                        "high-offset sensor - noise and sensitivity skipped; "
                        "its AIN1 reference interference was ignored",
                    )

                if reference_capture_error is not None:
                    recheck_v, suppressed = high_offset_recheck()
                    if suppressed is not None:
                        return suppressed
                    reason = (
                        f"Reference unit could not establish a stable five-cycle reading: "
                        f"{reference_capture_error} AIN0 was checked at {recheck_v:.3f} V, which is "
                        f"not above the {OFFSET_MAX_V:.1f} V high-offset limit, so a high-offset "
                        "sensor does not explain the reference failure. Replace/check the emitter, "
                        "then recalibrate the reference unit before testing."
                    )
                    invalidated = calibration.invalidated(reason)
                    self.reference_calibration = invalidated
                    try:
                        save_reference_calibration(invalidated)
                    except ReferenceCalibrationError as save_exc:
                        self.reference_calibration_error = str(save_exc)
                    raise ReferenceGateError(reason) from reference_capture_error

                self.last_reference_check_mv = reference_check_mv
                if not calibration.accepts(reference_check_mv):
                    push(lambda: self.set_measure_status(
                        token,
                        "Reference unit is outside its window. Re-checking AIN0 for a high-offset sensor…",
                    ))
                    recheck_v, suppressed = high_offset_recheck()
                    if suppressed is not None:
                        return suppressed
                    failure = ReferenceCheckFailedError(
                        reference_check_mv,
                        calibration,
                        dut_offset_v=recheck_v,
                    )
                    invalidated = calibration.invalidated(str(failure), reference_check_mv)
                    self.reference_calibration = invalidated
                    try:
                        save_reference_calibration(invalidated)
                    except ReferenceCalibrationError as exc:
                        self.reference_calibration_error = str(exc)
                    raise failure

                step_progress(2, "Reference check", 1.0)
                push(lambda: self.set_measure_status(
                    token,
                    "Reference unit passed.",
                ))
            else:
                push(lambda: self.set_measure_status(
                    token,
                    "Offset OK. Reference gate is disabled (op-amp crosstalk) — "
                    "continuing without the emitter-health check.",
                ))

            def preview(waveform_preview: np.ndarray, sync_preview: np.ndarray) -> None:
                # Keep the rolling preview current even while hidden so a
                # technician can turn the live scope on mid-measurement.
                push(lambda wf=waveform_preview, sy=sync_preview: self.on_preview_frame(token, wf, sy))

            # TP412 noise test, now BEFORE the driven capture (the emitter has
            # been off since the reference gate). A fixed quiet wait replaces
            # the old settle detection - with the emitter off there is no
            # signal to stabilize, only noise - and a noisy part is rejected
            # here instead of first spending up to three 60 s stabilization
            # attempts on the sensitivity capture.
            if NOISE_TEST_ENABLED:
                # Per-part extended soak: 3x the capture with the allowed
                # over-window count held ABSOLUTE (see NOISE_SOAK_* constants)
                # to catch intermittent burst noise like lot-500 part 44's.
                noise_capture_seconds = (
                    NOISE_SOAK_CAPTURE_SECONDS if noise_soak else NOISE_CAPTURE_SECONDS
                )
                noise_max_over_fraction = (
                    NOISE_SOAK_MAX_OVER_FRACTION if noise_soak else NOISE_MAX_OVER_FRACTION
                )
                soak_text = " EXTENDED SOAK" if noise_soak else ""
                push(lambda: self.set_measure_status(
                    token,
                    f"Measuring noise (emitter off{soak_text.lower()}): waiting for the offset "
                    f"level to settle (min {NOISE_WAIT_BEFORE_CAPTURE_S:.0f} s)…",
                ))
                push(lambda: self.set_preview_display(token, noise=True))

                def noise_progress(
                    capturing: bool,
                    elapsed_s: float,
                    baseline_delta_mv: float | None = None,
                ) -> None:
                    if capturing:
                        # elapsed includes the (variable) quiet wait; the
                        # remaining stream is exactly the capture window.
                        text = (
                            f"Measuring noise (emitter off{soak_text.lower()}): capturing "
                            f"the {noise_capture_seconds:.0f} s window… "
                            f"{elapsed_s:.1f} s total"
                        )
                        fraction = 0.2 + 0.8 * min(
                            1.0, elapsed_s / (NOISE_WAIT_BEFORE_CAPTURE_S + noise_capture_seconds)
                        )
                    else:
                        delta_text = (
                            ""
                            if baseline_delta_mv is None
                            else (
                                f" (level moving {baseline_delta_mv * 1000.0:.0f} µV/s, "
                                f"start at ≤{NOISE_BASELINE_SETTLE_DELTA_MV * 1000.0:.0f})"
                            )
                        )
                        text = (
                            "Measuring noise (emitter off): waiting for the "
                            f"offset level to settle {elapsed_s:.1f}/"
                            f"{NOISE_WAIT_MAX_S:.0f} s max{delta_text}…"
                        )
                        fraction = 0.2 * min(1.0, elapsed_s / NOISE_WAIT_MAX_S)
                    push(lambda value=text: self.set_measure_status(token, value))
                    step_progress(noise_step, "Noise (emitter off)", fraction)

                def noise_capture(attempt: int):
                    return device.read_noise_capture(
                        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
                        capture_seconds=noise_capture_seconds,
                        progress=noise_progress,
                        preview=preview,
                        cancelled=lambda: token != self.measure_token,
                    )

                def noise_capture_retry(attempt: int) -> None:
                    push(lambda: self.set_measure_status(
                        token,
                        "Serial stream glitch during the noise capture; "
                        f"nothing was recorded — restarting the capture "
                        f"({attempt}/{REFERENCE_READING_STREAM_RETRIES})…",
                    ))

                (
                    noise_waveform,
                    noise_left_context,
                    noise_right_context,
                    noise_rate,
                    noise_wait_s,
                    _noise_elapsed_s,
                    noise_baseline_settled,
                ) = call_with_stream_retries(
                    noise_capture, on_retry=noise_capture_retry
                )
                # Retain the RAW capture for this part so the operator can
                # opt in to saving it for spike-morphology analysis (rise
                # times etc.) - the band-limited verdict trace cannot recover
                # this. ~20k floats; freed when the next sensor is prepared.
                self.last_noise_raw_waveform = np.asarray(
                    noise_waveform, dtype=float
                )
                self.last_noise_raw_rate_hz = float(noise_rate)
                self.last_noise_raw_left_context = np.asarray(
                    noise_left_context, dtype=float
                )
                self.last_noise_raw_right_context = np.asarray(
                    noise_right_context, dtype=float
                )
                push(lambda: self.set_preview_display(token, noise=False))
                # Gate on the band-limited trace (see the NOISE_* constants:
                # the ~429 uV pin-level limit is unreadable at the raw 500 Hz
                # bandwidth); clipping is still checked on the raw samples.
                noise_analysis, noise_filtered, noise_filtered_rate = (
                    analyze_noise_capture_band_limited(
                        noise_waveform,
                        noise_rate,
                        decimation_factor=NOISE_DECIMATION_FACTOR,
                        window_s=NOISE_WINDOW_S,
                        threshold_mv=NOISE_PP_LIMIT_MV,
                        max_over_fraction=noise_max_over_fraction,
                        clip_limit_v=NOISE_CLIP_LIMIT_V,
                        left_context_v=noise_left_context,
                        right_context_v=noise_right_context,
                    )
                )
                noise_report = NoiseCaptureReport.from_analysis(
                    noise_analysis,
                    max_over_fraction=noise_max_over_fraction,
                    settle_s=noise_wait_s,
                    capture_s=len(noise_waveform) / noise_rate,
                    analysis_rate_hz=noise_filtered_rate,
                    baseline_settled=noise_baseline_settled,
                )
                self.last_noise_metrics = build_noise_waveform_metrics(
                    np.asarray(noise_filtered, dtype=float),
                    noise_filtered_rate,
                    offset_v=offset_v,
                    input_range_v=waveform_range_v,
                )
                if noise_report.windows_over:
                    # Any over-limit window is evidence worth keeping (burst
                    # episode or environmental transient): auto-save the raw
                    # capture for the spike-morphology library, PASS or FAIL.
                    push(lambda rep=noise_report: (
                        self.auto_save_noise_capture(token, rep)
                    ))
                if noise_report.outcome == OUTCOME_FAIL:
                    # Fail fast: sensitivity is not measured on a noisy part.
                    # The offset has had the settle wait plus the 20 s noise
                    # capture to stabilize - record the fresh value so the
                    # noisy part's offset matches its settled level (same
                    # policy as the post-sensitivity re-read below).
                    offset_v = device.read_offset_voltage(
                        waveform_range_v=waveform_range_v
                    )
                    push(lambda v=offset_v: self.on_offset_update(token, v))
                    metrics, final = build_noise_failure_result(
                        offset_v,
                        noise_report,
                        input_range_v=waveform_range_v,
                    )
                    self.last_capture_report = None
                    self.last_noise_report = noise_report
                    return metrics, final, offset_v
                step_progress(noise_step, "Noise (emitter off)", 1.0)
                worst_text = (
                    ""
                    if noise_report.worst_pp_mv is None
                    else f" (worst window {noise_report.worst_pp_mv:.1f} mV pk-pk)"
                )
                push(lambda text=(
                    f"Noise PASS{worst_text}. "
                    "Driving emitter for the sensitivity capture…"
                ): self.set_measure_status(token, text))
            else:
                noise_report = NoiseCaptureReport.skipped("noise test disabled")

            emitter_on_time: float | None = None
            emitter_off_time: float | None = None
            try:
                # The command may reach the ESP32 before its acknowledgement
                # fails, so PWM shutdown must cover activation as well as the
                # subsequent stream capture.

                def progress(current: StabilityAnalysis) -> None:
                    current_report = current.report
                    if current_report.stabilized:
                        text = (
                            f"Attempt {current_report.measurement_attempt}/{MAX_MEASUREMENT_ATTEMPTS}: "
                            f"stable at {current_report.stabilization_elapsed_s:.1f} s. "
                            f"Measuring sensitivity cycle {current_report.measurement_cycle_count}/"
                            f"{current_report.measurement_cycles_required}..."
                        )
                        fraction = 0.5 + 0.5 * min(
                            1.0,
                            current_report.measurement_cycle_count
                            / max(1, current_report.measurement_cycles_required),
                        )
                    else:
                        latest = current.cycles[-1] if current.cycles else None
                        delta_text = (
                            "waiting for first two peaks"
                            if latest is None or latest.absolute_peak_delta_mv is None
                            else f"peak Δ {latest.absolute_peak_delta_mv:.3f} mV"
                        )
                        confirmation = current_report.active_confirmation_run_length
                        text = (
                            f"Attempt {current_report.measurement_attempt}/{MAX_MEASUREMENT_ATTEMPTS}: "
                            f"stabilizing... {min(current_report.total_pwm_on_seconds, STABILITY_TIMEOUT_S):.1f}/"
                            f"{STABILITY_TIMEOUT_S:.1f} s · {delta_text} · "
                            f"{min(confirmation, current_report.active_confirmation_count)}/"
                            f"{current_report.active_confirmation_count} stable"
                        )
                        fraction = 0.5 * min(
                            1.0,
                            confirmation
                            / max(1, current_report.active_confirmation_count),
                        )
                    push(lambda value=text: self.set_measure_status(token, value))
                    step_progress(sensitivity_step, "Sensitivity", fraction)

                def driven_capture(attempt: int):
                    nonlocal emitter_on_time
                    # (Re-)activating restarts the PWM phase, so a stream
                    # retry gets a clean cycle boundary and a fresh anchor.
                    activation_time = device.configure_emitter_pwm(
                        channel=pwm_channel,
                        frequency_hz=pwm_hz,
                        duty_cycle_percent=pwm_duty,
                    )
                    emitter_on_time = (
                        float(activation_time)
                        if isinstance(activation_time, (int, float))
                        else time.monotonic()
                    )
                    push(lambda: self.set_measure_status(
                        token,
                        f"Emitter PWM on. Attempt 1/{MAX_MEASUREMENT_ATTEMPTS}: "
                        f"stabilizing peak (0/{DUT_STABILITY_CONFIRMATION_DELTAS})...",
                    ))
                    step_progress(sensitivity_step, "Sensitivity", 0.0)
                    return device.read_waveform_until_stable(
                        waveform_range_v=waveform_range_v,
                        settings=settings,
                        pwm_started_monotonic=emitter_on_time,
                        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
                        expected_frequency_hz=EXPECTED_FREQUENCY_HZ,
                        progress=progress,
                        preview=preview,
                        cancelled=lambda: token != self.measure_token,
                    )

                def driven_capture_retry(attempt: int) -> None:
                    push(lambda: self.set_measure_status(
                        token,
                        "Serial stream glitch during the sensor capture; "
                        f"nothing was recorded — restarting the capture "
                        f"({attempt}/{REFERENCE_READING_STREAM_RETRIES})…",
                    ))

                waveform, sync, actual_rate, stability_analysis = call_with_stream_retries(
                    driven_capture, on_retry=driven_capture_retry
                )
            finally:
                deactivation_time = device.disable_emitter_pwm(pwm_channel)
                emitter_off_time = (
                    float(deactivation_time)
                    if isinstance(deactivation_time, (int, float))
                    else time.monotonic()
                )

            # Settled-offset verification (2026-08-17): re-read AIN0 now that
            # the part has been powered in the fixture through the noise wait
            # plus both captures (~40 s minimum). The insertion-time read
            # above chases a still-rising offset (lot 500: mean 0.29 V low),
            # so the VERDICT and the CSV use this settled value; the early
            # read is recorded separately as offset_initial_v.
            offset_v = device.read_offset_voltage(waveform_range_v=waveform_range_v)

        push(lambda v=offset_v: self.on_offset_update(token, v))
        if stability_analysis.report.measurement_complete:
            metrics = analyze_v6_stable_measurement(
                waveform,
                sync,
                actual_rate,
                stability_analysis,
                offset_v=offset_v,
                input_range_v=waveform_range_v,
            )
            final = evaluate_result(offset_v, metrics, filter_setup)
            final = apply_signal_quality_gate(final, metrics)
            # The noise test already ran (and passed) before this capture; a
            # failed noise test never reaches this point. Applying the gate
            # here keeps the verdict correct even if the fail-fast policy is
            # ever relaxed.
            final = apply_noise_gate(final, noise_report)
        elif stability_analysis.report.unstable:
            metrics, final = build_stability_timeout_result(
                waveform,
                sync,
                actual_rate,
                stability_analysis,
                offset_v=offset_v,
                input_range_v=waveform_range_v,
            )
            # The emitter-off noise test ran before this driven capture, so
            # its measured PASS report is recorded alongside the Unstable
            # verdict instead of the old "skipped on unstable" placeholder.
        else:
            raise Esp32BackendError("Adaptive capture ended without a complete result.")
        host_pwm_on_seconds = None
        if emitter_on_time is not None and emitter_off_time is not None:
            host_pwm_on_seconds = emitter_off_time - emitter_on_time
        report = StabilityCaptureReport.from_analysis(
            stability_analysis,
            data_source="esp32_405m22",
            pwm_on_seconds=host_pwm_on_seconds,
        )
        self.last_capture_report = report
        self.last_noise_report = noise_report
        return metrics, final, offset_v

    def _simulate_measurement(self, filter_setup, sim_case, sim_low_battery, waveform_range_v, show_live, token, push, *, noise_soak=False):
        # Mirror the hardware battery gate so the lockout is testable without
        # the ESP32 (inert while BATTERY_MONITORING_ENABLED is False, exactly
        # like the hardware path).
        if BATTERY_MONITORING_ENABLED:
            battery_v = SIM_BATTERY_LOW_V if sim_low_battery else SIM_BATTERY_OK_V
            push(lambda v=battery_v: self.on_battery_update(v))
            if battery_v <= BATTERY_MIN_V:
                raise BatteryTooLowError(battery_v)
        settings = self.stability_settings
        if settings is None:
            raise StabilitySettingsError("V6.1 stability settings are unavailable.")
        reference_steps = 1 if REFERENCE_GATE_ENABLED else 0
        noise_step = 2 + reference_steps
        total_steps = reference_steps + (3 if NOISE_TEST_ENABLED else 2)
        sensitivity_step = total_steps

        def step_progress(step: int, label: str, fraction: float) -> None:
            push(
                lambda: self.set_measure_progress(
                    token, step, total_steps, label, fraction
                )
            )

        waveform, sync, actual_rate, offset_v = simulate_v6_startup_capture(
            filter_setup,
            sim_case,
        )
        push(lambda v=offset_v: self.on_initial_offset(token, v))
        push(lambda v=offset_v: self.on_offset_update(token, v))
        step_progress(1, "Offset", 1.0)

        # Mirror the hardware fail-fast, which is HIGH-SIDE ONLY (2026-08-17):
        # the "High offset" case rails at the ADC full scale like a real bad
        # part and records the failure immediately, skipping the reference,
        # noise and sensitivity steps. A low simulated offset (the "Low
        # offset" case) flows through the whole sequence like real hardware
        # and fails at the final settled-offset evaluation instead.
        if offset_v > OFFSET_MAX_V:
            push(lambda v=offset_v: self.set_measure_status(
                token,
                f"Offset {v:.3f} V is outside the {OFFSET_MIN_V:.1f}-"
                f"{OFFSET_MAX_V:.1f} V band (simulated) — recording the failure…",
            ))
            metrics, final = build_offset_failure_result(
                offset_v, input_range_v=waveform_range_v
            )
            self.last_capture_report = None
            self.last_noise_report = NoiseCaptureReport.skipped(
                "offset out of range - reference, noise and sensitivity skipped"
            )
            return metrics, final, offset_v

        if REFERENCE_GATE_ENABLED:
            self.last_reference_check_mv = 100.0
            step_progress(2, "Reference check", 1.0)
            push(lambda: self.set_measure_status(
                token,
                "Reference unit passed (simulated).",
            ))

        # Simulated emitter-off noise step, in the same order as hardware
        # (noise before sensitivity). The SNR gate and the noise gate are
        # intentionally NOT applied to synthetic data - the simulator is a
        # training model - but a plausible passing report plus a matching
        # synthetic trace exercise the UI/CSV plumbing and the relative
        # noise-range display.
        if NOISE_TEST_ENABLED:
            push(lambda: self.set_measure_status(
                token,
                "Measuring noise (emitter off, simulated)…",
            ))
            push(lambda: self.set_preview_display(token, noise=True))
            # A deterministic synthetic trace (~36 µV RMS raw, i.e. a healthy
            # part comfortably inside the ~429 µV pin-level limit) run through
            # the SAME band-limited analysis as hardware, so the report, the
            # µV noise scope, and the CSV plumbing are exercised end to end.
            sim_capture_seconds = (
                NOISE_SOAK_CAPTURE_SECONDS if noise_soak else NOISE_CAPTURE_SECONDS
            )
            sim_max_over_fraction = (
                NOISE_SOAK_MAX_OVER_FRACTION if noise_soak else NOISE_MAX_OVER_FRACTION
            )
            sim_noise_rng = np.random.default_rng(2412)
            sim_noise = offset_v + sim_noise_rng.normal(
                0.0, 36e-6, int(sim_capture_seconds * actual_rate)
            )
            # Edge context drawn AFTER the capture so the seeded capture
            # samples stay identical to the pre-2026-08-31 simulator.
            sim_left_context = offset_v + sim_noise_rng.normal(
                0.0, 36e-6, NOISE_EDGE_CONTEXT_SAMPLES
            )
            sim_right_context = offset_v + sim_noise_rng.normal(
                0.0, 36e-6, NOISE_EDGE_CONTEXT_SAMPLES
            )
            # Parity with hardware: the raw capture stays available so the
            # "Save noise capture" button can be exercised in training mode.
            self.last_noise_raw_waveform = np.asarray(sim_noise, dtype=float)
            self.last_noise_raw_rate_hz = float(actual_rate)
            self.last_noise_raw_left_context = np.asarray(
                sim_left_context, dtype=float
            )
            self.last_noise_raw_right_context = np.asarray(
                sim_right_context, dtype=float
            )
            sim_analysis, sim_filtered, sim_filtered_rate = (
                analyze_noise_capture_band_limited(
                    sim_noise,
                    actual_rate,
                    decimation_factor=NOISE_DECIMATION_FACTOR,
                    window_s=NOISE_WINDOW_S,
                    threshold_mv=NOISE_PP_LIMIT_MV,
                    max_over_fraction=sim_max_over_fraction,
                    clip_limit_v=NOISE_CLIP_LIMIT_V,
                    left_context_v=sim_left_context,
                    right_context_v=sim_right_context,
                )
            )
            self.last_noise_report = NoiseCaptureReport.from_analysis(
                sim_analysis,
                max_over_fraction=sim_max_over_fraction,
                settle_s=NOISE_WAIT_BEFORE_CAPTURE_S,
                capture_s=sim_capture_seconds,
                analysis_rate_hz=sim_filtered_rate,
                baseline_settled=True,
            )
            self.last_noise_metrics = build_noise_waveform_metrics(
                np.asarray(sim_filtered, dtype=float),
                sim_filtered_rate,
                offset_v=offset_v,
                input_range_v=waveform_range_v,
            )
            if show_live:
                noise_tail = sim_noise[-STREAM_PREVIEW_MAX_SAMPLES:].copy()
                push(lambda wf=noise_tail: self.on_preview_frame(
                    token, wf, np.zeros(len(wf), dtype=float)
                ))
            step_progress(noise_step, "Noise (emitter off)", 1.0)
            push(lambda: self.set_preview_display(token, noise=False))
        else:
            self.last_noise_report = NoiseCaptureReport.skipped(
                "noise test disabled"
            )
            self.last_noise_metrics = None

        push(lambda: self.set_measure_status(
            token,
            "Emitter PWM on (simulated). Evaluating startup peak drift...",
        ))
        step_progress(sensitivity_step, "Sensitivity", 0.0)
        dut_settings = dut_stability_settings(settings)
        full_analysis = analyze_stability(
            waveform,
            sync,
            actual_rate,
            dut_settings,
            stability_deadline_s=STABILITY_TIMEOUT_S,
            measurement_cycles_required=SENSITIVITY_MEASUREMENT_CYCLES,
            enforce_measurement_stability=True,
            max_measurement_attempts=MAX_MEASUREMENT_ATTEMPTS,
            data_source="simulator",
        )
        if full_analysis.report.measurement_complete:
            cut_sample = min(
                len(waveform),
                full_analysis.measurement_cycles[-1].end_index + 1,
            )
        else:
            cut_sample = min(
                len(waveform),
                int(math.ceil(STABILITY_TIMEOUT_S * actual_rate)) + 1,
            )
        waveform = waveform[:cut_sample]
        sync = sync[:cut_sample]
        stability_analysis = analyze_stability(
            waveform,
            sync,
            actual_rate,
            dut_settings,
            stability_deadline_s=STABILITY_TIMEOUT_S,
            measurement_cycles_required=SENSITIVITY_MEASUREMENT_CYCLES,
            enforce_measurement_stability=True,
            max_measurement_attempts=MAX_MEASUREMENT_ATTEMPTS,
            data_source="simulator",
        )
        if show_live:
            wf = waveform[-STREAM_PREVIEW_MAX_SAMPLES:].copy()
            sy = sync[-STREAM_PREVIEW_MAX_SAMPLES:].copy()
            push(lambda: self.on_preview_frame(token, wf, sy))
        if stability_analysis.report.measurement_complete:
            push(lambda: self.set_measure_status(
                token,
                f"Stable at {stability_analysis.report.stabilization_elapsed_s:.1f} s. "
                f"Measured {stability_analysis.report.measurement_cycles_required} "
                f"sensitivity cycles on attempt {stability_analysis.report.measurement_attempt}.",
            ))
            metrics = analyze_v6_stable_measurement(
                waveform,
                sync,
                actual_rate,
                stability_analysis,
                offset_v=offset_v,
                input_range_v=waveform_range_v,
            )
            final = evaluate_result(offset_v, metrics, filter_setup)
            # The SNR gate is intentionally NOT applied here: the simulator
            # is a training model rather than calibrated hardware noise. The
            # simulated emitter-off noise step already ran (and passed)
            # before this driven capture, matching the hardware order.
        elif stability_analysis.report.unstable:
            metrics, final = build_stability_timeout_result(
                waveform,
                sync,
                actual_rate,
                stability_analysis,
                offset_v=offset_v,
                input_range_v=waveform_range_v,
            )
            # The simulated noise step already recorded its passing report
            # before this capture; keep it, as the hardware path does.
        else:
            raise RuntimeError("Simulator capture did not reach a complete v6.1 decision.")
        report = StabilityCaptureReport.from_analysis(stability_analysis, data_source="simulator")
        self.last_capture_report = report
        return metrics, final, offset_v

    def set_measure_status(self, token: int, text: str) -> None:
        if token == self.measure_token:
            self.measure_status_var.set(text)

    def set_measure_progress(
        self, token: int, step: int, total: int, label: str, fraction: float
    ) -> None:
        """Advance the measuring screen's step ladder (never backward).

        ``fraction`` is progress within the current step; within a step the
        bar only ever moves forward (a restarted capture attempt keeps the
        bar where it was while the status text explains the retry).
        """
        if token != self.measure_token:
            return
        fraction = max(0.0, min(1.0, float(fraction)))
        if step < self.measure_progress_step:
            return
        if step > self.measure_progress_step:
            self.measure_progress_step = step
            self.measure_progress_fraction = 0.0
        self.measure_progress_total = max(1, int(total))
        self.measure_progress_fraction = max(
            self.measure_progress_fraction, fraction
        )
        self.measure_step_var.set(
            f"STEP {step}/{self.measure_progress_total} — {label.upper()}"
        )
        self._redraw_measure_progress()

    def _reset_measure_progress(self) -> None:
        self.measure_progress_step = 0
        self.measure_progress_fraction = 0.0
        self.measure_step_var.set("")
        self.preview_noise_display = False

    def set_preview_display(self, token: int, *, noise: bool) -> None:
        """Switch the live scope between absolute volts and noise-range mode."""
        if token != self.measure_token:
            return
        if self.preview_noise_display == bool(noise):
            return
        self.preview_noise_display = bool(noise)
        self._apply_preview_display_mode()

    def _apply_preview_display_mode(self) -> None:
        noise = self.preview_noise_display
        self.live_wave_header_var.set(
            (
                "LIVE NOISE (EMITTER OFF, BAND-LIMITED)  ·  RANGE AROUND MEAN — RED = "
                f"±{format_noise_pp(NOISE_PP_LIMIT_MV / 2, decimals=1)} CUTOFF "
                f"({format_noise_pp(NOISE_PP_LIMIT_MV)} PK-PK LIMIT)"
            )
            if noise
            else "LIVE SIGNAL  ·  ADS AIN0 SENSOR + PWM SYNC OVERLAY (HIGH = EMITTER ON)"
        )
        canvas = self.wave_canvas
        if canvas is None or not canvas.winfo_exists() or not self.measuring:
            return
        if noise:
            canvas.set_display_mode(
                relative_band_mv=NOISE_PP_LIMIT_MV,
                min_span_v=2.0 * NOISE_PP_LIMIT_MV / 1000.0,
                channel_label="AIN0 · NOISE RANGE (EMITTER OFF)",
                sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ / NOISE_DECIMATION_FACTOR,
            )
        else:
            canvas.set_display_mode(
                relative_band_mv=None,
                min_span_v=None,
                channel_label="AIN0 · SENSOR",
                sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
            )

    def on_initial_offset(self, token: int, offset_v: float) -> None:
        """Record the insertion-time offset read (diagnostic; see CSV column).

        The verdict uses the settled re-read taken after the sensitivity
        capture; keeping the first read alongside it makes the settling
        behavior of each part visible in the batch data.
        """
        if token != self.measure_token:
            return
        self.last_offset_initial_v = offset_v

    def on_offset_update(self, token: int, offset_v: float) -> None:
        if token != self.measure_token:
            return
        self.measure_status_var.set(f"DC offset: {offset_v:.3f} V.")

    def on_preview_frame(self, token: int, waveform: np.ndarray, sync: np.ndarray) -> None:
        if token != self.measure_token:
            return
        if self.preview_noise_display and waveform.size >= NOISE_DECIMATION_FACTOR:
            # The live noise view decimates preview frames with a plain
            # per-block mean (boxcar) - a DISPLAY approximation of the
            # verdict trace, which is computed offline over the full capture
            # by the Kaiser anti-alias FIR (up to ~3 dB apart at 10-22 Hz).
            # Read verdicts from the result screen, not off this preview.
            blocks = waveform.size // NOISE_DECIMATION_FACTOR
            waveform = (
                waveform[: blocks * NOISE_DECIMATION_FACTOR]
                .reshape(blocks, NOISE_DECIMATION_FACTOR)
                .mean(axis=1)
            )
            sync = np.zeros(len(waveform), dtype=float)
        self.preview_waveform = waveform
        self.preview_sync = sync
        self.redraw_waveform()

    def on_measure_done(self, token: int, metrics: WaveformMetrics, final: FinalResult) -> None:
        if token != self.measure_token:
            return
        self.measuring = False
        self.busy = False
        self._reset_measure_progress()
        self.last_metrics = metrics
        self.last_result = final
        self.failure_mode_var.set(suggest_failure_mode(final))
        self._log_attempt(attempt_history.EVENT_MEASURED, result=final)
        self.preview_waveform = metrics.waveform_v
        self.preview_sync = metrics.sync_v
        outcome = result_outcome(final)
        if outcome == OUTCOME_PASS and is_sensitivity_near_limit(final):
            self.status_var.set(
                f"{self.current_sensor_id}: PASS — sensitivity is near the limit (within the "
                "conversion-factor margin). Re-measure is suggested; saving records a PASS."
            )
        elif outcome == OUTCOME_PASS:
            self.status_var.set(f"{self.current_sensor_id}: {OUTCOME_PASS}.")
        else:
            self.status_var.set(
                f"{self.current_sensor_id}: FAIL — confirm the failure mode, then save the sensor."
            )
        self.write_autosave("measurement_complete")
        self.render_step()

    def on_battery_block(self, token: int, battery_v: float) -> None:
        if token != self.measure_token:
            return
        self.measuring = False
        self.busy = False
        self.battery_v = battery_v
        self.battery_state = "low"
        self._refresh_battery_pill()
        self.status_var.set(f"Battery too low ({battery_v:.2f} V). Recharge the sensor battery to continue.")
        self.measure_status_var.set("")
        self._reset_measure_progress()
        self.render_step()
        messagebox.showwarning(
            "Recharge the battery",
            f"The battery is at {battery_v:.2f} V, at or below the {BATTERY_MIN_V:.1f} V minimum.\n\n"
            "Recharge it, then press “Re-check battery” before testing again.",
        )

    def on_reference_block(self, token: int, exc: ReferenceGateError) -> None:
        """Return to the load step and keep DUT testing locked after AIN1 fails."""
        if token != self.measure_token:
            return
        self.measuring = False
        self.busy = False
        self.step = self.LOAD_STEP
        self.status_var.set("Reference-unit lockout — the sensor was not read.")
        self.measure_status_var.set("")
        self._reset_measure_progress()
        self.render_step()
        messagebox.showwarning("Reference unit blocked the sensor test", str(exc))

    def _confirm_sensor_loaded(self, exc: "NoSensorDetectedError") -> bool:
        """An empty slot and a shorted part both float AIN0 - ask which it is."""
        return bool(
            messagebox.askyesno(
                "Is a sensor loaded?",
                f"AIN0 reads {exc.offset_v:.3f} V — the same as an empty slot.\n\n"
                f"Is a sensor loaded in the rig?\n\n"
                "Yes = record {self.current_sensor_id} as a bad (no-offset) sensor.\n"
                "No = nothing is recorded; seat the sensor and measure again.",
            )
        )

    def record_bad_sensor(self, offset_v: float) -> None:
        """Technician confirmed a sensor IS loaded: it fails as a dead/shorted part."""
        metrics, final = build_no_output_sensor_result(
            offset_v, input_range_v=WAVEFORM_INPUT_RANGE_V
        )
        self.last_measure_error = None
        self.last_metrics = metrics
        self.last_result = final
        self.last_offset_initial_v = offset_v
        self.last_noise_report = None
        self.last_noise_metrics = None
        self.preview_waveform = metrics.waveform_v
        self.preview_sync = metrics.sync_v
        self.failure_mode_var.set(BAD_SENSOR_FAILURE_MODE)
        self._log_attempt(attempt_history.EVENT_MEASURED, result=final)
        self.status_var.set(
            f"{self.current_sensor_id}: FAIL — no offset with a sensor loaded (bad part). Save the sensor."
        )
        self.measure_status_var.set("")
        self.write_autosave("measurement_complete")
        self.render_step()

    def on_hardware_not_ready(self, token: int, exc: HardwareNotReadyError) -> None:
        """A pre-flight check failed: nothing was measured or recorded."""
        if token != self.measure_token:
            return
        self.measuring = False
        self.busy = False
        if isinstance(exc, NoSensorDetectedError) and self._confirm_sensor_loaded(exc):
            self.record_bad_sensor(exc.offset_v)
            return
        self.last_measure_error = str(exc)
        self._log_attempt(attempt_history.EVENT_MEASURE_ERROR, reason=str(exc))
        self.status_var.set("Rig not ready - nothing was recorded. Check the wiring and measure again.")
        self.measure_status_var.set("")
        self._reset_measure_progress()
        self.render_step()
        messagebox.showwarning("Plug everything in first", str(exc))

    @staticmethod
    def _friendly_hardware_error(text: str) -> str:
        upper = text.upper()
        if "PERMISSION" in upper or "ACCESS DENIED" in upper:
            return ("The ESP32 serial port is not accessible. Add this user to the dialout group, "
                    "log out and back in, then retry.")
        if "BUSY" in upper or "CLAIMED" in upper or "EXCLUSIV" in upper:
            return ("The ESP32 serial port is already in use. Close Arduino Serial Monitor, "
                    "live_waveform.py, and other serial tools, then retry.")
        if ("NOT_FOUND" in upper or "NOT FOUND" in upper or "NO ESP32" in upper
                or "NO SERIAL" in upper or "DISCONNECTED" in upper):
            return ("No Eltec ESP32 rig detected. Plug in its USB cable and rig power, then try again. "
                    "(For training without hardware, turn on Simulator mode under Advanced options.)")
        return text

    def on_measure_error(self, token: int, exc: Exception) -> None:
        if token != self.measure_token:
            return
        self.measuring = False
        self.busy = False
        text = self._friendly_hardware_error(str(exc))
        self.last_measure_error = text
        self._log_attempt(attempt_history.EVENT_MEASURE_ERROR, reason=text)
        self.status_var.set(text)
        self.measure_status_var.set("")
        self._reset_measure_progress()
        self.render_step()
        messagebox.showerror("Measurement problem", text)

    def toggle_live_view(self) -> None:
        if self.measuring or self.step == self.RESULT_STEP:
            self.render_step()

    def toggle_result_details(self) -> None:
        if self.step == self.RESULT_STEP and not self.measuring:
            self.render_step()

    def redraw_waveform(self) -> None:
        if self.wave_canvas is not None and self.wave_canvas.winfo_exists():
            self.wave_canvas.set_data(self.preview_waveform, self.preview_sync)
        if self.noise_canvas is not None and self.noise_canvas.winfo_exists():
            noise_metrics = self.last_noise_metrics
            if noise_metrics is not None:
                self.noise_canvas.set_data(
                    noise_metrics.waveform_v, np.array([], dtype=float)
                )

    # ----- comment / snapshot ----- #
    def open_comment_window(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(f"Comment for {self.current_sensor_id}")
        dialog.minsize(S(620), S(420))
        dialog.configure(bg=PAGE_BG)
        dialog.transient(self)

        frame = tk.Frame(dialog, bg=PAGE_BG)
        frame.grid(row=0, column=0, sticky="nsew", padx=18, pady=16)
        dialog.rowconfigure(0, weight=1)
        dialog.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        frame.columnconfigure(0, weight=1)
        tk.Label(frame, text="COMMENT —", bg=PAGE_BG, fg=ELTEC_RED, font=self.fm(11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        tk.Label(frame, text=f"Sensor {self.current_sensor_id}", bg=PAGE_BG, fg=TEXT_DARK, font=self.fd(19)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 10))
        text = tk.Text(frame, wrap="word", font=self.fb(13), undo=True, relief="flat", bd=0,
                       bg=CARD_BG, fg=TEXT_DARK, padx=12, pady=10, insertbackground=ELTEC_BLUE,
                       highlightbackground=CARD_BORDER, highlightcolor=ELTEC_BLUE, highlightthickness=1)
        text.grid(row=2, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scroll.grid(row=2, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", self.notes_var.get())

        buttons = tk.Frame(frame, bg=PAGE_BG)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(14, 0))

        def save_comment(_event: tk.Event | None = None) -> str:
            self.notes_var.set(text.get("1.0", "end-1c").strip())
            self.update_comment_snapshot_status()
            self.write_autosave("comment_updated")
            dialog.destroy()
            return "break"

        def newline(_event: tk.Event | None = None) -> str:
            text.insert("insert", "\n")
            return "break"

        dialog.bind("<Return>", save_comment)
        dialog.bind("<KP_Enter>", save_comment)
        text.bind("<Return>", save_comment)
        text.bind("<Shift-Return>", newline)
        self.btn(buttons, "Cancel", dialog.destroy, kind="ghost", size="sm").grid(row=0, column=0, padx=(0, 10))
        self.btn(buttons, "Save Comment (Enter)", save_comment, kind="primary", size="sm").grid(row=0, column=1)
        text.focus_set()

    def update_comment_snapshot_status(self) -> None:
        comment = self.notes_var.get().strip()
        self.comment_status_var.set(f"Comment saved ({len(comment)} chars)" if comment else "")
        image_count = sum(path.suffix.lower() == ".png" for path in self.snapshot_paths)
        csv_count = sum(path.suffix.lower() == ".csv" for path in self.snapshot_paths)
        if image_count == 0:
            self.snapshot_status_var.set("")
        elif image_count == 1:
            suffix = " + stability CSV diagnostics" if csv_count else ""
            self.snapshot_status_var.set("1 waveform snapshot saved" + suffix)
        else:
            suffix = " + stability CSV diagnostics" if csv_count else ""
            self.snapshot_status_var.set(
                f"{image_count} waveform snapshots saved" + suffix
            )

    def capture_waveform_snapshot(self) -> None:
        if self.last_metrics is None:
            messagebox.showinfo("No waveform yet", "Run the measurement before capturing a waveform.")
            return
        try:
            snapshot_paths = save_waveform_diagnostic_bundle(
                self.batch_number,
                self.current_sensor_id,
                self.last_metrics,
                self.last_capture_report,
                title=f"{MODEL_NAME} {self.current_sensor_id} waveform snapshot",
                detail_lines=snapshot_detail_lines(
                    self.batch_number,
                    self.current_sensor_id,
                    self.last_metrics,
                    self.notes_var.get(),
                    self.last_capture_report,
                ),
                filename_suffix="snapshot",
            )
        except Exception as exc:
            messagebox.showerror("Waveform snapshot problem", str(exc))
            return
        if not snapshot_paths:
            messagebox.showinfo("No waveform", "No waveform samples were available to capture.")
            return
        self.snapshot_paths.extend(snapshot_paths)
        self.stability_diagnostics_saved = self.last_capture_report is not None
        self.update_comment_snapshot_status()
        self.write_autosave("waveform_snapshot_saved")

    def auto_save_noise_capture(self, token: int, report: "NoiseCaptureReport") -> None:
        """Keep the raw capture automatically whenever any window went over.

        An over-limit window is evidence either way - a burst episode from the
        part or an environmental transient - and the 2026-08-18 re-run showed
        both kinds are too rare to lose. Runs on the UI thread via push();
        the report is passed in because self.last_noise_report is only
        assigned once the whole measurement returns.
        """
        if token != self.measure_token or self.noise_raw_auto_saved:
            return
        waveform = self.last_noise_raw_waveform
        rate = self.last_noise_raw_rate_hz
        if waveform is None or rate is None or waveform.size == 0:
            return
        windows_over = report.windows_over
        try:
            paths = save_raw_noise_capture(
                self.batch_number,
                self.current_sensor_id,
                waveform,
                rate,
                left_context_v=getattr(
                    self, "last_noise_raw_left_context", None
                ),
                right_context_v=getattr(
                    self, "last_noise_raw_right_context", None
                ),
                metadata={
                    "operator_requested": (
                        f"no (automatic: {windows_over} window(s) over limit)"
                    ),
                    "noise_outcome": report.outcome,
                    "noise_windows_over": windows_over,
                    "noise_windows_total": report.windows_total,
                    "noise_worst_pp_mv": report.worst_pp_mv,
                },
            )
        except Exception:
            return  # never let diagnostics-keeping break the measurement
        if paths:
            self.snapshot_paths.extend(paths)
            self.noise_raw_auto_saved = True
            self.noise_capture_status_var.set(
                f"Raw noise capture auto-saved ({windows_over} window(s) over): "
                f"{paths[0].name} + .npz"
            )

    def save_noise_capture_for_analysis(self) -> None:
        """Operator opt-in: keep this part's RAW noise capture on disk.

        For offline spike-morphology work (rise times, widths, spike-to-spike
        comparison) the full 1000 SPS record is required - the 50 SPS
        band-limited trace the verdict uses cannot resolve it. Saved per part
        on request; noise FAILURES are saved automatically at save time.
        """
        waveform = self.last_noise_raw_waveform
        rate = self.last_noise_raw_rate_hz
        if waveform is None or rate is None or waveform.size == 0:
            messagebox.showinfo(
                "No noise capture yet",
                "Run the measurement first - the raw noise capture of the "
                "current sensor is kept until the next sensor is loaded.",
            )
            return
        report = self.last_noise_report
        metadata: dict[str, object] = {"operator_requested": "yes"}
        if report is not None and report.windows_total:
            metadata.update(
                noise_outcome=report.outcome,
                noise_windows_over=report.windows_over,
                noise_windows_total=report.windows_total,
                noise_worst_pp_mv=report.worst_pp_mv,
            )
        try:
            paths = save_raw_noise_capture(
                self.batch_number,
                self.current_sensor_id,
                waveform,
                rate,
                left_context_v=getattr(
                    self, "last_noise_raw_left_context", None
                ),
                right_context_v=getattr(
                    self, "last_noise_raw_right_context", None
                ),
                metadata=metadata,
            )
        except Exception as exc:
            messagebox.showerror("Could not save the noise capture", str(exc))
            return
        if not paths:
            messagebox.showinfo("No noise capture", "The capture was empty.")
            return
        self.snapshot_paths.extend(paths)
        seconds = waveform.size / max(rate, 1.0)
        self.noise_capture_status_var.set(
            f"Raw noise capture saved ({seconds:.0f} s @ {rate:.0f} SPS): "
            f"{paths[0].name} + .npz"
        )
        self.write_autosave("noise_capture_saved")
        self.status_var.set(f"Saved waveform snapshot: {snapshot_paths[0]}")

    # ----- autosave ----- #
    def write_autosave(self, stage: str) -> None:
        if not self.batch_number or not self.current_sensor_id:
            return
        autosave_path = batch_autosave_path(self.batch_number)
        autosave_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "stage": stage,
            "batch_number": self.batch_number,
            "tester_name": self.tester_name,
            "sensor_number": self.current_sensor_number,
            "sensor_id": self.current_sensor_id,
            "filter_setup": self.filter_setup,
            "resuming_skipped": self.resuming_skipped,
            "measure_attempts": self.measure_attempts,
            "skip_count": self.skip_count,
            "offset_v": None if self.last_result is None else self.last_result.offset_v,
            "sensitivity_mv": None if self.last_result is None else self.last_result.sensitivity_mv,
            "sensitivity_raw_mv": None if self.last_result is None else self.last_result.sensitivity_mv,
            "sensitivity_legacy_equivalent_mv": (
                None
                if self.last_result is None or self.last_result.sensitivity_mv is None
                else legacy_equivalent_sensitivity_mv(self.last_result.sensitivity_mv)
            ),
            "sensitivity_correction_factor": SENSITIVITY_LEGACY_EQUIVALENT_FACTOR,
            "sensitivity_calibration_id": SENSITIVITY_CALIBRATION_ID,
            "sensitivity_gate_outcome": (
                None
                if self.last_result is None or self.last_result.sensitivity_mv is None
                else sensitivity_gate_outcome(
                    self.last_result.sensitivity_mv, self.filter_setup
                )
            ),
            "sensitivity_raw_fail_below_mv": sensitivity_raw_limits_mv(
                self.filter_setup
            )[0],
            "sensitivity_raw_pass_above_mv": sensitivity_raw_limits_mv(
                self.filter_setup
            )[1],
            "polarity": None if self.last_result is None else self.last_result.polarity,
            "pass_fail": result_outcome(self.last_result),
            "fail_reasons": [] if self.last_result is None else self.last_result.fail_reasons,
            "failure_mode": self.failure_mode_var.get(),
            "battery_v": self.battery_v,
            "reference_calibration_mv": (
                None if self.reference_calibration is None else self.reference_calibration.mean_mv
            ),
            "reference_check_mv": self.last_reference_check_mv,
            "comment": self.notes_var.get(),
            "waveform_snapshot_paths": [str(path) for path in self.snapshot_paths],
            # TP412 emitter-off noise test.
            "noise_test_outcome": (
                None if self.last_noise_report is None else self.last_noise_report.outcome
            ),
            "noise_windows_total": (
                None if self.last_noise_report is None else self.last_noise_report.windows_total
            ),
            "noise_windows_over": (
                None if self.last_noise_report is None else self.last_noise_report.windows_over
            ),
            "noise_worst_pp_mv": (
                None if self.last_noise_report is None else self.last_noise_report.worst_pp_mv
            ),
            "noise_settle_s": (
                None if self.last_noise_report is None else self.last_noise_report.settle_s
            ),
            "noise_pp_limit_mv": (
                None if self.last_noise_report is None else self.last_noise_report.pp_limit_mv
            ),
        }
        try:
            with autosave_path.open("w", encoding="utf-8") as autosave_file:
                json.dump(payload, autosave_file, indent=2)
        except Exception:
            pass

    def delete_autosave(self) -> None:
        if not self.batch_number:
            return
        try:
            batch_autosave_path(self.batch_number).unlink(missing_ok=True)
        except Exception:
            pass

    # ----- batch summary ----- #
    def show_batch_summary_window(self, batch_number: str, csv_path: Path) -> None:
        summary = tk.Toplevel(self)
        summary.title(f"Batch {batch_number} Summary")
        summary.minsize(S(900), S(500))
        summary.configure(bg=PAGE_BG)

        rows = self._read_summary_rows(csv_path)
        counts = summarize_batch_outcomes([row[-1] for row in rows])
        tested = counts["tested"]
        passed = counts["passed"]
        failed = counts["failed"]
        not_measured = counts["not_measured"]
        yield_pct = counts["yield_pct"]

        head = tk.Frame(summary, bg=PAGE_BG)
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 6))
        tk.Label(head, text="BATCH SUMMARY —", bg=PAGE_BG, fg=ELTEC_RED, font=self.fm(11, "bold")).pack(anchor="w")
        tk.Label(head, text=f"Batch {batch_number} results", bg=PAGE_BG, fg=TEXT_DARK, font=self.fd(24)).pack(anchor="w", pady=(2, 0))
        chips = tk.Frame(head, bg=PAGE_BG)
        chips.pack(anchor="w", pady=(10, 0))
        chip_specs = [
            (f"{tested} TESTED", ELTEC_BLUE_DARK, ELTEC_BLUE_LIGHT),
            (f"{passed} PASSED", PASS_FG, PASS_BG),
            (f"{failed} FAILED", FAIL_FG, FAIL_BG),
            (f"YIELD {yield_pct:.0f}%", ELTEC_BLUE_DARK, ELTEC_BLUE_LIGHT),
        ]
        if not_measured:
            chip_specs.insert(
                4, (f"{not_measured} NOT MEASURED", MUTED_FG, GHOST_BG)
            )
        for chip_text, chip_fg, chip_bg in chip_specs:
            tk.Label(chips, text=chip_text, bg=chip_bg, fg=chip_fg, font=self.fm(10, "bold"), padx=12, pady=5).pack(side="left", padx=(0, 8))
        waiting = attempt_history.skipped_queue(
            attempt_history.attempts_path_for(csv_path), csv_path
        )
        if waiting:
            tk.Label(
                head,
                text=f"Skipped, not measured yet ({len(waiting)}): {attempt_history.format_queue(waiting)}",
                bg=PAGE_BG,
                fg="#8a5a00",
                font=self.fb(12, "bold"),
                wraplength=S(860),
                justify="left",
            ).pack(anchor="w", pady=(10, 0))

        frame = tk.Frame(summary, bg=PAGE_BG)
        frame.grid(row=1, column=0, sticky="nsew", padx=20, pady=(8, 0))
        summary.rowconfigure(1, weight=1)
        summary.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        style = ttk.Style(summary)
        style.configure("Summary.Treeview", font=self.fb(12), rowheight=S(32), background=CARD_BG, fieldbackground=CARD_BG, foreground=TEXT_DARK, borderwidth=0)
        style.configure("Summary.Treeview.Heading", font=self.fb(12, "bold"), background=ELTEC_BLUE_LIGHT, foreground=ELTEC_BLUE_DARK, relief="flat")
        columns = ("sensor", "offset", "sensitivity", "polarity", "result")
        headings = {"sensor": "Sensor", "offset": "Offset", "sensitivity": "Sensitivity", "polarity": "Polarity", "result": "Result"}
        tree = ttk.Treeview(frame, columns=columns, show="headings", style="Summary.Treeview", height=14)
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=150, anchor="center", stretch=True)
        tree.tag_configure("pass", background=PASS_BG)
        tree.tag_configure("fail", background=FAIL_BG)
        tree.tag_configure("skipped", background=GHOST_BG, foreground=MUTED_FG)
        for row in rows:
            if row[-1] == OUTCOME_PASS:
                tag = "pass"
            elif row[-1] == OUTCOME_NOT_MEASURED:
                tag = "skipped"
            else:
                tag = "fail"
            tree.insert("", "end", values=row, tags=(tag,))
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=y_scroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        self.btn(summary, "Close", summary.destroy, kind="primary", size="md").grid(row=2, column=0, sticky="e", padx=20, pady=14)

    def _read_summary_rows(self, csv_path: Path) -> list[tuple[str, str, str, str, str]]:
        rows: list[tuple[str, str, str, str, str]] = []
        try:
            with csv_path.open("r", newline="", encoding="utf-8") as csv_file:
                for row in csv.DictReader(csv_file):
                    raw_sensitivity = (
                        row.get("sensitivity_raw_mv", "")
                        or row.get("sensitivity_mv", "")
                    )
                    equivalent_sensitivity = row.get(
                        "sensitivity_legacy_equivalent_mv", ""
                    )
                    if equivalent_sensitivity:
                        sensitivity_summary = (
                            f"{self._fmt(equivalent_sensitivity, 2, ' mV eq')} "
                            f"({self._fmt(raw_sensitivity, 2, ' raw')})"
                        )
                    else:
                        sensitivity_summary = self._fmt(
                            raw_sensitivity, 2, " mV"
                        )
                    rows.append(
                        (
                            row.get("sensor_id", ""),
                            self._fmt(row.get("offset_v", ""), 3, " V"),
                            sensitivity_summary,
                            row.get("polarity_good_bad", "") or row.get("polarity", ""),
                            row.get("pass_fail", ""),
                        )
                    )
        except Exception:
            rows.append(("Could not read batch CSV", "", "", "", "FAIL"))
        return rows

    def _fmt(self, value: str, decimals: int, suffix: str) -> str:
        if not value:
            return ""
        try:
            return f"{float(value):.{decimals}f}{suffix}"
        except ValueError:
            return value

    # ----- hardware lifecycle ----- #
    def ensure_connected(self) -> None:
        if self.device is None:
            self.device = EmitterEsp32Rig()
        self.device.connect()

    def startup_probe(self) -> None:
        if self.stability_config_error is not None:
            self.status_var.set(
                "V6.1 stability configuration error — measurement is disabled. "
                + self.stability_config_error
            )
            return
        ok, message = probe_esp32_status()
        # Simulator mode is an explicit choice (Advanced options) - NEVER
        # auto-enable it. Older builds silently switched to the simulator when
        # found, which let a technician run a "test" against synthetic numbers
        # (with plausible-looking results) without
        # noticing that nothing was plugged in.
        if not ok and not self.simulator_var.get():
            message = self._friendly_hardware_error(message)
        self.status_var.set(message)
        if self.simulator_var.get():
            self.refresh_battery()

    def on_simulator_toggle(self) -> None:
        """Make entering/leaving simulator mode loud and reset stale readings."""
        self.battery_v = None
        self.battery_state = "unknown"
        self.battery_read_time = None
        self._refresh_battery_pill()
        self._redraw_header()  # shows/hides the SIMULATOR badge
        if self.stability_config_error is not None:
            self.status_var.set(
                "V6.1 stability configuration error — measurement is disabled. "
                + self.stability_config_error
            )
        elif self.simulator_var.get():
            self.status_var.set("SIMULATOR MODE - results are synthetic, no hardware is read.")
            self.refresh_battery()
        else:
            self.startup_probe()
        if not self.busy and not self.measuring:
            self.render_step()
        else:
            self.update_navigation_state()

    def on_close(self) -> None:
        self.measure_token += 1
        self.animator.cancel_all()
        # The worker observes the invalidated token, stops its stream, and
        # releases this lock. Only then may the UI thread send final serial
        # commands; concurrent STREAM reads and PWM/OFF writes can corrupt the
        # protocol state.
        with self.hardware_lock:
            if self.device is not None:
                try:
                    self.device.disable_emitter_pwm(EMITTER_PWM_CHANNEL)
                except Exception:
                    pass
                self.device.close()
        self.destroy()


def main() -> None:
    app = EmitterTesterApp()
    app.mainloop()


if __name__ == "__main__":
    main()
