"""Offline adversarial fixtures; never contact the public archive server."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
import unittest
from dataclasses import replace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_source.py"
MODULE_SPEC = importlib.util.spec_from_file_location("kit_fetch_source", SCRIPT)
fetch = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = fetch
MODULE_SPEC.loader.exec_module(fetch)


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Response(io.BytesIO):
    def __init__(self, body, url="https://example.invalid/source"):
        super().__init__(body)
        self.url = url

    def geturl(self):
        return self.url


class FakeOpener:
    def __init__(self, bodies):
        self.bodies = {name: list(values) for name, values in bodies.items()}
        self.calls = []
        self.lock = threading.Lock()

    def open(self, request, timeout):
        name = request.full_url.rsplit("/", 1)[-1]
        with self.lock:
            self.calls.append(name)
            if name not in self.bodies or not self.bodies[name]:
                raise AssertionError(f"Unexpected network request: {name}")
            result = self.bodies[name].pop(0)
        if isinstance(result, Exception):
            raise result
        return Response(result)


def fixture(members=None):
    if members is None:
        members = [
            ("lean/lake-manifest.json", b"{}\n"),
            ("lean/lakefile.toml", b'name = "fixture"\n'),
            ("lean/lean-toolchain", b"leanprover/lean4:fixture\n"),
            ("lean/src/Main.lean", b"import Branch.Proof\n"),
            ("lean/src/Branch/Proof.lean", "theorem sample₂ : True := True.intro\n".encode()),
        ]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, content in members:
            info = tarfile.TarInfo(name)
            if isinstance(content, tarfile.TarInfo):
                content.name = name
                archive.addfile(content)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    body = buf.getvalue()
    cuts = [body[:len(body) // 2], body[len(body) // 2:]]
    parts = tuple(fetch.Part(f"lean.tar.gz.part{i:02d}", len(b), digest(b))
                  for i, b in enumerate(cuts, 1))
    regular = [(name, content) for name, content in members if isinstance(content, bytes)]
    configs = {name[5:]: digest(content) for name, content in regular
               if name[5:] in fetch.CONFIG_HASHES}
    inventory = "".join(f"{digest(content)}  {name[5:]}\n" for name, content in regular).encode()
    spec = fetch.SourceSpec(parts, digest(body), len(body), len(members),
                            sum(len(content) for _, content in regular), configs,
                            digest(inventory), "https://example.invalid/")
    return spec, body, dict(zip((p.name for p in parts), cuts))


class FetchSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lean-fetch-test-")
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.clean_owned_temp)
        self.spec, self.body, self.parts = fixture()
        self.work = self.root / "work"

    def clean_owned_temp(self):
        # Explicitly check the recursive-cleanup target is our own temp directory.
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.root.name.startswith("lean-fetch-test-"))
        self.temp.cleanup()

    def install(self):
        opener = FakeOpener({name: [body] for name, body in self.parts.items()})
        marker = fetch.fetch_source(self.work, spec=self.spec, opener=opener,
                                    workers=2, retries=1, sleep=lambda _: None)
        return marker, opener

    def symlink(self, target, link, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")

    def test_full_fetch_and_verify_resume_without_network(self):
        marker, opener = self.install()
        self.assertEqual(marker["inventory_sha256"], self.spec.inventory_sha256)
        self.assertEqual(set(opener.calls), set(self.parts))
        project = self.work / "project"
        source = project / "src" / "Branch" / "Proof.lean"
        before = source.stat().st_mtime_ns
        # Build artifacts outside src are intentionally allowed on resumption.
        (project / ".lake").mkdir()
        (project / ".lake" / "build-output").write_bytes(b"artifact")
        for verify_only in (False, True):
            fetch.fetch_source(self.work, verify_only=verify_only, spec=self.spec,
                               opener=FakeOpener({}))
        self.assertEqual(source.stat().st_mtime_ns, before)

    def test_verify_only_missing_work_does_not_download_or_create(self):
        with self.assertRaisesRegex(RuntimeError, "does not exist"):
            fetch.fetch_source(self.work, verify_only=True, spec=self.spec,
                               opener=FakeOpener({}))
        self.assertFalse(self.work.exists())

    def test_completed_parts_resume_and_partial_temp_is_not_trusted(self):
        archive = self.work / "archive"
        archive.mkdir(parents=True)
        first, second = self.spec.parts
        (archive / first.name).write_bytes(self.parts[first.name])
        partial = archive / (second.name + ".old.download")
        partial.write_bytes(self.parts[second.name][:7])
        opener = FakeOpener({second.name: [self.parts[second.name]]})
        fetch.fetch_source(self.work, spec=self.spec, opener=opener, workers=1, retries=1)
        self.assertEqual(opener.calls, [second.name])
        self.assertTrue(partial.exists())  # Not this process's temporary file.

    def test_truncated_download_retried_then_verified(self):
        part = self.spec.parts[0]
        self.work.mkdir()
        body = self.parts[part.name]
        opener = FakeOpener({part.name: [body[:-1], body]})
        fetch.download_part(part, self.work, self.spec, opener=opener,
                            retries=2, sleep=lambda _: None)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual((self.work / part.name).read_bytes(), body)
        self.assertFalse(list(self.work.glob("*.download")))

    def test_bad_checksum_never_published(self):
        part = self.spec.parts[0]
        self.work.mkdir()
        bad = b"x" * part.size
        with self.assertRaisesRegex(RuntimeError, "SHA256"):
            fetch.download_part(part, self.work, self.spec,
                                opener=FakeOpener({part.name: [bad]}), retries=1)
        self.assertFalse((self.work / part.name).exists())
        self.assertFalse(list(self.work.glob("*.download")))

    def test_mismatched_existing_part_is_not_overwritten(self):
        part = self.spec.parts[0]
        self.work.mkdir()
        path = self.work / part.name
        path.write_bytes(b"keep me")
        with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
            fetch.download_part(part, self.work, self.spec, opener=FakeOpener({}))
        self.assertEqual(path.read_bytes(), b"keep me")

    def test_joined_checksum_checked_before_extraction(self):
        spec = replace(self.spec, archive_sha256="0" * 64)
        opener = FakeOpener({name: [body] for name, body in self.parts.items()})
        with self.assertRaisesRegex(RuntimeError, "SHA256"):
            fetch.fetch_source(self.work, spec=spec, opener=opener, retries=1)
        self.assertFalse((self.work / "project").exists())
        self.assertFalse((self.work / "archive" / "lean.tar.gz").exists())

    def test_changed_source_rejected_even_with_rewritten_metadata(self):
        self.install()
        project = self.work / "project"
        path = project / "src" / "Main.lean"
        path.write_bytes(b"theorem modified : True := True.intro\n")
        with self.assertRaisesRegex(RuntimeError, "source changed"):
            fetch.verify_existing_project(project, self.spec)
        inventory = project / fetch.INVENTORY
        lines = inventory.read_text().splitlines()
        lines = [f"{digest(path.read_bytes())}  src/Main.lean" if line.endswith("  src/Main.lean")
                 else line for line in lines]
        inventory.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        marker_path = project / fetch.MARKER
        marker = json.loads(marker_path.read_text())
        marker["inventory_sha256"] = digest(inventory.read_bytes())
        marker_path.write_text(json.dumps(marker))
        with self.assertRaisesRegex(RuntimeError, "pinned source inventory"):
            fetch.verify_existing_project(project, self.spec)

    def test_extra_source_rejected(self):
        self.install()
        (self.work / "project" / "src" / "Extra.lean").write_bytes(b"import Main\n")
        with self.assertRaisesRegex(RuntimeError, "unexpected files"):
            fetch.verify_existing_project(self.work / "project", self.spec)

    def test_alternate_lake_configuration_rejected(self):
        self.install()
        (self.work / "project" / "lakefile.lean").write_bytes(b"import Lake\n")
        with self.assertRaisesRegex(RuntimeError, "override"):
            fetch.verify_existing_project(self.work / "project", self.spec)

    def test_symlink_work_ancestor_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.root / "link"
        self.symlink(outside, link, directory=True)
        with self.assertRaisesRegex(RuntimeError, "Symlink"):
            fetch.fetch_source(link / "work", spec=self.spec, opener=FakeOpener({}))
        self.assertFalse((outside / "work").exists())

    def test_symlink_download_rejected(self):
        self.work.mkdir()
        part = self.spec.parts[0]
        outside = self.root / "external-part"
        outside.write_bytes(self.parts[part.name])
        self.symlink(outside, self.work / part.name)
        with self.assertRaisesRegex(RuntimeError, "Symlink"):
            fetch.download_part(part, self.work, self.spec, opener=FakeOpener({}))

    def test_unsafe_archive_paths_rejected_without_escape(self):
        for name in ("lean/src/../../escape.lean", "lean/src/../evil.lean",
                     "/lean/src/evil.lean", "lean/src\\evil.lean",
                     "lean/src/C:evil.lean", "lean/src/CON.lean", "lean/src/file.lean:stream",
                     "lean/src/a/./b.lean", "lean/src//b.lean"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                fetch.source_relative(name, self.spec)
        spec, body, _ = fixture([("lean/src/../../escape.lean", b"bad")])
        archive = self.root / "bad.tar.gz"
        archive.write_bytes(body)
        with self.assertRaises(ValueError):
            fetch.extract_new(archive, self.root / "project", spec)
        self.assertFalse((self.root / "escape.lean").exists())
        self.assertFalse((self.root / "project").exists())

    def test_archive_symlinks_hardlinks_and_duplicates_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE):
            with self.subTest(kind=kind):
                info = tarfile.TarInfo("unused")
                info.type = kind
                info.linkname = "../../outside"
                spec, body, _ = fixture([("lean/src/Bad.lean", info)])
                archive = self.root / "bad.tar.gz"
                archive.write_bytes(body)
                with self.assertRaisesRegex(RuntimeError, "Non-regular"):
                    fetch.extract_new(archive, self.root / "project", spec)
        spec, body, _ = fixture([("lean/src/Main.lean", b"a"), ("lean/src/Main.lean", b"b")])
        archive.write_bytes(body)
        with self.assertRaisesRegex(RuntimeError, "Duplicate"):
            fetch.extract_new(archive, self.root / "project", spec)

    def test_existing_project_is_never_replaced(self):
        project = self.root / "project"
        project.mkdir()
        sentinel = project / "mine"
        sentinel.write_text("keep")
        archive = self.root / "source.tar.gz"
        archive.write_bytes(self.body)
        with self.assertRaisesRegex(RuntimeError, "Refusing to replace"):
            fetch.extract_new(archive, project, self.spec)
        self.assertEqual(sentinel.read_text(), "keep")

    def test_atomic_publication_does_not_clobber_racing_destination(self):
        source, dest = self.root / "temp", self.root / "dest"
        source.write_bytes(b"new")
        dest.write_bytes(b"old")
        with self.assertRaises(FileExistsError):
            fetch.publish_file(source, dest)
        self.assertEqual(dest.read_bytes(), b"old")
        staging, target = self.root / "stage", self.root / "target"
        staging.mkdir()
        target.mkdir()
        with self.assertRaises(OSError):
            fetch.publish_directory(staging, target)
        self.assertTrue(staging.is_dir())

    def test_concurrent_direct_fetch_is_rejected(self):
        self.work.mkdir()
        with fetch.fetch_lock(self.work):
            with self.assertRaisesRegex(RuntimeError, "Another source fetch"):
                with fetch.fetch_lock(self.work):
                    self.fail("second lock acquired")
        with fetch.fetch_lock(self.work):
            pass

    def test_https_downgrade_rejected_before_redirect(self):
        request = fetch.urllib.request.Request("https://example.invalid/archive")
        with self.assertRaisesRegex(RuntimeError, "non-HTTPS"):
            fetch.HTTPSRedirectHandler().redirect_request(
                request, None, 302, "Found", {}, "http://example.invalid/archive")


if __name__ == "__main__":
    unittest.main()
