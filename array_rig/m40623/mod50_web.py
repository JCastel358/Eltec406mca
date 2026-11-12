"""Local operator dashboard. Only the acquisition worker talks to the DAQ.

Loopback binding, an unguessable per-launch URL and same-origin JSON actions
keep ordinary web pages from starting a hardware run. No external service is
used. The saved HTML report works offline after this server is closed.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import webbrowser

import mod50_hardware as hw
from mod50_check import TrayCheck, results_root, normalize_tests, SCHEMA
from mod50_history import History
from mod50_recovery import load_noise_recovery

UI_DIR = Path(__file__).with_name("mod50_ui")


class Dashboard:
    def __init__(self, *, simulate=False, output_root=None, worker_factory=TrayCheck):
        self.simulate = simulate
        self.output_root = output_root
        self.worker_factory = worker_factory
        self.worker = None
        self.lock = threading.RLock()
        self.last_seen = time.monotonic()
        self.closed_at = None
        self.closing = False
        self.restart_required = False
        self.run_metadata = None
        self.run_id = None
        self.noise_recovery = None
        self._recovery_data = None
        self._recovery_checked_run = None
        self.saved_result = None
        # Startup only offers the most recent run for this data source. Never
        # fall back to an older tray when a newer run is not recoverable.
        root = Path(output_root) if output_root is not None else results_root(simulate)
        try:
            prefix = "SIMULATED_" if simulate else "MEASURED_"
            directories = [p for p in root.iterdir() if p.is_dir() and p.name.startswith(prefix)]
            latest = max(directories, key=lambda p: p.stat().st_mtime) if directories else None
        except OSError:
            latest = None
        self._offer_recovery(latest, simulate=simulate)
        if latest is not None:
            try:
                saved = json.loads((latest / "summary.json").read_text(encoding="utf-8"))
                if saved.get("schema") == SCHEMA and saved.get("simulated") is simulate and saved.get("current_parts"):
                    self.saved_result = saved
                    self.restart_required = bool(saved.get("restart_required"))
                    self.run_id = saved["run_id"]
                    self.run_metadata = {key: saved[key] for key in ("lot", "operator", "tray", "selected_tests")}
            except (OSError, ValueError, KeyError):
                pass

    def history(self, simulate):
        root = Path(self.output_root) if self.output_root is not None else results_root(simulate)
        return History(root, simulate)

    def preflight(self, payload):
        if not isinstance(payload, dict) or type(payload.get("simulate")) is not bool:
            raise ValueError("Select the data source")
        return {"slot_warnings": self.history(payload["simulate"]).warnings()}

    def _offer_recovery(self, directory, *, simulate):
        self.noise_recovery = self._recovery_data = None
        if directory is None or self.restart_required:
            return
        try:
            data = load_noise_recovery(directory, simulate=simulate)
        except (ValueError, OSError):
            return
        source = data["source"]
        self._recovery_data = data
        self.noise_recovery = {"id": secrets.token_urlsafe(18), "lot": source["lot"],
            "operator": source["operator"], "tray": source["tray"], "simulate": simulate,
            "created_at": source["created_at"], "requires_settling": True,
            "recorded_noise_seconds": source.get("recorded_noise_seconds", 0.),
            "dc_recorded_seconds": data["record"].get("recorded_seconds", 0.)}

    def snapshot(self):
        with self.lock:
            self.last_seen = time.monotonic()
            # A refresh also emits pagehide. Let a returning page cancel the
            # pending exit during the short close grace period.
            self.closed_at = None
            worker = self.worker
            if worker is None:
                if self.saved_result:
                    result = self.saved_result
                    return {"busy": False, "phase": "Saved check", "message": "Last saved tray. Retest a part or start the next batch.",
                            "progress": 1., "simulate": self.simulate, "report_url": None, "result": result,
                            "rows": result["rows"], "restart_required": bool(result.get("restart_required")),
                            "run_metadata": self.run_metadata, "run_id": self.run_id, "noise_recovery": self.noise_recovery}
                return {"busy": False, "phase": "Ready", "message": "Load the tray, then press Start.",
                        "progress": 0., "simulate": self.simulate, "report_url": None,
                        "result": None, "rows": [], "restart_required": False,
                        "run_metadata": None, "run_id": None, "noise_recovery": self.noise_recovery}
            state = worker.status()
            busy = worker.is_alive()
            result = worker.result if worker.finished.is_set() and not busy else None
            if result is not None:
                self.restart_required |= bool(result.get("restart_required"))
                if self._recovery_checked_run != self.run_id:
                    self._offer_recovery(worker.directory, simulate=worker.simulate)
                    self._recovery_checked_run = self.run_id
            report_exists = worker.directory and (worker.directory / "report.html").is_file()
            return {**{k: state.get(k) for k in ("phase", "message", "progress")},
                    "busy": busy, "simulate": worker.simulate, "result": result,
                    "rows": result["rows"] if result else state.get("rows", []),
                    "report_url": "report/report.html" if report_exists and not busy else None,
                    "restart_required": self.restart_required,
                    "run_metadata": dict(self.run_metadata) if self.run_metadata else None,
                    "run_id": self.run_id, "noise_recovery": None if busy else self.noise_recovery}

    def start(self, payload):
        return self._begin(payload)

    def _begin(self, payload, *, resume_dc=None, part_mapping=None, family_id=None):
        with self.lock:
            if self.closing or self.closed_at is not None:
                raise ValueError("The app is closing. Open Eltec Array Rig again.")
            if self.worker is not None and self.worker.is_alive():
                raise ValueError("A check is already running.")
            if self.worker and self.worker.result:
                self.restart_required |= bool(self.worker.result.get("restart_required"))
            if self.restart_required:
                raise ValueError("Close and restart this app after the driver shutdown error.")
            if not isinstance(payload, dict):
                raise ValueError("Invalid start request")
            if payload.get("model", "40623") != "40623":
                raise ValueError("Model 40623 is the only supported model for this tester.")
            simulate = payload.get("simulate", self.simulate)
            if type(simulate) is not bool:
                raise ValueError("Select Actual detectors or Generated demo")
            lot, operator = payload.get("lot", ""), payload.get("operator", "")
            if not isinstance(lot, str) or not isinstance(operator, str) or max(len(lot), len(operator)) > 120:
                raise ValueError("Enter a lot and technician name of at most 120 characters")
            selected_tests = normalize_tests(payload.get("selected_tests", ["noise"]))
            history = self.history(simulate)
            warnings = history.warnings()
            suspect = {w["position"] for w in warnings}
            repaired = set(hw.normalize_positions(payload.get("repair_positions", [])))
            if not repaired.issubset(suspect):
                raise ValueError("The slot warning changed; review it again before starting.")
            if suspect and payload.get("slot_warning_acknowledged") is not True:
                raise ValueError("Review the suspect-slot warning before starting.")
            blocked = suspect - repaired
            positions = hw.normalize_positions(payload.get("installed_positions", hw.POSITIONS))
            if part_mapping:
                positions = hw.normalize_positions(part_mapping)
                if blocked.intersection(positions):
                    raise ValueError("Choose a different retest slot, or verify the flagged slot has been repaired.")
            else:
                positions = tuple(p for p in positions if p not in blocked)
            kwargs = dict(lot=lot, operator=operator, tray=payload.get("tray", 1),
                          simulate=simulate, output_root=self.output_root, selected_tests=selected_tests,
                          installed_positions=positions, part_mapping=part_mapping, family_id=family_id,
                          excluded_suspect_slots=sorted(blocked))
            if resume_dc is not None:
                source = resume_dc["source"]
                if blocked.intersection(source["installed_positions"]):
                    raise ValueError("A saved recovery includes a suspect slot. Use a fresh retest after moving the part.")
                kwargs.update(resume_dc=resume_dc, installed_positions=source["installed_positions"],
                              selected_tests=("dc", "noise"), part_mapping=source.get("part_mapping"),
                              family_id=source.get("family_id"))
            worker = self.worker_factory(**kwargs)
            if repaired:
                history.verify_repairs(repaired, operator.strip())
            worker.start()
            self.worker = worker
            self.last_seen = time.monotonic()
            # Browser refreshes can restore the active batch, and multiple
            # tabs can distinguish a fresh run from an already reviewed one.
            # A rejected request must preserve the previous run's identity.
            self.run_metadata = {"model": "40623", "lot": lot.strip(), "operator": operator.strip(),
                                 "tray": payload.get("tray", 1), "selected_tests": list(kwargs["selected_tests"])}
            self.run_id = secrets.token_urlsafe(18)
            self.noise_recovery = self._recovery_data = None
            self.saved_result = None

    def retest(self, payload):
        with self.lock:
            if not isinstance(payload, dict) or payload.get("confirm_moved") is not True:
                raise ValueError("Confirm the same parts were moved using the fixture's power-off procedure and the fixture is ready.")
            result = self.worker.result if self.worker and not self.worker.is_alive() else self.saved_result
            if not result or not result.get("run_id") or payload.get("source_run_id") != result["run_id"]:
                raise ValueError("These retest results are no longer current. Refresh the tray.")
            parts = {p["part_id"]: p for p in self.history(result["simulated"]).current_parts(result["lot"], result["family_id"])}
            moves = payload.get("moves")
            if not isinstance(moves, list) or not moves or any(not isinstance(m, dict) for m in moves):
                raise ValueError("Select at least one part and its new slot.")
            destinations, identifiers = [m.get("position") for m in moves], [m.get("part_id") for m in moves]
            hw.normalize_positions(destinations)
            if any(not isinstance(p, str) or p not in parts for p in identifiers) or len(set(identifiers)) != len(identifiers):
                raise ValueError("Each original part can appear only once in a retest.")
            self._begin({**payload, "lot": result["lot"], "operator": result["operator"], "tray": result["tray"],
                         "simulate": result["simulated"], "model": "40623"},
                        part_mapping=dict(zip(destinations, identifiers)), family_id=result["family_id"])

    def retry_noise(self, payload):
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                raise ValueError("A check is already running.")
            if not isinstance(payload, dict) or payload.get("confirm_unchanged") is not True:
                raise ValueError("Confirm that the same detectors, wiring, alignment and uninterrupted fixture power are unchanged.")
            offer = self.noise_recovery
            if not offer or not self._recovery_data or payload.get("recovery_id") != offer["id"]:
                raise ValueError("This interrupted run is no longer available for noise retry.")
            # Verify saved evidence again at the operator action; a startup
            # listing is not authorization to use files that changed later.
            fresh = load_noise_recovery(self._recovery_data["raw_path"].parent, simulate=offer["simulate"])
            if fresh["source"]["summary_sha256"] != self._recovery_data["source"]["summary_sha256"]:
                raise ValueError("The saved run changed. Reopen the app before retrying.")
            source = fresh["source"]
            self._begin({"model": "40623", "lot": source["lot"], "operator": source["operator"],
                         "tray": source["tray"], "simulate": offer["simulate"],
                         "slot_warning_acknowledged": payload.get("slot_warning_acknowledged"),
                         "repair_positions": payload.get("repair_positions", [])}, resume_dc=fresh)

    def stop(self):
        with self.lock:
            if self.worker is not None:
                self.worker.stop()

    def close(self):
        with self.lock:
            self.closed_at = time.monotonic()
            self.stop()

    def should_shutdown(self):
        with self.lock:
            # Closing the page requests Stop, but the worker must finish
            # shutdown and saving before the server releases the launcher.
            if self.worker is not None and self.worker.is_alive():
                return False
            now = time.monotonic()
            if ((self.closed_at is not None and now - self.closed_at >= 2)
                    or now - self.last_seen > 1800):
                self.closing = True
            return self.closing


def make_server(dashboard, *, port=0):
    token = secrets.token_urlsafe(24)
    prefix = "/" + token + "/"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, body, content_type="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, allow_nan=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def route(self):
            expected_host = f"127.0.0.1:{self.server.server_port}"
            path = urlsplit(self.path).path
            if self.headers.get("Host") != expected_host or not path.startswith(prefix):
                self.respond(404, {"error": "Not found"})
                return None
            return path[len(prefix):]

        def do_GET(self):
            route = self.route()
            if route is None:
                return
            if route == "api/state":
                self.respond(200, dashboard.snapshot())
                return
            static = {"": "index.html", "index.html": "index.html", "app.js": "app.js", "styles.css": "styles.css",
                      "assets/eltec-logo-transparent.webp": "assets/eltec-logo-transparent.webp"}
            path = UI_DIR / static[route] if route in static else None
            if route.startswith("report/"):
                name = route[7:]
                if name in ("report.html", "results.csv", "summary.json", "dc_raw.npz", "noise_raw.npz"):
                    worker = dashboard.worker
                    if worker and not worker.is_alive() and worker.directory:
                        path = worker.directory / name
            if path is None or not path.is_file():
                self.respond(404, {"error": "Not found"})
                return
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.respond(200, path.read_bytes(), mime + ("; charset=utf-8" if mime.startswith("text/") else ""))

        def do_POST(self):
            # Consume bounded request bodies even when rejecting the action.
            # Closing a Windows socket with unread incoming data can reset it
            # before the browser receives the useful 400/403 response.
            try:
                length = int(self.headers.get("Content-Length", "0"))
                self.connection.settimeout(2)
                raw = self.rfile.read(length) if 0 < length <= 65536 else b""
                if length <= 0 or length > 65536:
                    # A malformed client can declare zero yet send bytes.
                    # Drain only data already arriving, under a short bounded
                    # deadline, so Windows does not reset the socket before
                    # the caller receives our rejection. No action is allowed
                    # for these lengths even if this drain obtains valid JSON.
                    self.connection.settimeout(.05)
                    drained = 0
                    deadline = time.monotonic() + .1
                    while drained < 65536 and time.monotonic() < deadline:
                        try:
                            extra = self.rfile.read1(min(8192, 65536 - drained))
                        except (TimeoutError, OSError):
                            break
                        if not extra:
                            break
                        drained += len(extra)
                    self.connection.settimeout(2)
            except (ValueError, OSError):
                self.respond(400, {"error": "Invalid request body"})
                return
            route = self.route()
            if route is None:
                return
            origin = self.headers.get("Origin")
            if origin != f"http://127.0.0.1:{self.server.server_port}" or self.headers.get_content_type() != "application/json":
                self.respond(403, {"error": "Only this local dashboard can control acquisition"})
                return
            try:
                if not 0 < length <= 16384:
                    raise ValueError("Invalid request size")
                payload = json.loads(raw)
                if route == "api/start":
                    dashboard.start(payload)
                elif route == "api/preflight":
                    self.respond(200, dashboard.preflight(payload))
                    return
                elif route == "api/retest":
                    dashboard.retest(payload)
                elif route == "api/retry-noise":
                    dashboard.retry_noise(payload)
                elif route == "api/stop":
                    dashboard.stop()
                elif route == "api/close":
                    dashboard.close()
                else:
                    self.respond(404, {"error": "Not found"})
                    return
                self.respond(200, {"ok": True})
            except (ValueError, TypeError, OSError) as exc:
                self.respond(400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.dashboard_url = f"http://127.0.0.1:{server.server_port}{prefix}"
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args(argv)
    dashboard = Dashboard(simulate=args.simulate or os.environ.get("ELTEC_ARRAY_SIMULATE") == "1", output_root=args.output_root)
    server = make_server(dashboard)
    done = threading.Event()
    def watch_browser():
        while not done.wait(1):
            # Browsers throttle background tabs. A missing poll is not an
            # operator Stop; pagehide explicitly requests a graceful close.
            if dashboard.should_shutdown():
                server.shutdown()
                return
    watcher = threading.Thread(target=watch_browser, daemon=True)
    watcher.start()
    try:
        print("Eltec array dashboard: " + server.dashboard_url, flush=True)
        webbrowser.open(server.dashboard_url)
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        dashboard.stop()
        if dashboard.worker is not None:
            dashboard.worker.join()
        done.set()
        server.server_close()
    return 0
