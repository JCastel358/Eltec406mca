"""
Eltec 406MCA rig - live waveform viewer (real-time rolling plot).

Streams the selected ADS1256 channel from the ESP32 and plots it as it
arrives. The y-axis auto-scales to the signal, so the mV-level chopped
waveform is visible on top of the ~0.7 V DC offset. A small lower panel
shows the firmware's sync bit (only meaningful with --pwm; flat 0 when the
emitter is driven by an external signal generator).

Live stats in the corner double as a serial-link health check: sample rate
should read ~1000 S/s, and the gap / torn counters should stay at 0 - if
they climb, the link is dropping data (same issue the capture scripts warn
about). "lag" is the receive backlog the host has not read yet: it should
stay near 0.0 s - if it grows, the plot is showing old data and emitter
toggles will appear late by that amount.

Emitter control while watching
------------------------------
The emitter drive can be switched on and off at any time WITHOUT restarting
the stream, so the sensor's driven response can be compared directly against
its undriven baseline:

    SPACE (or E)   toggle the emitter drive on/off
    click the "Emitter" button in the top-right corner

The current state is shown in the button, in the plot title, and in the stats
box. Each switch also drops a dashed marker line (green = on, red = off) so
the moment of the change stays visible as it scrolls past. The marker is
timed by the host clock, so treat it as approximate; the sync panel below is
the sample-accurate record of when the drive actually started and stopped.

Changing the time window while watching
---------------------------------------
The rolling window can be widened or narrowed at any time, without restarting
the stream, so a slow 1 Hz response and a fast transient can be inspected in
the same session:

    ] or + (or =)  show MORE time (wider window)
    [ or - (or _)  show LESS time (narrower window)
    click the "Window" button in the top-right corner to step up and wrap

The steps are 0.25, 0.5, 1, 2, 4, 6, 8, 10, 15, 20, 30, 45, 60 s, clamped to
--max-window
(default 60 s). Widening never invents history: samples older than the window
that was in force are simply not in the buffer, so a freshly widened plot fills
in from the left over the next few seconds.

Seeing a small signal through noise: the cycle-average panel
------------------------------------------------------------
When the ESP32 PWM drive is on, a third panel shows the sync-locked AVERAGE
of the last N drive cycles (default 64, --fold-cycles), like an
oscilloscope's averaging acquisition mode. Broadband noise shrinks with the
square root of N while the repeating response does not, so a response buried
in noise on the rolling trace (e.g. the 449 M18's ~20 mV at 18 Hz under a
~50 mV noise floor) becomes clearly visible here within a few seconds. The
panel needs the sync bit, so it works only with the ESP32 driving the
emitter - with an external drive (legacy fixture, signal generator) it shows
a hint instead. Raise --fold-cycles for a cleaner but slower-updating
average, e.g. 256 cycles is ~14 s of history at 18 Hz.

If a trace feature keeps happening with the emitter OFF, it is not a response
to the emitter - it is drift, mains pickup, or a thermal artifact. A genuine
pyroelectric response appears only while sync is toggling, and it takes some
seconds to settle after a switch (the Model 405 thermal breakpoint is ~0.2 Hz,
so it is still settling for several cycles of a 1 Hz drive).

Usage:
    python3 live_waveform.py                     # DUT sensor (AIN0), no drive change
    python3 live_waveform.py --pwm               # start with the ESP32 PWM drive on
    python3 live_waveform.py --freq 1            # 1 Hz drive for the 405 M22 sensor
                                                 # (needs firmware v1.9+; window
                                                 # auto-widens to show ~6 cycles).
                                                 # Starts OFF - press SPACE to drive.
    python3 live_waveform.py --pwm --freq 1      # ...same, but start with it on
    python3 live_waveform.py --freq 18 --duty 20 # 449 M18 drive: 18 Hz at the
                                                 # TP443 20/80 blade duty
                                                 # (needs firmware v3.2; the
                                                 # board boots at 50 %)
    python3 live_waveform.py --pwm --pin 26      # retarget the gate pin first
    python3 live_waveform.py --channel ref       # reference sensor (AIN1)
    python3 live_waveform.py --fe v19            # watch the same signal on the
                                                 # old gain-2/buffered front end
                                                 # (firmware v2.1+; reverts to
                                                 # v20 when the port closes)
    python3 live_waveform.py -w 8                # start with an 8 s rolling
                                                 # window (default 4); it can
                                                 # be changed live with ] / [
    python3 live_waveform.py --max-window 120    # allow windows up to 120 s
                                                 # (bigger history buffer)
    python3 live_waveform.py -w 0.5              # externally driven signal
                                                 # (legacy fixture, signal
                                                 # generator): no --pwm, leave
                                                 # the emitter OFF; 0.5 s shows
                                                 # ~9 cycles at 18 Hz
    add --port COM3 (or /dev/ttyUSB0) to skip auto-detection

Close the plot window (or Ctrl+C) to stop - the stream and PWM are shut down
cleanly. Needs matplotlib:  pip install matplotlib
"""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
from collections import deque

import numpy as np

try:
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from matplotlib.widgets import Button
except ImportError:
    sys.exit("matplotlib is not installed. Run:  pip install matplotlib")

from esp32_rig_readout import (
    FE_PRESETS,
    PWM_MAX_DUTY_PERCENT,
    PWM_MIN_DUTY_PERCENT,
    SAMPLE_RATE_HZ,
    Esp32Rig,
    find_port,
)

DEFAULT_WINDOW_S = 4.0
# Rungs the live [ / ] keys and the Window button step through, in seconds.
# Anything above --max-window is dropped, and the starting -w value is added
# as its own rung so the window it was launched with is always reachable.
# 0.25 s is ~4 cycles of an 18 Hz drive and ~1 cycle of the 5 Hz one - the
# useful low end when the question is cycle SHAPE rather than drift.
WINDOW_PRESETS_S = (0.25, 0.5, 1.0, 2.0, 4.0, 6.0, 8.0, 10.0, 15.0, 20.0,
                    30.0, 45.0, 60.0)
MAX_WINDOW_S = 60.0             # history buffer ceiling; raise with --max-window
FRAME_INTERVAL_MS = 50          # ~20 redraws/s
GAP_FACTOR = 2.5                # dt > this x nominal period counts as a gap
BYTES_PER_LINE_EST = 32         # "D,<t_us>,<raw>,<volts>,<sync>\n" ~ 32 bytes
CH_COLOR_A = "#0284c7"          # AIN0 - blue
DEFAULT_FOLD_CYCLES = 64        # cycles in the sync-locked average panel
FOLD_POINTS = 200               # phase resolution of the folded cycle


def fold_sync_cycles(t_us, volts, sync, max_cycles=DEFAULT_FOLD_CYCLES,
                     points=FOLD_POINTS):
    """Average the newest complete drive cycles, cut on sync rising edges.

    Returns (mean, std, n_cycles, period_ms) or None when fewer than two
    complete cycles are buffered (emitter off, or an external drive with no
    sync). Each cycle is resampled onto ``points`` phase steps between its
    own edges, so PWM-restart phase jumps and timer drift do not smear the
    average. Timestamps are unwrapped modularly, so the uint32 micros()
    rollover (~71 min) cannot corrupt the fold.
    """
    sync = np.asarray(sync)
    edges = np.flatnonzero((sync[1:] > 0.5) & (sync[:-1] < 0.5)) + 1
    if edges.size < 3:
        return None
    t = np.concatenate(
        [[0.0], np.cumsum(np.diff(np.asarray(t_us, dtype=np.int64))
                          & 0xFFFFFFFF)]
    )
    edge_t = t[edges]
    first = max(0, edges.size - 1 - int(max_cycles))
    cycles = [
        np.interp(a + (b - a) * np.arange(points) / points, t, volts)
        for a, b in zip(edge_t[first:-1], edge_t[first + 1:])
    ]
    stack = np.asarray(cycles)
    period_ms = float(np.mean(np.diff(edge_t[first:]))) / 1000.0
    return stack.mean(axis=0), stack.std(axis=0), len(cycles), period_ms


class StreamReader(threading.Thread):
    """Reads D-lines off the serial port into a rolling deque.

    ALL port I/O happens on this thread - reads, the STREAM,STOP drain in
    run()'s tail, and any command queued with send() from the GUI thread. That
    keeps the GUI thread off the serial port entirely, so a mid-stream
    PWM,ON/PWM,OFF can never interleave with a readline() on another thread.
    """

    def __init__(self, rig: Esp32Rig, channel: str, buffer_s: float) -> None:
        super().__init__(daemon=True)
        self.rig = rig
        self.channel = channel
        self.rate_hz = SAMPLE_RATE_HZ
        self.line_prefix = "D,"
        self.bytes_per_line = BYTES_PER_LINE_EST
        # Sized for the WIDEST window the viewer may be switched to, not the
        # one it starts on, so widening mid-stream has real samples to show.
        maxlen = int(buffer_s * self.rate_hz * 1.25)
        # (t_us, volts, sync)
        self.data: deque[tuple] = deque(maxlen=maxlen)
        self.lock = threading.Lock()
        self.total = 0
        self.torn = 0
        self._stop = threading.Event()
        self._commands: queue.Queue[str] = queue.Queue()
        self.command_error: str | None = None
        self.lag_s = 0.0        # unread receive backlog, in seconds of stream

    def send(self, command: str) -> None:
        """Queue a firmware command to be written by this thread.

        Safe to call from the GUI thread. The firmware's acknowledgement is
        consumed and discarded by the read loop below along with any other
        non-"D," line.
        """
        self._commands.put(command)

    def _flush_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                self.rig._send(command)
            except Exception as exc:              # port died mid-session
                self.command_error = f"{command} failed: {exc}"
                return

    def run(self) -> None:
        start = "STREAM,START,REF" if self.channel == "ref" else "STREAM,START"
        self.rig._command(start, "STREAM,BEGIN")
        ser = self.rig.ser
        # Read in BULK, not with readline(): pyserial's readline() costs one
        # syscall per byte, and with the GUI thread holding the GIL for
        # redraws this thread falls behind the 1000 line/s stream. The unread
        # backlog then piles up in the enlarged OS buffer and the plot lags
        # real time by tens of seconds (emitter toggles look delayed).
        buf = bytearray()
        while not self._stop.is_set():
            self._flush_commands()
            try:
                chunk = ser.read(ser.in_waiting or 1)   # blocks <= 1 s timeout
            except Exception as exc:                    # port died mid-session
                self.command_error = f"serial read failed: {exc}"
                return
            if not chunk:
                continue
            buf += chunk
            lines = buf.split(b"\n")
            buf = bytearray(lines.pop())                # keep the partial tail
            items = []
            for raw_line in lines:
                line = raw_line.decode("ascii", errors="replace").strip()
                if not line.startswith(self.line_prefix):
                    continue
                try:
                    _, t_us, _raw, volts, sync = line.split(",")
                    items.append((int(t_us), float(volts), int(sync)))
                except ValueError:
                    self.torn += 1
            if items:
                with self.lock:
                    self.data.extend(items)
                    self.total += len(items)
            try:
                pending = ser.in_waiting + len(buf)
            except Exception:
                pending = len(buf)
            self.lag_s = pending / (self.bytes_per_line * self.rate_hz)
        # Stop the stream from this thread and drain to the END marker.
        try:
            self._flush_commands()               # e.g. a final PWM,OFF
            self.rig._send("STREAM,STOP")
            deadline = time.time() + 2.0
            while time.time() < deadline:
                nl = buf.find(b"\n")
                if nl >= 0:
                    line = bytes(buf[:nl]).decode("ascii", errors="replace")
                    del buf[:nl + 1]
                    if line.startswith("STREAM,END"):
                        break
                    continue
                chunk = ser.read(ser.in_waiting or 1)
                if not chunk:
                    break
                buf += chunk
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> tuple[list[tuple], int, int]:
        with self.lock:
            return list(self.data), self.total, self.torn


def run_viewer(rig: Esp32Rig, args, emitter_on: bool) -> bool:
    """Show the rolling plot until the window closes.

    ``emitter_on`` is the drive state at entry; the returned bool is the state
    at exit so the caller knows what the board was left doing.
    """
    reader = StreamReader(rig, args.channel, args.max_window)
    reader.start()

    where = {"ref": "reference sensor (AIN1)",
             }.get(args.channel, "DUT sensor (AIN0)")
    if getattr(args, "fe", None):
        where += f" [FE {args.fe}]"

    # The fold panel has its own x-axis (ms into the cycle), so only the
    # rolling trace and the sync strip share the "seconds ago" axis.
    fold_cycles = int(getattr(args, "fold_cycles", DEFAULT_FOLD_CYCLES))
    rows = 3 if fold_cycles > 0 else 2
    ratios = [4, 1, 2][:rows]
    fig, axes = plt.subplots(
        rows, 1, figsize=(10, 6 + (1.6 if rows == 3 else 0)),
        gridspec_kw={"height_ratios": ratios},
    )
    ax, ax_sync = axes[0], axes[1]
    ax_fold = axes[2] if rows == 3 else None
    fig.subplots_adjust(top=0.88, right=0.97, hspace=0.45)
    fig.canvas.manager.set_window_title("Eltec rig - live waveform")
    trace, = ax.plot([], [], color=CH_COLOR_A, linewidth=0.8)
    sync_trace, = ax_sync.step([], [], color="#ca8a04", linewidth=1.0, where="post")
    ax.set_ylabel("Sensor (V)")
    ax.grid(True, alpha=0.25)
    ax_sync.set_ylabel("sync")
    ax_sync.set_xlabel("Seconds ago")
    ax_sync.set_ylim(-0.2, 1.2)
    ax_sync.set_yticks([0, 1])
    ax_sync.grid(True, alpha=0.25)
    fold_trace = None
    fold_extras: list = []          # fill_between/hint artists, redrawn per frame
    if ax_fold is not None:
        fold_trace, = ax_fold.plot([], [], color="#0f766e", linewidth=1.4)
        ax_fold.set_ylabel("avg (V)")
        ax_fold.set_xlabel("ms into drive cycle (0 = PWM ON edge)")
        ax_fold.grid(True, alpha=0.25)
    stats = ax.text(0.01, 0.98, "waiting for samples...", transform=ax.transAxes,
                    va="top", ha="left", fontsize=9, family="monospace",
                    bbox={"facecolor": "white", "alpha": 0.75, "edgecolor": "#cbd5e1"})

    # Emitter state lives here; the toggle only queues a command for the reader
    # thread, so the GUI thread never touches the serial port.
    state = {"on": bool(emitter_on)}
    # Monotonic-clock instants of each switch, so the marker lines can be drawn
    # on the same "seconds ago" axis as the samples.
    switch_marks: deque[tuple[float, bool]] = deque(maxlen=64)
    mark_lines: list = []

    # How much time the plot shows. Changed live, so every x-axis decision
    # below reads view["window"] rather than the launch value in args.
    view = {"window": float(args.window)}
    ladder = sorted({float(w) for w in WINDOW_PRESETS_S if w <= args.max_window}
                    | {float(args.window), float(args.max_window)})

    window_ax = fig.add_axes([0.58, 0.92, 0.19, 0.06])
    window_button = Button(window_ax, "")
    button_ax = fig.add_axes([0.78, 0.92, 0.19, 0.06])
    button = Button(button_ax, "")

    def refresh_labels() -> None:
        on = state["on"]
        button.label.set_text(f"Emitter: {'ON' if on else 'OFF'}  [space]")
        button_ax.set_facecolor("#bbf7d0" if on else "#e5e7eb")
        window_button.label.set_text(f"Window: {view['window']:g} s  [ ] ")
        drive = (
            f"ESP32 PWM drive ON ({rig.pwm_frequency_hz:g} Hz / "
            f"{rig.pwm_duty_percent:g}% duty)" if on
            else "emitter OFF - baseline"
        )
        ax.set_title(f"{where} - {drive} - {view['window']:g} s window")

    def set_emitter(on: bool) -> None:
        if on == state["on"]:
            return
        reader.send("PWM,ON" if on else "PWM,OFF")
        state["on"] = on
        switch_marks.append((time.monotonic(), on))
        refresh_labels()
        fig.canvas.draw_idle()

    def toggle(_event=None) -> None:
        set_emitter(not state["on"])

    def set_window(seconds: float) -> None:
        seconds = min(max(seconds, ladder[0]), ladder[-1])
        if abs(seconds - view["window"]) < 1e-9:
            return
        view["window"] = seconds
        refresh_labels()
        fig.canvas.draw_idle()

    def step_window(direction: int) -> None:
        """Move one rung wider (+1) or narrower (-1) along ``ladder``."""
        current = view["window"]
        if direction > 0:
            rungs = [w for w in ladder if w > current + 1e-9]
        else:
            rungs = [w for w in reversed(ladder) if w < current - 1e-9]
        if rungs:
            set_window(rungs[0])

    def cycle_window(_event=None) -> None:
        """Button click: one rung wider, wrapping back to the narrowest."""
        rungs = [w for w in ladder if w > view["window"] + 1e-9]
        set_window(rungs[0] if rungs else ladder[0])

    def on_key(event) -> None:
        if event.key in (" ", "e", "E"):
            toggle()
        elif event.key in ("]", "+", "="):
            step_window(+1)
        elif event.key in ("[", "-", "_"):
            step_window(-1)

    button.on_clicked(toggle)
    window_button.on_clicked(cycle_window)
    fig.canvas.mpl_connect("key_press_event", on_key)
    refresh_labels()

    nominal_dt_us = 1e6 / reader.rate_hz

    def autoscale(axis, values) -> tuple[float, float]:
        lo, hi = float(np.min(values)), float(np.max(values))
        pad = max((hi - lo) * 0.15, 1e-4)        # >= 0.1 mV so a flat line shows
        axis.set_ylim(lo - pad, hi + pad)
        return lo, hi

    def update(_frame):
        data, _total, torn = reader.snapshot()
        artists = (trace, sync_trace, stats)
        if len(data) < 2:
            return artists
        t_us = np.array([d[0] for d in data], dtype=np.int64)
        volts = np.array([d[1] for d in data], dtype=float)
        sync = np.array([d[2] for d in data], dtype=float)

        # The cycle average folds the WHOLE buffer (up to --max-window of
        # history), not just the displayed window - that is its entire point.
        if ax_fold is not None:
            for artist in fold_extras:
                artist.remove()
            fold_extras.clear()
            folded = fold_sync_cycles(t_us, volts, sync, fold_cycles)
            if folded is None:
                fold_trace.set_data([], [])
                ax_fold.set_title(
                    "cycle average - needs the ESP32 drive ON (sync edges); "
                    "press SPACE", fontsize=9, loc="left", color="0.4",
                )
                fold_extras.append(ax_fold.text(
                    0.5, 0.5, "no sync edges", transform=ax_fold.transAxes,
                    ha="center", va="center", color="0.6", fontsize=10,
                ))
            else:
                mean_cycle, std_cycle, n_cycles, period_ms = folded
                phase_ms = np.arange(mean_cycle.size) * period_ms / mean_cycle.size
                fold_trace.set_data(phase_ms, mean_cycle)
                fold_extras.append(ax_fold.fill_between(
                    phase_ms, mean_cycle - std_cycle, mean_cycle + std_cycle,
                    color="#0f766e", alpha=0.15, linewidth=0,
                ))
                ax_fold.set_xlim(0, period_ms)
                autoscale(ax_fold, mean_cycle)
                ax_fold.set_title(
                    f"sync-locked average of last {n_cycles} cycles - "
                    f"mean-cycle pk-pk {np.ptp(mean_cycle) * 1000:.2f} mV "
                    "(band = per-cycle spread)",
                    fontsize=9, loc="left",
                )

        window_s = view["window"]
        newest = t_us[-1]
        t = (t_us - newest) / 1e6                # seconds relative to newest
        keep = t >= -window_s
        t, volts, sync, t_us = t[keep], volts[keep], sync[keep], t_us[keep]
        if t.size < 2:
            return artists

        sync_trace.set_data(t, sync)
        ax.set_xlim(-window_s, 0)
        ax_sync.set_xlim(-window_s, 0)
        trace.set_data(t, volts)
        autoscale(ax, volts)

        # Redraw the on/off markers at their current age. The newest sample is
        # "now" on the x-axis, so age is measured from this frame's clock.
        now = time.monotonic()
        for line in mark_lines:
            line.remove()
        mark_lines.clear()
        for stamp, turned_on in switch_marks:
            age = stamp - now                    # negative = in the past
            if age < -window_s:
                continue
            mark_lines.append(ax.axvline(
                age, color="#16a34a" if turned_on else "#dc2626",
                linewidth=1.2, alpha=0.8, linestyle="--",
            ))

        span_s = (t_us[-1] - t_us[0]) / 1e6
        rate = (t.size - 1) / span_s if span_s > 0 else 0.0
        gaps = int(np.count_nonzero(np.diff(t_us) > GAP_FACTOR * nominal_dt_us))
        problem = f"   {reader.command_error}" if reader.command_error else ""
        link = (f"rate {rate:.0f} S/s   gaps {gaps}   torn {torn}   "
                f"lag {reader.lag_s:.1f}s{problem}")
        stats.set_text(
            f"emitter {'ON ' if state['on'] else 'OFF'}   "
            f"{rig.pwm_frequency_hz:g} Hz/{rig.pwm_duty_percent:g}%   "
            f"mean {np.mean(volts):.4f} V   "
            f"pk-pk {np.ptp(volts) * 1000:.2f} mV   {link}"
        )
        return artists

    ani = FuncAnimation(fig, update, interval=FRAME_INTERVAL_MS,
                        cache_frame_data=False)
    try:
        plt.show()                                # blocks until window closed
    except KeyboardInterrupt:
        pass
    finally:
        # Turn the emitter off through the same queue before the reader stops,
        # so the drive never outlives the window even if close() is skipped.
        if state["on"]:
            reader.send("PWM,OFF")
            state["on"] = False
        reader.stop()
        reader.join(timeout=3.0)
    print(f"Stream closed: {reader.total} samples received, {reader.torn} torn lines")
    if reader.command_error:
        print(f"WARNING: {reader.command_error}")
    del ani
    return state["on"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Live rolling plot of the rig waveform")
    ap.add_argument("--port",
                    help="serial port (COM3, /dev/ttyUSB0); auto-detect if omitted")
    ap.add_argument("--channel", choices=["sensor", "ref"],
                    default="sensor",
                    help="sensor = DUT on AIN0 (default), ref = AIN1")
    ap.add_argument("-w", "--window", type=float, default=None,
                    help="STARTING rolling window in seconds (default 4, or "
                         "~6 PWM periods when --freq is below 2 Hz); change it "
                         "live with ] / [ or the Window button")
    ap.add_argument("--max-window", type=float, default=MAX_WINDOW_S,
                    help=f"widest window the live control may reach, and so "
                         f"the length of the sample history kept in memory "
                         f"(default {MAX_WINDOW_S:g} s)")
    ap.add_argument("--pwm", action="store_true",
                    help="START with the ESP32's PWM emitter drive on; either "
                         "way it can be toggled live with SPACE or the button")
    ap.add_argument("--freq", type=float,
                    help="drive frequency in Hz, applied even when starting "
                         "with the emitter off (firmware v1.9+; 405 M22 = 1, "
                         "default = board's 10 Hz)")
    ap.add_argument("--duty", type=float,
                    help="drive duty cycle in percent ON, applied even when "
                         "starting with the emitter off (firmware v3.2+; "
                         f"{PWM_MIN_DUTY_PERCENT:g}-{PWM_MAX_DUTY_PERCENT:g}, "
                         "449 M18 = 20 for the TP443 20/80 blade, default = "
                         "the board's 50 %%)")
    ap.add_argument("--fold-cycles", type=int, default=DEFAULT_FOLD_CYCLES,
                    help="cycles in the sync-locked average panel (default "
                         f"{DEFAULT_FOLD_CYCLES}; noise on the average falls "
                         "with the square root of this; 0 hides the panel)")
    ap.add_argument("--pin", type=int,
                    help="retarget the gate GPIO first (12/13/14/25/26/27/32/33)")
    ap.add_argument("--fe", choices=["v19", "v20"],
                    help="ADS1256 front end (firmware v2.1+): v20 = gain 1/"
                         "buffer off (boot default), v19 = gain 2/buffer on "
                         "(406MCA-era, clips above ~2.4 V). Reverts to v20 "
                         "when the port closes.")
    args = ap.parse_args()

    if args.window is None:
        # A 1 Hz 405 M22 waveform needs a wider window than the 10 Hz default
        # to show a few complete cycles.
        args.window = DEFAULT_WINDOW_S
        if args.freq and args.freq < 2.0:
            args.window = max(args.window, 6.0 / args.freq)
    if args.window <= 0 or args.max_window <= 0:
        sys.exit("--window and --max-window must be positive")
    # The history buffer is sized from --max-window, so it has to cover the
    # starting window even when that was auto-widened for a slow drive.
    args.max_window = max(args.max_window, args.window)

    rig = Esp32Rig(args.port or find_port())
    rig.connect()
    # Bigger OS receive buffer (Windows/pyserial) so GUI redraws can't starve
    # the serial read and silently drop stream data.
    try:
        rig.ser.set_buffer_size(rx_size=1 << 20)
    except Exception:
        pass
    try:
        # Gate pin and frequency are set up front regardless of the starting
        # drive state, so the live toggle always switches the intended output
        # at the intended frequency.
        if args.pin:
            rig._command(f"PIN,{args.pin}", f"OK,PIN,{args.pin}")
        if args.fe:
            rig.set_front_end(args.fe)
            print(f"Front end: {args.fe} = {FE_PRESETS[args.fe]}")
        if args.freq:
            rig.set_pwm_frequency(args.freq)
        if args.duty is not None:
            # Duty survives a later PWM,FREQ, but set it after the frequency
            # anyway - same order the production apps use.
            try:
                rig.set_pwm_duty(args.duty)
            except (ValueError, RuntimeError) as exc:
                sys.exit(str(exc))
        drive = f"{rig.pwm_frequency_hz:g} Hz / {rig.pwm_duty_percent:g}% duty"
        if args.pwm:
            rig.enable_emitter_pwm()
            print(f"Emitter PWM ON ({drive})")
        else:
            print(f"Emitter OFF (drive prepared at {drive})")
        print("In the plot window: SPACE or the top-right button toggles the "
              "emitter. It is always turned off on exit.")
        print(f"                    ] / [ (or + / -) change the time shown: "
              f"{args.window:g} s now, up to {args.max_window:g} s.")
        run_viewer(rig, args, emitter_on=args.pwm)
    finally:
        rig.close()          # sends PWM,OFF and closes the port


if __name__ == "__main__":
    main()
