#!/usr/bin/env python3
"""R4 two-mode capture, replay and operator window. Legacy profile stays default."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import queue
import threading
import time

from r4_frontend import R4Rig, analyze_capture, load_capture, save_capture


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-mode", choices=("dc", "noise"), default="dc")
    parser.add_argument("--board-serial", default="ENGINEERING_UNQUALIFIED")
    parser.add_argument("--dedicated-r4-dio", action="store_true",
                        help="declare DIO0..15 exclusively wired to R4; required for hardware")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--output", type=Path, default=Path("r4_capture.npz"))
    parser.add_argument("--seconds", type=float, default=100.)
    parser.add_argument("--hz", type=float, default=2000.)
    parser.add_argument("--range", type=int, choices=range(8), default=None)
    parser.add_argument("--band", nargs=2, type=float, default=(1., 100.), metavar=("LOW_HZ", "HIGH_HZ"))
    parser.add_argument("--analysis", choices=("band", "3hz_q3"), default="band")
    parser.add_argument("--diagnostic-reason", default="", help="save an explicitly unsettled diagnostic capture")
    return parser


def report(path: Path, cap, metadata, args) -> dict:
    analysis = analyze_capture(cap, metadata, band=tuple(args.band), profile=args.analysis)
    output = path.with_suffix(".analysis.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(analysis, indent=2, allow_nan=False), encoding="utf-8")
    return analysis


def run_gui(args) -> int:
    """One worker owns the DAQ, including connect/mode/capture/close.

    Reset clears displayed statistics only. The reconnect/detector-change
    control re-applies the analog settling gate. Save precedes analysis.
    """
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title("ELTEC | 50 Sensor R4")
    root.geometry("900x620")
    root.configure(bg="#edf2f8")
    status = tk.StringVar(value="Connect the dedicated R4 control harness and analog power.")
    mode = tk.StringVar(value=args.frontend_mode)
    band = tk.StringVar(value="1–100 Hz" if args.analysis == "band" else "3 Hz / Q=3")
    jobs, events = queue.Queue(), queue.Queue()
    stop = threading.Event()
    closing = threading.Event()
    state = {"busy": False, "connected": False, "mode": None, "exit_code": 0}

    def worker():
        rig = None
        try:
            cal = json.loads(args.calibration.read_text(encoding="utf-8")) if args.calibration else None
            rig = R4Rig(board_serial=args.board_serial, dedicated_ports=args.dedicated_r4_dio or args.simulate,
                        simulate=args.simulate, calibration=cal, scan_hz=args.hz, quiet=True)
            rig.connect()
            rig.set_frontend_mode(args.frontend_mode)
            if args.range is not None:
                rig.set_range(args.range)
            events.put(("connected", rig.mode))
            while not closing.is_set():
                try:
                    action, value = jobs.get(timeout=.2)
                except queue.Empty:
                    continue
                try:
                    if action == "mode":
                        rig.set_frontend_mode(value)
                    elif action == "detectors":
                        rig.detector_connection_changed()
                    elif action == "capture":
                        while rig.settling_remaining_s and not stop.is_set():
                            events.put(("status", f"Settling {rig.mode}: {rig.settling_remaining_s:.0f} s remaining"))
                            stop.wait(min(1., rig.settling_remaining_s))
                        if stop.is_set():
                            events.put(("status", "Stopped before capture; no result recorded."))
                            continue
                        cap, metadata = rig.capture_r4(args.seconds, cancelled=stop.is_set,
                            progress=lambda elapsed: events.put(("status", f"Capturing {elapsed:.1f} / {args.seconds:g} s")))
                        path = args.output.with_name(args.output.stem + time.strftime("_%Y%m%d_%H%M%S") + ".npz")
                        save_capture(path, cap, metadata)
                        args.analysis = value
                        analysis = report(path, cap, metadata, args)
                        events.put(("result", (path, analysis)))
                    if action != "capture":
                        events.put(("status", f"{rig.mode.upper()} selected. Settling: {rig.settling_remaining_s:.0f} s. No acceptance limits."))
                except Exception as exc:
                    events.put(("fault", str(exc)))
                finally:
                    events.put(("mode", rig.mode))
                    events.put(("idle", None))
        except Exception as exc:
            events.put(("fault", str(exc)))
        finally:
            if rig is not None:
                try:
                    rig.close()
                except Exception as exc:
                    events.put(("fault", "Shutdown: " + str(exc)))
            events.put(("closed", None))

    def submit(action, value=None):
        if action == "mode":
            # Display the last confirmed mode until the owner commits the change.
            # A quick second selection or failed write must not mislabel hardware.
            mode.set(state["mode"] or "Unavailable")
        if state["busy"] or not state["connected"]:
            return
        stop.clear()
        state["busy"] = True
        jobs.put((action, value))

    tk.Label(root, text="ELTEC  |  50 Sensor R4" + ("  •  SIMULATION" if args.simulate else ""),
             bg="#133775", fg="white", font=("Segoe UI", 19, "bold"), pady=18).pack(fill="x")
    controls = ttk.Frame(root, padding=12)
    controls.pack(fill="x")
    ttk.Label(controls, text="Global measurement mode").pack(side="left")
    selector = ttk.Combobox(controls, textvariable=mode, values=("dc", "noise"), state="readonly", width=12)
    selector.pack(side="left", padx=8)
    selector.bind("<<ComboboxSelected>>", lambda _: submit("mode", mode.get()))
    ttk.Combobox(controls, textvariable=band, values=("1–100 Hz", "3 Hz / Q=3"), state="readonly", width=15).pack(side="left")
    ttk.Button(controls, text="Capture", command=lambda: submit("capture", "band" if band.get() == "1–100 Hz" else "3hz_q3")).pack(side="left", padx=8)
    ttk.Button(controls, text="Stop", command=stop.set).pack(side="left")
    ttk.Button(controls, text="Detectors changed", command=lambda: submit("detectors")).pack(side="left", padx=8)
    ttk.Label(root, textvariable=status, padding=12, wraplength=870).pack(fill="x")
    table = ttk.Treeview(root, columns=("channel", "value", "state"), show="headings", height=20)
    for key, text in (("channel", "Channel"), ("value", "Reading"), ("state", "Quality / acceptance")):
        table.heading(key, text=text)
    table.pack(fill="both", expand=True, padx=12, pady=8)
    ttk.Button(root, text="Reset statistics", command=lambda: table.delete(*table.get_children())).pack(pady=8)

    def close():
        closing.set()
        stop.set()
        status.set("Stopping acquisition and returning controls to DC…")

    def poll():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == "status":
                    status.set(value)
                elif kind == "fault":
                    state["exit_code"] = 2
                    status.set(value)
                elif kind == "connected":
                    state["connected"] = True
                    state["mode"] = value
                    mode.set(value)
                elif kind == "mode":
                    state["mode"] = value
                    mode.set(value or "Unavailable")
                elif kind == "idle":
                    state["busy"] = False
                elif kind == "result":
                    path, analysis = value
                    if analysis.get("status") == "INVALID" or any(row["status"] == "INVALID" for row in analysis["channels"]):
                        state["exit_code"] = 2
                    table.delete(*table.get_children())
                    for row in analysis["channels"]:
                        reading = (f'{row["input_dc_v"]:.6f} V' if "input_dc_v" in row else
                                   f'{row["input_band_rms_uv"]:.4f} µV RMS' if "input_band_rms_uv" in row else
                                   f'{row["raw_mean_v"]:.6f} V at DAQ' if row.get("raw_mean_v") is not None else "Invalid samples")
                        table.insert("", "end", values=(row["channel"], reading, row["status"] + " / NO_LIMIT"))
                    status.set(f"Saved {path}")
                elif kind == "closed":
                    state["connected"] = False
                    state["mode"] = None
                    mode.set("Unavailable")
                    if closing.is_set():
                        root.destroy()
                        return
        except queue.Empty:
            pass
        selector.configure(state="disabled" if state["busy"] or not state["connected"] else "readonly")
        if closing.is_set() and not thread.is_alive():
            root.destroy()
            return
        root.after(100, poll)

    thread = threading.Thread(target=worker, name="r4-device-owner", daemon=False)
    thread.start()
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, poll)
    root.mainloop()
    thread.join(timeout=5.)
    return 2 if thread.is_alive() else state["exit_code"]


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.replay:
            cap, metadata = load_capture(args.replay)
            analysis = report(args.output, cap, metadata, args)
        elif args.gui:
            return run_gui(args)
        else:
            cal = json.loads(args.calibration.read_text(encoding="utf-8")) if args.calibration else None
            with R4Rig(board_serial=args.board_serial, dedicated_ports=args.dedicated_r4_dio or args.simulate,
                       simulate=args.simulate, calibration=cal, scan_hz=args.hz) as rig:
                rig.set_frontend_mode(args.frontend_mode)
                if args.range is not None:
                    rig.set_range(args.range)
                while rig.settling_remaining_s and not args.diagnostic_reason:
                    print(f"Settling: {rig.settling_remaining_s:.0f} seconds remain", flush=True)
                    time.sleep(min(1., rig.settling_remaining_s))
                cap, metadata = rig.capture_r4(args.seconds, diagnostic_reason=args.diagnostic_reason)
                save_capture(args.output, cap, metadata)
                analysis = report(args.output, cap, metadata, args)
        print(json.dumps({"acceptance": "NO_LIMIT", "channels": [
            {key: value for key, value in row.items() if key not in ("frequency_hz", "input_psd_uv2_per_hz")}
            for row in analysis["channels"]]}, indent=2))
        return 2 if any(row["status"] == "INVALID" for row in analysis["channels"]) or analysis.get("status") == "INVALID" else 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"R4: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
