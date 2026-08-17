import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from core.install.model_manifest import (
    _main,
    create_manifest,
    ensure_model,
    refresh_manifest,
    validate_manifest,
)


class ModelManifestTests(unittest.TestCase):
    model_id = "test/model"
    revision = "0123456789abcdef"

    def _assets(self, directory: Path, value: bytes = b"weights") -> None:
        (directory / "config.json").write_text("{}", encoding="utf-8")
        (directory / "model.safetensors").write_bytes(value)

    def _canonical(self, directory: Path, value: bytes = b"weights") -> bytes:
        self._assets(directory, value)
        manifest = create_manifest(directory, self.model_id, resolved_revision=self.revision)
        raw = json.dumps(manifest, indent=2) + "\n"
        (directory / "manifest.json").write_text(raw, encoding="utf-8", newline="\n")
        return raw.encode()

    def test_manifest_does_not_depend_on_home_or_package_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._assets(directory)
            with patch.object(Path, "home", return_value=Path(tmp) / "other-home"):
                first = create_manifest(directory, self.model_id, resolved_revision=self.revision)
            with patch.dict(os.environ, {"HF_HOME": str(directory / "different-cache")}, clear=False):
                second = create_manifest(directory, self.model_id, resolved_revision=self.revision)
        self.assertEqual(first, second)
        self.assertEqual(set(first), {"schema_version", "model_id", "resolved_revision", "files"})

    def test_valid_ensure_never_mutates_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            original = self._canonical(directory)
            self.assertEqual(ensure_model(directory, self.model_id), "reused")
            self.assertEqual((directory / "manifest.json").read_bytes(), original)

    def test_missing_or_malformed_manifest_is_not_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._assets(directory)
            before = {path.name: path.read_bytes() for path in directory.iterdir()}
            with self.assertRaisesRegex(RuntimeError, "manifest missing"):
                ensure_model(directory, self.model_id, allow_download=True)
            self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, before)
            (directory / "manifest.json").write_text("{broken", encoding="utf-8")
            before = (directory / "manifest.json").read_bytes()
            with self.assertRaisesRegex(RuntimeError, "manifest unreadable"):
                ensure_model(directory, self.model_id, allow_download=True)
            self.assertEqual((directory / "manifest.json").read_bytes(), before)

    def test_pinned_staged_acquisition_replaces_assets_but_preserves_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "model"
            directory.mkdir()
            original = self._canonical(directory)
            (directory / "model.safetensors").write_bytes(b"bad")
            cache = Path(tmp) / "cache"
            cache.mkdir()
            self._assets(cache)

            calls = []

            def fake_download(**kwargs):
                calls.append(kwargs)
                return cache / kwargs["filename"]

            fake_module = types.SimpleNamespace(hf_hub_download=fake_download)
            with patch.dict("sys.modules", {"huggingface_hub": fake_module}):
                self.assertEqual(ensure_model(directory, self.model_id, allow_download=True), "exported")
            self.assertEqual((directory / "manifest.json").read_bytes(), original)
            self.assertTrue(validate_manifest(directory, self.model_id)[0])
            self.assertEqual({call["revision"] for call in calls}, {self.revision})
            self.assertEqual({call["repo_id"] for call in calls}, {self.model_id})
            self.assertEqual({call["local_files_only"] for call in calls}, {False})

    def test_failed_acquisition_preserves_manifest_and_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "model"
            directory.mkdir()
            original_manifest = self._canonical(directory)
            (directory / "model.safetensors").write_bytes(b"bad")
            original_assets = {path.name: path.read_bytes() for path in directory.iterdir()}
            cache = Path(tmp) / "cache"
            cache.mkdir()
            self._assets(cache, b"wrong")

            def fake_download(**kwargs):
                return cache / kwargs["filename"]

            fake_module = types.SimpleNamespace(hf_hub_download=fake_download)
            with patch.dict("sys.modules", {"huggingface_hub": fake_module}):
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    ensure_model(directory, self.model_id, allow_download=True)
            self.assertEqual((directory / "manifest.json").read_bytes(), original_manifest)
            self.assertEqual({path.name: path.read_bytes() for path in directory.iterdir()}, original_assets)

    def test_refresh_requires_explicit_revision_and_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._assets(directory)
            self.assertEqual(_main(["--model-dir", str(directory), "--refresh-manifest"]), 1)
            first = refresh_manifest(directory, self.model_id, self.revision)
            first_bytes = (directory / "manifest.json").read_bytes()
            second = refresh_manifest(directory, self.model_id, self.revision)
            self.assertEqual(first, second)
            self.assertEqual((directory / "manifest.json").read_bytes(), first_bytes)


if __name__ == "__main__":
    unittest.main()
