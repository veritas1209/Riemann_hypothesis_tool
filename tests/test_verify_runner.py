"""Small tests: no Lean, network, installs, or real proof build.
Run: python3 -m unittest discover -s tests -p 'test_verify_runner.py' -v
Fake-Lake lifecycle tests require Linux/WSL and are skipped on Windows.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_runner.py"
SPEC = importlib.util.spec_from_file_location("verify_runner", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def fixture(root):
    source = root / "src"
    source.mkdir()
    (root / "lean-toolchain").write_text(runner.EXPECTED_TOOLCHAIN + "\n", encoding="utf-8")
    (root / "lakefile.toml").write_text('name = "fixture"\nsrcDir = "src"\n', encoding="utf-8")
    (root / "lake-manifest.json").write_text('{"packages": []}\n', encoding="utf-8")
    for name, content in {
        "A": "/- fixture /- nested comment -/ header -/\nmodule -- opt in\npublic import Mathlib\ntheorem a : True := by trivial\n",
        "B": "import A\n",
        "Main": "import B\n",
        "Unused": "import Mathlib\n",
    }.items():
        (source / f"{name}.lean").write_text(content, encoding="utf-8")
    (root / "checks").mkdir()
    (root / "checks" / "Axioms.lean").write_text(
        "import Main\n" + "\n".join(f"#print axioms {name}" for name in runner.ENDPOINTS) + "\n",
        encoding="utf-8")
    return source


def args(root, **changes):
    values = dict(project=root, threads=2, batch_size=2, min_free_gib=0,
                  min_available_gib=0, poll_seconds=0.02, max_batches=0)
    values.update(changes)
    return argparse.Namespace(**values)


def artifact(root, module, suffix=".olean"):
    return root / ".lake" / "build" / "lib" / "lean" / (module.replace(".", "/") + suffix)


class UnitTests(unittest.TestCase):
    def test_unicode_identifiers_and_comment_import_parser(self):
        name = "Development.GaloisGeometry₂.AlgebraicCharacterWD"
        self.assertIsNotNone(runner.MODULE_NAME.fullmatch(name))
        text = f"""/- nested /- import Fake -/ comment -/
module
public import all Mathlib -- import Hidden
import {name} A
theorem value : True := by trivial
-- import Later
"""
        self.assertEqual(runner.header_imports(text), ["Mathlib", name, "A"])
        self.assertEqual(runner.parse_header(text), (["Mathlib", name, "A"], True))
        for legacy in ("/- module -/\nimport Mathlib\n",
                       "-- module\nprelude\nimport Mathlib\n",
                       "import Mathlib\ntheorem a : True := by trivial\nmodule\n"):
            self.assertEqual(runner.parse_header(legacy), (["Mathlib"], False))
        for invalid in ("../Outside", "A/B", "A\\B", "A;echo", "A:olean", "A B"):
            self.assertIsNone(runner.MODULE_NAME.fullmatch(invalid))
        for invalid in ("import\nMathlib\n", "/- unclosed"):
            with self.assertRaises(ValueError):
                runner.header_imports(invalid)

    def test_topology_scope_and_fingerprints(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = fixture(root)
            order, _, digest, summary, module_headers = runner.build_graph(root, lambda *_: None)
            self.assertEqual(order, ["A", "B", "Main"])
            self.assertEqual(summary["source_modules"], 4)
            self.assertEqual(summary["main_closure_modules"], 3)
            self.assertEqual(summary["unreachable_source_modules"], 1)
            self.assertEqual(module_headers, {"A"})
            self.assertEqual(summary["main_closure_module_header_modules"], 1)
            self.assertEqual(summary["main_closure_legacy_modules"], 2)
            (source / "Unused.lean").write_text("import Mathlib\n-- changed\n", encoding="utf-8")
            digest2 = runner.build_graph(root, lambda *_: None)[2]
            self.assertNotEqual(digest, digest2)
            (root / "lake-manifest.json").write_text('{"packages": [], "changed": true}', encoding="utf-8")
            self.assertNotEqual(digest2, runner.build_graph(root, lambda *_: None)[2])
            (source / "A.lean").write_text("import B\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Cycle"):
                runner.build_graph(root, lambda *_: None)

    def test_missing_local_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = fixture(root)
            (source / "B.lean").write_text("import A.Missing\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unresolved local imports"):
                runner.build_graph(root, lambda *_: None)

    def test_axiom_audit_exact_five_no_holes(self):
        good = "\n".join(f"'{name}' depends on axioms: [propext, Classical.choice, Quot.sound]" for name in runner.ENDPOINTS)
        self.assertTrue(runner.parse_axioms(good)["passed"])
        for extra in ("sorryAx", "Some.unprovedAxiom", "proof._native.native_decide.ax_1"):
            bad = good.replace("[propext, Classical.choice, Quot.sound]", f"[{extra}]", 1)
            self.assertFalse(runner.parse_axioms(bad)["passed"])
        for incomplete in (good.splitlines()[0], good + "\n" + good.splitlines()[0]):
            with self.assertRaises(ValueError):
                runner.parse_axioms(incomplete)
        no_axioms = "\n".join(f"'{name}' does not depend on any axioms" for name in runner.ENDPOINTS)
        self.assertTrue(runner.parse_axioms(no_axioms)["passed"])

    def test_audit_source_must_be_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture(root)
            self.assertEqual(len(runner.validate_audit_file(root)), 64)
            with (root / "checks" / "Axioms.lean").open("a", encoding="utf-8") as stream:
                stream.write("#eval 0\n")
            with self.assertRaisesRegex(RuntimeError, "exactly"):
                runner.validate_audit_file(root)

    def test_artifacts_follow_header_mode_and_must_be_nonempty(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            paths = {"A": Path("A.lean"), "B": Path("B.lean")}
            expected = {"A": (".olean", ".olean.private", ".olean.server"), "B": (".olean",)}
            for module, suffixes in expected.items():
                for suffix in suffixes:
                    path = artifact(root, module, suffix)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"fixture proof")
            self.assertIsNone(runner.first_missing_artifact(root, ["A", "B"], paths, {"A"}))
            self.assertFalse(artifact(root, "B", ".olean.private").exists())
            self.assertFalse(artifact(root, "B", ".olean.server").exists())
            for index, (module, suffixes) in enumerate(expected.items()):
                for suffix in suffixes:
                    with self.subTest(module=module, suffix=suffix):
                        path = artifact(root, module, suffix)
                        path.write_bytes(b"")
                        self.assertEqual(runner.first_missing_artifact(root, ["A", "B"], paths, {"A"}), (index, module))
                        path.unlink()
                        self.assertEqual(runner.first_missing_artifact(root, ["A", "B"], paths, {"A"}), (index, module))
                        path.write_bytes(b"fixture proof")

    def test_cleanup_exact_and_confined(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            ir = root / ".lake" / "build" / "ir"
            ir.mkdir(parents=True)
            selected, other, generated = (ir / name for name in ("A.setup.json", "B.setup.json", "A.c"))
            for path in (selected, other, generated):
                path.write_text("preserve unless selected", encoding="utf-8")
            result = runner.cleanup_setups(root, ["A"], {"A": Path("A.lean")})
            self.assertEqual(result["removed_setup_files"], 1)
            self.assertFalse(selected.exists())
            self.assertTrue(other.exists())
            self.assertTrue(generated.exists())
            outside = ir.parent / "outside.setup.json"
            outside.write_text("never delete", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "unsafe setup cleanup"):
                runner.cleanup_setups(root, ["A"], {"A": Path("../outside.lean")})
            self.assertTrue(outside.exists())

    def test_symlink_source_and_setup_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture(root)
            other = root / "other"
            other.mkdir()
            link = root / "src" / "Linked"
            try:
                link.symlink_to(other, target_is_directory=True)
            except OSError:
                self.skipTest("Symlink creation unavailable")
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                runner.build_graph(root, lambda *_: None)
            link.unlink()
            ir = root / ".lake" / "build" / "ir"
            ir.mkdir(parents=True)
            target = other / "keep.json"
            target.write_text("keep", encoding="utf-8")
            (ir / "A.setup.json").symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                runner.cleanup_setups(root, ["A"], {"A": Path("A.lean")})
            self.assertTrue(target.exists())

    def test_custom_elan_home_preserves_inherited_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fixture(root)
            custom = root / "custom-elan"
            with patch.dict(os.environ, {"ELAN_HOME": str(custom), "PATH": "inherited-path"}, clear=True):
                with patch.object(runner.shutil, "which", return_value="fake-lake"):
                    instance = runner.Runner(args(root))
            self.assertEqual(instance.environment["PATH"], str(custom / "bin") + os.pathsep + "inherited-path")
            self.assertEqual(instance.environment["ELAN_TOOLCHAIN"], runner.EXPECTED_TOOLCHAIN)
            with patch.dict(os.environ, {"PATH": "inherited-only"}, clear=True):
                with patch.object(runner.shutil, "which", return_value="fake-lake"):
                    instance = runner.Runner(args(root))
            self.assertEqual(instance.environment["PATH"], "inherited-only")

    def test_required_project_and_nonfinite_threshold(self):
        result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--project", result.stderr)
        for value in ("nan", "inf"):
            result = subprocess.run([sys.executable, str(SCRIPT), "--project", ".", "--min-free-gib", value], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)


FAKE_LAKE = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time
root = Path.cwd()
control = json.loads((root / "fake-control.json").read_text())
arguments = sys.argv[1:]
with (root / "fake-invocations.jsonl").open("a") as stream:
    stream.write(json.dumps({"arguments": arguments, "threads": os.getenv("LEAN_NUM_THREADS"), "toolchain": os.getenv("ELAN_TOOLCHAIN")}) + "\n")
if arguments and arguments[0] == "build":
    (root / "fake-build-active").write_text("running")
    time.sleep(control.get("delay", 0))
    for target in arguments[1:]:
        assert target.startswith("+") and target.endswith(":olean"), target
        module = target[1:-6]
        if module == control.get("fail_once_module") and not (root / "fake-failed-once").exists():
            (root / "fake-failed-once").write_text(module)
            print("intentional fixture compilation failure", flush=True)
            sys.exit(9)
        base = root / ".lake" / "build" / "lib" / "lean" / module.replace(".", "/")
        base.parent.mkdir(parents=True, exist_ok=True)
        # The fixture's A opts in to `module`; B and Main are legacy sources.
        suffixes = (".olean", ".olean.private", ".olean.server") if module == "A" else (".olean",)
        for suffix in suffixes:
            if control.get("omit_artifact") == module + suffix:
                continue
            Path(str(base) + suffix).write_bytes(b"fake nonempty proof artifact")
        Path(str(base) + ".olean.trace").write_text("lake-owned trace")
        setup = root / ".lake" / "build" / "ir" / (module.replace(".", "/") + ".setup.json")
        setup.parent.mkdir(parents=True, exist_ok=True)
        setup.write_text('{"fixture": true}')
    if control.get("create_stop"):
        (root / ".verification-run" / "STOP").write_text("stop after this command")
    (root / "fake-build-active").unlink()
    sys.exit(0)
if arguments[:2] == ["env", "lean"]:
    if control.get("mutate_during_audit"):
        with (root / "src" / "A.lean").open("a") as stream:
            stream.write("-- mutation during audit\n")
    if control.get("delete_during_audit"):
        (root / ".lake" / "build" / "lib" / "lean" / "A.olean.private").unlink()
    names = ["riemannHypothesis", "riemannZeta_eq_zero_re", "LFunction_ne_zero_of_half_lt_re", "LFunction_eq_zero_re", "LFunction_eq_zero_re_of_isPrimitive"]
    if control.get("missing_report"):
        names.pop()
    for index, name in enumerate(names):
        ax = "sorryAx" if control.get("bad_axiom") and index == 0 else "propext, Classical.choice, Quot.sound"
        print("'%s' depends on axioms: [%s]" % (name, ax))
    if control.get("create_stop_during_audit"):
        (root / ".verification-run" / "STOP").write_text("pause before completion")
    sys.exit(0)
raise SystemExit("unexpected fake Lake arguments: " + repr(arguments))
'''


@unittest.skipUnless(os.name == "posix", "Lifecycle integration requires POSIX flock (run in WSL)")
class FakeLakeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lean-replay-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        fixture(self.root)
        self.bin = self.root / "tools" / "elan" / "bin"
        self.bin.mkdir(parents=True)
        fake = self.bin / "lake"
        fake.write_text(FAKE_LAKE, encoding="utf-8")
        fake.chmod(0o755)
        self.environment = dict(os.environ, ELAN_HOME=str(self.bin.parent))
        self.control()

    def control(self, **values):
        (self.root / "fake-control.json").write_text(json.dumps(values), encoding="utf-8")

    def invocation(self, *extra):
        return [sys.executable, str(SCRIPT), "--project", str(self.root), "--threads", "2",
                "--batch-size", "2", "--min-free-gib", "0", "--min-available-gib", "0",
                "--poll-seconds", "0.02", *extra]

    def run_verifier(self, *extra, expected=0):
        result = subprocess.run(self.invocation(*extra), env=self.environment, capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def read(self, name):
        return json.loads((self.root / ".verification-run" / name).read_text())

    def calls(self):
        path = self.root / "fake-invocations.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_complete_scope_cleanup_and_idempotent_reaudit(self):
        self.run_verifier()
        status = self.read("status.json")
        self.assertEqual(status["phase"], "completed")
        self.assertEqual(status["verified_main_closure_modules"], 3)
        self.assertEqual(status["unreachable_source_modules"], 1)
        self.assertTrue(status["axiom_audit_passed"])
        self.assertFalse(status["semantic_review_performed"])
        self.assertFalse(status["independent_kernel_recheck_performed"])
        self.assertEqual(status["main_closure_module_header_modules"], 1)
        self.assertEqual(status["main_closure_legacy_modules"], 2)
        self.assertEqual(status["required_artifact_suffixes"], {
            "all_modules": [".olean"], "module_header_only": [".olean.private", ".olean.server"]})
        self.assertFalse(artifact(self.root, "B", ".olean.private").exists())
        self.assertFalse(artifact(self.root, "Main", ".olean.server").exists())
        self.assertFalse(artifact(self.root, "Unused").exists())
        self.assertFalse(list((self.root / ".lake" / "build" / "ir").rglob("*.setup.json")))
        self.assertTrue(artifact(self.root, "A", ".olean.trace").exists())
        count = len(self.calls())
        self.run_verifier()
        self.assertEqual([call["arguments"] for call in self.calls()[count:]],
                         [["build", "+Main:olean"], ["env", "lean", "--tstack=400000", "checks/Axioms.lean"]])
        self.assertTrue(all(call["threads"] == "2" for call in self.calls()))

    def test_batch_limit_pause_even_at_end_then_resume(self):
        self.run_verifier("--max-batches", "1", expected=3)
        self.assertEqual(self.read("state.json")["completed_modules"], 2)
        self.assertEqual(self.read("status.json")["phase"], "paused_batch_limit")
        self.assertFalse(self.read("status.json")["axiom_audit_passed"])
        self.run_verifier("--max-batches", "1", expected=3)
        self.assertEqual(self.read("state.json")["completed_modules"], 3)
        self.assertFalse(self.read("status.json")["full_build_completed"])
        self.run_verifier()

    def test_deleted_split_artifact_rewinds_completed_state(self):
        self.run_verifier()
        artifact(self.root, "A", ".olean.private").unlink()
        count = len(self.calls())
        self.run_verifier()
        self.assertEqual(self.calls()[count]["arguments"], ["build", "+A:olean", "+B:olean"])
        events = (self.root / ".verification-run" / "events.jsonl").read_text()
        self.assertIn('"event": "resume_artifact_missing"', events)
        self.assertTrue(artifact(self.root, "A", ".olean.private").exists())

    def test_success_without_artifacts_never_commits(self):
        self.control(omit_artifact="A.olean.private")
        self.run_verifier(expected=1)
        self.assertEqual(self.read("state.json")["completed_modules"], 0)
        self.assertFalse(self.read("status.json")["axiom_audit_passed"])

    def test_deleted_legacy_artifact_rewinds_completed_state(self):
        self.run_verifier()
        artifact(self.root, "B").unlink()
        count = len(self.calls())
        self.run_verifier()
        self.assertEqual(self.calls()[count]["arguments"], ["build", "+B:olean", "+Main:olean"])
        self.assertTrue(artifact(self.root, "B").exists())
        self.assertFalse(artifact(self.root, "B", ".olean.private").exists())

    def test_success_without_legacy_artifact_never_commits(self):
        self.control(omit_artifact="B.olean")
        self.run_verifier(expected=1)
        self.assertEqual(self.read("state.json")["completed_modules"], 0)
        self.assertFalse(self.read("status.json")["axiom_audit_passed"])

    def test_failed_batch_retries_without_false_commit(self):
        self.control(fail_once_module="B")
        self.run_verifier(expected=1)
        self.assertEqual(self.read("state.json")["completed_modules"], 0)
        self.run_verifier()
        self.assertEqual(self.calls()[0]["arguments"], self.calls()[1]["arguments"])

    def test_stop_after_active_command_then_resume(self):
        self.control(create_stop=True)
        self.run_verifier(expected=3)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.read("state.json")["completed_modules"], 2)
        self.assertEqual(self.read("status.json")["phase"], "stopped")
        self.run_verifier(expected=3)
        self.assertEqual(len(self.calls()), 1)
        (self.root / ".verification-run" / "STOP").unlink()
        self.control()
        self.run_verifier()

    def test_changed_source_refuses_stale_resume(self):
        self.run_verifier("--max-batches", "1", expected=3)
        (self.root / "src" / "A.lean").write_text("import Mathlib\n-- changed\n")
        count = len(self.calls())
        result = self.run_verifier(expected=1)
        self.assertIn("refusing stale resume", result.stderr)
        self.assertEqual(len(self.calls()), count)

    def test_source_or_artifact_changed_during_audit(self):
        for change in ("delete_during_audit", "mutate_during_audit"):
            with self.subTest(change=change):
                self.control(**{change: True})
                self.run_verifier(expected=1)
                self.assertNotEqual(self.read("status.json")["phase"], "completed")
                self.assertFalse(self.read("status.json")["axiom_audit_passed"])

    def test_bad_axiom_or_missing_report_fails(self):
        self.control(bad_axiom=True)
        self.run_verifier(expected=2)
        self.assertEqual(self.read("status.json")["phase"], "axiom_audit_failed")
        self.control(missing_report=True)
        self.run_verifier(expected=1)
        self.assertFalse(self.read("status.json")["axiom_audit_passed"])

    def test_stop_during_audit_is_pause(self):
        self.control(create_stop_during_audit=True)
        self.run_verifier(expected=3)
        self.assertEqual(self.read("status.json")["phase"], "stopped")
        self.assertFalse(self.read("state.json")["axiom_audit_passed"])

    def test_low_disk_pause_clears_previous_completion(self):
        self.run_verifier()
        count = len(self.calls())
        self.run_verifier("--min-free-gib", "1000000000000", expected=3)
        self.assertEqual(len(self.calls()), count)
        self.assertEqual(self.read("status.json")["phase"], "paused_low_disk")
        self.assertFalse(self.read("status.json")["full_build_completed"])
        self.assertFalse(self.read("state.json")["axiom_audit_passed"])

    def test_sigterm_keeps_lock_until_owned_command_ends(self):
        self.control(delay=1.0)
        process = subprocess.Popen(self.invocation(), env=self.environment, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / "fake-build-active").exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail("Fake Lake never reached its active build")
                time.sleep(0.01)
            process.send_signal(signal.SIGTERM)
            result = self.run_verifier(expected=1)
            self.assertIn("holds the project lock", result.stderr)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 3, stdout + stderr)
            self.assertFalse((self.root / "fake-build-active").exists())
            self.assertEqual(self.read("status.json")["phase"], "stopped")
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)
        self.control()
        self.run_verifier()


if __name__ == "__main__":
    unittest.main()
