"""Exercise the real Windows launchers with a harmless stand-in application."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


RIG_DIR = Path(__file__).resolve().parents[1]
LAUNCHERS = (
    (RIG_DIR, "run_eltec_array_tester.cmd", "eltec_array_tester.py", "eltec-array-rig"),
    (RIG_DIR / "m40623", "run_eltec_40623_array_tester.cmd",
     "eltec_40623_array_tester.py", "eltec-40623-array"),
)
STAND_IN = '''from pathlib import Path
import sys
import time
Path(sys.argv[1]).write_text("started")
print("application output " + sys.argv[1], flush=True)
deadline = time.monotonic() + 10
while not Path("release").exists() and time.monotonic() < deadline:
    time.sleep(.05)
raise SystemExit(int(sys.argv[2]))
'''


@unittest.skipUnless(os.name == "nt", "Windows command launchers")
class WindowsLaunchers(unittest.TestCase):
    def run_case(self, launcher, *, overlapping=False, exit_code=0):
        source_dir, command, script, log_dir = launcher
        with tempfile.TemporaryDirectory(prefix="eltec launcher ") as tmp:
            folder = Path(tmp)
            shutil.copyfile(source_dir / command, folder / command)
            (folder / script).write_text(STAND_IN, encoding="utf-8")
            env = {**os.environ, "LOCALAPPDATA": tmp, "ELTEC_PYTHON": sys.executable,
                   "ELTEC_LAUNCHER_NO_DIALOG": "1"}
            processes = []
            try:
                for marker in (["first", "second"] if overlapping else ["first"]):
                    if not overlapping:
                        (folder / "release").touch()
                    process = subprocess.Popen(
                        ["cmd.exe", "/d", "/c", command, marker, str(exit_code)],
                        cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    processes.append(process)
                    deadline = time.monotonic() + 5
                    while not (folder / marker).exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertTrue((folder / marker).exists(), f"{command}: {marker} launch did not start")
                    if overlapping:
                        self.assertIsNone(processes[0].poll(), "First app must still be running")
            finally:
                (folder / "release").touch()
                outputs = [p.communicate(timeout=15) for p in processes]
            for process, output in zip(processes, outputs):
                self.assertEqual(process.returncode, exit_code, output)
            logs = list((folder / log_dir).glob("launcher*.log"))
            self.assertEqual(len(logs), len(processes), "Each launch must own its log")
            text = "\n".join(p.read_text() for p in logs)
            self.assertIn("application output first", text)
            if overlapping:
                self.assertIn("application output second", text)
            if exit_code:
                self.assertIn(f"exited with status {exit_code}", text)

    def test_array_launcher_reopens_while_previous_app_is_still_running(self):
        self.run_case(LAUNCHERS[0], overlapping=True)

    def test_model_launcher_reopens_while_previous_app_is_still_running(self):
        self.run_case(LAUNCHERS[1], overlapping=True)

    def test_array_launcher_reports_application_failure(self):
        self.run_case(LAUNCHERS[0], exit_code=7)

    def test_model_launcher_reports_application_failure(self):
        self.run_case(LAUNCHERS[1], exit_code=7)


if __name__ == "__main__":
    unittest.main()
