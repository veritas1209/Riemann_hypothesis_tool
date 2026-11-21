#!/usr/bin/env python3
"""Install the pinned environment, then compile and audit the published archive.

Ubuntu x86_64 only. No Python packages are required. Each work directory owns
its elan installation, source tree, build artifacts, checkpoints and logs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time

KIT = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3
TOOLCHAIN = "leanprover/lean4:v4.33.1"
LEAN_COMMIT = "819816b2e0a3bf405af45ae5c7af2491d8f5bee6"
ELAN_SHA = "0e8250aa2be86c7af929ade68db617af9edc8b9ccf6eaf5ece228250d0d032e2"
ELAN_URL = "https://github.com/leanprover/elan/releases/download/v4.4.0/elan-x86_64-unknown-linux-gnu.tar.gz"
PACKAGES = ("ca-certificates", "curl", "git", "python3", "build-essential",
            "libgmp-dev", "unzip", "zstd", "util-linux", "procps")
STOP_REQUESTED = False
ACTIVE_CHILD = None
ACTIVE_IS_RUNNER = False


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def plain_path(path: Path) -> Path:
    """An owned work tree must not redirect writes through symlink components."""
    path = Path(os.path.abspath(path.expanduser()))
    for item in (path, *path.parents):
        if item.is_symlink():
            raise RuntimeError(f"Symlink path is not supported: {item}")
    return path


def write_json(path, value):
    plain_path(path)
    temporary = plain_path(path.with_name(path.name + ".tmp"))
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    plain_path(path)
    return json.loads(path.read_text(encoding="utf-8"))


def memory_available():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise RuntimeError("/proc/meminfo has no MemAvailable")


def auto_threads(available, cpus):
    return max(1, min(8, cpus, int(max(0, available / GIB - 8) // 4)))


def check_platform():
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise RuntimeError("Run in x86_64 Ubuntu 22.04/24.04 (WSL2 Ubuntu is supported)")
    info = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            info[key] = value.strip('"')
    if info.get("ID") != "ubuntu" or info.get("VERSION_ID") not in {"22.04", "24.04"}:
        raise RuntimeError("Supported systems: Ubuntu 22.04 and 24.04")
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required")
    return info


def request_stop(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print("\nStop requested. Waiting for the current setup command / build batch to finish.", flush=True)
    if ACTIVE_IS_RUNNER and ACTIVE_CHILD is not None and ACTIVE_CHILD.poll() is None:
        os.kill(ACTIVE_CHILD.pid, signal.SIGINT)


class Paused(Exception):
    pass


class Bootstrap:
    def __init__(self, args, os_info):
        self.args = args
        self.work = plain_path(args.work_dir)
        self.work.mkdir(parents=True, exist_ok=True)
        self.project = plain_path(self.work / "project")
        self.logs = plain_path(self.work / "logs")
        self.logs.mkdir(exist_ok=True)
        self.status_path = self.work / "bootstrap-status.json"
        self.status = {"started_at": now(), "phase": "preflight", "work_dir": str(self.work),
                       "os": os_info.get("PRETTY_NAME"), "completed": False,
                       "scope": "Source compilation and five endpoint axiom checks"}
        self.env = dict(os.environ)
        self.env.update(ELAN_HOME=str(self.work / "tools" / "elan"),
                        ELAN_TOOLCHAIN=TOOLCHAIN, XDG_CACHE_HOME=str(self.work / "cache"))
        # LAKE_HOME denotes Lake's own installation, not a cache directory.
        self.env.pop("LAKE_HOME", None)
        self.env.pop("LEAN_PATH", None)
        self.env.pop("LEAN_SRC_PATH", None)
        self.env["PATH"] = str(self.work / "tools" / "elan" / "bin") + os.pathsep + self.env.get("PATH", "")
        self.threads = args.threads or auto_threads(memory_available(), os.cpu_count() or 1)
        self.env["LEAN_NUM_THREADS"] = str(self.threads)

    def report(self, phase, **extra):
        self.status.update(phase=phase, updated_at=now(), **extra)
        write_json(self.status_path, self.status)
        print(f"[{now()}] {phase}", flush=True)

    def check_stop(self):
        if STOP_REQUESTED or (self.work / "STOP").exists() or (self.project / ".verification-run" / "STOP").exists():
            raise Paused("Stop requested. Use the same command with --resume to clear STOP markers.")

    def command(self, label, command, *, cwd=None, runner=False):
        global ACTIVE_CHILD, ACTIVE_IS_RUNNER
        self.check_stop()
        self.report(label)
        log = plain_path(self.logs / f"{label}-{time.time_ns()}.log")
        print("Command:", " ".join(map(str, command)), flush=True)
        with log.open("xb") as output:
            # sudo needs the caller's controlling terminal to prompt normally.
            separate_session = str(command[0]) != "sudo"
            process = subprocess.Popen(list(map(str, command)), cwd=cwd or self.work,
                                       env=self.env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=separate_session)
            ACTIVE_CHILD, ACTIVE_IS_RUNNER = process, runner
            try:
                for chunk in iter(lambda: process.stdout.read1(16384), b""):
                    output.write(chunk)
                    output.flush()
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                code = process.wait()
            except BaseException:
                # Drain the pipe even if recording/printing failed, so a child
                # cannot deadlock on a full pipe while we retain its lock.
                for _ in iter(lambda: process.stdout.read1(16384), b""):
                    pass
                raise
            finally:
                # Keep the work lock until all commands we started have finished.
                process.wait()
                ACTIVE_CHILD, ACTIVE_IS_RUNNER = None, False
        self.status.update(last_log=str(log), last_exit_code=code)
        if runner and code == 3:
            raise Paused("Verification stopped or reached a resource threshold; inspect --status.")
        if code:
            raise RuntimeError(f"{label} failed (exit {code}); see {log}")
        self.check_stop()

    def capture(self, command, cwd=None):
        result = subprocess.run(list(map(str, command)), cwd=cwd or self.work, env=self.env,
                                check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout.strip()

    def sudo(self):
        if os.geteuid() == 0:
            return []
        if not shutil.which("sudo"):
            raise RuntimeError("sudo is required to install missing system packages")
        return ["sudo"]

    def preflight(self):
        initial = not (self.project / ".remark-source.json").is_file()
        minimum = self.args.min_initial_free_gib if initial else self.args.min_free_gib
        free, available = shutil.disk_usage(self.work).free, memory_available()
        self.report("preflight", threads=self.threads, batch_size=self.args.batch_size,
                    disk_free_bytes=free, memory_available_bytes=available,
                    initial_disk_guard_gib=self.args.min_initial_free_gib)
        if free < minimum * GIB:
            raise RuntimeError(f"Need {minimum:g} GiB free in {self.work}; available {free / GIB:.1f} GiB. "
                               "Choose a larger disk with --work-dir.")
        ram_guard = 32 if initial else self.args.min_available_gib
        if available < ram_guard * GIB:
            raise RuntimeError(f"This launch requires {ram_guard:g} GiB available RAM; "
                               f"available {available / GIB:.1f} GiB. This is a conservative guard, not a measured minimum.")
        filesystem = subprocess.run(["stat", "-f", "-c", "%T", str(self.work)], text=True,
                                    capture_output=True, check=True).stdout.strip()
        if filesystem in {"9p", "drvfs", "fuseblk"}:
            raise RuntimeError("Use a Linux filesystem for --work-dir, not /mnt/c or another Windows mount.")

    def dependencies(self):
        missing = []
        for package in PACKAGES:
            result = subprocess.run(["dpkg-query", "-W", "-f=${Status}", package],
                                    text=True, capture_output=True)
            if result.returncode or result.stdout.strip() != "install ok installed":
                missing.append(package)
        if missing:
            print("Installing system packages (sudo may request your password):", ", ".join(missing), flush=True)
            self.command("apt-update", [*self.sudo(), "apt-get", "update"])
            self.command("apt-install", [*self.sudo(), "apt-get", "install", "-y", *missing])
        maps = int(Path("/proc/sys/vm/max_map_count").read_text())
        if maps < 1048576:
            print("Raising vm.max_map_count to 1048576 for this boot; sudo may request your password.", flush=True)
            self.command("mmap-limit", [*self.sudo(), "sysctl", "-w", "vm.max_map_count=1048576"])

    def toolchain(self):
        tools = plain_path(self.work / "tools")
        tools.mkdir(exist_ok=True)
        elan_dir = plain_path(tools / "elan")
        elan = plain_path(elan_dir / "bin" / "elan")
        if not elan.is_file():
            installer = plain_path(tools / "elan-installer")
            installer.mkdir(exist_ok=True)
            archive = plain_path(installer / "elan.tar.gz")
            if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != ELAN_SHA:
                partial = plain_path(installer / "elan.tar.gz.download")
                self.command("download-elan", ["curl", "--proto", "=https", "--proto-redir", "=https",
                             "--tlsv1.2", "--fail", "--location", "--retry", "3", "--output", partial, ELAN_URL])
                if hashlib.sha256(partial.read_bytes()).hexdigest() != ELAN_SHA:
                    raise RuntimeError("Pinned elan installer SHA-256 mismatch")
                partial.replace(archive)
            self.command("unpack-elan", ["tar", "-xzf", archive, "-C", installer])
            self.command("install-elan", [installer / "elan-init", "-y", "--no-modify-path", "--default-toolchain", "none"])
        self.command("install-lean", [elan, "toolchain", "install", TOOLCHAIN])
        lean = elan_dir / "bin" / "lean"
        commit = self.capture([lean, "--githash"])
        if commit != LEAN_COMMIT:
            raise RuntimeError(f"Lean compiler revision mismatch: {commit}")
        self.status.update(lean_version=self.capture([lean, "--version"]), lean_commit=commit)

    def prepare(self):
        self.dependencies()
        self.toolchain()
        self.command("source", [sys.executable, KIT / "scripts" / "fetch_source.py", "--work-dir", self.work])
        # Import only our local checksum helper; never evaluate downloaded source in Python.
        from fetch_source import CONFIG_HASHES
        def check_configs():
            for name, expected in CONFIG_HASHES.items():
                path = plain_path(self.project / name)
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    raise RuntimeError(f"Original configuration changed: {name}")
        check_configs()
        lake = self.work / "tools" / "elan" / "bin" / "lake"
        self.command("mathlib-cache", [lake, "exe", "cache", "get"], cwd=self.project)
        check_configs()
        manifest = read_json(self.project / "lake-manifest.json")
        versions = {}
        for package in manifest["packages"]:
            directory = plain_path(self.project / ".lake" / "packages" / package["name"])
            revision = self.capture(["git", "rev-parse", "HEAD"], cwd=directory)
            if revision != package["rev"]:
                raise RuntimeError(f"Dependency revision differs: {package['name']}")
            if self.capture(["git", "status", "--porcelain", "--untracked-files=no"], cwd=directory):
                raise RuntimeError(f"Tracked dependency files changed: {package['name']}")
            versions[package["name"]] = revision
        checks = plain_path(self.project / "checks")
        checks.mkdir(exist_ok=True)
        for name in ("Axioms.lean", "Smoke.lean"):
            source, target = KIT / "checks" / name, plain_path(checks / name)
            expected = source.read_bytes()
            if target.exists() and target.read_bytes() != expected:
                raise RuntimeError(f"Refusing to overwrite a different existing check: {target}")
            if not target.exists():
                target.write_bytes(expected)
        self.command("statements", [lake, "build", "+Statements:olean"], cwd=self.project)
        self.command("smoke", [lake, "env", "lean", "checks/Smoke.lean"], cwd=self.project)
        check_configs()
        write_json(self.work / "provenance.json", {"recorded_at": now(), "toolchain": TOOLCHAIN,
                   "lean_commit": LEAN_COMMIT, "dependencies": versions,
                   "source": read_json(self.project / ".remark-source.json"),
                   "config_sha256": CONFIG_HASHES, "threads": self.threads,
                   "batch_size": self.args.batch_size, "platform": platform.platform()})

    def run(self):
        self.preflight()
        self.check_stop()
        self.prepare()
        if self.args.prepare_only:
            self.report("prepared", completed=False, note="Environment and smoke test only; full proof not checked")
            return 0
        command = [sys.executable, KIT / "scripts" / "verify_runner.py", "--project", self.project,
                   "--threads", self.threads, "--batch-size", self.args.batch_size,
                   "--min-free-gib", self.args.min_free_gib, "--min-available-gib", self.args.min_available_gib]
        self.command("verification", command, runner=True)
        result = read_json(self.project / ".verification-run" / "status.json")
        if (result.get("phase") != "completed" or not result.get("full_build_completed")
                or not result.get("axiom_audit_passed")):
            raise RuntimeError("Runner exited without a completed build and successful axiom audit")
        self.report("completed", completed=True, full_build_completed=True, axiom_audit_passed=True,
                    independent_kernel_recheck_performed=False, semantic_review_performed=False)
        print(f"PASS: source build and all five endpoint axiom checks. Results: {self.project / '.verification-run'}")
        return 0


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--work-dir", type=Path, default=KIT / ".replay", help="Linux filesystem for all downloads/tools/builds")
    result.add_argument("--threads", type=int, help="Lean worker-pool threads; default is resource-aware, at most 8")
    result.add_argument("--batch-size", type=int, default=2048)
    result.add_argument("--min-initial-free-gib", type=float, default=160)
    result.add_argument("--min-free-gib", type=float, default=20)
    result.add_argument("--min-available-gib", type=float, default=8)
    actions = result.add_mutually_exclusive_group()
    actions.add_argument("--status", action="store_true", help="Read status without installing or running anything")
    actions.add_argument("--stop", action="store_true", help="Request stop after the current command/batch")
    actions.add_argument("--prepare-only", action="store_true", help="Install, download, and smoke test; do not run full verification")
    result.add_argument("--resume", action="store_true", help="Clear STOP markers and resume verified checkpoints")
    return result


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if args.batch_size < 1 or (args.threads is not None and args.threads < 1):
        cli.error("threads and batch size must be positive")
    if any(not (0 <= value < float('inf')) for value in (args.min_initial_free_gib, args.min_free_gib, args.min_available_gib)):
        cli.error("Resource thresholds must be finite and nonnegative")
    if args.resume and (args.status or args.stop):
        cli.error("--resume cannot be combined with --status or --stop")
    owner = None
    locked = False
    try:
        work = plain_path(args.work_dir)
        runner_dir = plain_path(work / "project" / ".verification-run")
        if args.status:
            for label, path in (("bootstrap", work / "bootstrap-status.json"), ("verification", runner_dir / "status.json")):
                print(json.dumps({label: read_json(path) if path.is_file() else {"phase": "not_started"}}, ensure_ascii=False, indent=2))
            return 0
        if args.stop:
            if not work.is_dir():
                raise RuntimeError("No work directory exists; nothing to stop")
            plain_path(work / "STOP").touch()
            if runner_dir.is_dir():
                plain_path(runner_dir / "STOP").touch()
            print("Stop requested; the active setup command or build batch will finish first.")
            return 0
        os_info = check_platform()
        work.mkdir(parents=True, exist_ok=True)
        import fcntl
        with plain_path(work / "bootstrap.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another verification owns this work directory; use --status or --stop")
            locked = True
            if args.resume:
                for marker in (work / "STOP", runner_dir / "STOP"):
                    plain_path(marker).unlink(missing_ok=True)
            owner = Bootstrap(args, os_info)
            signal.signal(signal.SIGINT, request_stop)
            signal.signal(signal.SIGTERM, request_stop)
            try:
                return owner.run()
            except Paused as error:
                owner.report("paused", reason=str(error))
                print(str(error), file=sys.stderr)
                return 3
            except Exception as error:
                failed_stage = owner.status.get("phase")
                owner.report("failed", failed_stage=failed_stage, error=str(error))
                print(f"ERROR: {error}", file=sys.stderr)
                return 1
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
