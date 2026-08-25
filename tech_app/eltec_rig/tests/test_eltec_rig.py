"""Tests for the unified Eltec Test Rig selector layer.

The per-model applications carry their own full suites
(``m405m22/tests``, ``m406mca/tests``); this file covers only the glue:
the sensor-version registry, launch-command construction, selection
persistence, and the launcher/installer file identities.
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

import eltec_rig_tester as rig  # noqa: E402
import sensor_versions  # noqa: E402


class RegistryTests(unittest.TestCase):
    def test_every_version_points_at_an_existing_app_script(self):
        self.assertGreaterEqual(len(sensor_versions.SENSOR_VERSIONS), 2)
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
        # The unified app must offer both current production models; new
        # models extend this set (update this test when adding one).
        self.assertIn("405m22", sensor_versions.version_keys())
        self.assertIn("406mca", sensor_versions.version_keys())

    def test_each_app_suite_exists(self):
        # Every bundled model keeps its own full test suite.
        for version in sensor_versions.SENSOR_VERSIONS:
            with self.subTest(version=version.key):
                tests_dir = version.app_dir_path / "tests"
                self.assertTrue(tests_dir.is_dir())
                self.assertTrue(list(tests_dir.glob("test_*.py")))


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
            key="bogus",
            display_name="Bogus",
            summary="s",
            details=("d",),
            app_dir="does_not_exist",
            app_script="nope.py",
            results_note="r",
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
            # Missing file -> default.
            self.assertEqual(
                rig.load_last_version_key(state), sensor_versions.DEFAULT_VERSION_KEY
            )
            # Round trip.
            rig.save_last_version_key("406mca", state)
            self.assertEqual(rig.load_last_version_key(state), "406mca")
            # A remembered model that no longer exists falls back to default.
            state.write_text(
                json.dumps({"last_sensor_version": "retired-model"}),
                encoding="utf-8",
            )
            self.assertEqual(
                rig.load_last_version_key(state), sensor_versions.DEFAULT_VERSION_KEY
            )
            # Corrupt state must never raise.
            state.write_text("{not json", encoding="utf-8")
            self.assertEqual(
                rig.load_last_version_key(state), sensor_versions.DEFAULT_VERSION_KEY
            )

    def test_state_file_path_is_per_user(self):
        path = rig.state_file_path()
        self.assertEqual(path.name, rig.STATE_FILE_NAME)
        self.assertEqual(path.parent.name, rig.STATE_DIR_NAME)
        self.assertTrue(str(path).startswith(str(Path.home())[:3]))


class LauncherFileTests(unittest.TestCase):
    def _read(self, name: str) -> str:
        return (RIG_DIR / name).read_text(encoding="utf-8")

    def test_all_launcher_files_exist(self):
        for name in (
            "run_eltec_rig_tester.cmd",
            "run_eltec_rig_tester.sh",
            "install_windows_launcher.ps1",
            "install_xubuntu_launcher.sh",
        ):
            with self.subTest(name=name):
                self.assertTrue((RIG_DIR / name).is_file())

    def test_launchers_point_at_the_selector_app(self):
        for name in (
            "run_eltec_rig_tester.cmd",
            "run_eltec_rig_tester.sh",
            "install_windows_launcher.ps1",
            "install_xubuntu_launcher.sh",
        ):
            with self.subTest(name=name):
                self.assertIn("eltec_rig_tester.py", self._read(name))

    def test_launchers_carry_the_unified_identity_only(self):
        # The unified launchers must not reuse a per-model identity - the
        # per-model launchers inside m405m22/m406mca keep those.
        for name in (
            "run_eltec_rig_tester.cmd",
            "run_eltec_rig_tester.sh",
            "install_windows_launcher.ps1",
            "install_xubuntu_launcher.sh",
        ):
            content = self._read(name)
            with self.subTest(name=name):
                self.assertNotIn("eltec-405m22-esp32", content)
                self.assertNotIn("eltec-406mca-esp32", content)
                self.assertNotIn("405m22-esp32-tester", content)
                self.assertNotIn("406mca-esp32-tester", content)

    def test_desktop_entry_identity(self):
        content = self._read("install_xubuntu_launcher.sh")
        self.assertIn("com.eltec.test-rig.desktop", content)
        self.assertIn("Name=Eltec Test Rig", content)

    def test_icons_are_bundled(self):
        self.assertTrue((RIG_DIR / "assets" / "eltec_desktop_icon.png").is_file())
        self.assertTrue((RIG_DIR / "assets" / "eltec_desktop_icon.ico").is_file())


class SelectorGuiSmokeTests(unittest.TestCase):
    def test_selector_builds_and_updates_description(self):
        # Keep the smoke test from touching the technician's real state file.
        real_save = rig.save_last_version_key
        saved_keys: list[str] = []
        rig.save_last_version_key = lambda key, path=None: saved_keys.append(key)
        self.addCleanup(lambda: setattr(rig, "save_last_version_key", real_save))
        try:
            app = rig.EltecRigSelector()
        except Exception as exc:  # e.g. no display on a headless CI host
            self.skipTest(f"Tk unavailable: {exc}")
        try:
            first = sensor_versions.SENSOR_VERSIONS[0]
            second = sensor_versions.SENSOR_VERSIONS[1]
            app.version_var.set(second.display_name)
            app._on_version_selected()
            self.assertEqual(app.selected_version().key, second.key)
            self.assertIn(second.summary, app.summary_label.cget("text"))
            app.version_var.set(first.display_name)
            app._on_version_selected()
            self.assertEqual(app.selected_version().key, first.key)
            app.update_idletasks()
        finally:
            app.destroy()

    def test_selector_opens_maximized(self):
        # The rig PC runs full screen; the technician should never have to
        # maximize the chooser by hand.
        try:
            app = rig.EltecRigSelector()
        except Exception as exc:  # e.g. no display on a headless CI host
            self.skipTest(f"Tk unavailable: {exc}")
        try:
            app.update_idletasks()
            # A fixed-size window cannot be maximized by the window manager.
            self.assertEqual(app.wm_resizable(), (True, True))
            if app.tk.call("tk", "windowingsystem") == "x11":
                app.update()  # the -zoomed request is applied once mapped
                self.assertTrue(app.attributes("-zoomed"))
            else:
                self.assertEqual(app.state(), "zoomed")
            # Screen-sized fallback for a window manager that supports neither.
            with unittest.mock.patch.object(
                rig.EltecRigSelector, "state", side_effect=rig.tk.TclError("no zoom")
            ), unittest.mock.patch.object(
                rig.EltecRigSelector, "_fill_screen"
            ) as fill, unittest.mock.patch.object(
                app, "tk", unittest.mock.Mock(call=lambda *a: "win32")
            ):
                rig.EltecRigSelector.start_maximized(app)
            fill.assert_called_once()
        finally:
            app.destroy()

    def test_content_block_is_centered_by_weighted_spacers(self):
        try:
            app = rig.EltecRigSelector()
        except Exception as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        try:
            app.update_idletasks()
            info = app.content.grid_info()
            self.assertEqual((int(info["row"]), int(info["column"])), (1, 1))
            # Spacer rows/columns take the extra space, so the block stays put
            # instead of the card stretching across a wide monitor.
            for index in (0, 2):
                self.assertEqual(app.grid_rowconfigure(index)["weight"], 1)
                self.assertEqual(app.grid_columnconfigure(index)["weight"], 1)
            self.assertEqual(app.grid_rowconfigure(1)["weight"], 0)
            self.assertEqual(app.grid_columnconfigure(1)["weight"], 0)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
