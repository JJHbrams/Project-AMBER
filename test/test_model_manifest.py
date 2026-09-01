import json
import os
import tempfile
import threading
import types
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from core.install.model_manifest import (
    _main,
    cache_dir_for_manifest,
    create_model_pack,
    create_manifest,
    ensure_cached_model,
    ensure_model,
    import_model_pack,
    manifest_sha256,
    model_stamp_from_manifest,
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

    def test_cache_is_keyed_by_exact_manifest_and_stamp_includes_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp) / "model"
            model.mkdir()
            self._canonical(model)
            manifest_path = model / "manifest.json"
            cache_root = Path(tmp) / "shared"

            first = cache_dir_for_manifest(manifest_path, cache_root)
            self.assertEqual(first.name, manifest_sha256(manifest_path))
            self.assertEqual(
                model_stamp_from_manifest(manifest_path),
                f"{self.model_id}@sha256:{first.name}",
            )
            manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
            self.assertNotEqual(first, cache_dir_for_manifest(manifest_path, cache_root))

    def test_pinned_cache_hydration_is_atomic_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical"
            canonical.mkdir()
            self._canonical(canonical)
            hub = root / "hub"
            hub.mkdir()
            self._assets(hub)
            cache_root = root / "cache"
            calls = []

            def fake_download(**kwargs):
                calls.append(kwargs)
                return hub / kwargs["filename"]

            fake_module = types.SimpleNamespace(hf_hub_download=fake_download)
            with patch.dict("sys.modules", {"huggingface_hub": fake_module}):
                resolved = ensure_cached_model(
                    canonical / "manifest.json",
                    cache_root=cache_root,
                    allow_download=True,
                    expected_model_id=self.model_id,
                )
                reused = ensure_cached_model(
                    canonical / "manifest.json",
                    cache_root=cache_root,
                    allow_download=False,
                    expected_model_id=self.model_id,
                )

            self.assertEqual(resolved, reused)
            self.assertTrue(validate_manifest(resolved, self.model_id)[0])
            self.assertEqual({call["repo_id"] for call in calls}, {self.model_id})
            self.assertEqual({call["revision"] for call in calls}, {self.revision})
            self.assertEqual({call["local_files_only"] for call in calls}, {False})
            self.assertEqual(list(cache_root.glob("*.stage-*")), [])

    def test_failed_hydration_never_publishes_and_corrupt_cache_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "canonical"
            canonical.mkdir()
            self._canonical(canonical)
            hub = root / "hub"
            hub.mkdir()
            self._assets(hub, b"wrong")
            cache_root = root / "cache"
            target = cache_dir_for_manifest(canonical / "manifest.json", cache_root)
            fake_module = types.SimpleNamespace(
                hf_hub_download=lambda **kwargs: hub / kwargs["filename"]
            )
            with patch.dict("sys.modules", {"huggingface_hub": fake_module}):
                with self.assertRaisesRegex(RuntimeError, "failed validation"):
                    ensure_cached_model(
                        canonical / "manifest.json",
                        cache_root=cache_root,
                        allow_download=True,
                        expected_model_id=self.model_id,
                    )
            self.assertFalse(target.exists())

            target.mkdir()
            sentinel = target / "user-sentinel"
            sentinel.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "already exists but is invalid"):
                ensure_cached_model(
                    canonical / "manifest.json",
                    cache_root=cache_root,
                    allow_download=True,
                    expected_model_id=self.model_id,
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_legacy_migration_preserves_source_and_concurrent_callers_share_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            legacy.mkdir()
            original_manifest = self._canonical(legacy)
            original_assets = {
                path.relative_to(legacy): path.read_bytes()
                for path in legacy.rglob("*") if path.is_file()
            }
            cache_root = root / "cache"
            barrier = threading.Barrier(2)

            def resolve():
                barrier.wait()
                return ensure_cached_model(
                    legacy / "manifest.json",
                    cache_root=cache_root,
                    migration_sources=[legacy],
                    expected_model_id=self.model_id,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _index: resolve(), range(2)))
            self.assertEqual(results[0], results[1])
            self.assertTrue(validate_manifest(results[0], self.model_id)[0])
            self.assertEqual((legacy / "manifest.json").read_bytes(), original_manifest)
            self.assertEqual(
                {
                    path.relative_to(legacy): path.read_bytes()
                    for path in legacy.rglob("*") if path.is_file()
                },
                original_assets,
            )

    def test_offline_pack_round_trip_and_invalid_pack_never_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = root / "model"
            model.mkdir()
            self._canonical(model)
            pack = create_model_pack(model, root / "model.engram-model.zip")
            imported = import_model_pack(
                pack,
                model / "manifest.json",
                cache_root=root / "valid-cache",
            )
            self.assertTrue(validate_manifest(imported, self.model_id)[0])

            with zipfile.ZipFile(pack) as source:
                members = {
                    item.filename: source.read(item.filename)
                    for item in source.infolist()
                }
            variants = {
                "extra": {**members, "extra.bin": b"extra"},
                "missing": {
                    name: value for name, value in members.items()
                    if name != "model.safetensors"
                },
                "corrupt": {**members, "model.safetensors": b"corrupt"},
            }
            for label, contents in variants.items():
                with self.subTest(label=label):
                    bad_pack = root / f"{label}.zip"
                    with zipfile.ZipFile(bad_pack, "w") as target:
                        for name, value in contents.items():
                            target.writestr(name, value)
                    bad_cache = root / f"{label}-cache"
                    with self.assertRaises(RuntimeError):
                        import_model_pack(
                            bad_pack,
                            model / "manifest.json",
                            cache_root=bad_cache,
                        )
                    self.assertFalse(
                        cache_dir_for_manifest(
                            model / "manifest.json", bad_cache
                        ).exists()
                    )


if __name__ == "__main__":
    unittest.main()
