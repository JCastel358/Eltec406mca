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
about).

Usage:
    python live_waveform.py                     # DUT sensor (AIN0), no drive change
    python live_waveform.py --pwm               # also turn the ESP32 PWM drive on
    python live_waveform.py --pwm --pin 26      # retarget the gate pin first
    python live_waveform.py --channel ref       # reference sensor (AIN1)
    python live_waveform.py -w 8                # 8 s rolling window (default 4)
    add --port COM3 (or /dev/ttyUSB0) to skip auto-detection

Close the plot window (or Ctrl+C) to stop - the stream and PWM are shut down
cleanly. Needs matplotlib:  pip install matplotlib
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import deque

import numpy as np

try:
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
except ImportError:
    sys.exit("matplotlib is not installed. Run:  pip install matplotlib")

from esp32_rig_readout import SAMPLE_RATE_HZ, Esp32Rig, find_port

DEFAULT_WINDOW_S = 4.0
FRAME_INTERVAL_MS = 50          # ~20 redraws/s
GAP_FACTOR = 2.5                # dt > this x nominal period counts as a gap


class StreamReader(threading.Thread):
    """Reads D-lines off the serial port into a rolling deque.

    All port READS happen on this thread (including the STREAM,STOP drain in
    run()'s tail), so the GUI thread never blocks on serial I/O.
    """

    def __init__(self, rig: Esp32Rig, channel: str, window_s: float) -> None:
        super().__init__(daemon=True)
        self.rig = rig
        self.channel = channel
        maxlen = int(window_s * SAMPLE_RATE_HZ * 1.25)
        self.data: deque[tuple[int, float, int]] = deque(maxlen=maxlen)
        self.lock = threading.Lock()
        self.total = 0
        self.torn = 0
        self._stop = threading.Event()

    def run(self) -> None:
        start = "STREAM,START,REF" if self.channel == "ref" else "STREAM,START"
        self.rig._command(start, "STREAM,BEGIN")
        while not self._stop.is_set():
            line = self.rig._readline()
            if not line.startswith("D,"):
                continue
            try:
                _, t_us, _raw, volts, sync = line.split(",")
                item = (int(t_us), float(volts), int(sync))
            except ValueError:
                self.torn += 1
                continue
            with self.lock:
                self.data.append(item)
                self.total += 1
        # Stop the stream from this thread and drain to the END marker.
        try:
            self.rig._send("STREAM,STOP")
            deadline = time.time() + 2.0
            while time.time() < deadline:
                line = self.rig._readline()
                if line.startswith("STREAM,END") or not line:
                    break
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> tuple[list[tuple[int, float, int]], int, int]:
        with self.lock:
            return list(self.data), self.total, self.torn


def run_viewer(rig: Esp32Rig, args) -> None:
    reader = StreamReader(rig, args.channel, args.window)
    reader.start()

    where = "reference sensor (AIN1)" if args.channel == "ref" else "DUT sensor (AIN0)"
    drive = "ESP32 PWM drive ON" if args.pwm else "external drive (sync flat)"
    fig, (ax, ax_sync) = plt.subplots(
        2, 1, sharex=True, figsize=(10, 6),
        gridspec_kw={"height_ratios": [4, 1]},
    )
    fig.canvas.manager.set_window_title("Eltec rig - live waveform")
    ax.set_title(f"{where} - {drive}")
    trace, = ax.plot([], [], color="#0284c7", linewidth=0.8)
    sync_trace, = ax_sync.step([], [], color="#ca8a04", linewidth=1.0, where="post")
    ax.set_ylabel("Sensor (V)")
    ax.grid(True, alpha=0.25)
    ax_sync.set_ylabel("sync")
    ax_sync.set_xlabel("Seconds ago")
    ax_sync.set_ylim(-0.2, 1.2)
    ax_sync.set_yticks([0, 1])
    ax_sync.grid(True, alpha=0.25)
    stats = ax.text(0.01, 0.98, "waiting for samples...", transform=ax.transAxes,
                    va="top", ha="left", fontsize=9, family="monospace",
                    bbox={"facecolor": "white", "alpha": 0.75, "edgecolor": "#cbd5e1"})

    nominal_dt_us = 1e6 / SAMPLE_RATE_HZ

    def update(_frame):
        data, _total, torn = reader.snapshot()
        if len(data) < 2:
            return trace, sync_trace, stats
        t_us = np.array([d[0] for d in data], dtype=np.int64)
        volts = np.array([d[1] for d in data], dtype=float)
        sync = np.array([d[2] for d in data], dtype=float)
        t = (t_us - t_us[-1]) / 1e6              # seconds relative to newest
        keep = t >= -args.window
        t, volts, sync, t_us = t[keep], volts[keep], sync[keep], t_us[keep]
        if t.size < 2:
            return trace, sync_trace, stats

        trace.set_data(t, volts)
        sync_trace.set_data(t, sync)
        vmin, vmax = float(np.min(volts)), float(np.max(volts))
        pad = max((vmax - vmin) * 0.15, 1e-4)    # >= 0.1 mV so a flat line shows
        ax.set_xlim(-args.window, 0)
        ax.set_ylim(vmin - pad, vmax + pad)

        span_s = (t_us[-1] - t_us[0]) / 1e6
        rate = (t.size - 1) / span_s if span_s > 0 else 0.0
        gaps = int(np.count_nonzero(np.diff(t_us) > GAP_FACTOR * nominal_dt_us))
        stats.set_text(
            f"mean {np.mean(volts):.4f} V   pk-pk {(vmax - vmin) * 1000:.2f} mV   "
            f"rate {rate:.0f} S/s   gaps {gaps}   torn {torn}"
        )
        return trace, sync_trace, stats

    ani = FuncAnimation(fig, update, interval=FRAME_INTERVAL_MS,
                        cache_frame_data=False)
    try:
        plt.show()                                # blocks until window closed
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        reader.join(timeout=3.0)
    print(f"Stream closed: {reader.total} samples received, {reader.torn} torn lines")
    del ani


def main() -> None:
    ap = argparse.ArgumentParser(description="Live rolling plot of the rig waveform")
    ap.add_argument("--port",
                    help="serial port (COM3, /dev/ttyUSB0); auto-detect if omitted")
    ap.add_argument("--channel", choices=["sensor", "ref"], default="sensor",
                    help="sensor = DUT on AIN0 (default), ref = AIN1")
    ap.add_argument("-w", "--window", type=float, default=DEFAULT_WINDOW_S,
                    help="rolling window in seconds (default 4)")
    ap.add_argument("--pwm", action="store_true",
                    help="turn the ESP32's own 10 Hz PWM emitter drive on while viewing")
    ap.add_argument("--pin", type=int,
                    help="with --pwm: retarget the gate GPIO first (12/13/14/25/26/27/32/33)")
    args = ap.parse_args()

    rig = Esp32Rig(args.port or find_port())
    rig.connect()
    # Bigger OS receive buffer (Windows/pyserial) so GUI redraws can't starve
    # the serial read and silently drop stream data.
    try:
        rig.ser.set_buffer_size(rx_size=1 << 20)
    except Exception:
        pass
    try:
        if args.pwm:
            if args.pin:
                rig._command(f"PIN,{args.pin}", f"OK,PIN,{args.pin}")
            rig.enable_emitter_pwm()
            print("Emitter PWM ON (10 Hz) - it turns off again on exit")
        run_viewer(rig, args)
    finally:
        rig.close()          # sends PWM,OFF and closes the port


if __name__ == "__main__":
    main()
