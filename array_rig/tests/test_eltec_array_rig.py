"""Tests for the Eltec Array Rig selector layer (mirror of the single rig's glue tests).

The per-model application carries its own full suite (``m40623/tests``);
this file covers only the glue: the model registry, launch-command
construction, selection persistence, and the launcher/installer identities.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

RIG_DIR = Path(__file__).resolve().parents[1]
if str(RIG_DIR) not in sys.path:
    sys.path.insert(0, str(RIG_DIR))

import eltec_array_tester as rig  # noqa: E402
import sensor_versions  # noqa: E402


class RegistryTests(unittest.TestCase):
    def test_every_version_points_at_an_existing_app_script(self):
        self.assertGreaterEqual(len(sensor_versions.SENSOR_VERSIONS), 1)
        for version in sensor_versions.SENSOR_VERSIONS:
            with self.subTest(version=version.key):
                self.assertTrue(version.app_dir_path.is_dir())
                self.assertTrue(version.app_script_path.is_file())
                self.assertTrue(version.display_name)
                self.assertTrue(version.summary)
                self.assertTrue(version.details)
                self.assertTrue(version.results_note)

    def test_keys_and_display_names_are_unique(self):
        keys = sensor_versions.version_keys()
        self.assertEqual(len(keys), len(set(keys)))
        names = [v.display_name for v in sensor_versions.SENSOR_VERSIONS]
        self.assertEqual(len(names), len(set(names)))

    def test_default_key_resolves(self):
        version = sensor_versions.get_version(sensor_versions.DEFAULT_VERSION_KEY)
        self.assertEqual(version.key, sensor_versions.DEFAULT_VERSION_KEY)

    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            sensor_versions.get_version("no-such-model")

    def test_registered_models_are_the_expected_ones(self):
        # Update this set when a model is added to the array rig.
        self.assertEqual(sensor_versions.version_keys(), ("40623",))

    def test_40623_entry_describes_tp120_and_the_pending_calibration(self):
        version = sensor_versions.get_version("40623")
        self.assertIn("TP120", version.display_name)
        self.assertIn("50 positions", version.display_name)
        details = " ".join(version.details)
        self.assertIn("0.3-1.2 V", details)
        self.assertIn("no pin-level limit", details)
        self.assertIn("not implemented", details)
        self.assertIn("PENDING", version.summary)
        self.assertEqual(version.app_dir, "m40623")
        self.assertEqual(version.results_note, "Documents/Eltec_40623_Test_Results/40623_array_daq")

    def test_required_hardware_names_the_daq(self):
        self.assertIn("USB-AIO16-64MA", sensor_versions.REQUIRED_HARDWARE)
        self.assertIn("0x8145", sensor_versions.REQUIRED_HARDWARE)
        self.assertIn("AIOUSB.dll", sensor_versions.REQUIRED_HARDWARE)

    def test_each_app_suite_exists(self):
        for version in sensor_versions.SENSOR_VERSIONS:
            with self.subTest(version=version.key):
                tests_dir = version.app_dir_path / "tests"
                self.assertTrue(tests_dir.is_dir())
                self.assertTrue(list(tests_dir.glob("test_*.py")))

    def test_no_cross_rig_import(self):
        for name in ("eltec_array_tester.py", "sensor_versions.py", "tray_history.py"):
            source = (RIG_DIR / name).read_text(encoding="utf-8")
            self.assertNotIn("import single_detector_rig", source)
            self.assertNotIn("from single_detector_rig", source)


class LaunchTests(unittest.TestCase):
    def test_launch_command_uses_this_interpreter_and_absolute_script(self):
        for version in sensor_versions.SENSOR_VERSIONS:
            with self.subTest(version=version.key):
                command = rig.build_launch_command(version)
                self.assertEqual(command[0], sys.executable)
                self.assertTrue(Path(command[1]).is_absolute())
                self.assertEqual(Path(command[1]), version.app_script_path)

    def test_launch_missing_script_raises_filenotfound(self):
        bogus = sensor_versions.SensorVersion(
            key="bogus", display_name="Bogus", summary="s", details=("d",),
            app_dir="does_not_exist", app_script="nope.py", results_note="r",
        )
        with self.assertRaises(FileNotFoundError):
            rig.launch_version(bogus)

    def test_launch_version_spawns_from_the_app_directory(self):
        version = sensor_versions.SENSOR_VERSIONS[0]
        recorded = {}
        real_popen = subprocess.Popen

        def fake_popen(command, cwd=None, **kwargs):
            recorded["command"] = command
            recorded["cwd"] = cwd

            class _Proc:
                def poll(self):
                    return 0

            return _Proc()

        rig.subprocess.Popen = fake_popen
        try:
            rig.launch_version(version)
        finally:
            rig.subprocess.Popen = real_popen
        self.assertEqual(recorded["cwd"], str(version.app_dir_path))
        self.assertEqual(recorded["command"], rig.build_launch_command(version))


class PersistenceTests(unittest.TestCase):
    def test_round_trip_and_default_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "deep" / "state.json"
            self.assertEqual(rig.load_last_version_key(state), sensor_versions.DEFAULT_VERSION_KEY)
            rig.save_last_version_key("40623", state)
            self.assertEqual(rig.load_last_version_key(state), "40623")
            state.write_text(json.dumps({"last_sensor_version": "retired-model"}), encoding="utf-8")
            self.assertEqual(rig.load_last_version_key(state), sensor_versions.DEFAULT_VERSION_KEY)
            state.write_text("{not json", encoding="utf-8")
            self.assertEqual(rig.load_last_version_key(state), sensor_versions.DEFAULT_VERSION_KEY)

    def test_state_file_is_per_user_and_separate_from_the_single_rig(self):
        path = rig.state_file_path()
        self.assertEqual(path.name, rig.STATE_FILE_NAME)
        self.assertEqual(path.parent.name, "eltec-array-rig")
        self.assertNotEqual(path.parent.name, "eltec-rig")
        self.assertTrue(str(path).startswith(str(Path.home())[:3]))


LAUNCHER_FILES = (
    "run_eltec_array_tester.cmd",
    "run_eltec_array_tester.sh",
    "install_windows_launcher.ps1",
    "install_xubuntu_launcher.sh",
)


class LauncherFileTests(unittest.TestCase):
    def _read(self, name: str) -> str:
        return (RIG_DIR / name).read_text(encoding="utf-8")

    def test_all_launcher_files_exist(self):
        for name in LAUNCHER_FILES:
            with self.subTest(name=name):
                self.assertTrue((RIG_DIR / name).is_file())

    def test_launchers_point_at_the_array_selector(self):
        for name in LAUNCHER_FILES:
            with self.subTest(name=name):
                self.assertIn("eltec_array_tester.py", self._read(name))

    def test_launchers_carry_the_array_identity_only(self):
        for name in LAUNCHER_FILES:
            content = self._read(name)
            with self.subTest(name=name):
                self.assertIn("eltec-array-rig", content)
                self.assertNotIn("eltec_rig_tester.py", content)
                self.assertNotIn("com.eltec.test-rig", content)
                self.assertNotIn("eltec-40623-array", content)
                self.assertNotIn("405", content)
                self.assertNotIn("ESP32", content)
                # the single rig's log/state folder must not be shared
                self.assertNotIn("eltec-rig\\", content)
                self.assertNotIn("/eltec-rig", content)

    def test_desktop_entry_identity(self):
        content = self._read("install_xubuntu_launcher.sh")
        self.assertIn("com.eltec.array-rig.desktop", content)
        self.assertIn("Name=Eltec Array Rig", content)
        self.assertIn('REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)', content)

    def test_windows_installer_display_name(self):
        content = self._read("install_windows_launcher.ps1")
        self.assertIn("$DisplayName = 'Eltec Array Rig'", content)
        self.assertIn("Eltec_40623_Test_Results", content)

    def test_icons_are_bundled(self):
        self.assertTrue((RIG_DIR / "assets" / "eltec_desktop_icon.png").is_file())
        self.assertTrue((RIG_DIR / "assets" / "eltec_desktop_icon.ico").is_file())


class SelectorGuiSmokeTests(unittest.TestCase):
    def _open(self):
        real_save = rig.save_last_version_key
        rig.save_last_version_key = lambda key, path=None: None
        self.addCleanup(lambda: setattr(rig, "save_last_version_key", real_save))
        try:
            return rig.EltecArraySelector()
        except Exception as exc:  # e.g. no display on a headless CI host
            self.skipTest(f"Tk unavailable: {exc}")

    def test_selector_builds_and_shows_the_model(self):
        app = self._open()
        try:
            version = sensor_versions.SENSOR_VERSIONS[0]
            app.version_var.set(version.display_name)
            app._on_version_selected()
            self.assertEqual(app.selected_version().key, version.key)
            self.assertIn(version.summary, app.summary_label.cget("text"))
            self.assertIn("USB-AIO16-64MA", app.status_var.get())
            app.update_idletasks()
        finally:
            app.destroy()

    def test_selector_opens_maximized(self):
        app = self._open()
        try:
            app.update_idletasks()
            self.assertEqual(app.wm_resizable(), (True, True))
            if app.tk.call("tk", "windowingsystem") == "x11":
                app.update()
                self.assertTrue(app.attributes("-zoomed"))
            else:
                self.assertEqual(app.state(), "zoomed")
        finally:
            app.destroy()

    def test_content_block_is_centered_by_weighted_spacers(self):
        app = self._open()
        try:
            app.update_idletasks()
            info = app.content.grid_info()
            self.assertEqual((int(info["row"]), int(info["column"])), (1, 1))
            for index in (0, 2):
                self.assertEqual(app.grid_rowconfigure(index)["weight"], 1)
                self.assertEqual(app.grid_columnconfigure(index)["weight"], 1)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
