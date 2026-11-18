"""Verify restart-safe pinned source downloads without network access."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "native_dependencies", ROOT / "tools/build-native-dependencies.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class NativeDependencyResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bedrock-download-cache-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive_bytes = b"fixture pinned archive contents"
        component = {
            "archive": "fixture.tar",
            "url": "https://example.invalid/fixture.tar",
            "sha256": hashlib.sha256(self.archive_bytes).hexdigest(),
            "name": "fixture",
            "source_dir": "fixture",
            "configure": [],
        }
        self.lock = self.root / "source-lock.json"
        self.lock.write_text(json.dumps({"components": [component]}))
        self.component = component
        self.cache = self.root / "persistent-cache"
        self.destination = self.root / "attempt-1"

    def build_fetch_only(self, destination):
        builder.build(destination, 1, fetch_only=True, cache_dir=self.cache)

    def test_interrupted_download_resumes_and_verified_archive_is_reused(self):
        calls = []

        def curl(command, check):
            calls.append(command)
            partial = Path(command[command.index("--output") + 1])
            if len(calls) == 1:
                partial.write_bytes(self.archive_bytes[:9])
                raise KeyboardInterrupt
            self.assertIn("--continue-at", command)
            partial.write_bytes(partial.read_bytes() + self.archive_bytes[9:])

        with patch.object(builder, "LOCK", self.lock), \
                patch.object(builder.subprocess, "run", side_effect=curl):
            with self.assertRaises(KeyboardInterrupt):
                self.build_fetch_only(self.destination)
            partial = self.cache / "downloads/fixture.tar.part"
            self.assertEqual(partial.read_bytes(), self.archive_bytes[:9])
            self.build_fetch_only(self.root / "attempt-2")
            self.build_fetch_only(self.root / "attempt-3")

        self.assertEqual(len(calls), 2)
        archive = self.cache / "downloads/fixture.tar"
        self.assertEqual(archive.read_bytes(), self.archive_bytes)
        self.assertFalse(partial.exists())

    def test_corrupt_partial_is_discarded_and_retried_from_start(self):
        downloads = self.cache / "downloads"
        downloads.mkdir(parents=True)
        (self.cache / "source-lock.sha256").write_text(builder.digest(self.lock) + "\n")
        partial = downloads / "fixture.tar.part"
        partial.write_bytes(b"corrupt cached prefix")
        calls = []

        def curl(command, check):
            calls.append(command)
            destination = Path(command[command.index("--output") + 1])
            if "--continue-at" in command:
                destination.write_bytes(destination.read_bytes() + self.archive_bytes[10:])
            else:
                destination.write_bytes(self.archive_bytes)

        with patch.object(builder, "LOCK", self.lock), \
                patch.object(builder.subprocess, "run", side_effect=curl):
            self.build_fetch_only(self.destination)

        self.assertEqual(len(calls), 2)
        self.assertIn("--continue-at", calls[0])
        self.assertNotIn("--continue-at", calls[1])
        self.assertEqual((downloads / "fixture.tar").read_bytes(), self.archive_bytes)
        self.assertFalse(partial.exists())

    def test_corrupt_completed_archive_is_discarded_before_refetch(self):
        downloads = self.cache / "downloads"
        downloads.mkdir(parents=True)
        (self.cache / "source-lock.sha256").write_text(builder.digest(self.lock) + "\n")
        archive = downloads / "fixture.tar"
        archive.write_bytes(b"not the pinned archive")
        calls = []

        def curl(command, check):
            calls.append(command)
            Path(command[command.index("--output") + 1]).write_bytes(self.archive_bytes)

        with patch.object(builder, "LOCK", self.lock), \
                patch.object(builder.subprocess, "run", side_effect=curl):
            self.build_fetch_only(self.destination)

        self.assertEqual(len(calls), 1)
        self.assertEqual(archive.read_bytes(), self.archive_bytes)

    def test_build_output_can_be_tee_streamed_and_remains_in_log(self):
        log = self.root / "build.log"
        captured = bytearray()
        terminal = SimpleNamespace()

        class ByteCapture:
            def write(self, data):
                captured.extend(data)

            def flush(self):
                return None

        terminal.buffer = ByteCapture()
        command = [sys.executable, "-c", "print('Compiling fixture source')"]
        with patch.object(builder.sys, "stdout", terminal):
            builder.run(command, log, os.environ.copy(), live_output=True)

        self.assertIn(b"Compiling fixture source", log.read_bytes())
        self.assertIn(b"Compiling fixture source", captured)

    def test_dependency_tree_copy_resumes_when_interrupted_after_symlink_creation(self):
        source = self.root / "source-prefix/lib"
        destination = self.root / "runtime-prefix/lib"
        source.mkdir(parents=True)
        (source / "libfreetype.6.dylib").write_bytes(b"freetype")
        (source / "libfreetype.dylib").symlink_to("libfreetype.6.dylib")
        destination.mkdir(parents=True)
        (destination / "libfreetype.6.dylib").write_bytes(b"freetype")
        (destination / "libfreetype.dylib").symlink_to("libfreetype.6.dylib")
        (source / "libgnutls.30.dylib").write_bytes(b"gnutls")

        builder.copy_dependency_tree(source, destination)
        builder.copy_dependency_tree(source, destination)

        self.assertEqual((destination / "libfreetype.dylib").readlink(),
                         Path("libfreetype.6.dylib"))
        self.assertEqual((destination / "libgnutls.30.dylib").read_bytes(), b"gnutls")
        self.assertEqual(list(destination.glob(".dependency-copy-*")), [])

    def test_dependency_copy_replaces_only_the_conflicting_verified_entry(self):
        source = self.root / "source-prefix/include"
        destination = self.root / "runtime-prefix/include"
        source.mkdir(parents=True)
        (source / "gnutls").mkdir()
        (source / "gnutls/gnutls.h").write_bytes(b"verified header")
        destination.mkdir(parents=True)
        (destination / "gnutls").write_text("interrupted conflicting output")

        builder.copy_dependency_tree(source, destination)

        self.assertTrue((destination / "gnutls").is_dir())
        self.assertEqual((destination / "gnutls/gnutls.h").read_bytes(), b"verified header")

    def test_interrupted_dependency_file_copy_preserves_previous_file_and_retries(self):
        source = self.root / "source-prefix/lib"
        destination = self.root / "runtime-prefix/lib"
        source.mkdir(parents=True)
        destination.mkdir(parents=True)
        (source / "libfreetype.6.dylib").write_bytes(b"complete verified library")
        previous = destination / "libfreetype.6.dylib"
        previous.write_bytes(b"previous complete library")

        with patch.object(builder.shutil, "copy2", side_effect=OSError("fixture interruption")):
            with self.assertRaisesRegex(OSError, "fixture interruption"):
                builder.copy_dependency_tree(source, destination)
        self.assertEqual(previous.read_bytes(), b"previous complete library")
        self.assertEqual(list(destination.glob(".dependency-copy-*")), [])

        builder.copy_dependency_tree(source, destination)
        self.assertEqual(previous.read_bytes(), b"complete verified library")


if __name__ == "__main__":
    unittest.main()
