#!/usr/bin/env python3
"""Eltec array rig - live DAQ waveform viewer (real-time rolling plot of any position).

The array rig's counterpart of the single-detector rig's ``live_waveform.py``
(``Arduino/Eltec``): streams the ACCES USB-AIO16-64MA through
``daq_rig_readout.ArrayRig`` + ``LiveStream`` and plots ONE of the fifty
positions as it arrives, switchable live. Every scanned channel streams
together on this DAQ (the scan list is one contiguous range), so switching
the displayed position costs nothing: all fifty are already in the ring
buffer, and the middle panel shows all of them at once. Engineering only:
nothing here is a verdict - the tester (``eltec_40623_array_tester.py``)
judges, this file looks.

Panels (top to bottom)
----------------------
1. The rolling wideband trace of the selected position over the current time
   window. The y-axis auto-scales to the signal (a flat line still gets
   0.1 mV of headroom) and then holds its scale while the signal stays
   inside it, like a scope, so mV-level activity is visible on top of the
   ~0.7 V DC offset without the grid jumping every frame. The stats box in
   the corner is the stream's health check: rate should read ~1000 scans/s
   and "lag" (how far behind real time the host is) should stay near
   0.00 s - if it grows, the plot is showing old data. Any LiveStream error
   is printed there in full.
2. The 5 x 10 grid of ALL positions as coloured tiles (row 1 at the top,
   column 1 at the left; TP120's ``row-col`` labels, DAQ channel =
   (row-1)*10 + (col-1)), each annotated with its value. The colour is the
   live metric, toggled with ``g``:

     mean   the DC offset over the last 1 s, in TP120's PROVISIONAL offset
            bands (``array_analysis.OFFSET_*``): grey below 0.05 V (empty
            socket or dead part), amber below 0.3 V (low, or still settling
            - offsets settle upward after power-on), blue inside 0.3-1.2 V,
            red above 1.2 V, dark red railed (>= 4.9 V). Context only.
     noise  the judged-band pk-pk in mV (the worst of the last complete 1 s
            windows, up to four) on a sequential scale, darker = noisier.
            There is NO pin-level noise limit yet (CALIBRATION PENDING,
            docs/CALIBRATION_RECORD.md section 4b): a tile is never a
            pass/fail colour, whatever it reads.

   The selected tile carries a thick outline; clicking a tile selects it.
3. The judged-band view of the selected position: the tester's exact noise
   pipeline (``array_analysis``: Kaiser anti-alias FIR decimating 1000 ->
   50 SPS, then a least-squares detrend per 1 s window) applied to the
   samples in the displayed window plus the 310 samples of real history
   before them that seat the FIR, plotted in mV on the same "seconds ago"
   axis, with every window's pk-pk listed (worst and median in the stats
   box). The windows are anchored to the newest sample, so the trace ends
   at "now" and a partial oldest second is not judged; it needs at least 1 s
   of data and shows a hint until then. The newest 0.31 s is filtered
   against an odd reflection of itself (there is no future yet) - the
   tester's capture has real right-hand context there, so the newest
   window's figure can differ slightly from a replay of the same samples.
   This panel replaces the ESP32 viewer's cycle-average panel: there is no
   emitter board on the array rig yet, so there is no sync to fold on.

Choosing the position while watching
------------------------------------
The position can be changed at any time WITHOUT restarting the stream:

    arrow keys     move the selection on the 5 x 10 grid (up/down = row,
                   left/right = column, wrapping around the edges)
    n / p          next / previous DAQ channel (CH0 ... CH49, wrapping;
                   channels above the array, if scanned, are CHn)
    click a tile   select that position
    click the "Pos" button in the top-right corner to step to the next one

    --position 2-4 (or CH13, or 13) sets the starting position (default 1-1).

Hold / Run
----------
SPACE (or the top-right button) freezes the display - a scope's Run/Stop.
The stream keeps running in its thread, so the ring buffer stays current and
"Run" resumes with no gap; while held, the frozen buffer can still be
re-selected, widened and saved (``s`` then writes what is on screen). The
button is green while running, grey while held, and the title says HOLD.

Changing the time window while watching
---------------------------------------
The rolling window can be widened or narrowed at any time, without
restarting the stream, so a slow drift and a fast transient can be
inspected in the same session:

    ] or + (or =)  show MORE time (wider window)
    [ or - (or _)  show LESS time (narrower window)
    click the "Window" button in the top-right corner to step up and wrap

The steps are 0.25, 0.5, 1, 2, 4, 6, 8, 10, 15, 20, 30, 45, 60 s, clamped
to --max-window (default 60 s). Widening never invents history: the
LiveStream ring buffer is sized from --max-window, and samples older than
the buffer are gone, so a freshly widened plot fills in from the left over
the next few seconds.

Grid metric and saving
----------------------
    g              toggle the grid metric between mean (offset bands) and
                   noise (judged-band pk-pk, no limit)
    s              save the CURRENT buffer of ALL channels (the whole
                   history, up to --max-window) to
                   <save-dir>/daq_live_<YYYYmmdd_HHMMSS>.npz in the
                   readout's Capture.to_npz layout (replayable with
                   engineer_tools/replot_noise_capture.py); prints the path
    q              close the window (matplotlib's own key, kept)

    --save-dir defaults to the current working directory - never Documents;
    --save-on-exit writes one such file when the window closes.

Usage (from the repository root; ``--simulate`` needs no hardware):
    python array_rig/m40623/daq_live_waveform.py                      # position 1-1, 4 s window
    python array_rig/m40623/daq_live_waveform.py --position 2-4       # start on row 2, part 4 (= CH13)
    python array_rig/m40623/daq_live_waveform.py -w 8                 # 8 s starting window, live [ / ]
    python array_rig/m40623/daq_live_waveform.py --max-window 120     # windows up to 120 s (bigger buffer)
    python array_rig/m40623/daq_live_waveform.py --grid-metric noise  # tiles coloured by judged-band pk-pk
    python array_rig/m40623/daq_live_waveform.py --save-on-exit --save-dir C:/bench/captures
    python array_rig/m40623/daq_live_waveform.py --simulate --exit-after 4   # unattended: closes after 4 s
    python array_rig/m40623/daq_live_waveform.py --fps 10             # lighter redraw on a slow laptop
    add --range/--hz/--oversample/--drop/--start/--end/--no-selfcal/
    --connect-timeout to run with other acquisition settings (the same
    options and defaults as daq_rig_readout.py).

With a non-interactive matplotlib backend (``MPLBACKEND=Agg``, or no
display) there is no window: the viewer drives its own update loop and
--exit-after ends it, which is how the smoke tests run it headless.

Close the plot window (or Ctrl+C) to stop - the stream is stopped, the
device closed, and one closing line reports what the stream delivered.
Needs matplotlib:  pip install matplotlib
"""

from __future__ import annotations

import argparse
import datetime as _dt
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

MODEL_DIR = Path(__file__).resolve().parent
if str(MODEL_DIR) not in sys.path:
    sys.path.insert(0, str(MODEL_DIR))

import array_analysis as aa  # noqa: E402
import daq_backend as daq  # noqa: E402
from daq_rig_readout import (  # noqa: E402
    CALIBRATION_STATUS_LINE,
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_DROP_FIRST,
    DEFAULT_LIVE_BUFFER_S,
    DEFAULT_OVERSAMPLE,
    DEFAULT_RANGE_CODE,
    DEFAULT_SCAN_HZ,
    JUDGED_BAND_TEXT,
    STREAM_START_TIMEOUT_S,
    ArrayRig,
    Capture,
    LiveStats,
    LiveStream,
    channel_label,
    describe_config,
    open_rig,
    parse_channel_token,
)

try:
    import matplotlib
    import matplotlib.pyplot as plt
    from matplotlib.colors import to_hex, to_rgb
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import MaxNLocator
    from matplotlib.widgets import Button
except ImportError:
    sys.exit("matplotlib is not installed. Run:  pip install matplotlib")

DEFAULT_POSITION = "1-1"
DEFAULT_WINDOW_S = 4.0
# Rungs the live [ / ] keys and the Window button step through, in seconds -
# the ESP32 viewer's ladder, unchanged, so a window that means something on
# one rig means the same on the other. Anything above --max-window is
# dropped, and the starting -w value is added as its own rung so the window
# the viewer was launched with is always reachable.
WINDOW_PRESETS_S = (0.25, 0.5, 1.0, 2.0, 4.0, 6.0, 8.0, 10.0, 15.0, 20.0,
                    30.0, 45.0, 60.0)
# History ceiling = the readout's live-buffer default (60 s); raise with
# --max-window. The LiveStream ring is sized from it (plus the FIR's edge
# context) so widening mid-stream has real samples to show.
MAX_WINDOW_S = DEFAULT_LIVE_BUFFER_S
DEFAULT_FPS = 20.0              # ~20 redraws/s like the ESP32 viewer's 50 ms frame interval
# The judged-band panel and the grid metric are recomputed at most this
# often, and only over the displayed window: the 621-tap FIR over 60 s of
# samples on every one of ~20 frames per second would starve the GUI, while
# four refreshes a second is faster than anyone reads a number. Drawing is
# blitted: a frame paints the trace and the stats box over a background
# cached at the last full draw, a slow refresh repaints the grid and the
# judged panel the same way, and the whole figure (~100 text artists, ~0.2 s
# at a laptop's DPI) is only redrawn when a label, a title or an axis scale
# changes - which is what keeps the trace at the frame rate.
SLOW_REFRESH_HZ = 4.0
GRID_METRICS = ("mean", "noise")
DEFAULT_GRID_METRIC = "mean"
# The "mean" grid metric averages the last second - LiveStream.latest()'s
# window, and short enough to follow a part being inserted.
GRID_MEAN_SECONDS = 1.0
# The "noise" grid metric is the worst of this many newest complete 1 s
# windows - the tester's headline number per position (its tiles show the
# worst window of the capture), steadier than a single window.
GRID_NOISE_WINDOWS = 4
# Colour-scale top of the noise grid is the smallest of these mantissas x
# 10^n at or above the noisiest tile, never below this floor (mV), so the
# scale reads as a round number and a quiet tray does not blow up to full
# contrast on instrument noise alone.
NOISE_SCALE_FLOOR_MV = 0.05
NOISE_SCALE_MANTISSAS = (1.0, 1.5, 2.0, 3.0, 5.0, 7.0, 10.0)
NOISE_COLOURMAP = "Blues"       # sequential: light = quiet, dark = noisy; never a pass/fail colour
# Autoscale: 15 % headroom and never less than 0.1 mV on the raw trace (the
# ESP32 viewer's rule, so a flat line still shows), 5 uV on the judged trace
# whose whole excursion is tens of uV on a quiet part. A scale is then HELD
# while the signal stays inside it and still fills at least
# TRACE_HOLD_FILL_FRACTION of it - a scope holds its vertical scale the same
# way, and a held scale is what lets the frames blit the trace instead of
# re-rendering every tick label of the figure twenty times a second. When
# the signal walks OUT of the held scale (a just-inserted part settling
# upward for ~20 s) the new scale gets the wider TRACE_DRIFT_PAD_FRACTION so
# the drift stays inside it for a while instead of forcing a full redraw
# every few frames; the tight pad returns once the signal fills too little.
TRACE_PAD_FRACTION = 0.15
TRACE_DRIFT_PAD_FRACTION = 0.35
TRACE_MIN_PAD_V = 1e-4
TRACE_HOLD_FILL_FRACTION = 0.4
# A flat or nearly flat trace can never fill 40 % of limits that carry
# min_pad on each side (autoscale's smallest span is 2 x min_pad), so the
# fill rule alone would rescale - and force a full figure redraw - on every
# frame of a quiet channel (a code-0 input, an empty socket): 6 fps with the
# CPU pegged instead of 20. Limits no wider than this many min_pads are at
# the minimum zoom already and are held while the signal stays inside them.
TRACE_HOLD_MIN_SPAN_PADS = 4.0
JUDGED_MIN_PAD_MV = 0.005
# Figure geometry: fits a 1280 x 800 laptop screen (the bench laptop's) with
# the Tk title bar and toolbar, three panels stacked.
FIGURE_SIZE_IN = (11.5, 7.0)
PANEL_HEIGHT_RATIOS = (3.2, 2.4, 2.0)
WINDOW_PP_LISTED = 12           # newest window pk-pk values listed under the judged trace
STATS_WAITING_TEXT = "waiting for samples..."
JUDGED_HINT_TEXT = "needs >= {window:g} s of data in the window - widen it with ] (or wait)"
SAVE_PREFIX = "daq_live_"

# Colours. The tile colours are the tester's GRID_COLOURS by value (same as
# the tester's - it is not imported here: it owns Tk and the results paths);
# the trace/button colours are the ESP32 viewer's, so the two live viewers
# look alike.
TILE_UNAVAILABLE = ("#d9dee7", "#4b5563")   # same as the tester's NOT_MEASURED: no data yet / not scanned
TILE_DEAD = ("#e8edf6", "#93a1bd")          # same as the tester's EMPTY: below OFFSET_DEAD_V (empty socket or dead part)
TILE_LOW = ("#fdf5dd", "#854d0e")           # same as the tester's SETTLING: below OFFSET_MIN_V (low or still settling)
TILE_IN_BAND = ("#e1e7f6", "#16336f")       # same as the tester's LOADED: inside the PROVISIONAL band
TILE_HIGH = ("#fde7e9", "#991b1b")          # same as the tester's OFFSET_FAIL: above OFFSET_MAX_V
TILE_RAILED = ("#f7bcc4", "#7f1d1d")        # this viewer's own deeper red for >= OFFSET_RAIL_V (the tester paints railed as OFFSET_FAIL)
ELTEC_BLUE = "#1e419c"                      # same as the tester's
TEXT_DARK = "#141d33"                       # same as the tester's
TRACE_COLOUR = "#0284c7"                    # the ESP32 viewer's AIN0 blue
JUDGED_COLOUR = "#0f766e"                   # the ESP32 viewer's cycle-average teal
BUTTON_RUNNING = "#bbf7d0"                  # the ESP32 viewer's emitter-ON green
BUTTON_HELD = "#e5e7eb"                     # the ESP32 viewer's emitter-OFF grey

# matplotlib binds these keys itself (s = save figure, g = grid, p = pan,
# left/right = view history); they are released so the viewer's own
# bindings win. Everything else (q = close, f = fullscreen, ...) is kept.
RELEASED_KEYMAPS = ("keymap.save", "keymap.grid", "keymap.grid_minor", "keymap.pan", "keymap.back", "keymap.forward")


# ----------------------------------------------------------------------
# Window ladder (GUI-free)
# ----------------------------------------------------------------------
def window_ladder(start_s: float, max_window_s: float) -> list[float]:
    """The rungs the live controls step through: presets up to the ceiling, plus the start and the ceiling.

    The ceiling is never below the start (the ESP32 viewer's
    ``max_window = max(max_window, window)`` rule), so the launch window is
    always reachable and the buffer always covers it.
    """

    start = float(start_s)
    ceiling = max(float(max_window_s), start)
    if not math.isfinite(start) or start <= 0 or not math.isfinite(ceiling):
        raise ValueError("the window and --max-window must be positive.")
    return sorted({float(w) for w in WINDOW_PRESETS_S if w <= ceiling} | {start, ceiling})


def step_window(current: float, ladder: Sequence[float], direction: int) -> float:
    """One rung wider (+1) or narrower (-1) along ``ladder``; the end rungs stay put."""

    if direction > 0:
        rungs = [w for w in ladder if w > current + 1e-9]
    elif direction < 0:
        rungs = [w for w in reversed(ladder) if w < current - 1e-9]
    else:
        return float(current)
    return float(rungs[0]) if rungs else float(current)


def cycle_window(current: float, ladder: Sequence[float]) -> float:
    """Button click: one rung wider, wrapping back to the narrowest."""

    rungs = [w for w in ladder if w > current + 1e-9]
    return float(rungs[0]) if rungs else float(ladder[0])


# ----------------------------------------------------------------------
# Position selection (GUI-free)
# ----------------------------------------------------------------------
@dataclass
class Selection:
    """The selected DAQ channel, moved over the 5 x 10 grid or along the scanned range.

    Only channels inside ``start_channel..end_channel`` (the scan) can be
    selected; a grid move that lands outside the scan keeps stepping in the
    same direction, wrapping, until it finds a scanned position. A channel
    above the array (CH50+, only when --end reaches it) has no grid position:
    the arrow keys then behave like n / p.
    """

    channel: int
    start_channel: int = 0
    end_channel: int = daq.CHANNEL_COUNT - 1

    def __post_init__(self) -> None:
        self.start_channel = int(self.start_channel)
        self.end_channel = int(self.end_channel)
        if not 0 <= self.start_channel <= self.end_channel <= daq.MAX_SCAN_CHANNEL:
            raise ValueError(f"scan range CH{self.start_channel}-CH{self.end_channel} is not valid.")
        self.channel = self._checked(self.channel)

    @classmethod
    def from_token(cls, token: Any, start_channel: int, end_channel: int) -> "Selection":
        """``'row-col'`` / ``'CHn'`` / ``'n'`` -> a Selection, or ValueError naming the scanned range."""

        return cls(parse_channel_token(token), start_channel, end_channel)

    def _checked(self, channel: Any) -> int:
        value = int(channel)
        if not self.start_channel <= value <= self.end_channel:
            raise ValueError(
                f"{channel_label(value)} is CH{value}, outside the scanned range "
                f"CH{self.start_channel}-CH{self.end_channel} (change --start/--end)."
            )
        return value

    # -- where it is ---------------------------------------------------------
    @property
    def label(self) -> str:
        return channel_label(self.channel)

    @property
    def index(self) -> int:
        """Row of the scanned range (0 = start_channel) - what LiveStream.snapshot() takes."""

        return self.channel - self.start_channel

    @property
    def on_grid(self) -> bool:
        return self.channel < daq.CHANNEL_COUNT

    @property
    def row(self) -> int | None:
        return self.channel // daq.COLS + 1 if self.on_grid else None

    @property
    def col(self) -> int | None:
        return self.channel % daq.COLS + 1 if self.on_grid else None

    # -- moving it -----------------------------------------------------------
    def select(self, channel: Any) -> int:
        self.channel = self._checked(channel)
        return self.channel

    def next(self) -> int:
        channel = self.channel + 1
        self.channel = self.start_channel if channel > self.end_channel else channel
        return self.channel

    def prev(self) -> int:
        channel = self.channel - 1
        self.channel = self.end_channel if channel < self.start_channel else channel
        return self.channel

    def move(self, d_row: int, d_col: int) -> int:
        """Arrow-key move on the grid, wrapping; keeps stepping past positions outside the scan."""

        if not self.on_grid:
            return self.next() if (d_row > 0 or d_col > 0) else self.prev()
        row, col = self.row, self.col
        assert row is not None and col is not None
        steps = daq.ROWS if d_row else daq.COLS
        for _ in range(steps):
            row = (row - 1 + d_row) % daq.ROWS + 1
            col = (col - 1 + d_col) % daq.COLS + 1
            channel = (row - 1) * daq.COLS + (col - 1)
            if self.start_channel <= channel <= self.end_channel:
                self.channel = channel
                break
        return self.channel


# ----------------------------------------------------------------------
# Judged-band trace and the grid metric (GUI-free, the tester's pipeline)
# ----------------------------------------------------------------------
def _judged_window_raw(scan_hz: float, decimation_factor: int, window_s: float) -> tuple[int, int]:
    """(filtered samples per window, raw samples per window) - 50 and 1000 at the production settings."""

    hz = float(scan_hz)
    if not math.isfinite(hz) or hz <= 0:
        raise ValueError("scan_hz must be a positive finite number.")
    factor = int(decimation_factor)
    if factor < 1:
        raise ValueError("decimation_factor must be >= 1.")
    window_samples = int(round(float(window_s) * hz / factor))
    if window_samples < 1:
        raise ValueError("window_s is shorter than one filtered sample period.")
    return window_samples, window_samples * factor


def judged_band_trace(
    volts_1d: Any,
    scan_hz: float,
    *,
    context_samples: int,
    decimation_factor: int = aa.NOISE_DECIMATION_FACTOR,
    window_s: float = aa.NOISE_WINDOW_S,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The tester's judged-band pipeline over one channel's newest samples.

    ``volts_1d`` is oldest-first; its first ``context_samples`` are history
    that precedes the displayed window (they only seat the FIR). Of the rest,
    the newest whole number of 1 s windows is judged (anchored to the newest
    sample); everything before that, context included, is the FIR's left
    context (it keeps the last 310). Returns ``(t_rel_s, mv, window_pp_mv)``:
    the filtered/detrended trace in mV at 50 SPS on a "seconds ago" axis
    (0 = the newest raw sample; each point sits at the centre of its
    decimation block), and the per-window pk-pk exactly as
    ``array_analysis.band_limited_window_pp_mv`` computes it on the same
    samples. All three are empty when no complete window fits.
    """

    samples = np.asarray(volts_1d, dtype=np.float64).ravel()
    hz = float(scan_hz)
    factor = int(decimation_factor)
    window_samples, window_raw = _judged_window_raw(hz, factor, window_s)
    context = min(max(int(context_samples), 0), samples.size)
    windows = (samples.size - context) // window_raw
    if windows == 0:
        empty = np.empty(0, dtype=np.float64)
        return empty, empty.copy(), empty.copy()
    start = samples.size - windows * window_raw
    core = samples[start:][None, :]
    left = samples[:start][None, :] if start > 0 else None
    filtered = aa.decimate_antialiased_multi(core, factor, left_context=left)
    judged = aa.detrend_window_segments_multi(filtered, window_samples)
    window_pp_mv = aa.window_peak_to_peak_mv_multi(judged, window_samples)[0]
    centres = np.arange(judged.shape[1], dtype=np.float64) * factor + factor // 2
    t_rel_s = (centres - (core.shape[1] - 1)) / hz
    return t_rel_s, judged[0] * 1000.0, window_pp_mv


def grid_metric_values(volts: Any, scan_hz: float, metric: str) -> np.ndarray:
    """One number per channel of a ``[channels, samples]`` block (oldest first): NaN where not available.

    ``mean``: the mean over the newest ``GRID_MEAN_SECONDS`` (or whatever is
    there). ``noise``: the worst judged-band pk-pk in mV over the newest
    complete 1 s windows (at most ``GRID_NOISE_WINDOWS``), the older samples
    seating the FIR; NaN until a whole window exists.
    """

    block = np.asarray(volts, dtype=np.float64)
    if block.ndim == 1:
        block = block[None, :]
    if block.ndim != 2:
        raise ValueError("volts must be a [channels, samples] array.")
    channels, samples = block.shape
    if metric == "mean":
        if samples == 0:
            return np.full(channels, np.nan)
        span = max(1, int(round(GRID_MEAN_SECONDS * float(scan_hz))))
        return block[:, -span:].mean(axis=1)
    if metric == "noise":
        factor = aa.NOISE_DECIMATION_FACTOR
        _window_samples, window_raw = _judged_window_raw(scan_hz, factor, aa.NOISE_WINDOW_S)
        windows = min(GRID_NOISE_WINDOWS, samples // window_raw)
        if windows == 0:
            return np.full(channels, np.nan)
        start = samples - windows * window_raw
        left = block[:, :start] if start > 0 else None
        pp = aa.band_limited_window_pp_mv(
            block[:, start:], scan_hz, decimation_factor=factor, window_s=aa.NOISE_WINDOW_S, left_context=left
        )
        return pp.max(axis=1)
    raise ValueError(f"unknown grid metric {metric!r} (choose from {', '.join(GRID_METRICS)}).")


# ----------------------------------------------------------------------
# Tile colours and text (GUI-free)
# ----------------------------------------------------------------------
def offset_tile_colour(volts: float) -> tuple[str, str]:
    """(background, text) for a mean-offset tile - TP120's PROVISIONAL bands via ``array_analysis.classify_offset``.

    The band edges (dead floor, min, max, rail) and their inclusive
    boundary rule are the analysis module's, never restated here. NaN (no
    data yet) is the unavailable grey.
    """

    value = float(volts)
    if not math.isfinite(value):
        return TILE_UNAVAILABLE
    offset_class = aa.classify_offset(value, occupancy=aa.Occupancy.LOADED)
    return {
        aa.OffsetClass.HO_RAILED: TILE_RAILED,
        aa.OffsetClass.HO: TILE_HIGH,
        aa.OffsetClass.DEAD: TILE_DEAD,
        aa.OffsetClass.LO: TILE_LOW,
    }.get(offset_class, TILE_IN_BAND)


def nice_ceiling(value: float) -> float:
    """Colour-scale top for the noise grid: the smallest NOISE_SCALE_MANTISSAS x 10^n at or above ``value``."""

    if not math.isfinite(value) or value <= NOISE_SCALE_FLOOR_MV:
        return NOISE_SCALE_FLOOR_MV
    exponent = math.floor(math.log10(value))
    for mantissa in NOISE_SCALE_MANTISSAS:
        candidate = mantissa * 10.0 ** exponent
        if value <= candidate * (1.0 + 1e-9):
            return float(candidate)
    return float(10.0 ** (exponent + 1))


def noise_tile_colour(pp_mv: float, scale_mv: float) -> tuple[str, str]:
    """(background, text) for a noise tile: ``NOISE_COLOURMAP`` from 0 to ``scale_mv``; NaN is the unavailable grey.

    The map starts a little way in so the quietest tile is still visibly
    tinted, and the text flips to white on the dark end. Sequential by
    design: there is no limit, so there is no pass/fail colour.
    """

    value = float(pp_mv)
    if not math.isfinite(value):
        return TILE_UNAVAILABLE
    top = float(scale_mv) if math.isfinite(scale_mv) and scale_mv > 0 else NOISE_SCALE_FLOOR_MV
    fraction = min(max(value / top, 0.0), 1.0)
    r, g, b, _a = matplotlib.colormaps[NOISE_COLOURMAP](0.12 + 0.83 * fraction)
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return to_hex((r, g, b)), (TEXT_DARK if luminance > 0.55 else "#ffffff")


def tile_value_text(value: float, metric: str) -> str:
    if not math.isfinite(float(value)):
        return "no data" if metric == "mean" else f"< {aa.NOISE_WINDOW_S:g} s"
    return f"{value:.3f} V" if metric == "mean" else f"{value:.3f} mV"


def grid_title(metric: str, *, scale_mv: float | None = None) -> str:
    """Two lines over the grid: what the colour is, then the legend - both say 'never a verdict'."""

    if metric == "mean":
        return (
            f"grid: mean V over the last {GRID_MEAN_SECONDS:g} s - context only, never a verdict   [g: noise]\n"
            f"grey < {aa.OFFSET_DEAD_V:.2f} V (empty/dead)   amber < {aa.OFFSET_MIN_V:.1f} V (low/settling)   "
            f"blue {aa.OFFSET_MIN_V:.1f}-{aa.OFFSET_MAX_V:.1f} V (TP120 band, {aa.OFFSET_LIMITS_STATUS})   "
            f"red > {aa.OFFSET_MAX_V:.1f} V   dark red >= {aa.OFFSET_RAIL_V:.1f} V (railed)"
        )
    scale = "" if scale_mv is None else f"scale 0-{scale_mv:g} mV, "
    return (
        f"grid: judged-band pk-pk (mV), worst of the last {GRID_NOISE_WINDOWS} complete {aa.NOISE_WINDOW_S:g} s windows   "
        f"[g: mean]\n{scale}darker = noisier - NO LIMIT yet (calibration {aa.CALIBRATION_STATUS}), never a verdict"
    )


# ----------------------------------------------------------------------
# Text (GUI-free)
# ----------------------------------------------------------------------
def format_stats(
    *,
    label: str,
    channel: int,
    mean_v: float,
    raw_pp_mv: float,
    judged_pp_mv: Sequence[float] | np.ndarray | None,
    stats: LiveStats,
    simulated: bool,
    held: bool = False,
) -> str:
    """The stats box: position and numbers on the first line, stream health on the second."""

    if judged_pp_mv is None or len(judged_pp_mv) == 0:
        judged = f"judged n/a (needs >= {aa.NOISE_WINDOW_S:g} s)"
    else:
        values = np.asarray(judged_pp_mv, dtype=np.float64)
        judged = (
            f"judged {values.max():.3f}/{np.median(values):.3f} mV "
            f"(worst/median {aa.NOISE_WINDOW_S:g} s window)"
        )
    first = f"pos {label} CH{channel}   mean {mean_v:.4f} V   raw pk-pk {raw_pp_mv:.2f} mV   {judged}"
    second = f"rate {stats.rate_hz:.1f} scans/s   lag {stats.lag_s:.2f} s   chunks {stats.chunks}"
    if simulated:
        second += "   [SIM]"
    if held:
        second += "   HOLD"
    if stats.error:
        second += f"   ERROR: {stats.error}"
    return first + "\n" + second


def format_window_pp(window_pp_mv: Sequence[float] | np.ndarray, *, max_listed: int = WINDOW_PP_LISTED) -> str:
    """``1 s windows (oldest -> newest): 0.057 0.052 0.060 mV pk-pk; worst 0.060, median 0.055 (3 windows)``."""

    values = np.asarray(window_pp_mv, dtype=np.float64)
    if values.size == 0:
        return ""
    listed = [f"{v:.3f}" for v in values]
    if len(listed) > max_listed:
        listed = ["...", *listed[-max_listed:]]
    plural = "" if values.size == 1 else "s"
    return (
        f"{aa.NOISE_WINDOW_S:g} s windows (oldest -> newest): {' '.join(listed)} mV pk-pk; "
        f"worst {values.max():.3f}, median {np.median(values):.3f} ({values.size} window{plural})"
    )


def format_closing_line(stats: LiveStats) -> str:
    """``Stream closed: N scans received, rate ..., lag ..., <diagnostics summary>, integrity OK/<problems>``."""

    text = (
        f"Stream closed: {stats.total_scans} scans received, rate {stats.rate_hz:.1f} scans/s, "
        f"lag {stats.lag_s:.2f} s, {stats.chunks} chunks"
    )
    if stats.diagnostics is not None:
        problems = stats.diagnostics.problems()
        text += f", {stats.diagnostics.summary()}, integrity " + ("OK" if not problems else "; ".join(problems))
    else:
        text += ", no stream diagnostics (the device stream never started)"
    if stats.error:
        text += f", ERROR: {stats.error}"
    return text


def device_summary_short(rig: ArrayRig) -> str:
    info = rig.info
    config = rig.config
    if info is None:
        return f"not connected - {rig.range.name}, {rig.scan_hz:g} scans/s"
    kind = "SIMULATED " if info.simulated else ""
    return (
        f"{kind}{info.name} serial {info.serial_number} - {rig.range.name}, {rig.scan_hz:g} scans/s, "
        f"CH{config.start_channel}-CH{config.end_channel}"
    )


def judged_title(scan_hz: float) -> str:
    factor = aa.NOISE_DECIMATION_FACTOR
    band = f"judged band {JUDGED_BAND_TEXT}" if scan_hz == DEFAULT_SCAN_HZ else "judged band"
    return (
        f"{band}: Kaiser anti-alias FIR {scan_hz:g} -> {scan_hz / factor:g} SPS + per-{aa.NOISE_WINDOW_S:g}-s "
        f"least-squares detrend (the tester's pipeline) - no noise limit yet, never a verdict"
    )


# ----------------------------------------------------------------------
# Saving the buffer
# ----------------------------------------------------------------------
def capture_from_buffer(rig: ArrayRig, live: LiveStream, t_s: np.ndarray, volts: np.ndarray) -> Capture:
    """A readout ``Capture`` (all scanned channels) from a LiveStream snapshot, so ``to_npz`` writes the probe layout.

    ``started_at`` is the host clock of the OLDEST sample (now minus the
    span); there are no stream diagnostics on a still-running stream.
    """

    info = rig.info
    span_s = float(-t_s[0]) if t_s.size else 0.0
    started = _dt.datetime.now() - _dt.timedelta(seconds=span_s)
    return Capture(
        volts=np.ascontiguousarray(volts),
        scan_hz=live.scan_hz,
        actual_timer_hz=live.actual_timer_hz,
        channels=rig.channels,
        positions=rig.positions,
        range_code=rig.range_code,
        oversample=rig.oversample,
        drop_first=rig.drop_first,
        diagnostics=None,
        started_at=started.isoformat(timespec="seconds"),
        daq_serial=info.serial_number if info else "unknown",
        daq_model=info.name if info else "unknown",
        simulated=bool(info.simulated) if info else False,
        quiet=True,
    )


def save_path(save_dir: str | Path, when: _dt.datetime | None = None) -> Path:
    """``<save_dir>/daq_live_<YYYYmmdd_HHMMSS>.npz``, suffixed ``_2``, ``_3`` ... if that name already exists."""

    stamp = (when or _dt.datetime.now()).strftime("%Y%m%d_%H%M%S")
    directory = Path(save_dir)
    path = directory / f"{SAVE_PREFIX}{stamp}.npz"
    counter = 2
    while path.exists():
        path = directory / f"{SAVE_PREFIX}{stamp}_{counter}.npz"
        counter += 1
    return path


# ----------------------------------------------------------------------
# The viewer (figure, artists, controls)
# ----------------------------------------------------------------------
def backend_is_interactive() -> bool:
    """False for Agg/PDF/SVG-type backends, where ``plt.show()`` returns at once and the viewer must loop itself."""

    name = str(matplotlib.get_backend()).lower()
    try:
        from matplotlib.backends.registry import BackendFilter, backend_registry

        non_interactive = {b.lower() for b in backend_registry.list_builtin(BackendFilter.NON_INTERACTIVE)}
    except Exception:  # older matplotlib: the well-known set
        non_interactive = {"agg", "cairo", "pdf", "pgf", "ps", "svg", "template"}
    return name not in non_interactive


def release_default_keys() -> None:
    """Free the keys the viewer binds from matplotlib's own key map (see RELEASED_KEYMAPS)."""

    for name in RELEASED_KEYMAPS:
        if name in plt.rcParams:
            plt.rcParams[name] = []


def autoscale(axis: Any, values: np.ndarray, min_pad: float, *, pad_fraction: float = TRACE_PAD_FRACTION) -> tuple[float, float]:
    """The ESP32 viewer's rule: 15 % headroom, never less than ``min_pad`` so a flat line shows."""

    lo, hi = float(np.min(values)), float(np.max(values))
    pad = max((hi - lo) * pad_fraction, min_pad)
    axis.set_ylim(lo - pad, hi + pad)
    return lo, hi


def hold_or_rescale(axis: Any, values: np.ndarray, min_pad: float) -> bool:
    """Autoscale like ``autoscale`` but only when the data leaves the current limits or fills too little of them.

    Returns True when the limits changed (the figure then needs a full
    redraw for its tick labels) and False when they were held. A signal
    that left the limits gets the wider drift pad (see the constants).
    """

    lo, hi = float(np.min(values)), float(np.max(values))
    cur_lo, cur_hi = axis.get_ylim()
    cur_span = cur_hi - cur_lo
    inside = cur_lo <= lo and hi <= cur_hi
    if inside and cur_span > 0:
        if (hi - lo) >= TRACE_HOLD_FILL_FRACTION * cur_span:
            return False
        if cur_span <= TRACE_HOLD_MIN_SPAN_PADS * min_pad:
            return False        # already at the minimum zoom: a flat trace can never fill 40 %
    autoscale(axis, values, min_pad, pad_fraction=TRACE_PAD_FRACTION if inside else TRACE_DRIFT_PAD_FRACTION)
    return True


class Viewer:
    """The figure, its artists and the live controls on top of one ``LiveStream``.

    ``update(frame)`` refreshes the data (the judged panel, the grid and the
    slow half of the stats at most ``SLOW_REFRESH_HZ`` times a second, or
    when something changed); ``render()`` draws. Every data artist is
    ANIMATED (matplotlib's blitting recipe): a normal figure draw skips
    them and caches the background, a fast frame restores that background
    and paints the trace line and the stats box, a slow refresh also paints
    the grid and the judged panel, and only a change of label, title or
    axis scale costs a full figure draw. ``tick()`` is one animation step
    (update + render), driven by a canvas timer in a window or by
    ``run_viewer``'s loop headless. All data goes through
    ``_channel_samples`` / ``_tray_samples`` so Hold can swap the live
    stream for one frozen snapshot without touching the drawing.
    """

    def __init__(
        self,
        rig: ArrayRig,
        live: LiveStream,
        *,
        selection: Selection,
        window_s: float = DEFAULT_WINDOW_S,
        max_window_s: float = MAX_WINDOW_S,
        grid_metric: str = DEFAULT_GRID_METRIC,
        save_dir: str | Path | None = None,
        exit_after_s: float | None = None,
    ) -> None:
        if grid_metric not in GRID_METRICS:
            raise ValueError(f"unknown grid metric {grid_metric!r} (choose from {', '.join(GRID_METRICS)}).")
        self.rig = rig
        self.live = live
        self.selection = selection
        self.ladder = window_ladder(window_s, max_window_s)
        self.window_s = float(window_s)
        self.grid_metric = grid_metric
        self.save_dir = Path(save_dir) if save_dir is not None else Path.cwd()
        self.held = False
        self.simulated = bool(rig.info.simulated) if rig.info is not None else False
        self.device_text = device_summary_short(rig)
        # Raw samples the FIR needs on the left of the displayed window (310
        # at factor 20), fetched with every window so the judged trace is
        # seated on real history whenever the buffer has it.
        self.context_samples = aa.antialias_edge_context_samples(aa.NOISE_DECIMATION_FACTOR)
        self.context_s = self.context_samples / float(live.scan_hz)
        self._slow_interval_s = 1.0 / SLOW_REFRESH_HZ
        self._slow_at = -math.inf
        self._dirty = True
        self._frozen: tuple[np.ndarray, np.ndarray] | None = None
        self._judged_pp: np.ndarray | None = None
        self.grid_values = np.full(len(rig.channels), np.nan)
        self.noise_scale_mv = NOISE_SCALE_FLOOR_MV
        self.exit_deadline = None if exit_after_s is None else time.monotonic() + float(exit_after_s)
        self.close_requested = False
        self.saved_paths: list[Path] = []
        self.full_draws = 0
        self.blit_draws = 0
        self._background: Any = None
        self._needs_full_draw = True
        self._slow_pending = True       # the grid / judged artists changed since they were last painted
        self._timer: Any = None
        self._frame_interval_s = 1.0 / DEFAULT_FPS
        self._build_figure()

    # -- construction ---------------------------------------------------------
    def _build_figure(self) -> None:
        release_default_keys()
        rig = self.rig
        # The three panels share the "seconds ago" axis only top and bottom;
        # the grid in the middle has its own row/column geometry.
        self.fig, axes = plt.subplots(
            3, 1, figsize=FIGURE_SIZE_IN, gridspec_kw={"height_ratios": list(PANEL_HEIGHT_RATIOS)},
        )
        self.ax_trace, self.ax_grid, self.ax_judged = axes
        self.fig.subplots_adjust(top=0.9, bottom=0.08, left=0.095, right=0.97, hspace=0.62)
        for axis in (self.ax_trace, self.ax_judged):
            axis.yaxis.set_major_locator(MaxNLocator(5))     # fewer tick labels: a full draw is mostly text
        manager = getattr(self.fig.canvas, "manager", None)
        if manager is not None:
            try:
                manager.set_window_title("Eltec array rig - live DAQ waveform")
            except Exception:
                pass

        # Top: the rolling trace. The line and the stats box are the FAST
        # animated artists: skipped by a normal figure draw and painted on
        # every frame over the cached background (matplotlib's blitting
        # recipe), so a frame costs the trace, not ~100 text artists.
        self.trace, = self.ax_trace.plot([], [], color=TRACE_COLOUR, linewidth=0.8, animated=True)
        self.ax_trace.set_ylabel("Sensor (V)")
        self.ax_trace.set_xlabel("Seconds ago")
        self.ax_trace.grid(True, alpha=0.25)
        self.stats_text = self.ax_trace.text(
            0.01, 0.98, STATS_WAITING_TEXT, transform=self.ax_trace.transAxes, va="top", ha="left", fontsize=8,
            family="monospace", bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "#cbd5e1"}, zorder=6,
            animated=True,
        )
        self._fast_artists = (self.trace, self.stats_text)

        # Middle: the grid as an RGB image (one cell per tile) with one text
        # per tile; the tiles carry their own row-col labels, so the axes
        # needs no tick labels. These and the judged panel's artists are the
        # SLOW animated set, painted by the slow refresh.
        self.grid_rgb = np.empty((daq.ROWS, daq.COLS, 3), dtype=np.float64)
        self.grid_rgb[:, :] = to_rgb(TILE_UNAVAILABLE[0])
        self.grid_image = self.ax_grid.imshow(
            self.grid_rgb, extent=(0.5, daq.COLS + 0.5, daq.ROWS + 0.5, 0.5), aspect="auto", interpolation="nearest",
            zorder=1, animated=True,
        )
        self.ax_grid.set_xlim(0.5, daq.COLS + 0.5)
        self.ax_grid.set_ylim(daq.ROWS + 0.5, 0.5)
        self.ax_grid.set_xticks([])
        self.ax_grid.set_yticks([])
        self.ax_grid.set_xticks(np.arange(0.5, daq.COLS + 1, 1.0), minor=True)
        self.ax_grid.set_yticks(np.arange(0.5, daq.ROWS + 1, 1.0), minor=True)
        self.ax_grid.grid(which="minor", color="white", linewidth=2.0)
        self.ax_grid.tick_params(which="both", length=0)
        scanned = set(rig.channels)
        self.tile_texts: dict[int, Any] = {}
        for channel in range(daq.CHANNEL_COUNT):
            row, col = divmod(channel, daq.COLS)
            label = daq.position_for_channel(channel)
            text = f"{label}\n{'no data' if channel in scanned else '-'}"
            self.tile_texts[channel] = self.ax_grid.text(
                col + 1, row + 1, text, ha="center", va="center", fontsize=7, family="monospace",
                color=TILE_UNAVAILABLE[1], zorder=3, animated=True,
            )
        self.selection_outline = Rectangle(
            (0.5, 0.5), 1.0, 1.0, fill=False, edgecolor=ELTEC_BLUE, linewidth=3.0, zorder=4, animated=True,
        )
        self.ax_grid.add_patch(self.selection_outline)

        # Bottom: the judged-band trace
        self.judged_line, = self.ax_judged.plot([], [], color=JUDGED_COLOUR, linewidth=0.9, animated=True)
        self.window_marks = self.ax_judged.vlines(
            [], 0.0, 1.0, transform=self.ax_judged.get_xaxis_transform(), colors="#94a3b8", linestyles=":",
            linewidth=0.8, animated=True,
        )
        self.ax_judged.set_ylabel("judged (mV)")
        self.ax_judged.set_xlabel("Seconds ago")
        self.ax_judged.grid(True, alpha=0.25)
        self.ax_judged.set_title(judged_title(float(self.live.scan_hz)), fontsize=8.5, loc="left")
        self.judged_hint = self.ax_judged.text(
            0.5, 0.5, JUDGED_HINT_TEXT.format(window=aa.NOISE_WINDOW_S), transform=self.ax_judged.transAxes,
            ha="center", va="center", color="0.5", fontsize=10, animated=True,
        )
        self.judged_text = self.ax_judged.text(
            0.01, 0.97, "", transform=self.ax_judged.transAxes, va="top", ha="left", fontsize=8, family="monospace",
            bbox={"facecolor": "white", "alpha": 0.8, "edgecolor": "#cbd5e1"}, zorder=6, animated=True,
        )
        # Draw order inside each axes = list order (image under the texts,
        # the outline on top; the judged line under its annotations).
        self._slow_artists = (
            self.grid_image, *self.tile_texts.values(), self.selection_outline,
            self.judged_line, self.window_marks, self.judged_hint, self.judged_text,
        )

        # Buttons along the top edge, like the ESP32 viewer's
        self.position_button_ax = self.fig.add_axes([0.38, 0.935, 0.19, 0.045])
        self.position_button = Button(self.position_button_ax, "")
        self.window_button_ax = self.fig.add_axes([0.58, 0.935, 0.19, 0.045])
        self.window_button = Button(self.window_button_ax, "")
        self.hold_button_ax = self.fig.add_axes([0.78, 0.935, 0.19, 0.045])
        self.hold_button = Button(self.hold_button_ax, "", color=BUTTON_RUNNING, hovercolor=BUTTON_RUNNING)
        self.position_button.on_clicked(lambda _event: self.next_position())
        self.window_button.on_clicked(lambda _event: self.cycle_window())
        self.hold_button.on_clicked(lambda _event: self.toggle_hold())
        canvas = self.fig.canvas
        canvas.mpl_connect("key_press_event", self.on_key)
        canvas.mpl_connect("button_press_event", self.on_click)
        canvas.mpl_connect("draw_event", self._on_draw)
        canvas.mpl_connect("resize_event", lambda _event: self._mark_full_draw())
        canvas.mpl_connect("close_event", self._on_close)
        self.refresh_labels()

    # -- labels ---------------------------------------------------------------
    def title_text(self) -> str:
        sel = self.selection
        text = f"{sel.label} (CH{sel.channel}) - {self.window_s:g} s window - {self.device_text}"
        return text + " - HOLD" if self.held else text

    def _mark_full_draw(self) -> None:
        self._needs_full_draw = True

    def refresh_labels(self) -> None:
        sel = self.selection
        self.position_button.label.set_text(f"Pos {sel.label} CH{sel.channel}  [n/p]")
        self.window_button.label.set_text(f"Window: {self.window_s:g} s  [ ] ")
        self.hold_button.label.set_text("HOLD  [space]" if self.held else "Run  [space]")
        colour = BUTTON_HELD if self.held else BUTTON_RUNNING
        self.hold_button.color = colour          # Button repaints its axes with .color after a hover
        self.hold_button.hovercolor = colour
        self.hold_button_ax.set_facecolor(colour)
        self.ax_trace.set_title(self.title_text(), fontsize=10)
        if sel.on_grid:
            assert sel.row is not None and sel.col is not None
            self.selection_outline.set_xy((sel.col - 0.5, sel.row - 0.5))
            self.selection_outline.set_visible(True)
        else:
            self.selection_outline.set_visible(False)
        self._dirty = True
        self._mark_full_draw()

    # -- controls ---------------------------------------------------------------
    def select_channel(self, channel: Any) -> int:
        if int(channel) != self.selection.channel:
            self.selection.select(channel)
            self.refresh_labels()
        return self.selection.channel

    def move_selection(self, d_row: int, d_col: int) -> int:
        self.selection.move(d_row, d_col)
        self.refresh_labels()
        return self.selection.channel

    def next_position(self) -> int:
        self.selection.next()
        self.refresh_labels()
        return self.selection.channel

    def prev_position(self) -> int:
        self.selection.prev()
        self.refresh_labels()
        return self.selection.channel

    def set_window(self, seconds: float) -> float:
        seconds = min(max(float(seconds), self.ladder[0]), self.ladder[-1])
        if abs(seconds - self.window_s) >= 1e-9:
            self.window_s = seconds
            self.refresh_labels()
        return self.window_s

    def step_window(self, direction: int) -> float:
        return self.set_window(step_window(self.window_s, self.ladder, direction))

    def cycle_window(self) -> float:
        return self.set_window(cycle_window(self.window_s, self.ladder))

    def set_hold(self, held: bool) -> bool:
        held = bool(held)
        if held == self.held:
            return self.held
        if held:
            # One snapshot of the WHOLE buffer, all channels: the display, the
            # grid and a later 's' all read from it until Run.
            self._frozen = self.live.snapshot()
        else:
            self._frozen = None
        self.held = held
        self.refresh_labels()
        return self.held

    def toggle_hold(self) -> bool:
        return self.set_hold(not self.held)

    def set_grid_metric(self, metric: str) -> str:
        if metric not in GRID_METRICS:
            raise ValueError(f"unknown grid metric {metric!r} (choose from {', '.join(GRID_METRICS)}).")
        if metric != self.grid_metric:
            self.grid_metric = metric
            self._dirty = True
            self._mark_full_draw()
        return self.grid_metric

    def toggle_grid_metric(self) -> str:
        index = GRID_METRICS.index(self.grid_metric)
        return self.set_grid_metric(GRID_METRICS[(index + 1) % len(GRID_METRICS)])

    def save_buffer(self) -> Path | None:
        """Write the buffer (frozen while held, live otherwise) as ``daq_live_<stamp>.npz``; None when empty."""

        t_s, volts = self._frozen if self._frozen is not None else self.live.snapshot()
        if volts.shape[1] == 0:
            print("nothing to save yet - no samples in the buffer")
            return None
        capture = capture_from_buffer(self.rig, self.live, t_s, volts)
        path = capture.to_npz(save_path(self.save_dir))
        span_s = volts.shape[1] / float(self.live.actual_timer_hz)
        source = "held" if self.held else "live"
        print(f"Saved {capture.samples} scans x {len(capture.channels)} channels ({span_s:.1f} s of {source} buffer) to {path}")
        self.saved_paths.append(path)
        return path

    def attach_timer(self, timer: Any, *, interval_s: float) -> None:
        """The canvas timer driving ``tick`` in a window; stopped before the figure closes so it never re-arms.

        ``interval_s`` is the frame budget: ``tick`` sets the timer's wait to
        whatever is left of it after each frame.
        """

        self._timer = timer
        self._frame_interval_s = float(interval_s)

    def request_close(self) -> None:
        if self.close_requested:
            return
        self.close_requested = True
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
        plt.close(self.fig)

    # -- events -----------------------------------------------------------------
    def on_key(self, event: Any) -> None:
        key = getattr(event, "key", None)
        if not key:
            return
        if key == " ":
            self.toggle_hold()
        elif key in ("]", "+", "="):
            self.step_window(+1)
        elif key in ("[", "-", "_"):
            self.step_window(-1)
        elif key == "up":
            self.move_selection(-1, 0)
        elif key == "down":
            self.move_selection(+1, 0)
        elif key == "left":
            self.move_selection(0, -1)
        elif key == "right":
            self.move_selection(0, +1)
        elif key in ("n", "N"):
            self.next_position()
        elif key in ("p", "P"):
            self.prev_position()
        elif key in ("g", "G"):
            self.toggle_grid_metric()
        elif key in ("s", "S"):
            self.save_buffer()

    def on_click(self, event: Any) -> None:
        if getattr(event, "inaxes", None) is not self.ax_grid or getattr(event, "button", 1) != 1:
            return
        xdata, ydata = getattr(event, "xdata", None), getattr(event, "ydata", None)
        if xdata is None or ydata is None:
            return
        col, row = int(round(float(xdata))), int(round(float(ydata)))
        if not (1 <= row <= daq.ROWS and 1 <= col <= daq.COLS):
            return
        channel = (row - 1) * daq.COLS + (col - 1)
        if self.selection.start_channel <= channel <= self.selection.end_channel:
            self.select_channel(channel)

    def _on_draw(self, _event: Any = None) -> None:
        """After every full draw: cache the background (animated artists are not in it) and paint them on top."""

        canvas = self.fig.canvas
        if getattr(canvas, "is_saving", lambda: False)():
            return                                  # savefig draws animated artists itself; keep the cache clean
        try:
            self._background = canvas.copy_from_bbox(self.fig.bbox)
        except Exception:                           # a backend without blitting support
            self._background = None
            return
        self._draw_fast()
        self._draw_slow()

    def _on_close(self, _event: Any = None) -> None:
        self.close_requested = True
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass

    # -- data access (live or frozen) -----------------------------------------
    def _channel_samples(self, index: int, seconds: float) -> tuple[np.ndarray, np.ndarray]:
        if self._frozen is None:
            t_s, block = self.live.snapshot([index], seconds=seconds)
            return t_s, block[0]
        t_all, v_all = self._frozen
        n = min(v_all.shape[1], int(round(seconds * float(self.live.actual_timer_hz))))
        if n <= 0:
            return np.empty(0), np.empty(0)
        return t_all[-n:], v_all[index, -n:]

    def _tray_samples(self, seconds: float) -> np.ndarray:
        if self._frozen is None:
            _t_s, block = self.live.snapshot(seconds=seconds)
            return block
        _t_all, v_all = self._frozen
        n = min(v_all.shape[1], int(round(seconds * float(self.live.actual_timer_hz))))
        return v_all[:, -n:] if n > 0 else v_all[:, :0]

    # -- per-frame update -------------------------------------------------------
    def update(self, _frame: Any = None, *, force: bool = False) -> tuple:
        """Refresh the data behind the artists (no drawing); ``force`` runs the slow refresh regardless of its timer."""

        artists = (self.trace, self.judged_line, self.stats_text, self.grid_image)
        if self.exit_deadline is not None and time.monotonic() >= self.exit_deadline:
            self.request_close()
            return artists
        hz = float(self.live.actual_timer_hz)
        window_s = self.window_s
        stats = self.live.stats()
        t_s, volts = self._channel_samples(self.selection.index, window_s + self.context_s)
        if volts.size < 2:
            self.stats_text.set_text(STATS_WAITING_TEXT + (f"   ERROR: {stats.error}" if stats.error else ""))
            return artists
        head = max(0, volts.size - int(round(window_s * hz)))
        t_win, v_win = t_s[head:], volts[head:]
        self.trace.set_data(t_win, v_win)
        if self.ax_trace.get_xlim() != (-window_s, 0.0):
            self.ax_trace.set_xlim(-window_s, 0.0)
            self._mark_full_draw()
        if self._dirty:
            autoscale(self.ax_trace, v_win, TRACE_MIN_PAD_V)      # a new position or window: rescale outright
        elif hold_or_rescale(self.ax_trace, v_win, TRACE_MIN_PAD_V):
            self._mark_full_draw()

        now = time.monotonic()
        if force or self._dirty or (not self.held and now - self._slow_at >= self._slow_interval_s):
            self._slow_at = now
            rescale = self._dirty
            self._dirty = False
            self._refresh_judged(volts, head, hz, window_s, rescale=rescale)
            self._refresh_grid(hz)
            self._slow_pending = True

        self.stats_text.set_text(format_stats(
            label=self.selection.label, channel=self.selection.channel, mean_v=float(np.mean(v_win)),
            raw_pp_mv=float(np.ptp(v_win)) * 1000.0, judged_pp_mv=self._judged_pp, stats=stats,
            simulated=self.simulated, held=self.held,
        ))
        return artists

    def _refresh_judged(self, volts: np.ndarray, context_samples: int, hz: float, window_s: float, *,
                        rescale: bool = False) -> None:
        t_rel, mv, pp = judged_band_trace(volts, hz, context_samples=context_samples)
        self._judged_pp = pp if pp.size else None
        if self.ax_judged.get_xlim() != (-window_s, 0.0):
            self.ax_judged.set_xlim(-window_s, 0.0)
            self._mark_full_draw()
        if mv.size == 0:
            self.judged_line.set_data([], [])
            self.window_marks.set_segments([])
            self.judged_text.set_text("")
            self.judged_hint.set_visible(True)
            return
        self.judged_line.set_data(t_rel, mv)
        if rescale:
            autoscale(self.ax_judged, mv, JUDGED_MIN_PAD_MV)
            self._mark_full_draw()
        elif hold_or_rescale(self.ax_judged, mv, JUDGED_MIN_PAD_MV):
            self._mark_full_draw()
        # Window boundaries: the windows are anchored to the newest sample.
        boundaries = [-(j * aa.NOISE_WINDOW_S) for j in range(pp.size + 1)]
        self.window_marks.set_segments([[(x, 0.0), (x, 1.0)] for x in boundaries])
        self.judged_text.set_text(format_window_pp(pp))
        self.judged_hint.set_visible(False)

    def _refresh_grid(self, hz: float) -> None:
        metric = self.grid_metric
        if metric == "mean":
            block = self._tray_samples(GRID_MEAN_SECONDS)
        else:
            block = self._tray_samples(GRID_NOISE_WINDOWS * aa.NOISE_WINDOW_S + self.context_s)
        values = grid_metric_values(block, hz, metric) if block.shape[1] else np.full(block.shape[0], np.nan)
        self.grid_values = values
        scale: float | None = None
        if metric == "noise":
            finite = values[np.isfinite(values)]
            scale = nice_ceiling(float(finite.max())) if finite.size else NOISE_SCALE_FLOOR_MV
            self.noise_scale_mv = scale
        self.grid_rgb[:, :] = to_rgb(TILE_UNAVAILABLE[0])
        for index, channel in enumerate(self.rig.channels):
            if channel >= daq.CHANNEL_COUNT:
                continue
            value = float(values[index])
            bg, fg = offset_tile_colour(value) if metric == "mean" else noise_tile_colour(value, scale or NOISE_SCALE_FLOOR_MV)
            row, col = divmod(channel, daq.COLS)
            self.grid_rgb[row, col] = to_rgb(bg)
            text = self.tile_texts[channel]
            text.set_text(f"{daq.position_for_channel(channel)}\n{tile_value_text(value, metric)}")
            text.set_color(fg)
        self.grid_image.set_data(self.grid_rgb)
        title = grid_title(metric, scale_mv=scale)
        if title != self.ax_grid.get_title(loc="left"):   # the title is not animated: a change needs a full draw
            self.ax_grid.set_title(title, fontsize=8, loc="left")
            self._mark_full_draw()

    # -- drawing ----------------------------------------------------------------
    def _draw_fast(self) -> None:
        for artist in self._fast_artists:
            artist.axes.draw_artist(artist)

    def _draw_slow(self) -> None:
        for artist in self._slow_artists:
            artist.axes.draw_artist(artist)
        self._slow_pending = False

    def render(self) -> bool:
        """Draw the figure: a full draw when needed (returns True), otherwise a blit of the animated artists.

        A blitted frame restores the cached background, paints the fast
        artists and pushes the trace axes to the screen; when the slow
        refresh changed the grid or the judged panel, their artists are
        painted and their axes pushed too. Everything else on screen stays
        as the last full draw left it.
        """

        canvas = self.fig.canvas
        if self._needs_full_draw or self._background is None:
            self._needs_full_draw = False
            canvas.draw()                           # fires draw_event -> _on_draw caches the background, paints everything
            self.full_draws += 1
            return True
        canvas.restore_region(self._background)
        self._draw_fast()
        if self._slow_pending:
            self._draw_slow()
            canvas.blit(self.ax_grid.bbox)
            canvas.blit(self.ax_judged.bbox)
        canvas.blit(self.ax_trace.bbox)
        self.blit_draws += 1
        return False

    def tick(self, _frame: Any = None) -> float:
        """One animation step: update the data, then draw; returns the seconds it took.

        The Tk timer re-arms AFTER the callback, so the wait it is given is
        what is left of the frame budget (never under 1 ms): a 15 ms blit
        still yields the frame rate, and a full draw is followed by the next
        frame at once.
        """

        started = time.perf_counter()
        if self.close_requested:
            return 0.0
        self.update(_frame)
        if not self.close_requested:
            self.render()
        spent_s = time.perf_counter() - started
        if self._timer is not None and not self.close_requested:
            self._timer.interval = max(1, int(round((self._frame_interval_s - spent_s) * 1000.0)))
        return spent_s

    # -- shutdown ---------------------------------------------------------------
    def stop_stream(self) -> LiveStats:
        self.live.stop()
        return self.live.stats()

    def closing_line(self) -> str:
        return format_closing_line(self.live.stats())

    def close(self) -> None:
        """Stop the stream and close the figure (safe to call twice; the rig is closed by the caller)."""

        self.stop_stream()
        self.request_close()


def build_viewer(rig: ArrayRig, live: LiveStream, args: argparse.Namespace, selection: Selection | None = None) -> Viewer:
    """Create the figure and artists from parsed CLI arguments (the stream must already be running)."""

    if selection is None:
        selection = Selection.from_token(args.position, rig.config.start_channel, rig.config.end_channel)
    return Viewer(
        rig, live, selection=selection, window_s=args.window, max_window_s=args.max_window,
        grid_metric=args.grid_metric, save_dir=args.save_dir, exit_after_s=args.exit_after,
    )


def run_viewer(rig: ArrayRig, live: LiveStream, args: argparse.Namespace, selection: Selection | None = None) -> Viewer:
    """Show the viewer until the window closes (or --exit-after), then stop the stream and print the closing line.

    With an interactive backend this blocks in ``plt.show()`` behind a
    canvas timer that calls ``Viewer.tick``; with a non-interactive one
    (Agg) ``plt.show()`` would return at once, so the loop is driven here
    with ``time.sleep`` between frames until --exit-after (or Ctrl+C, or a
    stream error).
    """

    viewer = build_viewer(rig, live, args, selection)
    fps = float(args.fps)
    interval_s = 1.0 / fps
    try:
        if backend_is_interactive():
            timer = viewer.fig.canvas.new_timer(interval=max(1, int(round(1000.0 / fps))))
            timer.add_callback(viewer.tick)
            viewer.attach_timer(timer, interval_s=interval_s)
            timer.start()
            try:
                plt.show()                            # blocks until the window closes
            except KeyboardInterrupt:
                pass
        else:
            frame = 0
            try:
                while True:
                    spent_s = viewer.tick(frame)
                    frame += 1
                    if viewer.close_requested or live.error:
                        break
                    if viewer.exit_deadline is not None and time.monotonic() >= viewer.exit_deadline:
                        break
                    time.sleep(max(0.0, interval_s - spent_s))
            except KeyboardInterrupt:
                pass
    finally:
        viewer.close()
        if getattr(args, "save_on_exit", False):
            viewer.save_buffer()
        print(viewer.closing_line())
    return viewer


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="daq_live_waveform.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--position", default=DEFAULT_POSITION,
                        help=f"starting position: row-col / CHn / channel number (default {DEFAULT_POSITION}); "
                             "change it live with the arrow keys, n / p or a click on a tile")
    parser.add_argument("-w", "--window", type=float, default=DEFAULT_WINDOW_S,
                        help=f"STARTING rolling window in seconds (default {DEFAULT_WINDOW_S:g}); change it live "
                             "with ] / [ or the Window button")
    parser.add_argument("--max-window", type=float, default=MAX_WINDOW_S,
                        help="widest window the live control may reach, and so the length of the sample history "
                             f"kept in memory (default {MAX_WINDOW_S:g} s)")
    parser.add_argument("--grid-metric", choices=GRID_METRICS, default=DEFAULT_GRID_METRIC,
                        help="what colours the 5 x 10 grid: mean = DC offset in TP120's provisional bands, "
                             "noise = judged-band pk-pk with no limit (default mean; g toggles live)")
    parser.add_argument("--save-dir", default=None,
                        help="where 's' and --save-on-exit write daq_live_<stamp>.npz (default: the current "
                             "working directory; never Documents)")
    parser.add_argument("--save-on-exit", action="store_true",
                        help="save the buffer of all channels once when the viewer closes")
    parser.add_argument("--exit-after", type=float, default=None, metavar="SECONDS",
                        help="close the viewer automatically after this long (unattended captures, smoke tests; "
                             "works headless with MPLBACKEND=Agg)")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS,
                        help=f"redraws per second of the rolling trace (default {DEFAULT_FPS:g}; the judged panel "
                             f"and the grid refresh at most {SLOW_REFRESH_HZ:g} times a second)")
    # The readout's acquisition options, same names and defaults (its
    # open_rig() reads exactly these attributes).
    parser.add_argument("--simulate", action="store_true", help="use SimulatedDaq(real_time=True) instead of the hardware")
    parser.add_argument("--range", type=int, default=DEFAULT_RANGE_CODE,
                        help=f"input range code 0-7 (default {DEFAULT_RANGE_CODE} = 0-5 V, the production range)")
    parser.add_argument("--hz", type=float, default=DEFAULT_SCAN_HZ,
                        help=f"scan rate per channel (default {DEFAULT_SCAN_HZ:g}; 1000 keeps the judged band the tester's)")
    parser.add_argument("--oversample", type=int, default=DEFAULT_OVERSAMPLE,
                        help=f"extra conversions per channel per scan (default {DEFAULT_OVERSAMPLE})")
    parser.add_argument("--drop", type=int, default=DEFAULT_DROP_FIRST,
                        help=f"conversions dropped after each multiplexer hop (default {DEFAULT_DROP_FIRST})")
    parser.add_argument("--start", type=int, default=0, help="first scanned channel (default 0)")
    parser.add_argument("--end", type=int, default=daq.CHANNEL_COUNT - 1,
                        help=f"last scanned channel (default {daq.CHANNEL_COUNT - 1})")
    parser.add_argument("--no-selfcal", action="store_true", help="skip ADC_SetCal(':AUTO:') at connect")
    parser.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT_S,
                        help=f"seconds to wait for the device to enumerate (default {DEFAULT_CONNECT_TIMEOUT_S:g})")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if not (args.window > 0 and args.max_window > 0):
        raise ValueError("--window and --max-window must be positive.")
    if args.fps <= 0:
        raise ValueError("--fps must be positive.")
    if args.exit_after is not None and args.exit_after < 0:
        raise ValueError("--exit-after must be >= 0.")
    # The history buffer is sized from --max-window, so it has to cover the
    # starting window (the ESP32 viewer's rule).
    args.max_window = max(float(args.max_window), float(args.window))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate_args(args)
        selection = Selection.from_token(args.position, args.start, args.end)   # fails before any hardware is touched
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    save_dir = Path(args.save_dir) if args.save_dir else Path.cwd()
    args.save_dir = str(save_dir)
    try:
        rig = open_rig(args)
    except daq.DaqError as exc:
        print(f"DAQ error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        context_s = aa.antialias_edge_context_samples(aa.NOISE_DECIMATION_FACTOR) / rig.scan_hz
        live = rig.live_stream(buffer_s=args.max_window + context_s)
        live.start()
        if not live.wait_ready(STREAM_START_TIMEOUT_S):
            live.stop()
            print(f"stream did not start: {live.error}", file=sys.stderr)
            return 2
        print(
            f"Live view of {len(rig.channels)} channels at {rig.scan_hz:g} scans/s "
            f"({describe_config(rig.config, rig.scan_hz, drop_first=rig.drop_first)}); "
            f"position {selection.label} (CH{selection.channel}), window {args.window:g} s (up to {args.max_window:g} s)"
        )
        print("In the plot window: arrow keys / n / p / a click on a tile select the position; SPACE = Hold/Run; "
              "] / [ (or + / -) change the time shown; g = grid metric (mean/noise); s = save the buffer; q = close.")
        print(f"                    's' and --save-on-exit write {SAVE_PREFIX}<stamp>.npz to {save_dir}")
        print(f"note: {CALIBRATION_STATUS_LINE}")
        if rig.info is not None and rig.info.simulated:
            print("  (simulated device: the lag figure only means something on the hardware)")
        run_viewer(rig, live, args, selection)
        if live.error:
            print(f"stream error: {live.error}", file=sys.stderr)
            return 2
        return 0
    except daq.DaqError as exc:
        print(f"DAQ error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        rig.close()


__all__ = [
    "DEFAULT_FPS", "DEFAULT_GRID_METRIC", "DEFAULT_POSITION", "DEFAULT_WINDOW_S", "GRID_MEAN_SECONDS", "GRID_METRICS",
    "GRID_NOISE_WINDOWS", "MAX_WINDOW_S", "SLOW_REFRESH_HZ", "Selection", "Viewer", "WINDOW_PRESETS_S",
    "backend_is_interactive", "build_parser", "build_viewer", "capture_from_buffer", "cycle_window",
    "format_closing_line", "format_stats", "format_window_pp", "grid_metric_values", "grid_title",
    "hold_or_rescale", "judged_band_trace", "main", "nice_ceiling", "noise_tile_colour", "offset_tile_colour",
    "run_viewer", "save_path", "step_window", "tile_value_text", "window_ladder",
]


if __name__ == "__main__":
    sys.exit(main())
