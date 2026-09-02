#!/usr/bin/env python3
"""Eltec array rig - USB-AIO16-64MA host-side readout (engineering only).

The array rig's counterpart of ``Arduino/Eltec/esp32_rig_readout.py``, built
on ``daq_backend.py``: the same bench habits (read an offset, capture a
channel or the whole tray, save a CSV, look at the noise, feed a live viewer)
on the ACCES DAQ that reads the 50-position array PCB. It never issues a
verdict - the tester (``eltec_40623_array_tester.py``) owns verdicts, this
file owns looking.

Positions and channels
----------------------
TP120 labels a position ``row-col`` (``1-3`` = row 1, part 3). The array PCB
feeds the DAQ's single-ended inputs in row order, so

    DAQ channel = (row - 1) * 10 + (col - 1)        CH0-CH49

Every command and API call that takes a position accepts ``row-col``,
``CHn`` or a plain channel number: ``2-4``, ``CH13`` and ``13`` are the same
input. Channels above CH49 (``--end`` past the array) are unwired and are
labelled ``CHn``.

Usage (from the repository root; ``--simulate`` needs no hardware and may be
given before or after the command):
    python array_rig/m40623/daq_rig_readout.py info                  # identity, DLL, configuration, wiring
    python array_rig/m40623/daq_rig_readout.py offset                # all fifty DC offsets as a 5 x 10 table
    python array_rig/m40623/daq_rig_readout.py offset 2-4 CH13 13    # one line per position (context, no verdict)
    python array_rig/m40623/daq_rig_readout.py stream -s 8 -o cap.csv --npz cap.npz   # capture -> table, CSV, npz
    python array_rig/m40623/daq_rig_readout.py stream -s 8 -p 2-4 3-6                 # only these positions
    python array_rig/m40623/daq_rig_readout.py noise -s 20 --quiet-wait 3             # emitter-off judged-band noise
    python array_rig/m40623/daq_rig_readout.py watch --interval 1                     # text-mode live readout, Ctrl+C
    python array_rig/m40623/daq_rig_readout.py watch -p 2-4 4-7 -s 30                 # two positions for 30 s
    python array_rig/m40623/daq_rig_readout.py test -s 20                             # identity -> offsets -> noise
    add --range/--hz/--oversample/--drop/--start/--end to run with other
    acquisition settings (defaults = the tester's production values), --reads
    to change the offset median depth, --no-selfcal to skip the
    ADC_SetCal(':AUTO:') at connect. Output files are always explicit
    arguments - nothing is ever written under Documents.

Honest differences from the ESP32 tool
--------------------------------------
* No emitter, PWM, gate or sync bit: there is no emitter board on the array
  rig yet. The tester's ``DriveDevice`` slot is where it will plug in; until
  then every capture here is an emitter-off capture, and there is no
  sensitivity analysis.
* No per-sample timestamps: the DAQ's bulk stream is a bare sequence of
  scans paced by the onboard 8254 clock. ``t_us`` in the CSV and ``t_s`` on
  a ``Capture`` are SYNTHESISED from the scan index and the frequency the
  pacing clock actually granted (``actual_timer_hz``). Stream integrity is
  judged by scan count against wall-clock time and the driver's pool flag
  (``StreamDiagnostics``), not by timestamp gaps.
* All scanned channels always stream together (the scan list is one
  contiguous range), so selecting a position is free: a single-position
  capture is one row picked out of the tray capture that ran anyway, and
  ``watch`` costs the same for one position or fifty.
* Volts are own-scale (raw counts x our range table), never ``ADC_GetScanV``.
* Stream integrity: a capture whose diagnostics report a problem (rate off by
  more than 1 %, driver pool exhausted, callback error) raises
  ``StreamIntegrityError`` - the ESP32 tool's overrun rule. The rate check
  compares delivered scans with elapsed wall time, so on the hardware keep
  captures a few seconds long (the driver's delivery latency is a larger
  fraction of a very short capture).

Python API (what the graphical viewer builds on):
    rig = ArrayRig(simulate=True)            # ArrayRig() for the hardware
    rig.connect()
    rig.read_offset_voltage("2-4")           # median of 24 immediate scans, volts
    rig.read_offsets()                       # {'1-1': 0.70, ...}, one read set for all
    cap = rig.capture(8.0)                   # Capture: volts [channels, N]
    cap.channel("2-4"); cap.band_limited_pp_mv(); cap.to_csv("x.csv"); cap.to_npz("x.npz")
    live = rig.live_stream(buffer_s=60.0); live.start(); live.wait_ready(5.0)
    t_s, volts = live.snapshot(seconds=4.0); live.latest(); live.stats(); live.stop()
    rig.close()
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

MODEL_DIR = Path(__file__).resolve().parent
if str(MODEL_DIR) not in sys.path:
    sys.path.insert(0, str(MODEL_DIR))

import array_analysis as aa  # noqa: E402
import daq_backend as daq  # noqa: E402

# The production acquisition constants, mirrored from
# eltec_40623_array_tester.py (DAQ_RANGE_CODE, DAQ_SCAN_HZ, DAQ_OVERSAMPLE,
# DAQ_DROP_CONVERSIONS_AFTER_MUX, STREAM_BUFFER_BYTES, STREAM_BUFFER_COUNT)
# exactly as the bench probe mirrors them: the tester is not imported here so
# this file stays free of Tk and of the tester's results paths, and
# tests/test_daq_rig_readout.py asserts the two sets are equal so they cannot
# drift apart silently. The why of each value is with the tester's copy.
DEFAULT_RANGE_CODE = 2          # 0-5 V on every 4-channel group, 76.3 uV per LSB
DEFAULT_SCAN_HZ = 1000.0        # keeps the noise pipeline's constants identical to the single rig
DEFAULT_OVERSAMPLE = 3          # 4 conversions per channel per scan ...
DEFAULT_DROP_FIRST = 1          # ... the first (right after the mux hop) dropped, three averaged
DEFAULT_BUFFER_BYTES = 64_000   # callback buffer: a multiple of 512 B and of the 400 B scan
DEFAULT_BUFFER_COUNT = 32       # ~5 s of driver slack before a pool event
# The ESP32 firmware's OFFSET? answers with the median of OFFSET_READ_SAMPLES
# = 24 single-channel reads ~3 ms apart (Eltec.ino), which is what the
# single-detector apps' read_offset_voltage() relies on. The DAQ has no such
# firmware helper - an "immediate" ADC_GetScan is one conversion per channel -
# so the same depth is applied host-side: the median of 24 immediate scans.
# Every scan reads all fifty channels, so read_offsets() costs the same as
# one position. The tester's live tiles poll shallower (OFFSET_POLL_READS =
# 3) because they refresh twice a second; a bench reading is not in a hurry.
DEFAULT_OFFSET_READS = 24
DEFAULT_CAPTURE_S = 8.0         # the ESP32 tool's default stream length
DEFAULT_NOISE_S = 20.0          # the 405 M22 noise test's length (TP120's own hold is 60 s)
DEFAULT_WATCH_INTERVAL_S = 1.0
DEFAULT_LIVE_BUFFER_S = 60.0    # live history ceiling (the viewer's widest window)
DEFAULT_CONNECT_TIMEOUT_S = 10.0
# Capture progress is reported once per second of data with a carriage
# return, like the ESP32 tool, so a long capture shows life without scrolling.
PROGRESS_EVERY_S = 1.0
# Wall-clock grace on top of the requested capture length before the stream
# is declared stalled (the bench probe's rule).
CAPTURE_EXTRA_TIMEOUT_S = 5.0
STREAM_START_TIMEOUT_S = 5.0
# Keys of the .npz a Capture writes: the bench probe's save_capture() set
# without its probe_command, plus drop_first (which the probe passes as an
# extra). engineer_tools/replot_noise_capture.py reads waveform_v [K, N],
# sample_rate_hz and positions and ignores the rest, so a readout capture
# replays there exactly like a probe capture (pass the file path; without
# an occupancy array every channel counts as LOADED).
NPZ_KEYS: tuple[str, ...] = (
    "waveform_v", "sample_rate_hz", "channels", "positions", "recorded_at", "daq_serial", "daq_model",
    "range_code", "range_span_v", "oversample", "actual_timer_hz", "simulated", "source", "drop_first",
)
NPZ_SOURCE = "daq_rig_readout"
# Printed by every noise-centred command: the pin-level limits are None
# until the paired lot derives the chain factor (docs/CALIBRATION_RECORD.md
# section 4b), so nothing here can be a verdict.
CALIBRATION_STATUS_LINE = (
    "no pin-level noise limit derived yet - every noise verdict is NO_LIMIT "
    "(docs/CALIBRATION_RECORD.md section 4b)"
)
OFFSET_CONTEXT_LINE = (
    "context only, never a verdict: the 0.3-1.2 V offset band is TP120's and PROVISIONAL on this rig "
    "until the PCB's loading is confirmed against fixture 9000054 (docs/CALIBRATION_RECORD.md section 4b)"
)
JUDGED_BAND_TEXT = "0.85-22 Hz"   # emergent band of the 1 s detrend + 20:1 anti-alias FIR at 1000 SPS


# ----------------------------------------------------------------------
# Position / channel tokens
# ----------------------------------------------------------------------
def _bad_token(token: Any) -> str:
    return (
        f"{token!r} is not a position: use 'row-col' (rows 1-{daq.ROWS}, columns 1-{daq.COLS}), "
        f"'CHn' or a channel number 0-{daq.MAX_SCAN_CHANNEL}."
    )


def parse_channel_token(token: Any) -> int:
    """``'row-col'``, ``'CHn'``, ``'n'`` or an int -> DAQ channel number (0-63, not range-checked against the scan)."""

    if isinstance(token, bool):
        raise ValueError(_bad_token(token))
    if isinstance(token, (int, np.integer)):
        channel = int(token)
    else:
        text = str(token).strip()
        upper = text.upper()
        if "-" in text and not text.startswith("-"):
            try:
                channel = daq.channel_for_position(text)
            except ValueError as exc:
                raise ValueError(f"{exc} {_bad_token(token)}") from None
        elif upper.startswith("CH") and upper[2:].strip().isdigit():
            channel = int(upper[2:].strip())
        elif text.isdigit():
            channel = int(text)
        else:
            raise ValueError(_bad_token(token))
    if not 0 <= channel <= daq.MAX_SCAN_CHANNEL:
        raise ValueError(f"Channel {channel} is outside 0-{daq.MAX_SCAN_CHANNEL}. {_bad_token(token)}")
    return channel


def channel_label(channel: int) -> str:
    """``row-col`` for a wired channel, ``CHn`` for one above the array."""

    if 0 <= channel < daq.CHANNEL_COUNT:
        return daq.position_for_channel(channel)
    return f"CH{channel}"


def offset_context(volts: float) -> str:
    """Informational context for a DC offset - the words the CLI prints, never a verdict."""

    if volts >= aa.OFFSET_RAIL_V:
        return f">= {aa.OFFSET_RAIL_V:.1f} V: railed"
    if volts < aa.OFFSET_DEAD_V:
        return "~0 V: empty socket or dead part"
    if volts < aa.OFFSET_MIN_V:
        return f"under {aa.OFFSET_MIN_V:.1f} V"
    if volts > aa.OFFSET_MAX_V:
        return f"over {aa.OFFSET_MAX_V:.1f} V"
    return f"in the PROVISIONAL {aa.OFFSET_MIN_V:.1f}-{aa.OFFSET_MAX_V:.1f} V band (TP120)"


def offset_flag(volts: float) -> str:
    """One-character version of ``offset_context`` for the 5 x 10 tables."""

    if volts >= aa.OFFSET_RAIL_V:
        return "R"
    if volts < aa.OFFSET_DEAD_V:
        return "o"
    if volts < aa.OFFSET_MIN_V:
        return "<"
    if volts > aa.OFFSET_MAX_V:
        return ">"
    return " "


OFFSET_FLAG_LEGEND = (
    f"  flags: ' ' in the PROVISIONAL {aa.OFFSET_MIN_V:.1f}-{aa.OFFSET_MAX_V:.1f} V band (TP120)   "
    f"'<' under {aa.OFFSET_MIN_V:.1f} V   '>' over {aa.OFFSET_MAX_V:.1f} V   "
    f"'o' ~0 V: empty socket or dead part   'R' >= {aa.OFFSET_RAIL_V:.1f} V: railed   '-' not scanned"
)


# ----------------------------------------------------------------------
# A finished capture
# ----------------------------------------------------------------------
@dataclass
class Capture:
    """One streamed capture: ``volts`` is ``[channels, samples]`` own-scale volts (float64)."""

    volts: np.ndarray
    scan_hz: float                    # nominal (what the pacing clock was asked for)
    actual_timer_hz: float            # what the pacing clock granted; the time axis uses this
    channels: tuple[int, ...]         # DAQ channel per row
    positions: tuple[str, ...]        # 'row-col' per row, '' for an unwired channel
    range_code: int
    oversample: int
    drop_first: int
    diagnostics: daq.StreamDiagnostics | None
    started_at: str                   # ISO seconds, host clock
    daq_serial: str
    daq_model: str
    simulated: bool
    quiet: bool = False               # suppresses the "Saved ..." lines

    def __post_init__(self) -> None:
        self.volts = np.asarray(self.volts, dtype=np.float64)
        if self.volts.ndim != 2:
            raise ValueError("volts must be a [channels, samples] array.")
        self.channels = tuple(int(c) for c in self.channels)
        self.positions = tuple(str(p) for p in self.positions)
        if len(self.channels) != self.volts.shape[0] or len(self.positions) != self.volts.shape[0]:
            raise ValueError("channels and positions must have one entry per row of volts.")

    # -- shape and time ----------------------------------------------------
    @property
    def samples(self) -> int:
        return int(self.volts.shape[1])

    @property
    def rate_hz(self) -> float:
        """The rate the time axis is built from (granted timer, nominal as a fallback)."""

        return float(self.actual_timer_hz) if self.actual_timer_hz > 0 else float(self.scan_hz)

    @property
    def t_s(self) -> np.ndarray:
        return np.arange(self.samples, dtype=np.float64) / self.rate_hz

    @property
    def t_us(self) -> np.ndarray:
        return np.rint(self.t_s * 1e6).astype(np.int64)

    @property
    def labels(self) -> tuple[str, ...]:
        """Position label, or ``CHn`` for an unwired channel - the CSV column names."""

        return tuple(p or f"CH{c}" for p, c in zip(self.positions, self.channels))

    # -- selecting a channel -----------------------------------------------
    def index_of(self, position_or_channel: Any) -> int:
        channel = parse_channel_token(position_or_channel)
        try:
            return self.channels.index(channel)
        except ValueError:
            raise ValueError(
                f"{channel_label(channel)} (CH{channel}) is not in this capture "
                f"(it holds {', '.join(self.labels)})."
            ) from None

    def channel(self, position_or_channel: Any) -> np.ndarray:
        return self.volts[self.index_of(position_or_channel)]

    def subset(self, positions: Iterable[Any]) -> "Capture":
        """The same capture reduced to ``positions``, in the order given."""

        indices = [self.index_of(p) for p in positions]
        return replace(
            self,
            volts=np.ascontiguousarray(self.volts[indices]),
            channels=tuple(self.channels[i] for i in indices),
            positions=tuple(self.positions[i] for i in indices),
        )

    # -- numbers -----------------------------------------------------------
    def means(self) -> np.ndarray:
        return self.volts.mean(axis=1)

    def peak_to_peak_mv(self) -> np.ndarray:
        """Raw wideband pk-pk per channel in mV (no band limiting - context, not the judged figure)."""

        return np.ptp(self.volts, axis=1) * 1000.0

    def band_limited_pp_mv(
        self, *, window_s: float = aa.NOISE_WINDOW_S, decimation_factor: int = aa.NOISE_DECIMATION_FACTOR
    ) -> np.ndarray:
        """``[channels, windows]`` pk-pk in mV in the judged band (the tester's exact pipeline, nominal rate)."""

        return aa.band_limited_window_pp_mv(
            self.volts, self.scan_hz, decimation_factor=decimation_factor, window_s=window_s
        )

    # -- files -------------------------------------------------------------
    def to_csv(self, path: str | Path) -> Path:
        """``t_us`` then one column per channel named by its position label (``CHn`` when unwired), volts to 6 decimals."""

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        table = np.column_stack((self.t_us.astype(np.float64), self.volts.T))
        with path.open("w", newline="", encoding="utf-8") as handle:
            np.savetxt(
                handle, table, fmt=["%d"] + ["%.6f"] * len(self.channels), delimiter=",",
                header=",".join(["t_us", *self.labels]), comments="",
            )
        if not self.quiet:
            print(f"Saved {self.samples} samples x {len(self.channels)} channels to {path}")
        return path

    def to_npz(self, path: str | Path) -> Path:
        """``np.savez_compressed`` with exactly ``NPZ_KEYS`` (the bench probe's layout; float32 waveform)."""

        path = Path(path)
        if path.suffix != ".npz":  # numpy's own rule is case-sensitive: 'cap.NPZ' becomes 'cap.NPZ.npz'
            path = path.with_name(path.name + ".npz")
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path,
            waveform_v=self.volts.astype(np.float32),
            sample_rate_hz=float(self.scan_hz),
            channels=np.asarray(self.channels, dtype=np.int64),
            positions=np.array(list(self.positions)),
            recorded_at=np.array(self.started_at),
            daq_serial=np.array(self.daq_serial),
            daq_model=np.array(self.daq_model),
            range_code=np.array(str(self.range_code)),
            range_span_v=np.array(str(daq.range_spec(self.range_code).span_v)),
            oversample=np.array(str(self.oversample)),
            actual_timer_hz=np.array(f"{self.actual_timer_hz:.6f}"),
            simulated=np.array(str(bool(self.simulated))),
            source=np.array(NPZ_SOURCE),
            drop_first=np.array(str(self.drop_first)),
        )
        if not self.quiet:
            print(f"Saved {self.samples} samples x {len(self.channels)} channels to {path}")
        return path


# ----------------------------------------------------------------------
# The rig
# ----------------------------------------------------------------------
class ArrayRig:
    """Bench wrapper over a ``DaqDevice`` mirroring the ESP32 tool's ``Esp32Rig`` surface.

    ``device=None`` opens the hardware (``AiousbDaq``) or, with
    ``simulate=True``, a wall-clock-paced ``SimulatedDaq``; an injected device
    is used as-is (the tests inject ``SimulatedDaq(real_time=False)``). Not
    thread-safe: while a ``LiveStream`` runs it owns every device call.
    """

    def __init__(
        self,
        *,
        simulate: bool = False,
        device: Any | None = None,
        range_code: int = DEFAULT_RANGE_CODE,
        scan_hz: float = DEFAULT_SCAN_HZ,
        oversample: int = DEFAULT_OVERSAMPLE,
        drop_first: int = DEFAULT_DROP_FIRST,
        start_channel: int = 0,
        end_channel: int = daq.CHANNEL_COUNT - 1,
        self_calibrate: bool = True,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        buffer_bytes: int = DEFAULT_BUFFER_BYTES,
        buffer_count: int = DEFAULT_BUFFER_COUNT,
        sim_profile: daq.SimProfile | None = None,
        quiet: bool = False,
    ) -> None:
        if device is None:
            device = daq.SimulatedDaq(sim_profile, real_time=True) if simulate else daq.AiousbDaq()
        if scan_hz <= 0:
            raise ValueError("scan_hz must be positive.")
        if not 0 <= drop_first <= oversample:
            raise ValueError(
                f"drop_first {drop_first} must leave at least one of the {oversample + 1} conversions per channel."
            )
        self.device = device
        self.simulate = bool(simulate)
        self.quiet = bool(quiet)
        self._config = daq.AdcConfig(
            range_code=int(range_code),
            start_channel=int(start_channel),
            end_channel=int(end_channel),
            oversample=int(oversample),
            trigger=daq.TRIGGER_TIMER | daq.TRIGGER_SCAN,
            cal_code=daq.CAL_NORMAL,
        )
        self._scan_hz = float(scan_hz)
        self._drop_first = int(drop_first)
        self._buffer_bytes = int(buffer_bytes)
        self._buffer_count = int(buffer_count)
        self._self_calibrate = bool(self_calibrate)
        self._connect_timeout_s = float(connect_timeout_s)
        self._info: daq.DaqInfo | None = None
        self._connected = False
        self._calibrated = False
        self._live: LiveStream | None = None

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> daq.DaqInfo:
        """Connect, write the configuration block, self-calibrate (when enabled and supported), re-apply the block."""

        info = self.device.connect(timeout_s=self._connect_timeout_s)
        self.device.configure(self._config)
        self._calibrated = False
        if self._self_calibrate and info.calibration_supported is not False:
            self.device.self_calibrate()
            self.device.configure(self._config)  # calibration can leave the block; make sure ours is live
            self._calibrated = True
        self._info = info
        self._connected = True
        if not self.quiet:
            print(f"Connected: {info.summary()}")
        return info

    def close(self) -> None:
        """Stop a live stream or a raw device stream that is still running, then close; safe to call twice."""

        live = self._live
        self._live = None
        if live is not None and live.ident is not None and live.is_alive():
            live.stop()
        if self._connected:
            if self.device.is_streaming:
                try:
                    self.device.stop_stream()
                except daq.DaqError:
                    pass
            self.device.close()
        self._connected = False
        self._info = None

    def __enter__(self) -> "ArrayRig":
        if not self._connected:
            self.connect()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- state -------------------------------------------------------------
    @property
    def info(self) -> daq.DaqInfo | None:
        return self._info

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def self_calibrated(self) -> bool:
        return self._calibrated

    @property
    def config(self) -> daq.AdcConfig:
        return self._config

    @property
    def scan_hz(self) -> float:
        return self._scan_hz

    @property
    def range_code(self) -> int:
        return self._config.range_code

    @property
    def range(self) -> daq.RangeSpec:
        return daq.range_spec(self._config.range_code)

    @property
    def oversample(self) -> int:
        return self._config.oversample

    @property
    def drop_first(self) -> int:
        return self._drop_first

    @property
    def buffer_bytes(self) -> int:
        return self._buffer_bytes

    @property
    def buffer_count(self) -> int:
        return self._buffer_count

    @property
    def channels(self) -> tuple[int, ...]:
        return tuple(range(self._config.start_channel, self._config.end_channel + 1))

    @property
    def positions(self) -> tuple[str, ...]:
        """Label per scanned channel, ``''`` for a channel above the array."""

        return tuple(daq.POSITIONS[c] if c < daq.CHANNEL_COUNT else "" for c in self.channels)

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(p or f"CH{c}" for p, c in zip(self.positions, self.channels))

    @property
    def is_streaming(self) -> bool:
        return bool(self.device.is_streaming)

    @property
    def live(self) -> "LiveStream | None":
        return self._live

    def resolve_channel(self, position_or_channel: Any) -> int:
        """``'row-col'`` / ``'CHn'`` / ``'n'`` / int -> DAQ channel, which must be inside the scanned range."""

        channel = parse_channel_token(position_or_channel)
        config = self._config
        if not config.start_channel <= channel <= config.end_channel:
            raise ValueError(
                f"{channel_label(channel)} is CH{channel}, outside the scanned range "
                f"CH{config.start_channel}-CH{config.end_channel} (change --start/--end)."
            )
        return channel

    def label_of(self, position_or_channel: Any) -> str:
        return channel_label(self.resolve_channel(position_or_channel))

    def index_of(self, position_or_channel: Any) -> int:
        """Row of the scanned range (0 = start_channel) for a position."""

        return self.resolve_channel(position_or_channel) - self._config.start_channel

    def _require_connected(self) -> None:
        if not self._connected:
            raise daq.StreamStateError("Not connected - call connect() first.")

    # -- immediate reads ---------------------------------------------------
    def read_scan_volts(self, *, reads: int = 1) -> np.ndarray:
        """Median of ``reads`` immediate scans, own-scale volts per scanned channel (never ADC_GetScanV)."""

        self._require_connected()
        if reads < 1:
            raise ValueError("reads must be >= 1.")
        counts = self.device.read_scan_counts(reads=reads)
        return daq.counts_to_volts(np.median(counts, axis=0), self.range_code)

    def read_offset_voltage(self, position_or_channel: Any, *, reads: int = DEFAULT_OFFSET_READS) -> float:
        """DC offset of one position: median of ``reads`` immediate scans (the ESP32 OFFSET? depth by default)."""

        index = self.index_of(position_or_channel)
        return float(self.read_scan_volts(reads=reads)[index])

    def read_offsets(
        self, positions: Iterable[Any] | None = None, *, reads: int = DEFAULT_OFFSET_READS
    ) -> dict[str, float]:
        """``{label: volts}`` for every wired position in the scan (or the given ones) from ONE set of scans.

        Keys are position labels however the position was spelled (``CH13``
        -> ``'2-4'``); an unwired channel is keyed ``CHn``.
        """

        wanted = None if positions is None else [(self.label_of(p), self.index_of(p)) for p in positions]
        volts = self.read_scan_volts(reads=reads)
        if wanted is None:
            return {label: float(volts[i]) for i, label in enumerate(self.positions) if label}
        return {label: float(volts[i]) for label, i in wanted}

    # -- streamed capture --------------------------------------------------
    def capture(
        self,
        seconds: float,
        positions: Iterable[Any] | None = None,
        *,
        progress: bool = True,
        quiet_wait_s: float = 0.0,
    ) -> Capture:
        """Stream ``seconds`` of every scanned channel and return a ``Capture``.

        ``quiet_wait_s`` streams and discards that much leading data first
        (a fixed version of the tester's quiet wait: let the tray settle on
        the same stream instead of restarting it). Raises
        ``StreamIntegrityError`` when the diagnostics report a problem and
        ``StreamTimeoutError`` when the stream stalls. ``positions`` reduces
        the result to those rows, in the order given.
        """

        self._require_connected()
        if seconds <= 0:
            raise ValueError("seconds must be positive.")
        if quiet_wait_s < 0:
            raise ValueError("quiet_wait_s must be >= 0.")
        if self._live is not None and self._live.running:
            raise daq.StreamStateError("Stop the live stream before a capture.")
        wanted = int(round(seconds * self._scan_hz))
        if wanted < 1:
            raise ValueError("seconds is shorter than one scan period.")
        discard = int(round(quiet_wait_s * self._scan_hz))
        selected = None if positions is None else list(positions)
        if selected is not None:
            for token in selected:
                self.resolve_channel(token)  # fail before the stream starts, not after
        show = progress and not self.quiet
        target = wanted + discard
        info = self._info
        started_at = _dt.datetime.now().isoformat(timespec="seconds")
        header = self.device.start_stream(
            scan_hz=self._scan_hz, buffer_bytes=self._buffer_bytes, buffer_count=self._buffer_count,
            drop_first=self._drop_first, average=True,
        )
        chunks: list[np.ndarray] = []
        received = 0
        grace = seconds + quiet_wait_s + CAPTURE_EXTRA_TIMEOUT_S
        deadline = time.monotonic() + grace
        next_report = PROGRESS_EVERY_S * self._scan_hz
        try:
            while received < target:
                chunk = self.device.read_stream(timeout_s=1.0)
                if chunk is None:
                    if time.monotonic() >= deadline:
                        raise daq.StreamTimeoutError(
                            f"only {received} of {target} scans arrived within {grace:.0f} s - the stream stalled."
                        )
                    continue
                chunks.append(chunk)
                received += int(chunk.shape[0])
                if show and received >= next_report:
                    next_report += PROGRESS_EVERY_S * self._scan_hz
                    print(f"  {min(received, target)}/{target} scans...", end="\r", flush=True)
        except BaseException:
            # The read failed (or Ctrl+C): stop the stream, but let the
            # original error through rather than a second one from the stop.
            try:
                self.device.stop_stream()
            except daq.DaqError:
                pass
            raise
        diagnostics = self.device.stop_stream()
        drain = getattr(self.device, "drain_stream", None)
        if drain is not None:
            for chunk in drain():
                chunks.append(chunk)
                received += int(chunk.shape[0])
        counts = np.concatenate(chunks) if chunks else np.empty((0, self._config.channels))
        counts = counts[discard : discard + wanted]
        if show:
            print(f"  captured {counts.shape[0]} scans x {self._config.channels} channels            ")
        diagnostics.check()
        if counts.shape[0] < wanted:
            raise daq.StreamIntegrityError(f"capture holds {counts.shape[0]} of {wanted} scans.")
        volts = np.ascontiguousarray(daq.counts_to_volts(counts, self.range_code).T)
        result = Capture(
            volts=volts,
            scan_hz=self._scan_hz,
            actual_timer_hz=float(header.actual_timer_hz),
            channels=self.channels,
            positions=self.positions,
            range_code=self.range_code,
            oversample=self.oversample,
            drop_first=self._drop_first,
            diagnostics=diagnostics,
            started_at=started_at,
            daq_serial=info.serial_number if info else "unknown",
            daq_model=info.name if info else "unknown",
            simulated=bool(info.simulated) if info else False,
            quiet=self.quiet,
        )
        return result if selected is None else result.subset(selected)

    # -- configuration -----------------------------------------------------
    def set_range(self, range_code: int) -> daq.RangeSpec:
        """Re-apply the configuration block with another input range (everything else unchanged)."""

        spec = daq.range_spec(int(range_code))
        if self.device.is_streaming or (self._live is not None and self._live.running):
            raise daq.StreamStateError("Stop the stream before changing the range.")
        config = replace(self._config, range_code=int(range_code))
        if self._connected:
            self.device.configure(config)
        self._config = config
        return spec

    def live_stream(self, *, buffer_s: float = DEFAULT_LIVE_BUFFER_S, read_timeout_s: float = 0.2) -> "LiveStream":
        """Construct (do not start) a ``LiveStream`` on this rig."""

        self._require_connected()
        if self._live is not None and self._live.running:
            raise daq.StreamStateError("A live stream is already running on this rig.")
        self._live = LiveStream(self, buffer_s=buffer_s, read_timeout_s=read_timeout_s)
        return self._live


# ----------------------------------------------------------------------
# Live stream (the viewer's data source)
# ----------------------------------------------------------------------
@dataclass
class LiveStats:
    total_scans: int
    elapsed_s: float
    rate_hz: float
    lag_s: float
    chunks: int
    error: str | None
    diagnostics: daq.StreamDiagnostics | None
    running: bool


class LiveStream(threading.Thread):
    """Reads the bulk stream into a rolling ring buffer - the mirror of ``live_waveform.StreamReader``.

    ALL device I/O happens on this thread: ``start_stream`` in ``run()``,
    every ``read_stream``, and the ``stop_stream`` + drain in ``run()``'s
    tail. A GUI thread only ever calls ``snapshot`` / ``latest`` / ``stats``
    / ``stop``, so it never touches the DLL. ``lag_s`` is how far behind
    real time the host is (wall-clock elapsed minus the data received, the
    DAQ analogue of the ESP32 receive backlog); with a ``real_time=False``
    simulated device the wall clock means nothing, so a viewer should check
    ``rig.info.simulated`` before reading anything into it.
    """

    def __init__(self, rig: ArrayRig, *, buffer_s: float = DEFAULT_LIVE_BUFFER_S, read_timeout_s: float = 0.2) -> None:
        super().__init__(name="daq-live-stream", daemon=True)
        if buffer_s <= 0:
            raise ValueError("buffer_s must be positive.")
        self.rig = rig
        self.scan_hz = float(rig.scan_hz)
        self.actual_timer_hz = float(rig.scan_hz)   # replaced by the granted value once the stream starts
        self.range_code = int(rig.range_code)
        self.channel_count = int(rig.config.channels)
        self.buffer_s = float(buffer_s)
        self.read_timeout_s = float(read_timeout_s)
        # Sized for the WIDEST window the viewer may be switched to (x1.25
        # slack), like the ESP32 viewer's deque, so widening mid-stream has
        # real history to show.
        self.maxlen = max(2, int(self.buffer_s * self.scan_hz * 1.25))
        self._ring = np.zeros((self.channel_count, self.maxlen), dtype=np.float64)
        self._write = 0
        self._fill = 0
        self.lock = threading.Lock()
        self.total_scans = 0
        self.chunks = 0
        self.lag_s = 0.0
        self.error: str | None = None
        self.diagnostics: daq.StreamDiagnostics | None = None
        self.header: daq.StreamHeader | None = None
        self.ready = threading.Event()      # the device stream started
        self.finished = threading.Event()   # run() has returned (started or not)
        self._stop = threading.Event()
        self._started_wall: float | None = None
        self._stopped_wall: float | None = None
        # Arrival clock of the first and the newest chunk (see stats()).
        self._first_push_wall: float | None = None
        self._first_push_scans = 0
        self._last_push_wall: float | None = None

    # -- thread body -------------------------------------------------------
    def run(self) -> None:
        device = self.rig.device
        # The rig may have been re-ranged between live_stream() and start():
        # the conversion scale is taken from the rig now, not at construction.
        self.range_code = int(self.rig.range_code)
        try:
            self.header = device.start_stream(
                scan_hz=self.scan_hz, buffer_bytes=self.rig.buffer_bytes, buffer_count=self.rig.buffer_count,
                drop_first=self.rig.drop_first, average=True,
            )
        except Exception as exc:  # recorded, never raised on the thread
            self.error = f"start_stream failed: {exc}"
            self.finished.set()
            return
        if self.header.actual_timer_hz > 0:
            self.actual_timer_hz = float(self.header.actual_timer_hz)
        self._started_wall = time.monotonic()
        self.ready.set()
        try:
            while not self._stop.is_set():
                try:
                    chunk = device.read_stream(timeout_s=self.read_timeout_s)
                except Exception as exc:
                    self.error = f"read_stream failed: {exc}"
                    break
                if chunk is None:
                    self._update_lag()
                    continue
                try:
                    self._push(chunk)
                except Exception as exc:  # a chunk of the wrong shape: record it, never die silently
                    self.error = f"stream chunk rejected: {exc}"
                    break
        finally:
            try:
                self.diagnostics = device.stop_stream()
                drain = getattr(device, "drain_stream", None)
                if drain is not None:
                    for chunk in drain():
                        self._push(chunk)
            except Exception as exc:
                if self.error is None:
                    self.error = f"stop_stream failed: {exc}"
            self._stopped_wall = time.monotonic()
            self.finished.set()

    def _update_lag(self) -> None:
        if self._started_wall is None:
            return
        elapsed = time.monotonic() - self._started_wall
        self.lag_s = max(0.0, elapsed - self.total_scans / self.actual_timer_hz)

    def _push(self, counts: np.ndarray) -> None:
        volts = daq.counts_to_volts(np.asarray(counts, dtype=np.float64), self.range_code).T  # [channels, n]
        n = int(volts.shape[1])
        if n == 0:
            return
        maxlen = self.maxlen
        with self.lock:
            if n >= maxlen:
                self._ring[:, :] = volts[:, -maxlen:]
                self._write = 0
                self._fill = maxlen
            else:
                end = self._write + n
                if end <= maxlen:
                    self._ring[:, self._write:end] = volts
                else:
                    first = maxlen - self._write
                    self._ring[:, self._write:] = volts[:, :first]
                    self._ring[:, : end - maxlen] = volts[:, first:]
                self._write = end % maxlen
                self._fill = min(maxlen, self._fill + n)
            self.total_scans += n
            self.chunks += 1
            now = time.monotonic()
            if self._first_push_wall is None:
                self._first_push_wall = now
                self._first_push_scans = n
            self._last_push_wall = now
        self._update_lag()

    # -- control -----------------------------------------------------------
    def wait_ready(self, timeout_s: float = STREAM_START_TIMEOUT_S) -> bool:
        """Block until the stream started (True) or the start failed / timed out (False; see ``error``)."""

        deadline = time.monotonic() + timeout_s
        while True:
            if self.ready.is_set():
                return True
            if self.finished.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.ready.wait(min(0.05, remaining))

    @property
    def running(self) -> bool:
        return self.ready.is_set() and not self.finished.is_set()

    def stop(self, timeout_s: float = 5.0) -> None:
        """Ask the thread to stop the device stream and wait for it (bounded)."""

        self._stop.set()
        if self.ident is not None and self.is_alive():
            self.join(timeout_s)

    # -- data --------------------------------------------------------------
    def _relative_indices(self, channels: Sequence[Any] | None) -> list[int] | None:
        if channels is None:
            return None
        indices = []
        for item in channels:
            if isinstance(item, str):
                indices.append(self.rig.index_of(item))
            else:
                index = int(item)
                if not 0 <= index < self.channel_count:
                    raise IndexError(f"channel index {index} is outside 0-{self.channel_count - 1} of the scanned range.")
                indices.append(index)
        return indices

    def snapshot(self, channels: Sequence[Any] | None = None, *, seconds: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        """``(t_s, volts)`` copies, oldest first; ``t_s`` <= 0 relative to the newest sample.

        ``channels`` are indices relative to the scanned range (0 =
        start_channel) or position tokens; None = all. ``seconds`` limits the
        history returned (None = the whole buffer fill).
        """

        indices = self._relative_indices(channels)
        # The wanted rows are picked INSIDE the ring slice: copying all fifty
        # channels of a 60 s window (24 MB) twenty times a second to keep one
        # of them is what the viewer would otherwise do on every frame.
        rows: Any = slice(None) if indices is None else indices
        row_count = self.channel_count if indices is None else len(indices)
        with self.lock:
            n = self._fill if seconds is None else min(self._fill, int(round(seconds * self.actual_timer_hz)))
            if n <= 0:
                block = np.empty((row_count, 0), dtype=np.float64)
            else:
                start = (self._write - n) % self.maxlen
                if start + n <= self.maxlen:
                    block = np.array(self._ring[rows, start : start + n], dtype=np.float64, copy=True)
                else:
                    block = np.concatenate((self._ring[rows, start:], self._ring[rows, : start + n - self.maxlen]), axis=1)
            hz = self.actual_timer_hz
        t_s = (np.arange(block.shape[1], dtype=np.float64) - (block.shape[1] - 1)) / hz
        return t_s, block

    def latest(self, channels: Sequence[Any] | None = None, *, seconds: float = 1.0) -> np.ndarray:
        """Mean over the last ``seconds`` per channel (NaN before any data) - the grid panel's numbers."""

        _t, block = self.snapshot(channels, seconds=seconds)
        if block.shape[1] == 0:
            return np.full(block.shape[0], np.nan)
        return block.mean(axis=1)

    def stats(self) -> LiveStats:
        with self.lock:
            total = self.total_scans
            chunks = self.chunks
            lag = self.lag_s
            first_wall, first_scans, last_wall = self._first_push_wall, self._first_push_scans, self._last_push_wall
        if self._started_wall is None:
            elapsed = 0.0
        else:
            elapsed = (self._stopped_wall or time.monotonic()) - self._started_wall
        # The rate is the DELIVERY rate between the first and the newest
        # chunk - the DAQ analogue of the ESP32 viewer's rate over the
        # timestamps of the samples it holds. Dividing the total by the time
        # since start would under-read by the driver's buffer latency (a
        # 64 000-byte buffer is 0.16 s of data that is always still in
        # flight: ~4 % low after 3 s, which looks like a fault and is not);
        # the stream's integrity verdict stays with StreamDiagnostics.
        if chunks >= 2 and first_wall is not None and last_wall is not None and last_wall > first_wall:
            rate = (total - first_scans) / (last_wall - first_wall)
        else:
            rate = total / elapsed if elapsed > 0 else 0.0
        return LiveStats(
            total_scans=total, elapsed_s=elapsed, rate_hz=rate, lag_s=lag, chunks=chunks,
            error=self.error, diagnostics=self.diagnostics, running=self.running,
        )


# ----------------------------------------------------------------------
# Text helpers shared by the commands
# ----------------------------------------------------------------------
def describe_config(config: daq.AdcConfig, scan_hz: float, *, drop_first: int | None = None,
                    buffer_bytes: int | None = None, buffer_count: int | None = None) -> str:
    spec = daq.range_spec(config.range_code)
    dropped = f", first {drop_first} dropped" if drop_first is not None else ""
    text = (
        f"range code {config.range_code} ({spec.name}, LSB {spec.lsb_v * 1e6:.1f} uV), channels "
        f"CH{config.start_channel}-CH{config.end_channel} ({config.channels}), oversample {config.oversample} "
        f"({config.conversions_per_channel} conversions/channel{dropped}), trigger {config.trigger:#04x}, "
        f"cal {config.cal_code:#04x}; at {scan_hz:g} scans/s: {config.conversions_per_second * scan_hz / 1000:.0f} kS/s "
        f"aggregate, {config.scan_bytes * scan_hz / 1000:.0f} KB/s over USB"
    )
    if buffer_bytes and buffer_count:
        text += f"; stream buffers {buffer_count} x {buffer_bytes} B"
    return text


def print_table(rows: list[list[str]], header: list[str]) -> None:
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    line = "  ".join(str(h).rjust(w) for h, w in zip(header, widths))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(str(c).rjust(w) for c, w in zip(row, widths)))


def grid_lines(cells: dict[int, str], *, width: int = 8, missing: str = "-") -> list[str]:
    """The 5 x 10 array as text: one cell per DAQ channel (row-major), ``missing`` where absent."""

    lines = ["      " + "".join(f"col {col}".rjust(width) for col in range(1, daq.COLS + 1))]
    for row in range(1, daq.ROWS + 1):
        line = f"row {row} "
        for col in range(1, daq.COLS + 1):
            channel = (row - 1) * daq.COLS + (col - 1)
            line += cells.get(channel, missing).rjust(width)
        lines.append(line)
    return lines


def offset_grid_lines(rig: ArrayRig, volts: np.ndarray) -> list[str]:
    cells = {}
    for index, channel in enumerate(rig.channels):
        if channel < daq.CHANNEL_COUNT:
            cells[channel] = f"{volts[index]:.3f}{offset_flag(float(volts[index]))}"
    return grid_lines(cells)


def offset_tally(volts: Iterable[float]) -> str:
    counts: dict[str, int] = {}
    for value in volts:
        key = offset_context(float(value))
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{n} {key}" for key, n in counts.items())


def band_limited_summary(cap: Capture) -> tuple[np.ndarray | None, str]:
    """Judged-band per-window pk-pk for the report, or None with the reason when no window fits."""

    pp = cap.band_limited_pp_mv()
    if pp.shape[1] == 0:
        return None, (
            f"(no complete {aa.NOISE_WINDOW_S:g} s window in a {cap.samples / cap.scan_hz:g} s capture - "
            "judged-band pk-pk needs at least one)"
        )
    return pp, ""


def print_capture_table(cap: Capture, *, noise_centred: bool) -> None:
    means = cap.means()
    raw_pp = cap.peak_to_peak_mv()
    pp, note = band_limited_summary(cap)
    rows = []
    for i, (label, channel) in enumerate(zip(cap.labels, cap.channels)):
        row = [label, f"CH{channel}", f"{means[i]:.4f}", f"{raw_pp[i]:.3f}"]
        if pp is None:
            row += ["n/a", "n/a", "0"]
        else:
            row += [f"{pp[i].max() * 1e3:.1f}", f"{np.median(pp[i]) * 1e3:.1f}", f"{pp.shape[1]}"]
        rows.append(row)
    print_table(rows, ["pos", "ch", "mean V", "raw pp mV", "worst pp uV", "median pp uV", "windows"])
    if pp is None:
        print(note)
        return
    worst = pp.max(axis=1)
    w = int(np.argmax(worst))
    kind = "noise" if noise_centred else "capture"
    print(
        f"judged band ({JUDGED_BAND_TEXT}: {aa.NOISE_WINDOW_S:g} s windows, decimation {aa.NOISE_DECIMATION_FACTOR}) "
        f"{kind}: worst position {cap.labels[w]} at {worst[w] * 1e3:.1f} uV pk-pk; median over positions of the "
        f"per-position median {np.median(np.median(pp, axis=1)) * 1e3:.1f} uV; {pp.shape[1]} window{'s' if pp.shape[1] != 1 else ''} each"
    )


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
def unique_positions(rig: ArrayRig, tokens: Iterable[Any]) -> list[Any]:
    """The given position tokens with duplicates dropped (by channel: ``2-4`` and ``CH13`` are one), order kept."""

    seen: set[int] = set()
    unique: list[Any] = []
    for token in tokens:
        channel = rig.resolve_channel(token)
        if channel not in seen:
            seen.add(channel)
            unique.append(token)
    return unique


def cmd_info(rig: ArrayRig, args: argparse.Namespace) -> int:
    info = rig.info
    assert info is not None
    print(f"device:  {info.summary()}")
    print(f"DLL:     {info.dll_version}")
    print(f"self-calibration: {selfcal_status(rig)}")
    if hasattr(rig.device, "read_config_block"):
        block = rig.device.read_config_block()
        print(f"config block ({len(block)} bytes): {block.hex(' ')}")
    print(
        "configuration: "
        + describe_config(rig.config, rig.scan_hz, drop_first=rig.drop_first,
                          buffer_bytes=rig.buffer_bytes, buffer_count=rig.buffer_count)
    )
    print(
        "wiring: position row-col -> DAQ single-ended channel CH = (row-1)*10 + (col-1): "
        "1-1 = CH0, 1-10 = CH9, 2-1 = CH10 ... 5-10 = CH49 (CH50-CH63 unwired)"
    )
    return 0


def selfcal_status(rig: ArrayRig) -> str:
    info = rig.info
    if info is not None and info.calibration_supported is False:
        return "not supported by this unit (ADC_QueryCal) - skipped"
    if rig.self_calibrated:
        return "ran ADC_SetCal(':AUTO:') at connect"
    return "skipped (--no-selfcal)"


def cmd_offset(rig: ArrayRig, args: argparse.Namespace) -> int:
    tokens = list(args.positions or [])
    if tokens and not args.all:
        offsets = rig.read_offsets(tokens, reads=args.reads)
        print(f"DC offsets, median of {args.reads} immediate scans (own-scale volts, {rig.range.name}):")
        for token in tokens:
            channel = rig.resolve_channel(token)
            label = channel_label(channel)
            volts = offsets[label]
            print(f"  {label:>5s} (CH{channel:<2d}): {volts:.3f} V  [{offset_context(volts)}]")
    else:
        volts = rig.read_scan_volts(reads=args.reads)
        print(f"DC offsets, median of {args.reads} immediate scans (own-scale volts, {rig.range.name}):")
        for line in offset_grid_lines(rig, volts):
            print(line)
        print(OFFSET_FLAG_LEGEND)
        wired = [float(volts[i]) for i, c in enumerate(rig.channels) if c < daq.CHANNEL_COUNT]
        print(f"  {len(wired)} positions read: {offset_tally(wired)}")
    print(OFFSET_CONTEXT_LINE)
    return 0


def _capture_and_report(rig: ArrayRig, args: argparse.Namespace, *, seconds: float, quiet_wait_s: float,
                        noise_centred: bool) -> Capture:
    selected = unique_positions(rig, args.positions) if getattr(args, "positions", None) else None
    what = "emitter-off noise" if noise_centred else "waveform"
    which = "all scanned channels" if selected is None else f"{len(selected)} selected position(s) out of the full scan"
    print(
        f"Capturing {seconds:g} s of {what}, {which}, at {rig.scan_hz:g} scans/s "
        f"({describe_config(rig.config, rig.scan_hz, drop_first=rig.drop_first)})"
        + (f"; discarding the first {quiet_wait_s:g} s (quiet wait)" if quiet_wait_s > 0 else "")
    )
    cap = rig.capture(seconds, selected, quiet_wait_s=quiet_wait_s)
    if cap.diagnostics is not None:
        print(f"stream: {cap.diagnostics.summary()}")
        print("integrity: OK")  # a problem would have raised StreamIntegrityError
    print_capture_table(cap, noise_centred=noise_centred)
    if getattr(args, "output", None):
        cap.to_csv(args.output)
    if getattr(args, "npz", None):
        cap.to_npz(args.npz)
    return cap


def cmd_stream(rig: ArrayRig, args: argparse.Namespace) -> int:
    _capture_and_report(rig, args, seconds=args.seconds, quiet_wait_s=0.0, noise_centred=False)
    return 0


def cmd_noise(rig: ArrayRig, args: argparse.Namespace) -> int:
    _capture_and_report(rig, args, seconds=args.seconds, quiet_wait_s=args.quiet_wait, noise_centred=True)
    print(f"calibration status: {aa.CALIBRATION_STATUS} - {CALIBRATION_STATUS_LINE}")
    return 0


def watch_frame_lines(rig: ArrayRig, live: LiveStream, tokens: list[str] | None, *, window_s: float = 1.0) -> list[str]:
    """One text frame of the live readout: stats line, then the grid or the selected positions."""

    stats = live.stats()
    lines = [
        f"[{stats.elapsed_s:7.1f} s] {stats.total_scans} scans, {stats.rate_hz:.1f} scans/s, "
        f"lag {stats.lag_s:.2f} s, {stats.chunks} chunks"
        + (f", ERROR: {stats.error}" if stats.error else "")
    ]
    _t, block = live.snapshot(seconds=window_s)
    if block.shape[1] == 0:
        lines.append("  (no data yet)")
        return lines
    means = block.mean(axis=1)
    pp_mv = np.ptp(block, axis=1) * 1000.0
    span = block.shape[1] / live.actual_timer_hz
    if tokens is None:
        mean_cells, pp_cells = {}, {}
        for index, channel in enumerate(rig.channels):
            if channel < daq.CHANNEL_COUNT:
                mean_cells[channel] = f"{means[index]:.3f}{offset_flag(float(means[index]))}"
                pp_cells[channel] = f"{pp_mv[index]:.2f}"
        lines.append(f"mean V over the last {span:.1f} s:")
        lines += grid_lines(mean_cells)
        lines.append(f"raw pk-pk mV over the last {span:.1f} s (wideband, not the judged band):")
        lines += grid_lines(pp_cells)
    else:
        for token in tokens:
            index = rig.index_of(token)
            label = channel_label(rig.channels[index])
            lines.append(
                f"  {label:>5s} (CH{rig.channels[index]:<2d})  mean {means[index]:.3f} V  "
                f"raw pk-pk {pp_mv[index]:.2f} mV  [{offset_context(float(means[index]))}]"
            )
    return lines


def cmd_watch(rig: ArrayRig, args: argparse.Namespace) -> int:
    tokens = unique_positions(rig, args.positions) if args.positions else None
    interval = float(args.interval)
    if interval <= 0:
        raise ValueError("--interval must be positive.")
    live = rig.live_stream()
    live.start()
    if not live.wait_ready(STREAM_START_TIMEOUT_S):
        live.stop()
        print(f"stream did not start: {live.error}", file=sys.stderr)
        return 2
    until = "" if args.seconds is None else f" for {args.seconds:g} s"
    print(f"live readout every {interval:g} s{until} - Ctrl+C stops (all channels stream; a selection is free)")
    if rig.info is not None and rig.info.simulated:
        print("  (simulated device: the lag figure only means something on the hardware)")
    stop_at = None if args.seconds is None else time.monotonic() + float(args.seconds)
    try:
        while True:
            time.sleep(interval)
            for line in watch_frame_lines(rig, live, tokens):
                print(line)
            if live.error:
                break
            if stop_at is not None and time.monotonic() >= stop_at:
                break
    except KeyboardInterrupt:
        print("\nstopping the stream...")
    finally:
        live.stop()
    stats = live.stats()
    if stats.diagnostics is not None:
        print(f"stream: {stats.diagnostics.summary()}")
        problems = stats.diagnostics.problems()
        print("integrity: " + ("OK" if not problems else "; ".join(problems)))
    if stats.error:
        print(f"stream error: {stats.error}", file=sys.stderr)
        return 2
    return 0


def cmd_test(rig: ArrayRig, args: argparse.Namespace) -> int:
    """Guided sequence like the ESP32 ``test``: identity -> offsets -> noise. Informational, no verdicts."""

    info = rig.info
    assert info is not None
    print("1/3  Identity and self-calibration")
    print(f"     device: {info.summary()}")
    print(f"     self-calibration: {selfcal_status(rig)}")
    print(f"2/3  DC offsets of every position (median of {args.reads} immediate scans)")
    volts = rig.read_scan_volts(reads=args.reads)
    for line in offset_grid_lines(rig, volts):
        print("     " + line)
    print(OFFSET_FLAG_LEGEND)
    wired = [float(volts[i]) for i, c in enumerate(rig.channels) if c < daq.CHANNEL_COUNT]
    print(f"     {len(wired)} positions read: {offset_tally(wired)}")
    print(f"3/3  Emitter-off noise capture ({args.seconds:g} s) in the judged band")
    _capture_and_report(rig, args, seconds=args.seconds, quiet_wait_s=args.quiet_wait, noise_centred=True)
    print(
        f"calibration status: {aa.CALIBRATION_STATUS} - {CALIBRATION_STATUS_LINE}; "
        f"offset band {aa.OFFSET_LIMITS_STATUS}. Informational only - this tool issues no verdict."
    )
    return 0


COMMANDS = {
    "info": cmd_info, "offset": cmd_offset, "stream": cmd_stream, "noise": cmd_noise, "watch": cmd_watch,
    "test": cmd_test,
}


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def _add_global_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """The acquisition options. Added to the main parser AND to every subcommand (with SUPPRESS defaults
    there, so a value given after the command overrides one given before it and the main defaults stand
    otherwise) - the bench probe accepts its options in either place and this tool should too."""

    def default(value: Any) -> Any:
        return argparse.SUPPRESS if suppress else value

    parser.add_argument("--simulate", action="store_true", default=default(False),
                        help="use SimulatedDaq(real_time=True) instead of the hardware")
    parser.add_argument("--range", type=int, default=default(DEFAULT_RANGE_CODE),
                        help="input range code 0-7 (2 = 0-5 V, the production range)")
    parser.add_argument("--hz", type=float, default=default(DEFAULT_SCAN_HZ), help="scan rate per channel (production 1000)")
    parser.add_argument("--oversample", type=int, default=default(DEFAULT_OVERSAMPLE),
                        help="extra conversions per channel per scan (production 3)")
    parser.add_argument("--drop", type=int, default=default(DEFAULT_DROP_FIRST),
                        help="conversions dropped after each multiplexer hop (production 1)")
    parser.add_argument("--reads", type=int, default=default(DEFAULT_OFFSET_READS),
                        help="immediate scans per offset reading, median taken (default 24 = the ESP32 OFFSET? depth)")
    parser.add_argument("--connect-timeout", type=float, default=default(DEFAULT_CONNECT_TIMEOUT_S),
                        help="seconds to wait for the device to enumerate")
    parser.add_argument("--no-selfcal", action="store_true", default=default(False),
                        help="skip ADC_SetCal(':AUTO:') at connect")
    parser.add_argument("--start", type=int, default=default(0), help="first scanned channel (default 0)")
    parser.add_argument("--end", type=int, default=default(daq.CHANNEL_COUNT - 1),
                        help=f"last scanned channel (default {daq.CHANNEL_COUNT - 1})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daq_rig_readout.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    _add_global_options(parser, suppress=False)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def positions_option(p: argparse.ArgumentParser) -> None:
        p.add_argument("-p", "--position", dest="positions", nargs="+", action="extend", metavar="POSITION",
                       help="row-col / CHn / channel number; repeatable, several per flag")

    p = sub.add_parser("info", help="device identity, DLL version, configuration, wiring reminder")
    _add_global_options(p, suppress=True)
    p = sub.add_parser("offset", help="DC offset(s): all fifty as a 5 x 10 table, or one line per position")
    p.add_argument("positions", nargs="*", metavar="POSITION", help="row-col / CHn / channel number (default: all)")
    p.add_argument("--all", action="store_true", help="the full table even when positions are given")
    _add_global_options(p, suppress=True)
    p = sub.add_parser("stream", help="capture every channel, report per position, save CSV/npz")
    p.add_argument("-s", "--seconds", type=float, default=DEFAULT_CAPTURE_S, help=f"capture length (default {DEFAULT_CAPTURE_S:g} s)")
    positions_option(p)
    p.add_argument("-o", "--output", help="save the capture to this CSV (t_us + one column per position)")
    p.add_argument("--npz", help="save the capture to this .npz (bench-probe layout, replayable)")
    _add_global_options(p, suppress=True)
    p = sub.add_parser("noise", help="emitter-off noise in the judged band (no limit yet: never a verdict)")
    p.add_argument("-s", "--seconds", type=float, default=DEFAULT_NOISE_S, help=f"capture length (default {DEFAULT_NOISE_S:g} s)")
    positions_option(p)
    p.add_argument("--quiet-wait", type=float, default=0.0, help="seconds of leading stream to discard first")
    p.add_argument("-o", "--output", help="save the capture to this CSV")
    p.add_argument("--npz", help="save the capture to this .npz")
    _add_global_options(p, suppress=True)
    p = sub.add_parser("watch", help="text-mode live readout (LiveStream), Ctrl+C to stop")
    positions_option(p)
    p.add_argument("-s", "--seconds", type=float, default=None, help="stop after this long (default: until Ctrl+C)")
    p.add_argument("--interval", type=float, default=DEFAULT_WATCH_INTERVAL_S, help="seconds between frames")
    _add_global_options(p, suppress=True)
    p = sub.add_parser("test", help="guided sequence: identity -> offsets -> noise capture (no verdicts)")
    p.add_argument("-s", "--seconds", type=float, default=DEFAULT_NOISE_S, help=f"noise capture length (default {DEFAULT_NOISE_S:g} s)")
    positions_option(p)
    p.add_argument("--quiet-wait", type=float, default=0.0, help="seconds of leading stream to discard first")
    p.add_argument("-o", "--output", help="save the noise capture to this CSV")
    p.add_argument("--npz", help="save the noise capture to this .npz")
    _add_global_options(p, suppress=True)
    return parser


def open_rig(args: argparse.Namespace) -> ArrayRig:
    rig = ArrayRig(
        simulate=args.simulate, range_code=args.range, scan_hz=args.hz, oversample=args.oversample,
        drop_first=args.drop, start_channel=args.start, end_channel=args.end, self_calibrate=not args.no_selfcal,
        connect_timeout_s=args.connect_timeout,
    )
    rig.connect()
    return rig


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rig = open_rig(args)
    except daq.DaqError as exc:
        print(f"DAQ error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        return COMMANDS[args.command](rig, args)
    except daq.DaqError as exc:
        print(f"DAQ error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        rig.close()


__all__ = [
    "ArrayRig", "CALIBRATION_STATUS_LINE", "Capture", "DEFAULT_BUFFER_BYTES", "DEFAULT_BUFFER_COUNT",
    "DEFAULT_CAPTURE_S", "DEFAULT_DROP_FIRST", "DEFAULT_LIVE_BUFFER_S", "DEFAULT_NOISE_S", "DEFAULT_OFFSET_READS",
    "DEFAULT_OVERSAMPLE", "DEFAULT_RANGE_CODE", "DEFAULT_SCAN_HZ", "LiveStats", "LiveStream", "NPZ_KEYS",
    "NPZ_SOURCE", "build_parser", "channel_label", "describe_config", "main", "offset_context", "offset_flag",
    "parse_channel_token",
]


if __name__ == "__main__":
    sys.exit(main())
