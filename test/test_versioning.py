import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.install.versioning import resolve_version, write_snapshot


class VersioningTests(unittest.TestCase):
    def test_environment_override_precedes_git_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "VERSION").write_text("1.5.5\n", encoding="utf-8")
            with patch("core.install.versioning._run_git") as run_git:
                run_git.side_effect = ["abcdef0"]
                version = resolve_version(root, {"SEMVER4_BUILD": "812"})

        self.assertEqual(version.version, "1.5.5.812")
        self.assertEqual(version.build_source, "SEMVER4_BUILD")
        self.assertEqual(version.commit, "abcdef0")

    def test_git_count_is_local_development_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "VERSION").write_text("1.5.5", encoding="utf-8")
            with patch(
                "core.install.versioning._run_git",
                side_effect=["500", "deadbee"],
            ):
                version = resolve_version(root, {})

        self.assertEqual(version.version, "1.5.5.500")
        self.assertEqual(version.build_source, "git")

    def test_snapshot_is_immutable_runtime_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "VERSION").write_text("1.5.5", encoding="utf-8")
            with patch(
                "core.install.versioning._run_git",
                side_effect=["500", "deadbee"],
            ):
                source = resolve_version(root, {})
            snapshot = write_snapshot(root / "engram-version.json", source)
            frozen = resolve_version(
                root,
                {"SEMVER4_BUILD": "999"},
                snapshot_path=snapshot,
            )

        self.assertEqual(frozen, source)

    def test_rejects_malformed_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "VERSION").write_text("1.5.5", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SEMVER4_BUILD"):
                resolve_version(root, {"SEMVER4_BUILD": "dev"})

    def test_snapshot_payload_contains_four_part_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "VERSION").write_text("1.5.5", encoding="utf-8")
            with patch(
                "core.install.versioning._run_git",
                side_effect=["500", "deadbee"],
            ):
                path = write_snapshot(root / "version.json", resolve_version(root, {}))
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["version"], "1.5.5.500")
        self.assertEqual(payload["build"], 500)


if __name__ == "__main__":
    unittest.main()
