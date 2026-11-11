"""Can this DAQ, as configured, resolve the noise of a 40623 part? A bench check.

One capture of all fifty channels (or a saved capture), every socket given a
ROLE by the engineer, then the same numbers for every role side by side::

    empty         nothing in the socket, as-is (the buffer input rests where the
                  PCB leaves it) - the same-path instrument background
    grounded      an empty socket shorted to ground with a wire (the "does the
                  noise go away?" recommendation) - read the clipping trap below
    quiet-source  a socket driven by a quiet DC source at a detector-like level
                  (a battery through a divider with a large capacitor across it)
    loaded        a real 40623 in the socket
    reference     the DAQ's own onboard reference through the same multiplexer
                  and amplifier path (``--daq-reference``, the probe's floor source)

The tool NEVER issues a part verdict. It answers one engineering question,
with every formula printed: is the instrument floor small enough, next to
TP120's noise window mapped to the pin, for a part's noise to be measured at
all on this acquisition setup? Three figures decide it:

* the FLOOR - the median 3 Hz/Q=3 band RMS of the usable floor sockets
  (empty sockets as-is by preference: same PCB, same cable, same DAQ; a
  quiet source or the onboard reference when no empty socket is usable);
* the WINDOW - TP120's 10.0-37.9 mV meter limits brought to the pin with the
  NOMINAL chain (9000232 x1000 at 3 Hz, 9000272 x10, meter = smoothed
  mean-absolute value) and a Gaussian mean-abs to RMS factor. This is a
  scale, not a calibration (NOISE_METHOD.md forbids dividing the meter
  limits by the nominal gain to make limits) - so the tool also prints the
  BREAK-EVEN chain gain: the real chain gain below which the measured floor
  would already be acceptable. Paired legacy readings decide the real gain;
* the MARGIN - the floor must be at most 1/margin of the low limit (default
  3: a floor at one third adds 5 % in quadrature to a part sitting exactly
  on the limit). ``--margin`` changes it; the rule is printed.

Verdicts on the acquisition setup (never on a part): RESOLVABLE (floor <=
low/margin), MARGINAL (floor <= low but above low/margin), NOT_RESOLVABLE
(the floor alone reads above the low limit: an empty socket would "pass" the
window), UNDETERMINED (no usable floor socket).

The clipping trap
-----------------
On the production unipolar 0-5 V range the converter's bottom rail is code
0 = 0 V. A socket shorted to ground sits ON that rail: the ADC cannot
represent the negative half of the noise and truncates the positive half, so
the trace goes flat. That is not the noise going away - it is the converter
refusing to show it (bench finding 2026-09-02: the onboard ground reference
reads exactly code 0 on every channel of a unipolar range). A grounded
socket that sits within ``RAIL_MARGIN_CODES`` of a rail, or whose raw
samples touch a rail, is reported CLIPPED and excluded from every floor
figure. The question the recommendation was after - is it the wiring or the
DAQ? - is answered instead by (a) the empty sockets as-is, whose buffers
rest a few mV above zero and are NOT clipped, (b) a grounded socket only
when it happens to stay off the rail (an op-amp offset can keep it there),
(c) a quiet source mid-range, and (d) the onboard reference: each removes
one more part of the path, and the RMS values subtract in quadrature.

Bench procedure (only one program on the DAQ at a time)
-------------------------------------------------------
1. Leave the tray as it is (some empty sockets, some parts), or add one quiet
   source and one grounded socket if you want those comparisons.
2. ``python array_rig/m40623/daq_noise_floor_check.py capture --loaded 5-5 5-10
   --grounded 2-1 --quiet-source 4-4 --daq-reference --npz C:\\bench\\floor.npz
   --report C:\\bench\\floor.json`` - 60 s, the tester's settings, every
   unassigned socket counts as empty.
3. Read the assessment block. Re-run under vacuum if the parts' noise moves.

Replay a capture you already have (the tester's ``tray_<n>_raw.npz`` carries
its own LOADED/EMPTY map; readout/viewer/probe files take roles from the
options)::

    python array_rig/m40623/daq_noise_floor_check.py replay "...\\tray_1_raw.npz" --report C:\\bench\\tray1_floor.json

``--simulate`` runs the capture on ``SimulatedDaq`` (no hardware; its noise
model is white and level-independent and it has no reference model, so the
numbers only exercise the tool). Files are written only at the explicit
paths given, never under ``Documents`` and never into an
``Eltec_*_Test_Results`` evidence folder; existing files are never replaced.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import json
import math
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

MODEL_DIR = Path(__file__).resolve().parent
if str(MODEL_DIR) not in sys.path:
    sys.path.insert(0, str(MODEL_DIR))

import array_analysis as aa  # noqa: E402
import daq_backend as daq  # noqa: E402
import daq_rig_readout as readout  # noqa: E402
import legacy_noise as ln  # noqa: E402

TOOL_NAME = "daq_noise_floor_check"
REPORT_SCHEMA_VERSION = 1

# ----------------------------------------------------------------------
# Roles and criteria
# ----------------------------------------------------------------------
ROLE_EMPTY = "empty"
ROLE_GROUNDED = "grounded"
ROLE_QUIET_SOURCE = "quiet-source"
ROLE_LOADED = "loaded"
ROLE_EXCLUDED = "excluded"
ROLE_REFERENCE = "reference"          # the onboard reference capture, never a socket
SOCKET_ROLES = (ROLE_EMPTY, ROLE_GROUNDED, ROLE_QUIET_SOURCE, ROLE_LOADED, ROLE_EXCLUDED)
# Which roles may supply the floor figure, in order of preference: the empty
# socket is the same PCB, cable and DAQ as a loaded one, so it is the floor
# that matters; a quiet source removes the socket's own pickup; the onboard
# reference removes the PCB entirely.
FLOOR_ROLE_PREFERENCE = (ROLE_EMPTY, ROLE_QUIET_SOURCE, ROLE_REFERENCE)

# TP120's noise window at the DC meter after the legacy chain (the constants
# live in legacy_noise, next to the filter they belong to).
LEGACY_METER_LOW_MV = ln.LEGACY_METER_LOW_MV
LEGACY_METER_HIGH_MV = ln.LEGACY_METER_HIGH_MV
# The nominal chain: 9000232 rev B x1000 at its 3 Hz centre, 9000272 rev J
# x10 - the same 10 000 legacy_noise uses for its "nominal meter estimate".
CHAIN_GAIN_NOMINAL = 1000.0 * 10.0
# The meter (nominally) shows 10 000 x the smoothed mean-absolute value of
# the 3 Hz-band noise; for Gaussian noise mean-abs = sqrt(2/pi) x RMS. The
# measured empties on 2026-09-10 gave mean-abs / RMS = 0.80, i.e. Gaussian.
GAUSSIAN_MEAN_ABS_OVER_RMS = math.sqrt(2.0 / math.pi)
# Floor <= low limit / margin. At 3 the floor adds sqrt(1 + 1/9) - 1 = 5.4 %
# in quadrature to a part sitting exactly on the low limit; the single rig
# runs at ~1 % of its limit (CALIBRATION_RECORD §2.2), i.e. margin ~100.
DEFAULT_MARGIN = 3.0
# A channel whose mean sits within this many codes of a rail has part of its
# noise distribution truncated by the converter (the raw noise is +-3-4
# codes on the production range): it is CLIPPED for floor purposes.
RAIL_MARGIN_CODES = 4.0
# A slow DC ramp (a part still settling) is not rejected by the 3 Hz band-pass:
# its steady-state response to a ramp of slope a is a / (Q w0) (final-value
# theorem on H(s) = (w0/Q) s / (s^2 + (w0/Q) s + w0^2)), i.e. slope / 56.5 at
# 3 Hz, Q = 3. Position 3-2 of the 2026-09-10 tray-1 capture drifted 17 mV in
# 60 s and read 5.7 uV "noise" - the ramp explains it. When the estimated
# leak is at least this fraction of the band RMS the reading is settling,
# not noise, and the channel is not usable for any figure.
SETTLING_LEAK_FRACTION = 0.5
# The judged-band (0.85-22 Hz) pk-pk the viewer shows is kept for continuity
# with the screenshots; the 3 Hz RMS is the figure the assessment uses. The
# band text is only true at the production 1000 scans/s (the 20:1 FIR's
# passband scales with the rate).
JUDGED_BAND_TEXT = readout.JUDGED_BAND_TEXT
PRODUCTION_SCAN_HZ = readout.DEFAULT_SCAN_HZ
# The USB-AIO16-64MA converts at most 500 kS/s in total, however the scan
# rate and the oversampling split it (CALIBRATION_RECORD §6; production uses
# 200 kS/s). While the front-end noise is white, the band-limited floor falls
# as 1/sqrt(conversions per channel per second), so this ceiling - not the
# code step - bounds what any settings change can do without gain.
DAQ_AGGREGATE_MAX_SPS = 500_000.0
# The experiment the assessment proposes runs at this share of the ceiling
# (production is 40 %; the stream integrity check guards the rest).
EXPERIMENT_AGGREGATE_FRACTION = 0.8
EXPERIMENT_SCAN_HZ = 500.0          # >= 20 SPS for the 3 Hz method; leaves room for 16 conversions per channel

# Acquisition defaults = the readout's = the tester's.
DEFAULT_SECONDS = ln.MIN_CAPTURE_S           # 60 s: the tester's capture and the 3 Hz metric's minimum
DEFAULT_QUIET_WAIT_S = 3.0                   # the tester's minimum quiet wait
DEFAULT_REFERENCE_SECONDS = 20.0             # the probe's floor length

VERDICT_RESOLVABLE = "RESOLVABLE"
VERDICT_MARGINAL = "MARGINAL"
VERDICT_NOT_RESOLVABLE = "NOT_RESOLVABLE"
VERDICT_UNDETERMINED = "UNDETERMINED"


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def split_tokens(values: Iterable[Any] | None) -> list[str]:
    """``['1-1,1-2', '5-5']`` -> ``['1-1', '1-2', '5-5']`` (commas and whitespace both separate)."""

    tokens: list[str] = []
    for value in values or ():
        for part in str(value).replace(",", " ").split():
            tokens.append(part)
    return tokens


def assign_roles(
    channels: Sequence[int],
    *,
    loaded: Iterable[Any] = (),
    grounded: Iterable[Any] = (),
    quiet_source: Iterable[Any] = (),
    excluded: Iterable[Any] = (),
    default_roles: dict[int, str] | None = None,
) -> dict[int, str]:
    """Role per captured channel. Explicit assignments win over ``default_roles``; the rest are empty.

    A channel named in two roles, or one that is not in the capture, is an
    error - a wrong map would silently move a part into the floor.
    """

    channel_set = {int(c) for c in channels}
    roles: dict[int, str] = {c: ROLE_EMPTY for c in channel_set}
    for channel, role in (default_roles or {}).items():
        if int(channel) in channel_set:
            roles[int(channel)] = role
    claimed: dict[int, str] = {}
    for role, tokens in ((ROLE_LOADED, loaded), (ROLE_GROUNDED, grounded),
                         (ROLE_QUIET_SOURCE, quiet_source), (ROLE_EXCLUDED, excluded)):
        for token in split_tokens(tokens):
            channel = readout.parse_channel_token(token)
            if channel not in channel_set:
                raise ValueError(f"{token!r} (CH{channel}) is not in this capture.")
            if channel in claimed and claimed[channel] != role:
                raise ValueError(
                    f"{readout.channel_label(channel)} is given two roles ({claimed[channel]} and {role})."
                )
            claimed[channel] = role
            roles[channel] = role
    return roles


def is_evidence_folder(path: str | Path) -> bool:
    """True when ``path`` lies inside a ``Documents/Eltec_*_Test_Results`` evidence folder."""

    resolved = Path(path).resolve()
    return any(fnmatch.fnmatch(part, "Eltec_*_Test_Results") for part in resolved.parts[:-1])


def check_output_path(path: str | Path, *, suffixes: tuple[str, ...]) -> Path:
    """An explicit, new file path outside the evidence folders, with one of ``suffixes``."""

    target = Path(path)
    if target.suffix.lower() not in suffixes:
        raise ValueError(f"{target} must end in {' or '.join(suffixes)}.")
    if is_evidence_folder(target):
        raise ValueError(
            f"{target} is inside an Eltec_*_Test_Results evidence folder; engineering tools never write there."
        )
    if target.exists():
        raise ValueError(f"{target} already exists; this tool never replaces a file.")
    return target


def quantization_rms_uv(range_code: int, sample_rate_hz: float, averaged: int = 1) -> float:
    """Ideal white quantization noise of one code step, in the 3 Hz/Q=3 band, in uV rms.

    The q/sqrt(12) x sqrt(2 ENBW / fs) model legacy_noise reports per channel
    (uniform, uncorrelated error, ENBW = pi f0 / (2 Q)), divided by
    sqrt(``averaged``) for the conversions averaged into each scan sample -
    legitimate here because the ~0.7-code front-end noise dithers the
    converter. A lower bound for a noiseless front end, never a measured floor.
    """

    if averaged < 1:
        raise ValueError("averaged must be >= 1.")
    enbw_hz = math.pi * ln.CENTER_HZ / (2.0 * ln.FILTER_Q)
    lsb_uv = daq.lsb_volts(range_code) * 1e6
    return lsb_uv / math.sqrt(12.0 * averaged) * math.sqrt(2.0 * enbw_hz / float(sample_rate_hz))


def averaged_conversions(oversample: int, drop_first: int) -> int:
    """Conversions averaged into each scan sample: oversample + 1 taken, ``drop_first`` discarded."""

    return max(1, int(oversample) + 1 - int(drop_first))


def tightest_offset_range(current_code: int) -> daq.RangeSpec:
    """The narrowest DAQ range that still holds a DC-coupled TP120 offset (0.3-1.2 V); the current one if none does."""

    fitting = [spec for spec in daq.RANGE_CODES.values()
               if spec.low_v <= aa.OFFSET_MIN_V and spec.high_v >= aa.OFFSET_MAX_V]
    if not fitting:
        return daq.range_spec(current_code)
    return min(fitting, key=lambda spec: spec.span_v)


def judged_label(sample_rate_hz: float) -> str:
    """``judged 0.85-22 Hz`` at the production rate; otherwise the FIR's passband has moved with the rate."""

    if math.isclose(float(sample_rate_hz), PRODUCTION_SCAN_HZ, rel_tol=1e-3):
        return f"judged {JUDGED_BAND_TEXT}"
    return f"judged ({aa.NOISE_DECIMATION_FACTOR}:1 FIR at {float(sample_rate_hz):g} SPS)"


def window_rms_uv(meter_mv: float, chain_gain: float = CHAIN_GAIN_NOMINAL) -> float:
    """A TP120 meter reading brought to the pin as an RMS (nominal chain, Gaussian mean-abs -> RMS)."""

    return meter_mv * 1000.0 / chain_gain / GAUSSIAN_MEAN_ABS_OVER_RMS


def settling_leak_uv(drift_mv: float, seconds: float) -> float:
    """The 3 Hz/Q=3 filter's steady-state output for a ramp of ``drift_mv`` over ``seconds``, in uV."""

    if seconds <= 0:
        return 0.0
    slope_uv_per_s = drift_mv * 1000.0 / seconds
    return slope_uv_per_s / (ln.FILTER_Q * 2.0 * math.pi * ln.CENTER_HZ)


def quadrature_excess(total_uv: float, floor_uv: float) -> float:
    """The part of ``total`` that is not the floor, RMS values adding in quadrature (0 when total <= floor)."""

    return math.sqrt(max(total_uv * total_uv - floor_uv * floor_uv, 0.0))


def _median(values: Sequence[float]) -> float:
    return float(np.median(values)) if len(values) else float("nan")


def _fmt(value: float, digits: int = 2) -> str:
    return "n/a" if value is None or not math.isfinite(value) else f"{value:.{digits}f}"


# ----------------------------------------------------------------------
# Loading a capture (live or saved)
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class FloorCapture:
    """Own-scale volts ``[channels, samples]`` plus the acquisition identity the analysis is stamped with."""

    volts: np.ndarray
    rate_hz: float                  # the rate the analysis runs at (granted timer when known)
    nominal_rate_hz: float
    actual_timer_recorded: bool
    channels: tuple[int, ...]
    positions: tuple[str, ...]
    range_code: int
    oversample: int
    drop_first: int
    daq_serial: str
    simulated: bool
    source: str                     # 'live' or the replayed file
    occupancy: tuple[str, ...] | None = None   # the tester's saved map, when the file has one

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(p or f"CH{c}" for p, c in zip(self.positions, self.channels))

    @property
    def seconds(self) -> float:
        return self.volts.shape[1] / self.rate_hz

    def legacy_config(self) -> ln.LegacyNoiseConfig:
        span = daq.range_spec(self.range_code)
        return ln.LegacyNoiseConfig(
            input_gain=1.0, adc_min_v=span.low_v, adc_max_v=span.high_v, adc_bits=16,
            range_code=self.range_code, oversample_extra=self.oversample, drop_first=self.drop_first,
            setup_id=f"{self.daq_serial}:buffer-1x:drop-{self.drop_first}",
        )

    def default_roles(self) -> dict[int, str]:
        """The tester's LOADED/EMPTY map as roles (anything else is excluded); empty when the file has none."""

        if self.occupancy is None:
            return {}
        roles = {}
        for channel, occupancy in zip(self.channels, self.occupancy):
            text = str(occupancy).upper()
            roles[channel] = ROLE_LOADED if text == "LOADED" else ROLE_EMPTY if text == "EMPTY" else ROLE_EXCLUDED
        return roles


def from_capture(cap: readout.Capture, *, source: str = "live") -> FloorCapture:
    return FloorCapture(
        volts=np.asarray(cap.volts, dtype=np.float64), rate_hz=cap.rate_hz, nominal_rate_hz=float(cap.scan_hz),
        actual_timer_recorded=cap.actual_timer_hz > 0, channels=cap.channels, positions=cap.positions,
        range_code=cap.range_code, oversample=cap.oversample, drop_first=cap.drop_first,
        daq_serial=cap.daq_serial, simulated=bool(cap.simulated), source=source,
    )


def _scalar(data: Any, key: str, default: Any = None) -> Any:
    if key not in data.files:
        if default is not None:
            return default
        raise ValueError(f"Capture metadata is missing {key!r}; the acquisition settings cannot be inferred safely.")
    value = np.asarray(data[key])
    if value.size != 1:
        raise ValueError(f"Capture metadata {key!r} must hold a single value.")
    return value.reshape(-1)[0].item()


def load_npz(path: str | Path) -> FloorCapture:
    """Read a tester ``tray_<n>_raw.npz`` or a readout / viewer / probe ``.npz`` (pickle disabled)."""

    source = Path(path).resolve(strict=True)
    with np.load(source, allow_pickle=False) as data:
        if "waveform_v" not in data.files:
            raise ValueError("Capture is missing 'waveform_v'.")
        volts = np.asarray(data["waveform_v"], dtype=np.float64)
        if volts.ndim != 2 or volts.shape[1] < 2:
            raise ValueError("Saved waveform_v must be [channels, samples] with at least two samples.")
        count = volts.shape[0]
        if "channels" not in data.files or "positions" not in data.files:
            raise ValueError("Capture is missing 'channels' or 'positions'.")
        channels = tuple(int(c) for c in np.asarray(data["channels"]).reshape(-1))
        positions = tuple(str(p) for p in np.asarray(data["positions"]).reshape(-1))
        if len(channels) != count or len(positions) != count or len(set(channels)) != count:
            raise ValueError("Saved channels/positions must name every waveform row once.")
        nominal = float(_scalar(data, "sample_rate_hz"))
        actual_known = "actual_timer_hz" in data.files
        rate = float(_scalar(data, "actual_timer_hz", default=nominal))
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("Saved sample rate must be positive and finite.")
        range_code = int(str(_scalar(data, "range_code")))
        daq.range_spec(range_code)
        oversample = int(str(_scalar(data, "oversample")))
        drop_key = "drop_conversions" if "drop_conversions" in data.files else "drop_first"
        drop = int(str(_scalar(data, drop_key)))
        serial = str(_scalar(data, "daq_serial", default="unknown"))
        simulated = str(_scalar(data, "simulated", default="False")).strip().lower() in ("true", "1")
        occupancy = None
        if "occupancy" in data.files:
            values = np.asarray(data["occupancy"]).reshape(-1)
            if values.size != count:
                raise ValueError("Saved occupancy must have one entry per waveform row.")
            occupancy = tuple(str(v) for v in values)
    return FloorCapture(
        volts=volts, rate_hz=rate, nominal_rate_hz=nominal, actual_timer_recorded=actual_known,
        channels=channels, positions=positions, range_code=range_code, oversample=oversample, drop_first=drop,
        daq_serial=serial, simulated=simulated, source=str(source), occupancy=occupancy,
    )


# ----------------------------------------------------------------------
# Per-channel numbers
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ChannelFloor:
    channel: int
    position: str
    role: str
    mean_v: float
    raw_rms_uv: float               # about the per-second means (slow drift removed)
    raw_span_lsb: float
    drift_mv: float                 # peak-to-peak of the per-second means
    settling_leak_uv: float         # what that drift alone puts through the 3 Hz filter (slope / (Q w0))
    clipped_samples: int            # raw samples within one code of a rail (legacy_noise's rule)
    bottom_rail: bool               # mean within RAIL_MARGIN_CODES of the range bottom
    top_rail: bool
    band_rms_uv: float              # 3 Hz / Q=3, legacy_noise
    band_mean_abs_uv: float
    smoothed_abs_uv: float
    nominal_meter_mv: float         # smoothed x 1000 x 10: scale only, never compared with TP120 by this tool
    judged_pp_median_uv: float      # 0.85-22 Hz 1 s windows (nan without a complete window)
    judged_pp_worst_uv: float
    usable: bool                    # contributes to its role's floor / summary
    notes: tuple[str, ...]


def analyze_channels(capture: FloorCapture, roles: dict[int, str]) -> list[ChannelFloor]:
    """Every channel's numbers; ``usable`` marks the ones that may stand for their role."""

    volts = capture.volts
    rate = capture.rate_hz
    config = capture.legacy_config()
    span = daq.range_spec(capture.range_code)
    lsb = span.lsb_v
    labels = capture.labels
    results, _, _ = ln.analyze_legacy_noise(volts, rate, positions=labels, channels=capture.channels, config=config)
    judged = np.empty((volts.shape[0], 0))
    if volts.shape[1] >= int(round(aa.NOISE_WINDOW_S * rate)):
        try:
            judged = aa.band_limited_window_pp_mv(volts, rate)
        except ValueError:
            judged = np.empty((volts.shape[0], 0))
    block = max(1, int(round(rate)))
    rows: list[ChannelFloor] = []
    for index, channel in enumerate(capture.channels):
        role = roles.get(channel, ROLE_EMPTY)
        trace = volts[index]
        blocks = trace[: (trace.size // block) * block].reshape(-1, block) if trace.size >= block else trace[None, :]
        block_means = blocks.mean(axis=1)
        detrended = blocks - block_means[:, None]
        raw_rms_uv = float(np.sqrt(np.mean(detrended * detrended))) * 1e6
        mean_v = float(trace.mean())
        result = results[index]
        bottom = mean_v < span.low_v + RAIL_MARGIN_CODES * lsb
        top = mean_v > span.high_v - RAIL_MARGIN_CODES * lsb
        notes: list[str] = []
        usable = True
        if bottom or top or result.clipped_samples:
            usable = False
            where = "bottom" if bottom else "top" if top else "a"
            notes.append(
                f"CLIPPED: sits at the {where} rail of the {span.name} range "
                f"({result.clipped_samples} raw samples within one code of it); the converter cannot show "
                "this channel's noise - not a floor measurement."
            )
        elif result.raw_span_lsb <= 1.0 + 1e-9:
            usable = False
            notes.append("Raw span <= one ADC code: undithered, the noise is unresolved here.")
        drift_mv = float(np.ptp(block_means)) * 1000.0
        leak_uv = settling_leak_uv(drift_mv, capture.seconds)
        band_rms_uv = result.band_rms_mv * 1000.0
        if usable and band_rms_uv > 0 and leak_uv >= SETTLING_LEAK_FRACTION * band_rms_uv:
            usable = False
            notes.append(
                f"SETTLING: a {drift_mv:.1f} mV DC ramp over the capture leaks about {leak_uv:.2f} uV through the 3 Hz "
                f"filter (slope / (Q w0)) - most of the {band_rms_uv:.2f} uV reading; let it settle and capture again."
            )
        if role == ROLE_LOADED and usable:
            if mean_v < aa.OFFSET_DEAD_V:
                usable = False
                notes.append(
                    f"Reads {mean_v:.3f} V, like an empty socket (below {aa.OFFSET_DEAD_V:g} V): no contact or no "
                    "output - says nothing about the DAQ."
                )
            elif not aa.OFFSET_MIN_V <= mean_v <= aa.OFFSET_MAX_V:
                notes.append(
                    f"Offset {mean_v:.3f} V is outside TP120's provisional {aa.OFFSET_MIN_V:g}-{aa.OFFSET_MAX_V:g} V band."
                )
        if role == ROLE_EXCLUDED:
            usable = False
        pp = judged[index] * 1000.0 if judged.shape[1] else np.empty(0)
        rows.append(ChannelFloor(
            channel=int(channel), position=labels[index], role=role, mean_v=mean_v, raw_rms_uv=raw_rms_uv,
            raw_span_lsb=float(result.raw_span_lsb), drift_mv=drift_mv, settling_leak_uv=leak_uv,
            clipped_samples=int(result.clipped_samples), bottom_rail=bool(bottom), top_rail=bool(top),
            band_rms_uv=band_rms_uv, band_mean_abs_uv=result.band_mean_abs_mv * 1000.0,
            smoothed_abs_uv=result.smoothed_abs_final_mv * 1000.0, nominal_meter_mv=result.nominal_meter_estimate_mv,
            judged_pp_median_uv=float(np.median(pp)) if pp.size else float("nan"),
            judged_pp_worst_uv=float(pp.max()) if pp.size else float("nan"),
            usable=usable, notes=tuple(notes),
        ))
    return rows


# ----------------------------------------------------------------------
# Roles side by side, and the assessment
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class RoleSummary:
    role: str
    count: int
    usable: int
    band_rms_median_uv: float
    band_rms_min_uv: float
    band_rms_max_uv: float
    smoothed_abs_median_uv: float
    nominal_meter_median_mv: float
    judged_pp_median_uv: float
    positions: tuple[str, ...]
    excluded: tuple[str, ...]


def summarize_roles(rows: Sequence[ChannelFloor]) -> dict[str, RoleSummary]:
    summaries: dict[str, RoleSummary] = {}
    for role in (*SOCKET_ROLES, ROLE_REFERENCE):
        members = [r for r in rows if r.role == role]
        if not members:
            continue
        usable = [r for r in members if r.usable]
        rms = [r.band_rms_uv for r in usable]
        judged = [r.judged_pp_median_uv for r in usable if math.isfinite(r.judged_pp_median_uv)]
        summaries[role] = RoleSummary(
            role=role, count=len(members), usable=len(usable),
            band_rms_median_uv=_median(rms), band_rms_min_uv=min(rms) if rms else float("nan"),
            band_rms_max_uv=max(rms) if rms else float("nan"),
            smoothed_abs_median_uv=_median([r.smoothed_abs_uv for r in usable]),
            nominal_meter_median_mv=_median([r.nominal_meter_mv for r in usable]),
            judged_pp_median_uv=_median(judged),
            positions=tuple(r.position for r in usable),
            excluded=tuple(f"{r.position}: {r.notes[0]}" if r.notes else r.position for r in members if not r.usable),
        )
    return summaries


@dataclass(frozen=True)
class RangeOption:
    code: int
    name: str
    lsb_uv: float
    quantization_rms_uv: float
    quantization_over_required: float
    fits_offset_band: bool          # can a DC-coupled 0.3-1.2 V offset sit inside it?


def range_options(sample_rate_hz: float, required_floor_uv: float, averaged: int = 1) -> list[RangeOption]:
    """What a range change alone could do to the ideal quantization floor - every range the DAQ has."""

    options = []
    for code, spec in sorted(daq.RANGE_CODES.items()):
        q = quantization_rms_uv(code, sample_rate_hz, averaged)
        options.append(RangeOption(
            code=code, name=spec.name, lsb_uv=spec.lsb_v * 1e6, quantization_rms_uv=q,
            quantization_over_required=q / required_floor_uv if required_floor_uv > 0 else float("inf"),
            fits_offset_band=spec.low_v <= aa.OFFSET_MIN_V and spec.high_v >= aa.OFFSET_MAX_V,
        ))
    return options


@dataclass(frozen=True)
class Assessment:
    verdict: str
    floor_role: str | None
    floor_rms_uv: float
    floor_rms_max_uv: float
    floor_channels: int
    averaged_conversions: int               # conversions averaged into each scan sample in this capture
    quantization_rms_uv: float              # ideal, at that averaging
    quantization_single_conversion_uv: float
    quantization_over_required: float
    window_low_rms_uv: float
    window_high_rms_uv: float
    chain_gain_nominal: float
    margin: float
    required_floor_rms_uv: float
    floor_over_low: float
    floor_over_high: float
    break_even_gain_low: float      # real chain gain below which this floor already meets the margin (low limit)
    break_even_gain_high: float
    min_front_end_gain: float       # gain ahead of the DAQ that meets the margin at the nominal chain gain
    dc_coupled_gain_limit: float    # above this a 1.2 V offset clips the range top: AC coupling needed
    kept_conversions_per_s: float   # conversions per channel per second averaged in this capture
    max_conversions_per_s: float    # the DAQ's aggregate ceiling shared over the scanned channels
    oversampling_ceiling_gain: float    # sqrt(max / kept): the most any oversampling / rate change can lower the floor
    tightest_range_code: int        # narrowest range that still fits a DC-coupled TP120 offset
    range_ceiling_gain: float       # current span / that span: the most a range change can add, if noise is fixed in codes
    best_case_floor_uv: float       # floor / (both gains): the best any settings change could reach without gain
    best_case_verdict: str
    suggested_experiment: str       # the capture command that measures that best case
    loaded_count: int
    loaded_usable: int
    loaded_above_floor: int         # loaded sockets noisier than the noisiest usable floor socket
    loaded_excess_median_uv: float  # median sqrt(part^2 - floor^2)
    wiring_rms_uv: float            # sqrt(empty^2 - grounded^2), when both usable
    buffer_path_rms_uv: float       # sqrt(empty^2 - reference^2), when both measured
    lines: tuple[str, ...]


def _verdict_for(floor_uv: float, *, low_uv: float, required_uv: float) -> str:
    if not math.isfinite(floor_uv):
        return VERDICT_UNDETERMINED
    if floor_uv <= required_uv:
        return VERDICT_RESOLVABLE
    if floor_uv <= low_uv:
        return VERDICT_MARGINAL
    return VERDICT_NOT_RESOLVABLE


def suggested_experiment(range_code: int, channels: int) -> tuple[str, int, float]:
    """``(command, oversample, scan_hz)`` that runs the tightest fitting range near the aggregate ceiling."""

    per_scan = int(EXPERIMENT_AGGREGATE_FRACTION * DAQ_AGGREGATE_MAX_SPS / (max(1, channels) * EXPERIMENT_SCAN_HZ))
    oversample = min(daq.MAX_OVERSAMPLE, max(1, per_scan - 1))
    command = (f"{TOOL_NAME}.py capture --range {range_code} --hz {EXPERIMENT_SCAN_HZ:g} "
               f"--oversample {oversample} --daq-reference")
    return command, oversample, EXPERIMENT_SCAN_HZ


def assess(rows: Sequence[ChannelFloor], summaries: dict[str, RoleSummary], *, range_code: int,
           sample_rate_hz: float, margin: float = DEFAULT_MARGIN, averaged: int = 1,
           channels: int = daq.CHANNEL_COUNT) -> Assessment:
    """The assessment, with the reasoning as printable lines. Never a part verdict."""

    if not math.isfinite(margin) or margin < 1.0:
        raise ValueError("margin must be a finite number >= 1.")
    low = window_rms_uv(LEGACY_METER_LOW_MV)
    high = window_rms_uv(LEGACY_METER_HIGH_MV)
    required = low / margin
    quantization = quantization_rms_uv(range_code, sample_rate_hz, averaged)
    quantization_single = quantization_rms_uv(range_code, sample_rate_hz)
    span = daq.range_spec(range_code)
    floor_role = next((r for r in FLOOR_ROLE_PREFERENCE if r in summaries and summaries[r].usable), None)
    lines: list[str] = []
    nan = float("nan")
    if floor_role is None:
        floor, floor_max, floor_n = nan, nan, 0
        verdict = VERDICT_UNDETERMINED
        lines.append("No usable floor socket: every empty / quiet-source / reference channel is clipped or excluded. "
                     "Leave at least one empty socket as-is (its buffer rests above the rail) or add a quiet source.")
    else:
        summary = summaries[floor_role]
        floor, floor_max, floor_n = summary.band_rms_median_uv, summary.band_rms_max_uv, summary.usable
        verdict = _verdict_for(floor, low_uv=low, required_uv=required)
    lines.append(
        f"floor = median 3 Hz/Q=3 band RMS of the usable {floor_role or '-'} channels "
        f"({floor_n}): {_fmt(floor)} uV rms (worst channel {_fmt(floor_max)})"
    )
    lines.append(
        f"ideal quantization of the {span.name} range ({span.lsb_v * 1e6:.1f} uV/code) in the same band with the "
        f"{averaged} conversion{'s' if averaged != 1 else ''} averaged per scan here: {quantization:.2f} uV rms "
        f"({quantization_single:.2f} for a single conversion)"
        + (f" - the measured floor is {floor / quantization:.1f} x that: the front end, not the code step, sets it"
           if math.isfinite(floor) and quantization > 0 else "")
    )
    lines.append(
        f"TP120 window {LEGACY_METER_LOW_MV:g}-{LEGACY_METER_HIGH_MV:g} mV at the meter -> at the pin with the NOMINAL "
        f"x{CHAIN_GAIN_NOMINAL:g} chain and Gaussian noise (mean-abs = {GAUSSIAN_MEAN_ABS_OVER_RMS:.3f} x rms): "
        f"{low:.2f}-{high:.2f} uV rms  [scale only: the real chain gain is not known]"
    )
    lines.append(f"rule: floor <= low limit / margin = {low:.2f} / {margin:g} = {required:.2f} uV rms")
    floor_over_low = floor / low if math.isfinite(floor) else nan
    floor_over_high = floor / high if math.isfinite(floor) else nan
    if verdict == VERDICT_RESOLVABLE:
        lines.append(f"{verdict}: the floor is {floor_over_low:.2f} x the low limit (<= 1/{margin:g}); a part on the low "
                     f"limit would read {math.sqrt(low * low + floor * floor) / low - 1:+.1%} from the floor alone.")
    elif verdict == VERDICT_MARGINAL:
        lines.append(f"{verdict}: the floor is {floor_over_low:.2f} x the low limit - under it, but above 1/{margin:g}; "
                     f"a part on the low limit would read {math.sqrt(low * low + floor * floor) / low - 1:+.1%} from the "
                     "floor alone, and a dead crystal is only just distinguishable.")
    elif verdict == VERDICT_NOT_RESOLVABLE:
        lines.append(f"{verdict}: the floor alone is {floor_over_low:.2f} x the low limit and {floor_over_high:.2f} x "
                     "the high limit - an EMPTY socket would read inside the window. A dead crystal (low limit) "
                     "cannot be told from a good part on this setup; the high side is buried too unless the "
                     "real chain gain is far below nominal.")
    break_even_low = LEGACY_METER_LOW_MV * 1000.0 / (GAUSSIAN_MEAN_ABS_OVER_RMS * margin * floor) if floor > 0 else nan
    break_even_high = LEGACY_METER_HIGH_MV * 1000.0 / (GAUSSIAN_MEAN_ABS_OVER_RMS * margin * floor) if floor > 0 else nan
    if math.isfinite(break_even_low):
        lines.append(
            f"break-even chain gain: this floor meets the margin for the low limit only if the real 9000232+9000272 "
            f"chain gain is below {break_even_low:,.0f} (nominal {CHAIN_GAIN_NOMINAL:,.0f}); for the high limit, below "
            f"{break_even_high:,.0f}. The 405's legacy amplifier ran ~6x under its nameplate - paired legacy readings "
            "decide this number, nothing else."
        )
    min_gain = floor / required if math.isfinite(floor) and required > 0 else nan
    dc_limit = span.high_v / aa.OFFSET_MAX_V
    q_over_required = quantization / required
    # What settings alone could still do. While the noise is white the
    # band-limited floor falls as 1/sqrt(conversions per channel per second)
    # (the same total noise power spread over a wider Nyquist band, or
    # averaged down) - so the DAQ's aggregate ceiling, shared over the scanned
    # channels, bounds every oversampling / scan-rate trade. A narrower range
    # helps only if the front-end noise is fixed in codes, the SAR's usual
    # behaviour; a range that clips the TP120 offset is not a candidate. Both
    # are ceilings, so the best case is a number to measure, not to believe.
    kept_per_s = float(averaged) * float(sample_rate_hz)
    max_per_s = DAQ_AGGREGATE_MAX_SPS / max(1, channels)
    os_gain = math.sqrt(max_per_s / kept_per_s) if kept_per_s > 0 else nan
    os_gain = max(1.0, os_gain) if math.isfinite(os_gain) else nan
    tightest = tightest_offset_range(range_code)
    range_gain = max(1.0, span.span_v / tightest.span_v)
    best = floor / (os_gain * range_gain) if math.isfinite(floor) and math.isfinite(os_gain) else nan
    best_verdict = _verdict_for(best, low_uv=low, required_uv=required)
    command, _exp_oversample, _exp_hz = suggested_experiment(tightest.code, channels)
    if math.isfinite(floor):
        lines.append(
            f"best case without gain ahead of the DAQ: while the noise is white the floor falls as "
            f"1/sqrt(conversions per channel per second); {kept_per_s:,.0f}/s are averaged here and the "
            f"{DAQ_AGGREGATE_MAX_SPS / 1000:g} kS/s aggregate over {channels} channels allows at most {max_per_s:,.0f}/s, "
            f"i.e. {os_gain:.2f} x lower; the tightest range that still fits a DC-coupled "
            f"{aa.OFFSET_MIN_V:g}-{aa.OFFSET_MAX_V:g} V offset ({tightest.name}) could add {range_gain:.1f} x IF the "
            f"front-end noise is fixed in codes -> {best:.2f} uV rms = {best / required:.1f} x the required floor "
            f"-> {best_verdict} at best. Measure it before believing it: {command}"
        )
    if math.isfinite(min_gain):
        lines.append(
            f"gain ahead of the DAQ needed to meet the margin at the nominal chain gain: >= {min_gain:.1f} x "
            f"(x100-x1000 gives comfortable headroom; 9000232 is x1000 at 3 Hz). Any gain above {dc_limit:.1f} x "
            f"puts a {aa.OFFSET_MAX_V:g} V offset over the {span.name} top, so the stage must be AC-coupled and the "
            "offset check kept on its own DC path."
        )
    loaded = summaries.get(ROLE_LOADED)
    loaded_rows = [r for r in rows if r.role == ROLE_LOADED and r.usable]
    above = sum(1 for r in loaded_rows if math.isfinite(floor_max) and r.band_rms_uv > floor_max)
    excess = _median([quadrature_excess(r.band_rms_uv, floor) for r in loaded_rows]) if loaded_rows and math.isfinite(floor) else nan
    if loaded is not None:
        lines.append(
            f"loaded sockets: {loaded.usable} usable of {loaded.count}; median {_fmt(loaded.band_rms_median_uv)} uV rms; "
            f"{above} read above the noisiest floor channel ({_fmt(floor_max)} uV); median excess over the floor in "
            f"quadrature {_fmt(excess)} uV rms"
            + (" - the parts are not distinguishable from empty sockets on this setup." if above == 0 and loaded.usable else "")
        )
    wiring = nan
    grounded = summaries.get(ROLE_GROUNDED)
    if grounded is not None:
        if grounded.usable == 0:
            lines.append(
                f"grounded sockets ({grounded.count}): ALL CLIPPED at the rail - the flat trace is the converter's "
                "bottom rail, not the noise going away. This comparison needs a quiet source mid-range instead."
            )
        elif ROLE_EMPTY in summaries and summaries[ROLE_EMPTY].usable:
            empty_floor = summaries[ROLE_EMPTY].band_rms_median_uv
            wiring = quadrature_excess(empty_floor, grounded.band_rms_median_uv)
            if grounded.band_rms_median_uv >= empty_floor / math.sqrt(2.0):
                lines.append(
                    f"grounded sockets ({grounded.usable} usable): {grounded.band_rms_median_uv:.2f} uV rms vs "
                    f"{empty_floor:.2f} uV empty as-is: shorting the input did not halve the floor power, so the floor "
                    f"is the buffer + DAQ, not the socket wiring (wiring share {wiring:.2f} uV rms in quadrature)."
                )
            else:
                lines.append(
                    f"grounded sockets ({grounded.usable} usable): {grounded.band_rms_median_uv:.2f} uV rms vs "
                    f"{empty_floor:.2f} uV empty as-is: the open socket / wiring contributes {wiring:.2f} uV rms in "
                    f"quadrature; the remaining {grounded.band_rms_median_uv:.2f} uV is the buffer + DAQ."
                )
        else:
            lines.append(f"grounded sockets ({grounded.usable} usable): {grounded.band_rms_median_uv:.2f} uV rms "
                         "(no usable empty socket to compare with).")
    buffer_path = nan
    reference = summaries.get(ROLE_REFERENCE)
    if reference is not None and reference.usable and ROLE_EMPTY in summaries and summaries[ROLE_EMPTY].usable:
        empty_floor = summaries[ROLE_EMPTY].band_rms_median_uv
        buffer_path = quadrature_excess(empty_floor, reference.band_rms_median_uv)
        lines.append(
            f"onboard reference (DAQ alone, same multiplexer/amplifier path): {reference.band_rms_median_uv:.2f} uV rms; "
            f"the PCB buffer path adds {buffer_path:.2f} uV rms in quadrature to reach the {empty_floor:.2f} uV empty-socket floor."
        )
    elif reference is not None and not reference.usable:
        lines.append("onboard reference: clipped or excluded - not usable as a floor.")
    return Assessment(
        verdict=verdict, floor_role=floor_role, floor_rms_uv=floor, floor_rms_max_uv=floor_max, floor_channels=floor_n,
        averaged_conversions=int(averaged), quantization_rms_uv=quantization,
        quantization_single_conversion_uv=quantization_single, quantization_over_required=q_over_required,
        window_low_rms_uv=low, window_high_rms_uv=high, chain_gain_nominal=CHAIN_GAIN_NOMINAL, margin=margin,
        required_floor_rms_uv=required, floor_over_low=floor_over_low, floor_over_high=floor_over_high,
        break_even_gain_low=break_even_low, break_even_gain_high=break_even_high, min_front_end_gain=min_gain,
        dc_coupled_gain_limit=dc_limit, kept_conversions_per_s=kept_per_s, max_conversions_per_s=max_per_s,
        oversampling_ceiling_gain=os_gain, tightest_range_code=tightest.code, range_ceiling_gain=range_gain,
        best_case_floor_uv=best, best_case_verdict=best_verdict, suggested_experiment=command,
        loaded_count=loaded.count if loaded else 0,
        loaded_usable=loaded.usable if loaded else 0, loaded_above_floor=above, loaded_excess_median_uv=excess,
        wiring_rms_uv=wiring, buffer_path_rms_uv=buffer_path, lines=tuple(lines),
    )


# ----------------------------------------------------------------------
# The whole check
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class FloorCheck:
    capture: FloorCapture
    roles: dict[int, str]
    rows: list[ChannelFloor]
    summaries: dict[str, RoleSummary]
    assessment: Assessment
    ranges: list[RangeOption]
    warnings: tuple[str, ...]
    reference: FloorCapture | None = None

    def report(self) -> dict[str, Any]:
        cap = self.capture
        return {
            "schema_version": REPORT_SCHEMA_VERSION, "tool": TOOL_NAME, "method_id": ln.METHOD_ID,
            "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "scope": "Instrument-floor assessment of the acquisition setup; never a detector verdict.",
            "source": cap.source, "simulated": cap.simulated, "seconds": cap.seconds,
            "actual_timer_recorded": cap.actual_timer_recorded,
            "acquisition": cap.legacy_config().acquisition_signature(cap.rate_hz),
            "criteria": {
                "chain_gain_nominal": CHAIN_GAIN_NOMINAL, "legacy_meter_low_mv": LEGACY_METER_LOW_MV,
                "legacy_meter_high_mv": LEGACY_METER_HIGH_MV, "gaussian_mean_abs_over_rms": GAUSSIAN_MEAN_ABS_OVER_RMS,
                "margin": self.assessment.margin, "rail_margin_codes": RAIL_MARGIN_CODES,
                "settling_leak_fraction": SETTLING_LEAK_FRACTION, "daq_aggregate_max_sps": DAQ_AGGREGATE_MAX_SPS,
                "floor_role_preference": list(FLOOR_ROLE_PREFERENCE), "judged_band": judged_label(cap.rate_hz),
            },
            "roles": {readout.channel_label(c): r for c, r in sorted(self.roles.items())},
            "channels": [asdict(r) for r in self.rows],     # reference rows carry role 'reference'
            "summaries": {role: asdict(s) for role, s in self.summaries.items()},
            "assessment": asdict(self.assessment),
            "range_options": [asdict(r) for r in self.ranges],
            "warnings": list(self.warnings),
        }


def run_check(capture: FloorCapture, roles: dict[int, str], *, margin: float = DEFAULT_MARGIN,
              reference: FloorCapture | None = None) -> FloorCheck:
    """Analyse one capture (plus an optional onboard-reference capture) under the given roles."""

    warnings: list[str] = []
    if capture.seconds < ln.MIN_CAPTURE_S and not math.isclose(capture.seconds, ln.MIN_CAPTURE_S, rel_tol=1e-6):
        warnings.append(f"Capture is {capture.seconds:.1f} s; the 3 Hz metric excludes its first {ln.FILTER_SETTLE_S:g} s "
                        f"and the tester uses {ln.MIN_CAPTURE_S:.0f} s - treat these numbers as exploratory.")
    if not capture.actual_timer_recorded:
        warnings.append("The granted timer frequency was not recorded; the nominal rate was used.")
    if capture.simulated:
        warnings.append("Simulated capture: the simulator's noise is white and level-independent and it has no "
                        "reference model - the numbers only exercise the tool.")
    rows = analyze_channels(capture, roles)
    if reference is not None:
        ref_roles = {c: ROLE_REFERENCE for c in reference.channels}
        rows.extend(analyze_channels(reference, ref_roles))
    summaries = summarize_roles(rows)
    averaged = averaged_conversions(capture.oversample, capture.drop_first)
    assessment = assess(rows, summaries, range_code=capture.range_code, sample_rate_hz=capture.rate_hz, margin=margin,
                        averaged=averaged, channels=len(capture.channels))
    ranges = range_options(capture.rate_hz, assessment.required_floor_rms_uv, averaged)
    return FloorCheck(capture=capture, roles=roles, rows=rows, summaries=summaries, assessment=assessment,
                      ranges=ranges, warnings=tuple(warnings), reference=reference)


def json_safe(value: Any) -> Any:
    """Non-finite floats become ``null`` (a missing judged window, an undetermined floor); JSON has no NaN."""

    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (np.floating, np.integer)):
        return json_safe(value.item())
    return value


def write_report(check: FloorCheck, path: str | Path) -> Path:
    target = check_output_path(path, suffixes=(".json",))
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(json_safe(check.report()), indent=2, allow_nan=False) + "\n"
    with target.open("x", encoding="utf-8") as handle:
        handle.write(payload)
    return target


# ----------------------------------------------------------------------
# Printing
# ----------------------------------------------------------------------
def channel_table_lines(rows: Sequence[ChannelFloor], sample_rate_hz: float = PRODUCTION_SCAN_HZ) -> list[str]:
    header = ["pos", "role", "mean V", "raw rms uV", "span LSB", "drift mV", "leak uV", "3Hz rms uV",
              "meter(nom) mV", f"{judged_label(sample_rate_hz)} med/worst uV", "usable", "note"]
    table = []
    for r in rows:
        judged = "n/a" if not math.isfinite(r.judged_pp_median_uv) else f"{r.judged_pp_median_uv:.0f}/{r.judged_pp_worst_uv:.0f}"
        table.append([r.position, r.role, f"{r.mean_v:.4f}", f"{r.raw_rms_uv:.1f}", f"{r.raw_span_lsb:.1f}",
                      f"{r.drift_mv:.2f}", f"{r.settling_leak_uv:.2f}", f"{r.band_rms_uv:.2f}",
                      f"{r.nominal_meter_mv:.1f}", judged, "yes" if r.usable else "NO", r.notes[0] if r.notes else ""])
    widths = [max(len(str(row[i])) for row in [header, *table]) for i in range(len(header))]
    lines = ["  ".join(str(h).ljust(w) for h, w in zip(header, widths))]
    lines.append("-" * len(lines[0]))
    lines.extend("  ".join(str(c).ljust(w) for c, w in zip(row, widths)) for row in table)
    return lines


def summary_lines(check: FloorCheck) -> list[str]:
    lines = [f"per role (usable channels only; 3 Hz/Q=3 band RMS at the pin, median [min .. max]; "
             f"'meter(nom)' = x{CHAIN_GAIN_NOMINAL:g} nominal, scale only):"]
    for role, s in check.summaries.items():
        lines.append(
            f"  {role:<13} n={s.count:<3} usable={s.usable:<3} {_fmt(s.band_rms_median_uv)} uV rms "
            f"[{_fmt(s.band_rms_min_uv)} .. {_fmt(s.band_rms_max_uv)}]   meter(nom) {_fmt(s.nominal_meter_median_mv, 1)} mV   "
            f"judged pk-pk median {_fmt(s.judged_pp_median_uv, 0)} uV"
        )
        for excluded in s.excluded:
            lines.append(f"      excluded {excluded}")
    return lines


def range_lines(check: FloorCheck) -> list[str]:
    a = check.assessment
    lines = [f"what a range change alone could do (ideal quantization in the 3 Hz band with {a.averaged_conversions} "
             f"averaged conversion{'s' if a.averaged_conversions != 1 else ''} per scan vs the required floor "
             f"{a.required_floor_rms_uv:.2f} uV rms; the measured floor is "
             f"{_fmt(a.floor_rms_uv / a.quantization_rms_uv, 1)} x the ideal on the current range):"]
    for option in check.ranges:
        current = " <- current" if option.code == check.capture.range_code else ""
        fits = "fits a DC-coupled 0.3-1.2 V offset" if option.fits_offset_band else "CLIPS a DC-coupled 0.3-1.2 V offset"
        lines.append(f"  code {option.code} {option.name:<9} {option.lsb_uv:6.1f} uV/code  quantization {option.quantization_rms_uv:.2f} uV rms"
                     f" = {option.quantization_over_required:.2f} x required  {fits}{current}")
    return lines


def print_check(check: FloorCheck) -> None:
    cap = check.capture
    span = daq.range_spec(cap.range_code)
    print(f"{TOOL_NAME} - {cap.source}")
    print(f"  {span.name} ({span.lsb_v * 1e6:.1f} uV/code), {cap.rate_hz:.6g} scans/s, oversample {cap.oversample} "
          f"(first {cap.drop_first} dropped), DAQ {cap.daq_serial}{' SIMULATED' if cap.simulated else ''}, "
          f"{cap.seconds:.1f} s, {len(cap.channels)} channels")
    counts = {role: sum(1 for r in check.roles.values() if r == role) for role in SOCKET_ROLES}
    print("  roles: " + "  ".join(f"{role} {n}" for role, n in counts.items() if n)
          + (f"  reference {len(check.reference.channels)}" if check.reference is not None else ""))
    for warning in check.warnings:
        print(f"  warning: {warning}")
    print()
    for line in channel_table_lines(check.rows, cap.rate_hz):
        print(line)
    print()
    for line in summary_lines(check):
        print(line)
    print()
    print("assessment (of the acquisition setup - never a part verdict):")
    for line in check.assessment.lines:
        print(f"  {line}")
    print()
    for line in range_lines(check):
        print(line)


# ----------------------------------------------------------------------
# Acquisition
# ----------------------------------------------------------------------
def capture_reference(rig: readout.ArrayRig, *, seconds: float, quiet_wait_s: float = 1.0) -> readout.Capture:
    """Stream the onboard reference through the same path, then put the normal input configuration back.

    Unipolar ranges use the full-scale reference (the ground reference sits
    on the bottom rail there and clips - bench finding 2026-09-02); bipolar
    ranges use the onboard zero, which sits mid-scale.
    """

    normal = rig.config
    cal_code = daq.CAL_BIPOLAR_ZERO if daq.range_spec(rig.range_code).low_v < 0 else daq.CAL_REF_UNIPOLAR
    rig.device.configure(replace(normal, cal_code=cal_code))
    try:
        return rig.capture(seconds, quiet_wait_s=quiet_wait_s, progress=False)
    finally:
        rig.device.configure(normal)


def acquire(rig: readout.ArrayRig, *, seconds: float, quiet_wait_s: float, reference_seconds: float | None,
            npz: str | Path | None = None) -> tuple[FloorCapture, FloorCapture | None]:
    """The main capture (all scanned channels) and, when asked, the onboard-reference capture."""

    target = None if npz is None else check_output_path(npz, suffixes=(".npz",))
    cap = rig.capture(seconds, quiet_wait_s=quiet_wait_s, progress=not rig.quiet)
    if target is not None:
        cap.to_npz(target)
    reference = None
    if reference_seconds:
        ref = capture_reference(rig, seconds=reference_seconds)
        reference = from_capture(ref, source="onboard reference")
    return from_capture(cap), reference


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{TOOL_NAME}.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def roles_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--loaded", nargs="+", action="extend", default=[], metavar="POS",
                       help="sockets holding a real 40623 (row-col / CHn / channel; commas or spaces)")
        p.add_argument("--grounded", nargs="+", action="extend", default=[], metavar="POS",
                       help="empty sockets shorted to ground with a wire")
        p.add_argument("--quiet-source", nargs="+", action="extend", default=[], metavar="POS",
                       help="sockets driven by a quiet DC source at a detector-like level")
        p.add_argument("--exclude", nargs="+", action="extend", default=[], metavar="POS",
                       help="sockets to leave out of every figure")
        p.add_argument("--margin", type=float, default=DEFAULT_MARGIN,
                       help=f"floor must be <= low limit / margin (default {DEFAULT_MARGIN:g})")
        p.add_argument("--report", metavar="FILE.json", help="write the full report to this NEW path (never Documents)")

    p = sub.add_parser("capture", help="stream every channel now and assess (one program on the DAQ at a time)")
    p.add_argument("-s", "--seconds", type=float, default=DEFAULT_SECONDS,
                   help=f"capture length (default {DEFAULT_SECONDS:g} s = the tester's)")
    p.add_argument("--quiet-wait", type=float, default=DEFAULT_QUIET_WAIT_S,
                   help=f"seconds of leading stream discarded first (default {DEFAULT_QUIET_WAIT_S:g})")
    p.add_argument("--daq-reference", action="store_true",
                   help="also stream the onboard reference through the same path (the DAQ alone)")
    p.add_argument("--reference-seconds", type=float, default=DEFAULT_REFERENCE_SECONDS,
                   help=f"length of the reference capture (default {DEFAULT_REFERENCE_SECONDS:g} s)")
    p.add_argument("--npz", metavar="FILE.npz", help="archive the raw capture at this NEW path (readout layout)")
    roles_options(p)
    readout._add_global_options(p, suppress=False)
    p = sub.add_parser("replay", help="assess a saved capture (tester tray_*_raw.npz, readout / viewer / probe .npz)")
    p.add_argument("capture", help="the .npz to read (never modified)")
    roles_options(p)
    return parser


def main(argv: list[str] | None = None, *, device: Any | None = None) -> int:
    """``device`` injects a DAQ double for the capture command (tests); otherwise the options decide."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "replay":
            capture = load_npz(args.capture)
            reference = None
        else:
            if args.report is not None:
                check_output_path(args.report, suffixes=(".json",))   # fail before an hour on the bench, not after
            if device is None:
                rig = readout.open_rig(args)
            else:
                rig = readout.ArrayRig(device=device, range_code=args.range, scan_hz=args.hz, oversample=args.oversample,
                                       drop_first=args.drop, start_channel=args.start, end_channel=args.end,
                                       self_calibrate=not args.no_selfcal, quiet=True)
                rig.connect()
            try:
                capture, reference = acquire(rig, seconds=args.seconds, quiet_wait_s=args.quiet_wait,
                                             reference_seconds=args.reference_seconds if args.daq_reference else None,
                                             npz=args.npz)
            finally:
                rig.close()
        roles = assign_roles(capture.channels, loaded=args.loaded, grounded=args.grounded,
                             quiet_source=args.quiet_source, excluded=args.exclude,
                             default_roles=capture.default_roles())
        check = run_check(capture, roles, margin=args.margin, reference=reference)
        print_check(check)
        if args.report is not None:
            path = write_report(check, args.report)
            print(f"\nreport written to {path}")
    except (OSError, ValueError, daq.DaqError) as exc:
        parser.exit(2, f"{TOOL_NAME} failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
