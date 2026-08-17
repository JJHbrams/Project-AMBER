"""Canonical, reproducible offline embedding model manifest helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 2
DEFAULT_MODEL_ID = "intfloat/multilingual-e5-small"


def _normalise_model_id(value: str) -> str:
    value = str(value or "").strip()
    if value.startswith("sentence-transformers/"):
        return value[len("sentence-transformers/") :]
    return value


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_hashes(model_dir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(model_dir.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        result[path.relative_to(model_dir).as_posix()] = _hash_file(path)
    return result


def create_manifest(
    model_dir: Path,
    model_id: str = DEFAULT_MODEL_ID,
    *,
    resolved_revision: str,
) -> dict[str, Any]:
    """Build a canonical manifest without inspecting local cache or packages."""
    hashes = _file_hashes(model_dir)
    if not hashes:
        raise ValueError(f"embedding model has no exported files: {model_dir}")
    revision = str(resolved_revision or "").strip()
    if not revision:
        raise ValueError("resolved revision is required")
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "model_id": _normalise_model_id(model_id),
        "resolved_revision": revision,
        "files": {name: hashes[name] for name in sorted(hashes)},
    }


def read_manifest(model_dir: Path) -> dict[str, Any]:
    return json.loads((model_dir / MANIFEST_NAME).read_text(encoding="utf-8"))


def _canonical_manifest(model_dir: Path, expected_model_id: str | None = None) -> tuple[dict[str, Any] | None, str]:
    path = model_dir / MANIFEST_NAME
    if not path.is_file():
        return None, f"manifest missing: {path}"
    try:
        manifest = read_manifest(model_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, f"manifest unreadable: {exc}"
    if not isinstance(manifest, dict) or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        return None, "unsupported embedding manifest schema"
    actual_model_id = _normalise_model_id(manifest.get("model_id", ""))
    if not actual_model_id:
        return None, "model id missing"
    if expected_model_id and actual_model_id != _normalise_model_id(expected_model_id):
        return None, f"model id mismatch: {actual_model_id}"
    if not str(manifest.get("resolved_revision", "")).strip():
        return None, "resolved revision missing"
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        return None, "model file hashes missing"
    normalized: dict[str, str] = {}
    for name, value in files.items():
        if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts:
            return None, "invalid model file path"
        digest = str(value).lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            return None, f"invalid model file hash: {name}"
        normalized[name] = digest
    if list(files) != sorted(files):
        return None, "model file hashes are not sorted"
    manifest["files"] = normalized
    return manifest, "valid"


def validate_manifest(model_dir: Path, expected_model_id: str | None = None) -> tuple[bool, str]:
    manifest, reason = _canonical_manifest(model_dir, expected_model_id)
    if manifest is None:
        return False, reason
    actual_files = _file_hashes(model_dir)
    expected_files = manifest["files"]
    if set(expected_files) != set(actual_files):
        return False, "model file list changed"
    for name, expected_hash in expected_files.items():
        if expected_hash != actual_files[name].lower():
            return False, f"model file hash mismatch: {name}"
    return True, "valid"


def _write_manifest_atomic(model_dir: Path, manifest: dict[str, Any]) -> None:
    target = model_dir / MANIFEST_NAME
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=model_dir, delete=False) as stream:
        stream.write(payload)
        temporary = Path(stream.name)
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def refresh_manifest(model_dir: Path, model_id: str, resolved_revision: str) -> dict[str, Any]:
    """Explicitly regenerate the tracked manifest from local model assets."""
    if not model_dir.is_dir():
        raise RuntimeError(f"embedding model directory missing: {model_dir}")
    manifest = create_manifest(model_dir, model_id=model_id, resolved_revision=resolved_revision)
    _write_manifest_atomic(model_dir, manifest)
    valid, reason = validate_manifest(model_dir, expected_model_id=model_id)
    if not valid:
        raise RuntimeError(f"refreshed embedding manifest failed validation: {reason}")
    return manifest


def _export_to_staging(stage_dir: Path, manifest: dict[str, Any], allow_download: bool) -> None:
    """Hydrate exact pinned Hub files without version-dependent reserialization."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise RuntimeError("huggingface-hub is required to hydrate the model") from exc
    for relative_name in manifest["files"]:
        try:
            source = hf_hub_download(
                repo_id=manifest["model_id"],
                filename=relative_name,
                revision=manifest["resolved_revision"],
                local_files_only=not allow_download,
            )
        except Exception as download_error:
            mode = "download" if allow_download else "offline cache lookup"
            raise RuntimeError(
                f"embedding model {mode} failed: {relative_name}"
            ) from download_error
        destination = stage_dir / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _replace_assets_from_stage(model_dir: Path, stage_dir: Path) -> None:
    """Replace only ignored model assets; preserve canonical manifest bytes."""
    manifest_path = model_dir / MANIFEST_NAME
    manifest_bytes = manifest_path.read_bytes()
    backup = model_dir.parent / f".{model_dir.name}.backup-{next(tempfile._get_candidate_names())}"
    replacement = model_dir.parent / f".{model_dir.name}.replacement-{next(tempfile._get_candidate_names())}"
    replacement.mkdir()
    try:
        for path in stage_dir.iterdir():
            shutil.move(str(path), replacement / path.name)
        (replacement / MANIFEST_NAME).write_bytes(manifest_bytes)
        os.replace(model_dir, backup)
        os.replace(replacement, model_dir)
        shutil.rmtree(backup)
    except Exception:
        if not model_dir.exists() and backup.exists():
            os.replace(backup, model_dir)
        raise
    finally:
        if replacement.exists():
            shutil.rmtree(replacement, ignore_errors=True)
        if backup.exists() and model_dir.exists():
            shutil.rmtree(backup, ignore_errors=True)


def ensure_model(model_dir: Path, model_id: str = DEFAULT_MODEL_ID, allow_download: bool = False) -> str:
    """Validate canonical assets or hydrate their exact pinned inventory."""
    model_dir = model_dir.resolve()
    manifest, reason = _canonical_manifest(model_dir, expected_model_id=model_id)
    if manifest is None:
        raise RuntimeError(reason)
    valid, reason = validate_manifest(model_dir, expected_model_id=model_id)
    if valid:
        return "reused"
    if not allow_download:
        raise RuntimeError(reason)
    stage_root = Path(tempfile.mkdtemp(prefix="engram-model-stage-", dir=model_dir.parent))
    stage_dir = stage_root / "model"
    stage_dir.mkdir()
    try:
        _export_to_staging(stage_dir, manifest, allow_download=True)
        if manifest["files"] != _file_hashes(stage_dir):
            raise RuntimeError("downloaded embedding model does not match canonical manifest")
        _replace_assets_from_stage(model_dir, stage_dir)
    finally:
        shutil.rmtree(stage_root, ignore_errors=True)
    valid, reason = validate_manifest(model_dir, expected_model_id=model_id)
    if not valid:
        raise RuntimeError(f"exported embedding model failed validation: {reason}")
    return "exported"


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--ensure", action="store_true")
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--refresh-manifest", action="store_true")
    parser.add_argument("--revision")
    args = parser.parse_args(argv)
    try:
        if args.refresh_manifest:
            if not args.revision:
                raise RuntimeError("--refresh-manifest requires --revision")
            refresh_manifest(args.model_dir, args.model_id, args.revision)
            print("refreshed")
        elif args.ensure:
            print(ensure_model(args.model_dir, args.model_id, args.allow_download))
        valid, reason = validate_manifest(args.model_dir, args.model_id)
        print(json.dumps({"valid": valid, "reason": reason}))
        return 0 if valid else 1
    except Exception as exc:
        print(json.dumps({"valid": False, "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(_main())
