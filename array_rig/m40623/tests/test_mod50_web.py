"""Loopback dashboard actions and ownership; no driver or physical hardware."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mod50_web as web


class FakeWorker:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.simulate = kwargs["simulate"]
        self.directory = None
        self.result = None
        self.finished = threading.Event()
        self.alive = False
        self.stops = 0

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def status(self):
        return {"phase": "Recording DC" if self.alive else "Complete", "message": "Test fixture",
                "progress": .25, "rows": [{"position": "1-1", "dc_v": .7}]}

    def stop(self):
        self.stops += 1

    def complete(self, *, restart_required=False):
        self.result = {"data_complete": True, "rows": [{"position": "1-1", "dc_v": .7}],
                       "restart_required": restart_required}
        self.alive = False
        self.finished.set()


class DashboardHttp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.created = []
        def factory(**kwargs):
            worker = FakeWorker(**kwargs)
            self.created.append(worker)
            return worker
        self.dashboard = web.Dashboard(output_root=self.directory, worker_factory=factory)
        self.server = web.make_server(self.dashboard)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        self.thread.start()
        parsed = urlsplit(self.server.dashboard_url)
        self.prefix, self.origin = parsed.path, f"http://{parsed.netloc}"
        self.host = parsed.netloc

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, route="api/state", *, method="GET", data=None, body=None, headers=None, raw_path=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        request_headers = {"Host": self.host}
        if method == "POST":
            request_headers.update(Origin=self.origin, **{"Content-Type": "application/json"})
            if body is None:
                body = json.dumps({} if data is None else data)
        request_headers.update(headers or {})
        conn.request(method, raw_path or self.prefix + route, body=body, headers=request_headers)
        response = conn.getresponse()
        code, result_headers, raw = response.status, dict(response.getheaders()), response.read()
        conn.close()
        if result_headers.get("Content-Type", "").startswith("application/json"):
            raw = json.loads(raw)
        return code, result_headers, raw

    def start(self, **overrides):
        payload = {"lot": "LOT-TEST", "operator": "Test only", "tray": 1, "simulate": False}
        payload.update(overrides)
        return self.request("api/start", method="POST", data=payload)

    def test_get_state_never_constructs_hardware_and_updates_heartbeat(self):
        self.dashboard.last_seen = 0
        code, headers, state = self.request()
        self.assertEqual(code, 200)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertFalse(state["busy"])
        self.assertFalse(state["simulate"])
        self.assertIsNone(state["result"])
        self.assertIsNone(state["run_metadata"])
        self.assertIsNone(state["run_id"])
        self.assertEqual(self.created, [])
        self.assertGreater(self.dashboard.last_seen, 0)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_start_stop_and_completion_keep_single_worker_owner(self):
        self.assertEqual(self.start()[0], 200)
        worker = self.created[0]
        self.assertTrue(self.request()[2]["busy"])
        self.assertEqual(self.request()[2]["rows"][0]["dc_v"], .7)
        self.assertEqual(self.start()[0], 400)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.request("api/stop", method="POST")[0], 200)
        self.assertEqual(worker.stops, 1)
        self.assertTrue(worker.is_alive(), "Stop should request cancellation, not drop ownership")
        worker.complete()
        state = self.request()[2]
        self.assertFalse(state["busy"])
        self.assertTrue(state["result"]["data_complete"])

    def test_simultaneous_starts_cannot_create_two_workers(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            codes = list(executor.map(lambda _: self.start()[0], range(2)))
        self.assertEqual(sorted(codes), [200, 400])
        self.assertEqual(len(self.created), 1)

    def test_run_identity_and_metadata_survive_polling_completion_and_rejected_start(self):
        self.assertEqual(self.start(lot=" LOT-42 ", operator=" Technician ", tray=3, model="40623")[0], 200)
        state = self.request()[2]
        self.assertEqual(state["run_metadata"], {"model": "40623", "lot": "LOT-42", "operator": "Technician", "tray": 3})
        first_id = state["run_id"]
        self.assertIsInstance(first_id, str)
        self.assertTrue(first_id)
        self.assertEqual(self.start(lot="SHOULD NOT REPLACE")[0], 400)
        self.assertEqual(self.request()[2]["run_id"], first_id)
        self.created[-1].complete()
        finished = self.request()[2]
        self.assertEqual(finished["run_id"], first_id)
        self.assertEqual(finished["run_metadata"], state["run_metadata"])
        self.assertEqual(self.start(lot="LOT-43")[0], 200)
        self.assertNotEqual(self.request()[2]["run_id"], first_id)
        self.assertEqual(self.request()[2]["run_metadata"]["lot"], "LOT-43")

    def test_unsupported_model_cannot_create_a_worker(self):
        for model in ("406MCA", "40623 ", 40623, None, [], {}):
            with self.subTest(model=model):
                code, _, reply = self.start(model=model)
                self.assertEqual(code, 400)
                self.assertIn("40623", reply["error"])
        self.assertEqual(self.created, [])
        self.assertIsNone(self.request()[2]["run_id"])

    def test_failed_worker_start_keeps_previous_run_identity(self):
        self.start()
        self.created[-1].complete()
        previous = self.dashboard.snapshot()
        with patch.object(FakeWorker, "start", side_effect=ValueError("Cannot start worker")):
            self.assertEqual(self.start(lot="NEW-LOT")[0], 400)
        current = self.dashboard.snapshot()
        self.assertEqual(current["run_id"], previous["run_id"])
        self.assertEqual(current["run_metadata"], previous["run_metadata"])
        self.assertEqual(current["result"], previous["result"])

    def test_generated_and_actual_source_is_explicit_boolean(self):
        self.assertEqual(self.start(simulate=True)[0], 200)
        self.assertTrue(self.created[-1].simulate)
        self.assertTrue(self.request()[2]["simulate"])
        self.created[-1].complete()
        self.assertEqual(self.start(simulate=False)[0], 200)
        self.assertFalse(self.created[-1].simulate)
        self.assertEqual(self.created[-1].options["output_root"], self.directory)

    def test_truthy_string_cannot_select_a_data_source(self):
        for value in ("false", "true", 1, 0, None):
            with self.subTest(value=value):
                self.assertEqual(self.start(simulate=value)[0], 400)
        self.assertEqual(self.created, [])

    def test_stop_without_a_run_is_idempotent(self):
        self.assertEqual(self.request("api/stop", method="POST")[0], 200)
        self.assertEqual(self.created, [])

    def test_requires_per_launch_path_and_expected_host(self):
        for path in ("/api/state", "/not-the-token/api/state", self.prefix.rstrip("/")):
            with self.subTest(path=path):
                self.assertEqual(self.request(raw_path=path)[0], 404)
        self.assertEqual(self.request(headers={"Host": "attacker.invalid"})[0], 404)
        self.assertEqual(self.request("api/start", method="POST", raw_path="/api/start")[0], 404)
        self.assertEqual(self.created, [])

    def test_cross_origin_and_non_json_actions_are_refused(self):
        for origin in ("null", "http://attacker.invalid", "http://localhost:1", ""):
            with self.subTest(origin=origin):
                self.assertEqual(self.request("api/start", method="POST", headers={"Origin": origin})[0], 403)
        self.assertEqual(self.request("api/start", method="POST", headers={"Content-Type": "text/plain"})[0], 403)
        self.assertEqual(self.created, [])

    def test_bad_json_size_and_structure_cannot_start(self):
        self.assertEqual(self.request("api/start", method="POST", body="{")[0], 400)
        self.assertEqual(self.request("api/start", method="POST", body="[]")[0], 400)
        self.assertEqual(self.request("api/start", method="POST", body=" ", headers={"Content-Length": "0"})[0], 400)
        self.assertEqual(self.request("api/start", method="POST", body="x" * 16385)[0], 400)
        self.assertEqual(self.start(operator=12)[0], 400)
        self.assertEqual(self.start(lot="x" * 121)[0], 400)
        self.assertEqual(self.created, [])

    def test_get_cannot_invoke_actions(self):
        self.assertEqual(self.request("api/start")[0], 404)
        self.assertEqual(self.request("api/stop")[0], 404)
        self.assertEqual(self.created, [])

    def test_report_is_available_only_when_worker_releases_ownership(self):
        self.start()
        worker = self.created[-1]
        worker.directory = self.directory
        (self.directory / "report.html").write_text("<h1>Test report</h1>", encoding="utf-8")
        self.assertEqual(self.request("report/report.html")[0], 404)
        self.assertIsNone(self.request()[2]["report_url"])
        worker.complete()
        code, _, report = self.request("report/report.html")
        self.assertEqual(code, 200)
        self.assertIn(b"Test report", report)
        self.assertEqual(self.request()[2]["report_url"], "report/report.html")

    def test_report_and_static_paths_never_allow_traversal_or_arbitrary_files(self):
        self.start()
        worker = self.created[-1]
        worker.directory = self.directory
        worker.complete()
        (self.directory / "private.txt").write_text("do not serve", encoding="utf-8")
        for route in ("report/private.txt", "report/../private.txt", "../private.txt",
                      "report/%2e%2e/private.txt", "assets/../../mod50_web.py", "mod50_web.py"):
            with self.subTest(route=route):
                self.assertEqual(self.request(route)[0], 404)

    def test_shutdown_failure_blocks_a_new_worker_even_before_polling(self):
        self.start()
        self.created[-1].complete(restart_required=True)
        self.assertEqual(self.start()[0], 400)
        self.assertEqual(len(self.created), 1)
        self.assertTrue(self.request()[2]["restart_required"])

    def test_static_get_uses_only_bundled_files(self):
        (self.directory / "index.html").write_text("<h1>Fixture</h1>", encoding="utf-8")
        with patch.object(web, "UI_DIR", self.directory):
            self.assertEqual(self.request("")[2], b"<h1>Fixture</h1>")
        self.assertEqual(self.created, [])

    def recovery_fixture(self):
        return {"record": {"recorded_seconds": 10., "raw_sha256": "dc-hash"},
                "raw_path": self.directory / "dc_raw.npz",
                "source": {"lot": "RETAINED-BATCH", "operator": "Original technician", "tray": 7,
                    "created_at": "2026-11-04T13:07:31-05:00", "summary_sha256": "summary-hash",
                    "recorded_noise_seconds": 4.07, "installed_positions": ["1-1", "5-10"]}}

    def test_noise_retry_requires_confirmation_and_uses_saved_identity(self):
        data = self.recovery_fixture()
        with patch.object(web, "load_noise_recovery", return_value=data):
            self.dashboard._offer_recovery(self.directory, simulate=False)
            offer = self.request()[2]["noise_recovery"]
            self.assertEqual(offer["recorded_noise_seconds"], 4.07)
            for confirmation in (None, False, "true", 1):
                self.assertEqual(self.request("api/retry-noise", method="POST", data={
                    "recovery_id": offer["id"], "confirm_unchanged": confirmation})[0], 400)
            self.assertEqual(self.request("api/retry-noise", method="POST", data={
                "recovery_id": "wrong", "confirm_unchanged": True})[0], 400)
            self.assertEqual(self.created, [])
            self.assertEqual(self.request("api/retry-noise", method="POST", data={
                "recovery_id": offer["id"], "confirm_unchanged": True,
                "lot": "cannot override", "simulate": True})[0], 200)
            options = self.created[-1].options
            self.assertEqual(options["lot"], "RETAINED-BATCH")
            self.assertEqual(options["operator"], "Original technician")
            self.assertEqual(options["tray"], 7)
            self.assertFalse(options["simulate"])
            self.assertEqual(options["installed_positions"], ["1-1", "5-10"])
            self.assertEqual(options["resume_dc"], data)
            self.assertIsNone(self.request()[2]["noise_recovery"])
            self.assertEqual(self.request("api/retry-noise", method="POST", data={
                "recovery_id": offer["id"], "confirm_unchanged": True})[0], 400)
            self.assertEqual(len(self.created), 1)

    def test_noise_retry_revalidates_evidence_before_constructing_worker(self):
        data = self.recovery_fixture()
        with patch.object(web, "load_noise_recovery", return_value=data):
            self.dashboard._offer_recovery(self.directory, simulate=False)
        payload = {"recovery_id": self.dashboard.noise_recovery["id"], "confirm_unchanged": True}
        changed = {**data, "source": {**data["source"], "summary_sha256": "changed"}}
        with patch.object(web, "load_noise_recovery", return_value=changed):
            self.assertEqual(self.request("api/retry-noise", method="POST", data=payload)[0], 400)
        with patch.object(web, "load_noise_recovery", side_effect=ValueError("Raw hash mismatch")):
            self.assertEqual(self.request("api/retry-noise", method="POST", data=payload)[0], 400)
        self.assertEqual(self.created, [])

    def test_noise_retry_uses_same_origin_and_post_requirements(self):
        self.assertEqual(self.request("api/retry-noise")[0], 404)
        self.assertEqual(self.request("api/retry-noise", method="POST", headers={"Origin": "null"})[0], 403)
        self.assertEqual(self.created, [])

    def test_startup_offers_only_latest_run_and_never_starts_it(self):
        older, newest = self.directory / "MEASURED_older", self.directory / "MEASURED_newest"
        older.mkdir()
        newest.mkdir()
        import os
        os.utime(older, (100, 100))
        os.utime(newest, (200, 200))
        with patch.object(web, "load_noise_recovery", return_value=self.recovery_fixture()) as loader:
            dashboard = web.Dashboard(output_root=self.directory, worker_factory=FakeWorker)
            loader.assert_called_once_with(newest, simulate=False)
            self.assertIsNone(dashboard.worker)
            self.assertIsNotNone(dashboard.snapshot()["noise_recovery"])
        with patch.object(web, "load_noise_recovery", side_effect=ValueError("Newest run cannot resume")) as loader:
            dashboard = web.Dashboard(output_root=self.directory, worker_factory=FakeWorker)
            loader.assert_called_once_with(newest, simulate=False)
            self.assertIsNone(dashboard.snapshot()["noise_recovery"])


class ImportIsolation(unittest.TestCase):
    def test_import_does_not_load_the_daq_driver(self):
        model_dir = Path(__file__).resolve().parents[1]
        script = '''import ctypes
original = ctypes.CDLL
def guarded(name, *args, **kwargs):
    if "aiousb" in str(name).lower():
        raise AssertionError("Driver opened while importing dashboard")
    return original(name, *args, **kwargs)
ctypes.CDLL = guarded
import mod50_web
assert mod50_web.Dashboard().worker is None
'''
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=model_dir,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
