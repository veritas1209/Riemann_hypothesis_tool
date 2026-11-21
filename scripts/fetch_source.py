#!/usr/bin/env python3
"""Fetch the pinned RH source into WORK_DIR/{archive,project}.

Only verified complete downloads are published. Existing source is checked against
a pinned inventory, never repaired or replaced. The wrapper owns the work lock;
this script also serializes direct invocations with its own advisory lock.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import tempfile
import time
from typing import Callable
import urllib.parse
import urllib.request


ARCHIVE_SHA = "9631b4f020199e580ba86178cb56c6e9b09f690c2369a80dc4334aa93f49c7fc"
TOTAL_BYTES = 239275554
FILE_COUNT = 175190
SOURCE_BYTES = 1030536719
INVENTORY_SHA = "d35a64b1fe17ee9a64f18eafe23a4e4953cae671c75e9b75b519aae865989dc5"
CONFIG_HASHES = {
    "lake-manifest.json": "fee57ee9ed399d0a638ca955e5ad40a37a36eeb9b6dfd9f5964114fa7bbe4774",
    "lakefile.toml": "b831ad8cac0babb7bf713433259bdfe5163e58c90734e86d71536d4e30ddec4c",
    "lean-toolchain": "3aac669c7a910ec2389f4e4f921b605adf6ebf2d1e0c9b9cd0be4d33f3f5db71",
}
PART_HASHES = (
    "86974b1c242ca7b6748f3299df4fc64ee0f6e06c776ac964fb64888101a3de77",
    "1c2a2477354a9dfa97ff1ce54861e96ed885bd456fffbaaabc2028314a229c42",
    "1465a23b1b579a881e24e5ffaf7c4c3727af28ea24741f1ec80e1d67947530a3",
    "3929e327eecb4e2c5c7cae30e104e6a4a52fa6b82d28325bcda8e9da1c86893b",
    "f25d7e80f7c4ad6d674aa18b323ca760f848857c8755501c149317c38a66c75d",
    "a8c651a68327811b37dce3cca8eda5e0b2102fa227c0d395cbb7553e4dd69939",
    "65c7e502a9b236a0f68d9e0aa6e089d4c09b9fc7a0960a149384063ccbe5e869",
    "71b027ad6e4179c3a50ce72921e3fec0a2dd751ec7472ac2c644ad145a50ec7e",
    "47718977ebaa8613f1647e93134479780db9320c5fa911463fd312a6257b796f",
    "1d85ece78e0779cec26b0cce29c6462d9f90180e56d78120b152df101715240b",
    "cf83a159d1a9540012229c376562587dd966da10cd657cbcef53acfd03232448",
    "1aa5415bd2db83b2689afeeb946419303b3c4f599ae790ad74d38465f768b502",
    "2de4edbce8ebdbfd0b94bc89bab518c7294209f408ec5b14a7be9e4832d9da81",
    "53f8ea43a9b5dcab97b84a52f3ac2848711c844a5420a4c56418093bacb3c6f6",
)
MARKER = ".remark-source.json"
INVENTORY = ".remark-source.sha256"
CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Part:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class SourceSpec:
    """Immutable release inputs; custom instances are for offline unit tests."""
    parts: tuple[Part, ...]
    archive_sha256: str
    archive_bytes: int
    file_count: int
    source_bytes: int
    config_hashes: dict[str, str]
    inventory_sha256: str
    base_url: str = "https://alpo.ge/"


PINNED = SourceSpec(
    tuple(Part(f"lean.tar.gz.part{i:02d}", 18000000 if i < 14 else 5275554, h)
          for i, h in enumerate(PART_HASHES, 1)),
    ARCHIVE_SHA, TOTAL_BYTES, FILE_COUNT, SOURCE_BYTES, CONFIG_HASHES, INVENTORY_SHA,
)


def exists(path: Path) -> bool:
    return os.path.lexists(path)


def reject_link(path: Path) -> os.stat_result:
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400):
        raise RuntimeError(f"Symlink/reparse point is not allowed: {path}")
    return info


def real_directory(path: Path, *, create: bool = False) -> Path:
    """Reject symlinks, including ancestor links and Windows junctions."""
    path = Path(os.path.abspath(path))
    for item in (*reversed(path.parents), path):
        if not exists(item):
            if not create:
                raise RuntimeError(f"Directory does not exist: {item}")
            try:
                item.mkdir(mode=0o700)
            except FileExistsError:
                pass
        if not stat.S_ISDIR(reject_link(item).st_mode):
            raise RuntimeError(f"Not a directory: {item}")
    return path


@contextmanager
def regular_reader(path: Path):
    if not stat.S_ISREG(reject_link(path).st_mode):
        raise RuntimeError(f"Not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError(f"Not a regular file: {path}")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            yield stream
    finally:
        if fd is not None:
            os.close(fd)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with regular_reader(path) as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_file(path: Path, size: int, digest: str) -> None:
    info = reject_link(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size != size or sha256(path) != digest:
        raise RuntimeError(f"Size or SHA256 mismatch; refusing to replace: {path}")


@contextmanager
def fetch_lock(work: Path):
    path = work / ".fetch-source.lock"
    if exists(path) and not stat.S_ISREG(reject_link(path).st_mode):
        raise RuntimeError(f"Not a regular lock file: {path}")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError("Fetch lock is not a regular file")
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except OSError as exc:
        if not locked and exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise RuntimeError(f"Another source fetch is using {work}") from exc
        raise
    finally:
        if locked:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Reject a downgrade before urllib sends an HTTP request.
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise RuntimeError("Refusing a non-HTTPS redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def publish_file(temp: Path, dest: Path) -> None:
    if os.name == "nt":
        # Windows rename fails if dest exists, without requiring hard-link rights.
        os.rename(temp, dest)
        return
    # Unlike rename on POSIX, link fails atomically if dest already exists.
    os.link(temp, dest, follow_symlinks=False)
    temp.unlink()


def publish_directory(staging: Path, dest: Path) -> None:
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise RuntimeError("Atomic directory installation requires Linux renameat2")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(staging), -100, os.fsencode(dest), 1) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(dest))
    elif os.name == "nt":
        os.rename(staging, dest)  # Windows rename never replaces a directory.
    else:
        raise RuntimeError("Source installation currently supports Linux/WSL and Windows")


def source_relative(name: str, spec: SourceSpec = PINNED) -> str:
    if not name.startswith("lean/"):
        raise ValueError(f"Unsafe archive name: {name!r}")
    rel = name[5:]
    parts = rel.split("/")
    for part in parts:
        if (part in ("", ".", "..") or part.endswith((".", " "))
                or any(ord(c) < 32 or ord(c) == 127 or c in '\\:<>"|?*' for c in part)
                or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", part.split(".")[0], re.I)):
            raise ValueError(f"Unsafe archive path: {name!r}")
    if rel not in spec.config_hashes and not (parts[0] == "src" and rel.endswith(".lean")):
        raise ValueError(f"Unexpected archive member: {name!r}")
    return rel


def download_part(part: Part, archive_dir: Path, spec: SourceSpec = PINNED,
                  *, opener=None, retries: int = 3, sleep: Callable = time.sleep) -> Path:
    if Path(part.name).name != part.name or any(c in part.name for c in "/\\:"):
        raise ValueError("Invalid part name")
    dest = archive_dir / part.name
    if exists(dest):
        check_file(dest, part.size, part.sha256)
        print(f"Reused verified {part.name}", flush=True)
        return dest
    url = spec.base_url + part.name
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise ValueError("Only HTTPS source URLs are permitted")
    opener = opener or urllib.request.build_opener(HTTPSRedirectHandler())
    for attempt in range(1, retries + 1):
        fd, name = tempfile.mkstemp(prefix=part.name + ".", suffix=".download", dir=archive_dir)
        temp = Path(name)
        try:
            count = 0
            digest = hashlib.sha256()
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with os.fdopen(fd, "wb") as output:
                with opener.open(request, timeout=90) as response:
                    if urllib.parse.urlsplit(response.geturl()).scheme.lower() != "https":
                        raise RuntimeError("Refusing a non-HTTPS response")
                    for chunk in iter(lambda: response.read(CHUNK), b""):
                        count += len(chunk)
                        if count > part.size:
                            raise RuntimeError(f"Download exceeds expected size: {part.name}")
                        digest.update(chunk)
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if count != part.size or digest.hexdigest() != part.sha256:
                raise RuntimeError(f"Size or SHA256 mismatch for {part.name}")
            try:
                publish_file(temp, dest)
            except FileExistsError:
                check_file(dest, part.size, part.sha256)
            print(f"Downloaded and verified {part.name}: {count} bytes", flush=True)
            return dest
        except Exception:
            if attempt == retries:
                raise
            sleep(attempt * 2)
        finally:
            # This is only the private temporary file created by this attempt.
            temp.unlink(missing_ok=True)
    raise ValueError("retries must be positive")


def concatenate(parts: list[Path], archive_dir: Path, spec: SourceSpec) -> Path:
    dest = archive_dir / "lean.tar.gz"
    if exists(dest):
        check_file(dest, spec.archive_bytes, spec.archive_sha256)
        return dest
    fd, name = tempfile.mkstemp(prefix="lean.tar.gz.", suffix=".concat", dir=archive_dir)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            for part in parts:
                with regular_reader(part) as source:
                    for chunk in iter(lambda: source.read(CHUNK), b""):
                        output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        check_file(temp, spec.archive_bytes, spec.archive_sha256)
        try:
            publish_file(temp, dest)
        except FileExistsError:
            check_file(dest, spec.archive_bytes, spec.archive_sha256)
        return dest
    finally:
        temp.unlink(missing_ok=True)


def verify_configs(project: Path, spec: SourceSpec) -> None:
    for name, digest in spec.config_hashes.items():
        if sha256(project / name) != digest:
            raise RuntimeError(f"Original configuration differs: {project / name}")


def verify_existing_project(project: Path, spec: SourceSpec = PINNED) -> dict:
    real_directory(project)
    if exists(project / "lakefile.lean"):
        raise RuntimeError("Unexpected lakefile.lean could override the pinned lakefile.toml")
    with regular_reader(project / MARKER) as stream:
        raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise RuntimeError("Source marker is too large")
        marker = json.loads(raw)
    if not isinstance(marker, dict):
        raise RuntimeError("Source marker must be a JSON object")
    inventory = project / INVENTORY
    if (marker.get("archive_sha256") != spec.archive_sha256
            or marker.get("files") != spec.file_count
            or marker.get("bytes") != spec.source_bytes
            or marker.get("inventory_sha256") != spec.inventory_sha256
            or sha256(inventory) != spec.inventory_sha256):
        raise RuntimeError("Existing project does not match the pinned source inventory")
    actual = set()
    real_directory(project / "src")
    for current, dirs, files in os.walk(project / "src", followlinks=False):
        for name in dirs:
            if not stat.S_ISDIR(reject_link(Path(current) / name).st_mode):
                raise RuntimeError("Non-directory inside source tree")
        for name in files:
            path = Path(current) / name
            if not stat.S_ISREG(reject_link(path).st_mode):
                raise RuntimeError(f"Non-regular source file: {path}")
            actual.add(path.relative_to(project).as_posix())
    names = set()
    total = 0
    with regular_reader(inventory) as stream:
        for raw in stream:
            expected, separator, relative = raw.decode("utf-8").rstrip("\n").partition("  ")
            if (not separator or not re.fullmatch("[0-9a-f]{64}", expected)
                    or source_relative("lean/" + relative, spec) != relative or relative in names):
                raise RuntimeError("Malformed or duplicate source inventory entry")
            names.add(relative)
            path = project / relative
            if sha256(path) != expected:
                raise RuntimeError(f"Existing source changed: {path}")
            total += path.stat().st_size
    if (len(names) != spec.file_count or total != spec.source_bytes
            or actual != {name for name in names if name.startswith("src/")}):
        raise RuntimeError("Existing source has missing or unexpected files")
    verify_configs(project, spec)
    print(f"Verified unchanged source: {len(names)} files at {project}", flush=True)
    return marker


def extract_new(archive_path: Path, project: Path, spec: SourceSpec = PINNED) -> dict:
    check_file(archive_path, spec.archive_bytes, spec.archive_sha256)
    if exists(project):
        raise RuntimeError(f"Refusing to replace existing project: {project}")
    staging = Path(tempfile.mkdtemp(prefix=".remark-extract-", dir=project.parent))
    print(f"Extracting into private staging tree: {staging}", flush=True)
    count = total = 0
    seen = set()
    inventory_path = staging / INVENTORY
    # Failed staging trees are retained for diagnosis; never remove a user's tree.
    with inventory_path.open("x", encoding="utf-8", newline="\n") as inventory:
        with regular_reader(archive_path) as compressed, tarfile.open(fileobj=compressed, mode="r|gz") as archive:
            for member in archive:
                if not member.isfile():
                    raise RuntimeError(f"Non-regular archive member: {member.name!r}")
                relative = source_relative(member.name, spec)
                key = os.path.normcase(relative)
                if key in seen:
                    raise RuntimeError(f"Duplicate/colliding archive member: {relative}")
                seen.add(key)
                count += 1
                total += member.size
                if count > spec.file_count or total > spec.source_bytes or member.size < 0:
                    raise RuntimeError("Archive exceeds pinned inventory bounds")
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                copied = 0
                with archive.extractfile(member) as source, target.open("xb") as output:
                    for chunk in iter(lambda: source.read(CHUNK), b""):
                        digest.update(chunk)
                        copied += len(chunk)
                        output.write(chunk)
                if copied != member.size:
                    raise RuntimeError(f"Truncated archive member: {relative}")
                target.chmod(0o644)
                inventory.write(f"{digest.hexdigest()}  {relative}\n")
                if count % 25000 == 0:
                    print(f"Extracted {count}/{spec.file_count} files", flush=True)
    if count != spec.file_count or total != spec.source_bytes:
        raise RuntimeError(f"Unexpected archive inventory: {count} files, {total} bytes")
    if sha256(inventory_path) != spec.inventory_sha256:
        raise RuntimeError("Extracted source inventory differs from the pinned inventory")
    verify_configs(staging, spec)
    marker = {"archive_sha256": spec.archive_sha256, "files": count, "bytes": total,
              "inventory_sha256": spec.inventory_sha256}
    with (staging / MARKER).open("x", encoding="utf-8", newline="\n") as output:
        json.dump(marker, output, indent=2)
        output.write("\n")
    publish_directory(staging, project)
    print(f"Installed exact source at {project}", flush=True)
    return marker


def fetch_source(work_dir: Path, *, verify_only: bool = False, spec: SourceSpec = PINNED,
                 opener=None, retries: int = 3, workers: int = 4,
                 sleep: Callable = time.sleep) -> dict:
    work = real_directory(work_dir, create=not verify_only)
    project = work / "project"
    with fetch_lock(work):
        if exists(project):
            return verify_existing_project(project, spec)
        if verify_only:
            raise RuntimeError(f"No installed project to verify: {project}")
        archive_dir = real_directory(work / "archive", create=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            parts = list(pool.map(lambda part: download_part(
                part, archive_dir, spec, opener=opener, retries=retries, sleep=sleep), spec.parts))
        archive_path = concatenate(parts, archive_dir, spec)
        return extract_new(archive_path, project, spec)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True,
                        help="Directory containing archive/ and project/")
    parser.add_argument("--verify-only", action="store_true",
                        help="Verify installed source without downloading or extracting")
    args = parser.parse_args(argv)
    try:
        marker = fetch_source(args.work_dir, verify_only=args.verify_only)
        print(json.dumps({"source_verified": True, **marker}, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, tarfile.TarError) as exc:
        print(f"Source verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
