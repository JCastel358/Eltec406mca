from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "setup_xubuntu.sh",
    "doctor_xubuntu.sh",
    "update_xubuntu.sh",
    "rollback_xubuntu.sh",
    "make_update_bundle.sh",
    "backup_eltec_results.sh",
    "restore_eltec_results.sh",
)


def run_command(*args: str | os.PathLike[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(arg) for arg in args],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **kwargs,
    )


class ScriptContractTests(unittest.TestCase):
    def test_all_scripts_parse_and_expose_help(self) -> None:
        syntax = run_command("bash", "-n", *(REPO_ROOT / name for name in SCRIPTS))
        self.assertEqual(syntax.returncode, 0, syntax.stdout)

        for name in SCRIPTS:
            with self.subTest(script=name):
                result = run_command("bash", REPO_ROOT / name, "--help")
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn("Usage:", result.stdout)

    def test_setup_dry_run_has_no_home_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            home = Path(temporary_home)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "XDG_DATA_HOME": str(home / ".local" / "share"),
                    "XDG_STATE_HOME": str(home / ".local" / "state"),
                    "XDG_CONFIG_HOME": str(home / ".config"),
                }
            )
            result = run_command(
                "bash",
                REPO_ROOT / "setup_xubuntu.sh",
                "--dry-run",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("required Ubuntu packages", result.stdout)
            self.assertIn("install_xubuntu_launcher.sh", result.stdout)
            self.assertIn("dry run complete", result.stdout)
            self.assertLess(
                result.stdout.index("Software verification"),
                result.stdout.index("Installing the verified production launcher"),
            )
            self.assertEqual(list(home.iterdir()), [], result.stdout)

    def test_update_paths_are_non_destructive_and_fast_forward_only(self) -> None:
        updater = (REPO_ROOT / "update_xubuntu.sh").read_text(encoding="utf-8")
        installer = (REPO_ROOT / "setup_xubuntu.sh").read_text(encoding="utf-8")

        self.assertIn("merge --ff-only", updater)
        self.assertIn("diff --quiet", updater)
        self.assertNotIn("reset --hard", updater)
        self.assertNotIn("git clean", updater)
        self.assertNotIn("git stash", updater)
        self.assertNotIn("chmod 666", installer)
        self.assertNotIn("reference_sensor_calibration.json", installer)

    def test_software_doctor_does_not_create_home_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            home = Path(temporary_home)
            env = os.environ.copy()
            env.pop("DISPLAY", None)
            env.update(
                {
                    "HOME": str(home),
                    "XDG_DATA_HOME": str(home / ".local" / "share"),
                    "XDG_STATE_HOME": str(home / ".local" / "state"),
                    "XDG_CONFIG_HOME": str(home / ".config"),
                    "XDG_CACHE_HOME": str(home / ".cache"),
                }
            )
            result = run_command(
                "bash",
                REPO_ROOT / "doctor_xubuntu.sh",
                env=env,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(home.iterdir()), [], result.stdout)

    def test_documented_runtime_packages_are_in_installer(self) -> None:
        installer = (REPO_ROOT / "setup_xubuntu.sh").read_text(encoding="utf-8")
        for package in (
            "python3-tk",
            "python3-numpy",
            "python3-serial",
            "python3-matplotlib",
            "libnotify-bin",
            "desktop-file-utils",
            "xdg-user-dirs",
            "libglib2.0-bin",
            "git",
            "ca-certificates",
            "usbutils",
        ):
            with self.subTest(package=package):
                self.assertIn(package, installer)


class BackupTests(unittest.TestCase):
    def test_backup_contains_results_and_valid_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            home = Path(temporary_home)
            results = home / "Documents" / "Eltec_406MCA_Test_Results" / "v6_esp32"
            results.mkdir(parents=True)
            calibration = results / "reference_sensor_calibration.json"
            calibration.write_text('{"schema_version": 2}\n', encoding="utf-8")
            batch = results / "406mca_esp32_lot_TEST.csv"
            batch.write_text("sensor_id,pass_fail\n1,PASS\n", encoding="utf-8")
            destination = home / "usb-backup"
            env = os.environ.copy()
            env["HOME"] = str(home)

            result = run_command(
                "bash",
                REPO_ROOT / "backup_eltec_results.sh",
                destination,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stdout)

            archives = list(destination.glob("*.tar.gz"))
            checksums = list(destination.glob("*.tar.gz.sha256"))
            self.assertEqual(len(archives), 1)
            self.assertEqual(len(checksums), 1)

            digest = hashlib.sha256(archives[0].read_bytes()).hexdigest()
            self.assertTrue(checksums[0].read_text(encoding="utf-8").startswith(digest))
            with tarfile.open(archives[0], "r:gz") as archive:
                self.assertIn(
                    "Eltec_406MCA_Test_Results/v6_esp32/reference_sensor_calibration.json",
                    archive.getnames(),
                )
                self.assertTrue(
                    any(
                        name.endswith("/backup-station.txt")
                        and "/_station_metadata/" in name
                        for name in archive.getnames()
                    )
                )

            restore_home = home / "restored-results-only"
            restore_home.mkdir()
            restore_env = os.environ.copy()
            restore_env["HOME"] = str(restore_home)
            restored = run_command(
                "bash",
                REPO_ROOT / "restore_eltec_results.sh",
                archives[0],
                env=restore_env,
            )
            self.assertEqual(restored.returncode, 0, restored.stdout)
            restored_root = (
                restore_home / "Documents" / "Eltec_406MCA_Test_Results" / "v6_esp32"
            )
            self.assertTrue((restored_root / batch.name).is_file())
            self.assertFalse((restored_root / calibration.name).exists())

            same_fixture_home = home / "restored-same-fixture"
            same_fixture_home.mkdir()
            same_fixture_env = os.environ.copy()
            same_fixture_env["HOME"] = str(same_fixture_home)
            same_fixture = run_command(
                "bash",
                REPO_ROOT / "restore_eltec_results.sh",
                "--same-fixture",
                archives[0],
                env=same_fixture_env,
            )
            self.assertEqual(same_fixture.returncode, 0, same_fixture.stdout)
            self.assertTrue(
                (
                    same_fixture_home
                    / "Documents"
                    / "Eltec_406MCA_Test_Results"
                    / "v6_esp32"
                    / calibration.name
                ).is_file()
            )

    def test_backup_rejects_destination_inside_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            home = Path(temporary_home)
            results = home / "Documents" / "Eltec_406MCA_Test_Results"
            results.mkdir(parents=True)
            env = os.environ.copy()
            env["HOME"] = str(home)

            result = run_command(
                "bash",
                REPO_ROOT / "backup_eltec_results.sh",
                results / "bad-backup-location",
                env=env,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot be inside", result.stdout)

    def test_restore_rejects_checksum_for_a_different_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            home = Path(temporary_home)
            selected = home / "selected.tar.gz"
            different = home / "different.tar.gz"
            selected.write_bytes(b"selected")
            different.write_bytes(b"different")
            digest = hashlib.sha256(different.read_bytes()).hexdigest()
            Path(f"{selected}.sha256").write_text(
                f"{digest}  {different.name}\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["HOME"] = str(home)

            restored = run_command(
                "bash",
                REPO_ROOT / "restore_eltec_results.sh",
                selected,
                env=env,
            )
            self.assertNotEqual(restored.returncode, 0)
            self.assertIn("not the selected archive", restored.stdout)

    def test_restore_rejects_symbolic_link_members_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            home = Path(temporary_home)
            archive_path = home / "unsafe.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                root = tarfile.TarInfo("Eltec_406MCA_Test_Results")
                root.type = tarfile.DIRTYPE
                archive.addfile(root)
                unsafe = tarfile.TarInfo("Eltec_406MCA_Test_Results/escape")
                unsafe.type = tarfile.SYMTYPE
                unsafe.linkname = "../../outside"
                archive.addfile(unsafe)
            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            Path(f"{archive_path}.sha256").write_text(
                f"{digest}  {archive_path.name}\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["HOME"] = str(home)

            restored = run_command(
                "bash",
                REPO_ROOT / "restore_eltec_results.sh",
                archive_path,
                env=env,
            )
            self.assertNotEqual(restored.returncode, 0)
            self.assertIn("safe-member validation", restored.stdout)


class OfflineBundleTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_bundle_builder_emits_verifiable_bundle_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            source.mkdir()
            for name in SCRIPTS:
                shutil.copy2(REPO_ROOT / name, source / name)
            (source / "setup_xubuntu.sh").write_text(
                "#!/usr/bin/env bash\nset -eu\nprintf '%s\\n' \"$*\" > setup-args.log\n",
                encoding="utf-8",
            )
            (source / "setup_xubuntu.sh").chmod(0o755)
            (source / "application-version.txt").write_text("v1\n", encoding="utf-8")

            commands = (
                ("git", "init", "-b", "main"),
                ("git", "add", "."),
                (
                    "git",
                    "-c",
                    "user.name=Provisioning Test",
                    "-c",
                    "user.email=provisioning-test@example.invalid",
                    "commit",
                    "-m",
                    "test release",
                ),
            )
            for command in commands:
                result = subprocess.run(
                    command,
                    cwd=source,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                self.assertEqual(result.returncode, 0, result.stdout)

            station = root / "station"
            clone = subprocess.run(
                ("git", "clone", str(source), str(station)),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(clone.returncode, 0, clone.stdout)

            (source / "application-version.txt").write_text("v2\n", encoding="utf-8")
            subprocess.run(("git", "add", "."), cwd=source, check=True)
            second_commit = subprocess.run(
                (
                    "git",
                    "-c",
                    "user.name=Provisioning Test",
                    "-c",
                    "user.email=provisioning-test@example.invalid",
                    "commit",
                    "-m",
                    "offline v2",
                ),
                cwd=source,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(second_commit.returncode, 0, second_commit.stdout)

            bundle = root / "station-update.bundle"
            result = subprocess.run(
                ("bash", str(source / "make_update_bundle.sh"), str(bundle), "main"),
                cwd=source,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue(bundle.is_file())
            self.assertTrue(Path(f"{bundle}.manifest").is_file())
            self.assertTrue(Path(f"{bundle}.sha256").is_file())

            verify = subprocess.run(
                ("git", "bundle", "verify", str(bundle)),
                cwd=source,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(verify.returncode, 0, verify.stdout)
            approved_revision = subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=source,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            detach = subprocess.run(
                ("git", "checkout", "--detach"),
                cwd=station,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(detach.returncode, 0, detach.stdout)
            station_env = os.environ.copy()
            station_env["HOME"] = str(root / "station-home")
            station_env["XDG_STATE_HOME"] = str(root / "station-state")

            offline_update = subprocess.run(
                (
                    "bash",
                    str(station / "update_xubuntu.sh"),
                    "--bundle",
                    str(bundle),
                    "--revision",
                    approved_revision,
                    "--skip-tests",
                ),
                cwd=station,
                env=station_env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(offline_update.returncode, 0, offline_update.stdout)
            self.assertEqual(
                (station / "application-version.txt").read_text(encoding="utf-8"),
                "v2\n",
            )
            self.assertIn("--skip-apt", (station / "setup-args.log").read_text(encoding="utf-8"))


class UpdaterIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_online_update_fast_forwards_and_refuses_tracked_edits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            station = root / "station"
            source.mkdir()
            shutil.copy2(REPO_ROOT / "update_xubuntu.sh", source / "update_xubuntu.sh")
            setup_stub = source / "setup_xubuntu.sh"
            setup_stub.write_text(
                "#!/usr/bin/env bash\nset -eu\nprintf 'setup stub invoked\\n'\n",
                encoding="utf-8",
            )
            setup_stub.chmod(0o755)
            (source / "application-version.txt").write_text("v1\n", encoding="utf-8")

            def git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ("git", *arguments),
                    cwd=cwd,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

            self.assertEqual(git(source, "init", "-b", "main").returncode, 0)
            self.assertEqual(git(source, "add", ".").returncode, 0)
            first_commit = git(
                source,
                "-c",
                "user.name=Updater Test",
                "-c",
                "user.email=updater-test@example.invalid",
                "commit",
                "-m",
                "v1",
            )
            self.assertEqual(first_commit.returncode, 0, first_commit.stdout)

            clone = subprocess.run(
                ("git", "clone", str(source), str(station)),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(clone.returncode, 0, clone.stdout)
            self.assertEqual(git(station, "checkout", "--detach").returncode, 0)

            (source / "application-version.txt").write_text("v2\n", encoding="utf-8")
            self.assertEqual(git(source, "add", "application-version.txt").returncode, 0)
            second_commit = git(
                source,
                "-c",
                "user.name=Updater Test",
                "-c",
                "user.email=updater-test@example.invalid",
                "commit",
                "-m",
                "v2",
            )
            self.assertEqual(second_commit.returncode, 0, second_commit.stdout)
            approved_revision = git(source, "rev-parse", "HEAD").stdout.strip()
            station_env = os.environ.copy()
            station_env["HOME"] = str(root / "station-home")
            station_env["XDG_STATE_HOME"] = str(root / "station-state")

            update = subprocess.run(
                (
                    "bash",
                    str(station / "update_xubuntu.sh"),
                    "--skip-tests",
                    "--revision",
                    approved_revision,
                ),
                cwd=station,
                env=station_env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(update.returncode, 0, update.stdout)
            self.assertIn("setup stub invoked", update.stdout)
            self.assertEqual(
                (station / "application-version.txt").read_text(encoding="utf-8"),
                "v2\n",
            )

            (source / "application-version.txt").write_text("v3\n", encoding="utf-8")
            self.assertEqual(git(source, "add", "application-version.txt").returncode, 0)
            third_commit = git(
                source,
                "-c",
                "user.name=Updater Test",
                "-c",
                "user.email=updater-test@example.invalid",
                "commit",
                "-m",
                "v3 tagged",
            )
            self.assertEqual(third_commit.returncode, 0, third_commit.stdout)
            tagged_commit = git(source, "rev-parse", "HEAD").stdout.strip()
            tag = git(
                source,
                "-c",
                "user.name=Updater Test",
                "-c",
                "user.email=updater-test@example.invalid",
                "tag",
                "-a",
                "fleet-v3",
                "-m",
                "fleet v3",
            )
            self.assertEqual(tag.returncode, 0, tag.stdout)
            tagged_update = subprocess.run(
                (
                    "bash",
                    str(station / "update_xubuntu.sh"),
                    "--skip-tests",
                    "--revision",
                    "fleet-v3",
                ),
                cwd=station,
                env=station_env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(tagged_update.returncode, 0, tagged_update.stdout)
            self.assertEqual(git(station, "rev-parse", "HEAD").stdout.strip(), tagged_commit)
            self.assertEqual(
                (station / "application-version.txt").read_text(encoding="utf-8"),
                "v3\n",
            )

            (station / "application-version.txt").write_text("local edit\n", encoding="utf-8")
            refused = subprocess.run(
                ("bash", str(station / "update_xubuntu.sh")),
                cwd=station,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("local changes", refused.stdout)
            self.assertEqual(
                (station / "application-version.txt").read_text(encoding="utf-8"),
                "local edit\n",
            )

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_failed_candidate_test_leaves_live_commit_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            station = root / "station"
            source.mkdir()
            shutil.copy2(REPO_ROOT / "update_xubuntu.sh", source / "update_xubuntu.sh")

            simple_scripts = (
                "setup_xubuntu.sh",
                "doctor_xubuntu.sh",
                "rollback_xubuntu.sh",
                "make_update_bundle.sh",
                "backup_eltec_results.sh",
                "restore_eltec_results.sh",
            )
            for name in simple_scripts:
                path = source / name
                path.write_text("#!/usr/bin/env bash\nset -eu\n", encoding="utf-8")
                path.chmod(0o755)
            app_dir = source / "tech_app" / "v6_esp32"
            app_tests = app_dir / "tests"
            root_tests = source / "tests"
            app_tests.mkdir(parents=True)
            root_tests.mkdir()
            for name in ("install_xubuntu_launcher.sh", "run_eltec_406mca_esp32_tester.sh"):
                path = app_dir / name
                path.write_text("#!/usr/bin/env bash\nset -eu\n", encoding="utf-8")
                path.chmod(0o755)
            candidate_test = app_tests / "test_candidate.py"
            candidate_test.write_text(
                "import unittest\n\n"
                "class CandidateTest(unittest.TestCase):\n"
                "    def test_candidate(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (root_tests / "test_provisioning.py").write_text(
                "import unittest\n\n"
                "class ProvisioningTest(unittest.TestCase):\n"
                "    def test_scripts(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (source / "application-version.txt").write_text("v1\n", encoding="utf-8")

            def git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ("git", *arguments),
                    cwd=cwd,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

            self.assertEqual(git(source, "init", "-b", "main").returncode, 0)
            self.assertEqual(git(source, "add", ".").returncode, 0)
            first = git(
                source,
                "-c",
                "user.name=Updater Test",
                "-c",
                "user.email=updater-test@example.invalid",
                "commit",
                "-m",
                "passing v1",
            )
            self.assertEqual(first.returncode, 0, first.stdout)
            old_commit = git(source, "rev-parse", "HEAD").stdout.strip()

            clone = subprocess.run(
                ("git", "clone", str(source), str(station)),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(clone.returncode, 0, clone.stdout)
            self.assertEqual(git(station, "checkout", "--detach").returncode, 0)

            candidate_test.write_text(
                "import unittest\n\n"
                "class CandidateTest(unittest.TestCase):\n"
                "    def test_candidate(self):\n"
                "        self.fail('candidate rejected')\n",
                encoding="utf-8",
            )
            (source / "application-version.txt").write_text("v2-broken\n", encoding="utf-8")
            self.assertEqual(git(source, "add", ".").returncode, 0)
            second = git(
                source,
                "-c",
                "user.name=Updater Test",
                "-c",
                "user.email=updater-test@example.invalid",
                "commit",
                "-m",
                "broken v2",
            )
            self.assertEqual(second.returncode, 0, second.stdout)
            broken_commit = git(source, "rev-parse", "HEAD").stdout.strip()

            update = subprocess.run(
                (
                    "bash",
                    str(station / "update_xubuntu.sh"),
                    "--skip-apt",
                    "--revision",
                    broken_commit,
                ),
                cwd=station,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertNotEqual(update.returncode, 0, update.stdout)
            self.assertIn("candidate rejected", update.stdout)
            self.assertEqual(git(station, "rev-parse", "HEAD").stdout.strip(), old_commit)
            self.assertEqual(
                (station / "application-version.txt").read_text(encoding="utf-8"),
                "v1\n",
            )


class RollbackIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_last_rollback_restores_previous_commit_and_preserves_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            station = root / "station"
            station_home = root / "station-home"
            source.mkdir()
            station_home.mkdir()
            shutil.copy2(REPO_ROOT / "rollback_xubuntu.sh", source / "rollback_xubuntu.sh")

            for name in (
                "setup_xubuntu.sh",
                "doctor_xubuntu.sh",
                "update_xubuntu.sh",
                "make_update_bundle.sh",
                "backup_eltec_results.sh",
                "restore_eltec_results.sh",
            ):
                path = source / name
                path.write_text("#!/usr/bin/env bash\nset -eu\nprintf 'setup helper %s\\n' \"$*\"\n", encoding="utf-8")
                path.chmod(0o755)

            app_dir = source / "tech_app" / "v6_esp32"
            app_tests = app_dir / "tests"
            root_tests = source / "tests"
            app_tests.mkdir(parents=True)
            root_tests.mkdir()
            for name in (
                "eltec_406mca_esp32_tester.py",
                "install_xubuntu_launcher.sh",
                "run_eltec_406mca_esp32_tester.sh",
            ):
                path = app_dir / name
                path.write_text("#!/usr/bin/env bash\nset -eu\n", encoding="utf-8")
                path.chmod(0o755)
            (app_tests / "test_release.py").write_text(
                "import unittest\n\n"
                "class ReleaseTest(unittest.TestCase):\n"
                "    def test_release(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (root_tests / "test_provisioning.py").write_text(
                "import unittest\n\n"
                "class ProvisioningTest(unittest.TestCase):\n"
                "    def test_scripts(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            (source / "application-version.txt").write_text("v1\n", encoding="utf-8")

            def git(cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ("git", *arguments),
                    cwd=cwd,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

            self.assertEqual(git(source, "init", "-b", "main").returncode, 0)
            self.assertEqual(git(source, "add", ".").returncode, 0)
            first = git(
                source,
                "-c",
                "user.name=Rollback Test",
                "-c",
                "user.email=rollback-test@example.invalid",
                "commit",
                "-m",
                "v1",
            )
            self.assertEqual(first.returncode, 0, first.stdout)
            first_commit = git(source, "rev-parse", "HEAD").stdout.strip()

            (source / "application-version.txt").write_text("v2\n", encoding="utf-8")
            self.assertEqual(git(source, "add", "application-version.txt").returncode, 0)
            second = git(
                source,
                "-c",
                "user.name=Rollback Test",
                "-c",
                "user.email=rollback-test@example.invalid",
                "commit",
                "-m",
                "v2",
            )
            self.assertEqual(second.returncode, 0, second.stdout)
            second_commit = git(source, "rev-parse", "HEAD").stdout.strip()

            clone = subprocess.run(
                ("git", "clone", str(source), str(station)),
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(clone.returncode, 0, clone.stdout)
            self.assertEqual(git(station, "checkout", "--detach").returncode, 0)

            state_dir = station_home / ".local" / "state" / "eltec-406mca-esp32-v6"
            state_dir.mkdir(parents=True)
            (state_dir / "rollback-target.txt").write_text(
                f"previous_commit={first_commit}\nincoming_commit={second_commit}\n",
                encoding="utf-8",
            )
            results = station_home / "Documents" / "Eltec_406MCA_Test_Results" / "v6_esp32"
            results.mkdir(parents=True)
            result_file = results / "lot.csv"
            result_file.write_text("sensor_id,pass_fail\n1,PASS\n", encoding="utf-8")

            env = os.environ.copy()
            env["HOME"] = str(station_home)
            rollback = subprocess.run(
                (
                    "bash",
                    str(station / "rollback_xubuntu.sh"),
                    "--last",
                    "--skip-tests",
                ),
                cwd=station,
                env=env,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self.assertEqual(rollback.returncode, 0, rollback.stdout)
            self.assertEqual(git(station, "rev-parse", "HEAD").stdout.strip(), first_commit)
            self.assertEqual(
                (station / "application-version.txt").read_text(encoding="utf-8"),
                "v1\n",
            )
            self.assertEqual(
                result_file.read_text(encoding="utf-8"),
                "sensor_id,pass_fail\n1,PASS\n",
            )


if __name__ == "__main__":
    unittest.main()
