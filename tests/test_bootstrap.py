import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("bootstrap", Path(__file__).resolve().parents[1] / "scripts" / "bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def test_resource_aware_threads(self):
        self.assertEqual(bootstrap.auto_threads(256 * bootstrap.GIB, 32), 8)
        self.assertEqual(bootstrap.auto_threads(32 * bootstrap.GIB, 64), 6)
        self.assertEqual(bootstrap.auto_threads(256 * bootstrap.GIB, 2), 2)
        self.assertEqual(bootstrap.auto_threads(4 * bootstrap.GIB, 32), 1)

    def test_status_is_read_only_even_without_work_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp) / "not-created"
            output = io.StringIO()
            with contextlib.redirect_stdout(output), mock.patch.object(bootstrap, "check_platform", side_effect=AssertionError("must not install")):
                self.assertEqual(bootstrap.main(["--work-dir", str(work), "--status"]), 0)
            self.assertFalse(work.exists())
            self.assertEqual(output.getvalue().count('"not_started"'), 2)

    def test_stop_marks_both_setup_and_runner(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp).resolve()
            runner_dir = work / "project" / ".verification-run"
            runner_dir.mkdir(parents=True)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(bootstrap.main(["--work-dir", str(work), "--stop"]), 0)
            self.assertTrue((work / "STOP").is_file())
            self.assertTrue((runner_dir / "STOP").is_file())

    def test_symlink_work_and_status_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            real, link = root / "real", root / "link"
            real.mkdir()
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("Symlinks unavailable")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bootstrap.main(["--work-dir", str(link), "--status"]), 1)

    def fixture(self, temp):
        args = bootstrap.parser().parse_args(["--work-dir", str(Path(temp).resolve()), "--threads", "2"])
        with mock.patch.object(bootstrap, "memory_available", return_value=64 * bootstrap.GIB):
            return bootstrap.Bootstrap(args, {"PRETTY_NAME": "test"})

    def test_private_elan_and_clean_lean_paths(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {"LEAN_PATH": "/foreign", "LEAN_SRC_PATH": "/foreign", "ELAN_HOME": "/foreign", "LAKE_HOME": "/foreign"}):
            owner = self.fixture(temp)
            self.assertEqual(owner.env["ELAN_HOME"], str(owner.work / "tools" / "elan"))
            self.assertNotIn("LEAN_PATH", owner.env)
            self.assertNotIn("LEAN_SRC_PATH", owner.env)
            self.assertNotIn("LAKE_HOME", owner.env)
            self.assertEqual(owner.env["ELAN_TOOLCHAIN"], bootstrap.TOOLCHAIN)

    def test_resource_shortage_blocks_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            owner = self.fixture(temp)
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(bootstrap.shutil, "disk_usage", return_value=mock.Mock(free=159 * bootstrap.GIB)), mock.patch.object(bootstrap, "memory_available", return_value=64 * bootstrap.GIB), mock.patch.object(owner, "prepare") as prepare:
                with self.assertRaisesRegex(RuntimeError, "160 GiB"):
                    owner.run()
                prepare.assert_not_called()

    def test_exit_zero_without_success_status_is_not_success(self):
        with tempfile.TemporaryDirectory() as temp:
            owner = self.fixture(temp)
            status = owner.project / ".verification-run" / "status.json"
            status.parent.mkdir(parents=True)
            status.write_text(json.dumps({"phase": "paused", "full_build_completed": False, "axiom_audit_passed": False}))
            with mock.patch.object(owner, "preflight"), mock.patch.object(owner, "prepare"), mock.patch.object(owner, "command"):
                with self.assertRaisesRegex(RuntimeError, "without a completed build"):
                    owner.run()

    def test_prepare_only_is_not_proof_success(self):
        with tempfile.TemporaryDirectory() as temp:
            owner = self.fixture(temp)
            owner.args.prepare_only = True
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(owner, "preflight"), mock.patch.object(owner, "prepare"), mock.patch.object(owner, "command") as command:
                self.assertEqual(owner.run(), 0)
                command.assert_not_called()
            record = bootstrap.read_json(owner.status_path)
            self.assertEqual(record["phase"], "prepared")
            self.assertFalse(record["completed"])

    def test_success_requires_both_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            owner = self.fixture(temp)
            status = owner.project / ".verification-run" / "status.json"
            status.parent.mkdir(parents=True)
            status.write_text(json.dumps({"phase": "completed", "full_build_completed": True, "axiom_audit_passed": True}))
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(owner, "preflight"), mock.patch.object(owner, "prepare"), mock.patch.object(owner, "command"):
                self.assertEqual(owner.run(), 0)
            record = bootstrap.read_json(owner.status_path)
            self.assertTrue(record["completed"])
            self.assertFalse(record["independent_kernel_recheck_performed"])

    def test_non_finite_threshold_rejected(self):
        for value in ("nan", "inf", "-1"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                bootstrap.main(["--min-free-gib", value])

    @unittest.skipUnless(os.name == "posix", "POSIX work lock")
    def test_lock_rejection_does_not_overwrite_active_status(self):
        import fcntl
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp).resolve()
            status = work / "bootstrap-status.json"
            status.write_text('{"phase":"active"}')
            with (work / "bootstrap.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(bootstrap, "check_platform", return_value={}), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(bootstrap.main(["--work-dir", str(work)]), 1)
            self.assertEqual(status.read_text(), '{"phase":"active"}')

    @unittest.skipUnless(os.name == "posix", "Bash entry point")
    def test_shell_help_and_status_without_installation(self):
        kit = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp) / "not-created"
            for flags in (["--help"], ["--work-dir", str(work), "--status"]):
                result = subprocess.run(["bash", str(kit / "verify.sh"), *flags], text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(work.exists())


if __name__ == "__main__":
    unittest.main()
