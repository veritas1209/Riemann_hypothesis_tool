#!/usr/bin/env python3
"""Resume a bounded-batch stock Lake build, retaining proof artifacts.

Run on Ubuntu as the project owner. This script never changes Lean sources,
toolchain/manifests, host settings, or processes it did not start. A STOP file in
.verification-run requests a stop after the active Lake command finishes.

LEAN_NUM_THREADS reduces worker-pool concurrency; it is not a hard RAM/job cap.
Use an external user-service memory limit when a hard resource bound is needed.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

GIB = 1024 ** 3
EXPECTED_TOOLCHAIN = "leanprover/lean4:v4.33.1"
ENDPOINTS = (
    "riemannHypothesis",
    "riemannZeta_eq_zero_re",
    "LFunction_ne_zero_of_half_lt_re",
    "LFunction_eq_zero_re",
    "LFunction_eq_zero_re_of_isPrimitive",
)
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
MODULE_NAME = re.compile(r"(?:[^\W\d]|_)[\w']*(?:\.(?:[^\W\d]|_)[\w']*)*\Z")
IMPORT_LINE = re.compile(r"(?:(?:public|private|meta)\s+)*import\s+(.+)\Z")
STOP_REQUESTED = False
STATE_VERSION = 2
LEGACY_PROOF_SUFFIXES = (".olean",)
MODULE_PROOF_SUFFIXES = (".olean", ".olean.private", ".olean.server")
COMPLETION_SCOPE = (
    "Stock Lake compilation of Main's transitive local source imports and an "
    "axiom audit of the five named endpoints. Unreachable local modules are not "
    "built; imported dependency artifacts may come from the upstream cache. "
    "This does not perform semantic review, independent kernel replay, or "
    "a clean rebuild of all dependency sources."
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise RuntimeError(f"Refusing symlink status path: {path}")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def plain_directory(path: Path, root: Path) -> Path:
    """Reject symlink components and anything outside the selected project."""
    relative = path.relative_to(root)
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise RuntimeError(f"Refusing symlink component: {current}")
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise RuntimeError(f"Path escaped project: {path}")
    return resolved


def safe_file(path: Path, root: Path) -> Path:
    plain_directory(path.parent, root)
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlink file: {path}")
    return path


@contextmanager
def project_lock(path: Path):
    """Keep the descriptor alive for the whole run, including child completion."""
    if os.name != "posix":
        raise RuntimeError("Run this verifier inside Linux/WSL; POSIX flock is required")
    import fcntl
    if path.is_symlink():
        raise RuntimeError("Refusing symlink runner lock")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another verification runner holds the project lock") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def parse_header(text: str) -> tuple[list[str], bool]:
    """Read imports and the module opt-in, including nested and line comments.

    The supplied project uses one-line imports. Unsupported split import syntax
    is rejected rather than silently constructing an incomplete graph.
    """
    imports: list[str] = []
    is_module = False
    block_depth = 0
    for line in text.splitlines():
        if block_depth == 0 and "/-" not in line and "--" not in line:
            cleaned = line.strip()
        else:
            chars: list[str] = []
            index = 0
            while index < len(line):
                if block_depth:
                    if line.startswith("/-", index):
                        block_depth += 1
                        index += 2
                    elif line.startswith("-/", index):
                        block_depth -= 1
                        index += 2
                    else:
                        index += 1
                elif line.startswith("--", index):
                    break
                elif line.startswith("/-", index):
                    block_depth += 1
                    index += 2
                    chars.append(" ")
                else:
                    chars.append(line[index])
                    index += 1
            cleaned = "".join(chars).strip()
        if cleaned == "module":
            is_module = True
            continue
        if not cleaned or cleaned == "prelude":
            continue
        match = IMPORT_LINE.fullmatch(cleaned)
        if match:
            tokens = match.group(1).split()
            if tokens and tokens[0] == "all":
                tokens = tokens[1:]
            if not tokens or any(not MODULE_NAME.fullmatch(token) for token in tokens):
                raise ValueError(f"Unsupported import syntax: {cleaned!r}")
            imports.extend(tokens)
        elif cleaned == "import" or re.fullmatch(r"(?:(?:public|private|meta)\s+)+import", cleaned):
            raise ValueError("Split-line import header needs explicit parser support")
        else:
            break
    if block_depth:
        raise ValueError("Unterminated comment in import header")
    return imports, is_module


def header_imports(text: str) -> list[str]:
    return parse_header(text)[0]


def build_graph(project: Path, progress) -> tuple[list[str], dict[str, Path], str, dict, set[str]]:
    source = plain_directory(project / "src", project)
    paths: dict[str, Path] = {}
    imports: dict[str, list[str]] = {}
    module_headers: set[str] = set()
    fingerprint = hashlib.sha256()
    for name in ("lean-toolchain", "lakefile.toml", "lake-manifest.json"):
        path = project / name
        safe_file(path, project)
        fingerprint.update(name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    if not source.is_dir():
        raise ValueError("Project must contain its Lean sources in src/")
    files = []
    for folder, directories, filenames in os.walk(source, followlinks=False):
        for name in directories:
            if (Path(folder) / name).is_symlink():
                raise RuntimeError(f"Refusing source directory symlink: {Path(folder) / name}")
        files.extend(Path(folder) / name for name in filenames if name.endswith(".lean"))
    files.sort()
    for index, path in enumerate(files, 1):
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise RuntimeError(f"Refusing source symlink/outside path: {path}")
        relative = path.relative_to(source)
        module = ".".join(relative.with_suffix("").parts)
        if not MODULE_NAME.fullmatch(module) or module in paths:
            raise ValueError(f"Unsupported or duplicate module name: {module}")
        data = path.read_bytes()
        fingerprint.update(relative.as_posix().encode() + b"\0" + hashlib.sha256(data).digest())
        paths[module] = relative
        try:
            imports[module], is_module = parse_header(data.decode("utf-8-sig"))
            if is_module:
                module_headers.add(module)
        except ValueError as error:
            raise ValueError(f"{path}: {error}") from error
        if index % 5000 == 0:
            progress(index, len(files))
    if "Main" not in paths:
        raise ValueError("Main.lean was not found")
    local_roots = {module.split(".")[0] for module in paths}
    missing = sorted({dep for values in imports.values() for dep in values
                      if dep.split(".")[0] in local_roots and dep not in paths})
    if missing:
        raise ValueError(f"Unresolved local imports: {missing[:20]}")
    dependencies = {module: set(dep for dep in values if dep in paths)
                    for module, values in imports.items()}
    reachable: set[str] = set()
    pending = ["Main"]
    while pending:
        module = pending.pop()
        if module not in reachable:
            reachable.add(module)
            pending.extend(dependencies[module])
    reverse: dict[str, list[str]] = {module: [] for module in reachable}
    remaining = {module: len(dependencies[module]) for module in reachable}
    for module in reachable:
        for dependency in dependencies[module]:
            reverse[dependency].append(module)
    ready = [module for module, count in remaining.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        module = heapq.heappop(ready)
        ordered.append(module)
        for user in reverse[module]:
            remaining[user] -= 1
            if remaining[user] == 0:
                heapq.heappush(ready, user)
    if len(ordered) != len(reachable):
        raise ValueError("Cycle detected in the local import graph")
    summary = {"source_modules": len(paths), "main_closure_modules": len(ordered),
               "unreachable_source_modules": len(paths) - len(ordered),
               "main_closure_module_header_modules": len(reachable & module_headers),
               "main_closure_legacy_modules": len(reachable - module_headers),
               "scope": COMPLETION_SCOPE,
               "local_import_edges": sum(len(dependencies[m]) for m in reachable),
               "order_sha256": hashlib.sha256("\n".join(ordered).encode()).hexdigest()}
    return ordered, paths, fingerprint.hexdigest(), summary, module_headers


def validate_audit_file(project: Path) -> str:
    audit_file = safe_file(project / "checks" / "Axioms.lean", project)
    data = audit_file.read_bytes()
    expected = ["import Main", *(f"#print axioms {name}" for name in ENDPOINTS)]
    actual = [line.strip() for line in data.decode("utf-8-sig").splitlines() if line.strip()]
    if actual != expected:
        raise RuntimeError("checks/Axioms.lean must contain exactly import Main and the five expected #print axioms commands")
    return hashlib.sha256(data).hexdigest()


def first_missing_artifact(project: Path, modules: list[str], paths: dict[str, Path],
                           module_headers: set[str]) -> tuple[int, str] | None:
    """Presence checks support restart bookkeeping, not cryptographic validation.

    A successful Lake command remains responsible for freshness/type checking.
    Never trust a saved cursor when a recorded proof artifact has disappeared.
    Lean 4.33.1 emits split oleans only for sources opting in with `module`.
    Header modes are captured during graph scanning, never reread per batch.
    """
    lib_root = plain_directory(project / ".lake" / "build" / "lib" / "lean", project)
    checked_parents: set[Path] = set()
    for index, module in enumerate(modules):
        suffixes = MODULE_PROOF_SUFFIXES if module in module_headers else LEGACY_PROOF_SUFFIXES
        for suffix in suffixes:
            path = lib_root / paths[module].with_suffix(suffix)
            if path.parent not in checked_parents:
                plain_directory(path.parent, project)
                checked_parents.add(path.parent)
            if path.is_symlink():
                raise RuntimeError(f"Refusing symlink proof artifact: {path}")
            if not path.is_file() or path.stat().st_size == 0:
                return index, module
    return None


def resources(project: Path, process_pid: int | None = None) -> dict:
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.split()[0]) * 1024
    result = {"time": utc_now(), "disk_free_bytes": shutil.disk_usage(project).free,
              "memory_total_bytes": memory["MemTotal"],
              "memory_available_bytes": memory["MemAvailable"],
              "swap_used_bytes": memory["SwapTotal"] - memory["SwapFree"],
              "load_average": os.getloadavg()}
    if process_pid is not None:
        pids, queue, rss = set(), [process_pid], 0
        while queue:
            pid = queue.pop()
            if pid in pids:
                continue
            pids.add(pid)
            proc = Path(f"/proc/{pid}")
            try:
                for line in (proc / "status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        rss += int(line.split()[1]) * 1024
                for task in (proc / "task").iterdir():
                    try:
                        queue.extend(int(child) for child in (task / "children").read_text().split())
                    except (FileNotFoundError, ProcessLookupError, PermissionError):
                        pass
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass
        result.update(process_count=len(pids), process_tree_rss_sum_bytes=rss,
                      rss_note="Sum of owned process-tree RSS; shared pages may be counted repeatedly")
    return result


def cleanup_setups(project: Path, modules: list[str], paths: dict[str, Path]) -> dict:
    ir_root = plain_directory(project / ".lake" / "build" / "ir", project)
    count = size = 0
    for module in modules:
        path = ir_root / paths[module].with_suffix(".setup.json")
        plain_directory(path.parent, project)
        if path.is_symlink():
            raise RuntimeError(f"Refusing setup symlink: {path}")
        if not path.exists():
            continue
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(ir_root) or not path.name.endswith(".setup.json") or not path.is_file():
            raise RuntimeError(f"Refusing unsafe setup cleanup: {path}")
        size += path.stat().st_size
        path.unlink()
        count += 1
    return {"removed_setup_files": count, "removed_setup_bytes": size}


def parse_axioms(output: str) -> dict:
    found: dict[str, list[str]] = {}
    pattern = re.compile(r"'([^']+)'\s+(?:depends on axioms:\s*\[([^\]]*)\]|does not depend on any axioms)")
    for match in pattern.finditer(output):
        name = match.group(1)
        if name not in ENDPOINTS:
            continue
        if name in found:
            raise ValueError(f"Duplicate axiom report for {name}")
        found[name] = [] if match.group(2) is None else [value.strip() for value in match.group(2).split(",") if value.strip()]
    missing = set(ENDPOINTS) - found.keys()
    if missing:
        raise ValueError(f"Missing endpoint axiom reports: {sorted(missing)}")
    unexpected = {name: sorted(set(axioms) - ALLOWED_AXIOMS)
                  for name, axioms in found.items() if set(axioms) - ALLOWED_AXIOMS}
    return {"passed": not unexpected, "endpoints": found, "unexpected_axioms": unexpected}


class Runner:
    def __init__(self, args):
        self.args = args
        self.project = args.project.expanduser().resolve(strict=True)
        self.directory = plain_directory(self.project / ".verification-run", self.project)
        self.directory.mkdir(exist_ok=True)
        self.logs = plain_directory(self.directory / "logs", self.project)
        self.logs.mkdir(exist_ok=True)
        self.stop_file = self.directory / "STOP"
        self.state_file = self.directory / "state.json"
        self.status_file = self.directory / "status.json"
        if self.state_file.is_symlink():
            raise RuntimeError("Refusing symlink resume state")
        # Load state only after acquiring the project lock.
        self.state = {}
        self.status = {"pid": os.getpid(), "started_at": utc_now(), "project": str(self.project),
                       "threads": args.threads, "batch_size": args.batch_size,
                       "full_build_completed": False,
                       "axiom_audit_passed": False, "semantic_review_performed": False,
                       "independent_kernel_recheck_performed": False,
                       "completion_scope": COMPLETION_SCOPE,
                       "audited_endpoints": list(ENDPOINTS),
                       "artifact_presence_check_only": True,
                       "required_artifact_suffixes": {
                           "all_modules": list(LEGACY_PROOF_SUFFIXES),
                           "module_header_only": list(MODULE_PROOF_SUFFIXES[1:])},
                       "artifact_requirements_note": "Lean 4.33.1: .olean for every module; .olean.private and .olean.server additionally required only for sources with the module header",
                       "lake_trace_freshness": "Handled by stock Lake, including the final +Main:olean build"}
        self.environment = dict(os.environ, LEAN_NUM_THREADS=str(args.threads), ELAN_TOOLCHAIN=EXPECTED_TOOLCHAIN)
        self.environment.pop("LEAN_PATH", None)
        self.environment.pop("LEAN_SRC_PATH", None)
        # Honor the install chosen by the wrapper/user, including custom ELAN_HOME.
        # Otherwise keep PATH unchanged; never force a different user's ~/.elan.
        if self.environment.get("ELAN_HOME"):
            elan_bin = str(Path(self.environment["ELAN_HOME"]).expanduser() / "bin")
            self.environment["PATH"] = elan_bin + os.pathsep + self.environment.get("PATH", "")
        self.lake = shutil.which("lake", path=self.environment["PATH"])
        if not self.lake:
            raise RuntimeError("lake not found; install the pinned Lean toolchain first")
        self.lock_held = False

    def report(self, phase: str, **values) -> None:
        self.status.update(phase=phase, updated_at=utc_now(), **values)
        atomic_json(self.status_file, self.status)

    def event(self, name: str, **values) -> None:
        event = {"time": utc_now(), "event": name, **values}
        with safe_file(self.directory / "events.jsonl", self.project).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        if name == "batch_committed":
            done = values["completed_modules"]
            total = self.status["total_modules"]
            message = f"Built {done:,}/{total:,} modules ({100 * done / total:.2f}%)"
        elif name == "command_started":
            message = f"{values['label']} started; log: {values['log']}"
        elif name == "command_finished":
            message = f"{values['label']} finished in {values['seconds']}s (exit {values['exit_code']})"
        elif name == "graph_ready":
            message = f"Dependency scan complete; resuming at module {values['resume_cursor']:,}"
        else:
            message = name + " " + json.dumps(values, ensure_ascii=False)
        print(f"[{event['time']}] {message}", flush=True)

    def should_stop(self) -> bool:
        return STOP_REQUESTED or self.stop_file.exists()

    def preflight(self) -> bool:
        sample = resources(self.project)
        if self.should_stop():
            self.report("stopped", resources=sample, reason="Stop requested; remove STOP to resume")
            return False
        if sample["disk_free_bytes"] < self.args.min_free_gib * GIB:
            self.report("paused_low_disk", resources=sample)
            self.event("paused_low_disk", minimum_gib=self.args.min_free_gib, resources=sample)
            return False
        if sample["memory_available_bytes"] < self.args.min_available_gib * GIB:
            self.report("paused_low_memory", resources=sample)
            self.event("paused_low_memory", minimum_gib=self.args.min_available_gib, resources=sample)
            return False
        return True

    def command(self, label: str, arguments: list[str]) -> tuple[int, Path]:
        log = self.logs / f"{label}-{time.time_ns()}.log"
        self.event("command_started", label=label, command=arguments, log=str(log))
        with log.open("xb") as stream:
            process = subprocess.Popen(arguments, cwd=self.project, env=self.environment,
                                       stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
            started, last_sample = time.monotonic(), 0.0
            try:
                while True:
                    now = time.monotonic()
                    if now - last_sample >= self.args.poll_seconds:
                        sample = resources(self.project, process.pid)
                        with safe_file(self.directory / "resources.jsonl", self.project).open("a", encoding="utf-8") as metrics:
                            metrics.write(json.dumps({"label": label, **sample}) + "\n")
                        self.report("stopping_after_command" if self.should_stop() else label,
                                    child_pid=process.pid, current_log=str(log),
                                    command_elapsed_seconds=round(now - started, 1), resources=sample)
                        last_sample = now
                    code = process.poll()
                    if code is not None:
                        break
                    time.sleep(min(0.25, self.args.poll_seconds))
            except BaseException:
                # Retain the project lock until our compiler command really ends.
                # In particular, a logging error must not leave an untracked build.
                process.wait()
                raise
        self.event("command_finished", label=label, exit_code=code,
                   seconds=round(time.monotonic() - started, 1), log=str(log))
        if code:
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 65536))
                tail = stream.read().decode("utf-8", errors="replace")[-12000:]
            failed = re.findall(r"[✖✗]\s*\[[^\]]+\]\s*(?:Building|Built)\s+([^\s(]+)", tail)
            self.report("command_failed", failed_stage=label, exit_code=code,
                        failed_targets=sorted(set(failed)), failure_log=str(log), failure_tail=tail)
        return code, log

    def run(self) -> int:
        lock_path = self.directory / "runner.lock"
        with project_lock(lock_path):
            self.lock_held = True
            try:
                return self.run_locked()
            except Exception as error:
                if self.state.get("schema_version") == STATE_VERSION:
                    self.state.update(full_build_completed=False, axiom_audit_passed=False)
                    atomic_json(self.state_file, self.state)
                self.report("runner_error", error=f"{type(error).__name__}: {error}",
                            full_build_completed=False, axiom_audit_passed=False)
                raise
            finally:
                self.lock_held = False

    def run_locked(self) -> int:
        self.state = json.loads(safe_file(self.state_file, self.project).read_text(encoding="utf-8")) if self.state_file.exists() else {}
        if not isinstance(self.state, dict):
            raise RuntimeError("Resume state must be a JSON object")
        if self.state and self.state.get("schema_version") != STATE_VERSION:
            raise RuntimeError("Unsupported resume-state version; use a fresh verification state directory")
        required = {"project", "input_fingerprint", "order_sha256", "audit_fingerprint", "total_modules", "completed_modules"}
        if self.state and not required.issubset(self.state):
            raise RuntimeError("Incomplete resume state; source identity cannot be established")
        # Prior completion is historical. It never satisfies this run's checks.
        self.state.update(full_build_completed=False, axiom_audit_passed=False)
        if self.state.get("schema_version") == STATE_VERSION:
            atomic_json(self.state_file, self.state)
        self.report("starting", full_build_completed=False, axiom_audit_passed=False)
        if (self.project / "lean-toolchain").read_text().strip() != EXPECTED_TOOLCHAIN:
            raise RuntimeError(f"This runner requires {EXPECTED_TOOLCHAIN}")
        audit_fingerprint = validate_audit_file(self.project)
        if not self.preflight():
            return 3
        self.report("scanning_sources")
        def progress(done, total):
            self.report("scanning_sources", scanned_modules=done, source_modules=total)
        ordered, paths, fingerprint, graph, module_headers = build_graph(self.project, progress)
        if self.state.get("input_fingerprint") and (self.state.get("project") != str(self.project) or
                           self.state.get("input_fingerprint") != fingerprint or
                           self.state.get("order_sha256") != graph["order_sha256"] or
                           self.state.get("audit_fingerprint") != audit_fingerprint):
            raise RuntimeError("Source/configuration or module order changed; refusing stale resume state")
        cursor = self.state.get("completed_modules", 0)
        if type(cursor) is not int or not 0 <= cursor <= len(ordered):
            raise RuntimeError("Invalid resume cursor")
        missing = first_missing_artifact(self.project, ordered[:cursor], paths, module_headers)
        if missing is not None:
            previous = cursor
            cursor, module = missing
            self.event("resume_artifact_missing", module=module, previous_cursor=previous, rewind_cursor=cursor)
        self.state.update(schema_version=STATE_VERSION, project=str(self.project), input_fingerprint=fingerprint,
                          audit_fingerprint=audit_fingerprint, order_sha256=graph["order_sha256"],
                          total_modules=len(ordered), completed_modules=cursor)
        atomic_json(self.directory / "graph-summary.json", graph)
        atomic_json(self.state_file, self.state)
        self.status.update(total_modules=len(ordered), completed_modules=cursor,
                           source_modules=graph["source_modules"],
                           unreachable_source_modules=graph["unreachable_source_modules"],
                           main_closure_module_header_modules=graph["main_closure_module_header_modules"],
                           main_closure_legacy_modules=graph["main_closure_legacy_modules"],
                           input_fingerprint=fingerprint, validated_resume_modules=cursor)
        self.event("graph_ready", **graph, resume_cursor=cursor)
        batches = 0
        while cursor < len(ordered):
            if not self.preflight():
                return 3
            batch = ordered[cursor:cursor + self.args.batch_size]
            self.status.update(batch_start=cursor, batch_end=cursor + len(batch),
                               batch_first=batch[0], batch_last=batch[-1])
            arguments = [self.lake, "build", *(f"+{module}:olean" for module in batch)]
            code, _ = self.command(f"batch-{cursor:06d}-{cursor + len(batch):06d}", arguments)
            if code:
                return 1
            missing = first_missing_artifact(self.project, batch, paths, module_headers)
            if missing is not None:
                raise RuntimeError(f"Lake succeeded but proof artifact is missing/empty: {missing[1]}")
            cleaned = cleanup_setups(self.project, batch, paths)
            cursor += len(batch)
            batches += 1
            self.state.update(completed_modules=cursor, updated_at=utc_now())
            atomic_json(self.state_file, self.state)
            self.status["completed_modules"] = cursor
            self.event("batch_committed", completed_modules=cursor, **cleaned)
            if self.args.max_batches and batches >= self.args.max_batches:
                self.report("paused_batch_limit")
                return 3
        if not self.preflight():
            return 3
        code, _ = self.command("final_main_build", [self.lake, "build", "+Main:olean"])
        if code:
            return 1
        missing = first_missing_artifact(self.project, ordered, paths, module_headers)
        if missing is not None:
            raise RuntimeError(f"Final build did not retain proof artifact: {missing[1]}")
        self.event("final_setup_cleanup", **cleanup_setups(self.project, ordered, paths))
        self.state["full_build_completed"] = True
        atomic_json(self.state_file, self.state)
        self.status["full_build_completed"] = True
        if not self.preflight():
            return 3
        if validate_audit_file(self.project) != audit_fingerprint:
            raise RuntimeError("Audit file changed while verification was running")
        code, log = self.command("axiom_audit", [self.lake, "env", "lean", "--tstack=400000", "checks/Axioms.lean"])
        if code:
            return 1
        if not self.preflight():
            return 3
        audit = parse_axioms(log.read_text(encoding="utf-8", errors="replace"))
        self.report("checking_final_inputs", axiom_audit_passed=False)
        final_order, _, final_fingerprint, _, _ = build_graph(self.project, progress)
        if (final_fingerprint != fingerprint or final_order != ordered or
                validate_audit_file(self.project) != audit_fingerprint):
            raise RuntimeError("Sources/configuration changed during verification; refusing completion")
        missing = first_missing_artifact(self.project, ordered, paths, module_headers)
        if missing is not None:
            raise RuntimeError(f"Proof artifact disappeared during the final audit: {missing[1]}")
        atomic_json(self.directory / "axiom-audit.json", audit)
        self.state.update(axiom_audit_passed=audit["passed"], updated_at=utc_now())
        atomic_json(self.state_file, self.state)
        self.report("completed" if audit["passed"] else "axiom_audit_failed",
                    axiom_audit_passed=audit["passed"], axiom_audit=audit,
                    verified_main_closure_modules=len(ordered), completion_scope=COMPLETION_SCOPE)
        self.event("run_finished", full_build_completed=True, axiom_audit_passed=audit["passed"])
        return 0 if audit["passed"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--min-free-gib", type=float, default=20)
    parser.add_argument("--min-available-gib", type=float, default=8)
    parser.add_argument("--poll-seconds", type=float, default=15)
    parser.add_argument("--max-batches", type=int, default=0,
                        help="Pause with exit code 3 after this many successful batches; 0 means no limit")
    args = parser.parse_args()
    if (not all(math.isfinite(v) for v in (args.poll_seconds, args.min_free_gib, args.min_available_gib)) or
            min(args.batch_size, args.threads, args.poll_seconds) <= 0 or
            min(args.min_free_gib, args.min_available_gib, args.max_batches) < 0):
        parser.error("Counts/intervals must be positive and thresholds/limits non-negative")
    def request_stop(signum, frame):
        global STOP_REQUESTED
        STOP_REQUESTED = True
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    runner = None
    try:
        runner = Runner(args)
        return runner.run()
    except Exception as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
