"""Canonical, reproducible offline embedding model manifest helpers."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any

try:
    import msvcrt as _platform_file_lock
except ImportError:
    import fcntl as _platform_file_lock


MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 2
DEFAULT_MODEL_ID = "intfloat/multilingual-e5-small"
MODEL_CACHE_ENV = "ENGRAM_MODEL_CACHE_DIR"
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


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


def manifest_sha256(manifest_path: Path) -> str:
    return _hash_file(Path(manifest_path))


def model_stamp_from_manifest(
    manifest_path: Path,
    expected_model_id: str | None = None,
) -> str:
    """Return the immutable semantic-vector identity for a canonical manifest."""
    manifest, reason = _canonical_manifest(
        Path(manifest_path).parent,
        expected_model_id,
    )
    if manifest is None:
        raise RuntimeError(reason)
    return f"{manifest['model_id']}@sha256:{manifest_sha256(manifest_path)}"


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


def validate_manifest_metadata(
    manifest_path: Path,
    expected_model_id: str | None = None,
) -> tuple[bool, str]:
    """Validate only the canonical manifest metadata, without requiring payload."""
    manifest, reason = _canonical_manifest(Path(manifest_path).parent, expected_model_id)
    return (manifest is not None), reason


def default_model_cache_root() -> Path:
    override = os.environ.get(MODEL_CACHE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".engram" / "models"


def cache_dir_for_manifest(manifest_path: Path, cache_root: Path | None = None) -> Path:
    root = Path(cache_root) if cache_root is not None else default_model_cache_root()
    return root / manifest_sha256(Path(manifest_path))


def bundled_manifest_path() -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return root / "resource" / "embedding-model" / MANIFEST_NAME


def _process_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


@contextlib.contextmanager
def _cache_publish_lock(lock_path: Path):
    """Serialize cache publication across threads and processes."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _process_lock(lock_path):
        handle = lock_path.open("a+b")
        try:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                _platform_file_lock.locking(
                    handle.fileno(), _platform_file_lock.LK_LOCK, 1
                )
            else:
                _platform_file_lock.flock(
                    handle.fileno(), _platform_file_lock.LOCK_EX
                )
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    _platform_file_lock.locking(
                        handle.fileno(), _platform_file_lock.LK_UNLCK, 1
                    )
                else:
                    _platform_file_lock.flock(
                        handle.fileno(), _platform_file_lock.LOCK_UN
                    )
        finally:
            handle.close()


def _copy_verified_source(source_dir: Path, stage_dir: Path, manifest: dict[str, Any]) -> None:
    valid, reason = validate_manifest(source_dir, manifest["model_id"])
    if not valid:
        raise RuntimeError(f"embedding model migration source invalid: {reason}")
    for relative_name in manifest["files"]:
        destination = stage_dir / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_dir / relative_name, destination)


def _write_canonical_manifest(stage_dir: Path, manifest_path: Path) -> None:
    shutil.copyfile(manifest_path, stage_dir / MANIFEST_NAME)


def _verify_stage(stage_dir: Path, expected_model_id: str) -> None:
    valid, reason = validate_manifest(stage_dir, expected_model_id)
    if not valid:
        raise RuntimeError(f"staged embedding model failed validation: {reason}")


def _publish_stage(stage_dir: Path, target_dir: Path) -> None:
    if target_dir.exists():
        raise RuntimeError(f"embedding model cache path already exists but is invalid: {target_dir}")
    os.replace(stage_dir, target_dir)


def ensure_cached_model(
    manifest_path: Path,
    *,
    cache_root: Path | None = None,
    allow_download: bool = False,
    migration_sources: tuple[Path, ...] | list[Path] = (),
    expected_model_id: str | None = None,
) -> Path:
    """Resolve a verified immutable user cache entry keyed by manifest SHA-256."""
    manifest_path = Path(manifest_path).resolve()
    manifest, reason = _canonical_manifest(manifest_path.parent, expected_model_id)
    if manifest is None:
        raise RuntimeError(reason)
    target_dir = cache_dir_for_manifest(manifest_path, cache_root)
    valid, _ = validate_manifest(target_dir, manifest["model_id"])
    if valid:
        return target_dir

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target_dir.parent / f".{target_dir.name}.lock"
    with _cache_publish_lock(lock_path):
        valid, _ = validate_manifest(target_dir, manifest["model_id"])
        if valid:
            return target_dir
        if target_dir.exists():
            raise RuntimeError(f"embedding model cache path already exists but is invalid: {target_dir}")

        stage_root = Path(
            tempfile.mkdtemp(prefix=f".{target_dir.name}.stage-", dir=target_dir.parent)
        )
        stage_dir = stage_root / "model"
        stage_dir.mkdir()
        try:
            migrated = False
            for source in migration_sources:
                source = Path(source)
                source_valid, _ = validate_manifest(source, manifest["model_id"])
                if not source_valid:
                    continue
                _copy_verified_source(source, stage_dir, manifest)
                migrated = True
                break
            if not migrated:
                if not allow_download:
                    raise RuntimeError("verified embedding model payload is not available offline")
                _export_to_staging(stage_dir, manifest, allow_download=True)
            _write_canonical_manifest(stage_dir, manifest_path)
            _verify_stage(stage_dir, manifest["model_id"])
            _publish_stage(stage_dir, target_dir)
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)
    return target_dir


def _safe_pack_members(archive: zipfile.ZipFile) -> list[str]:
    entries = archive.infolist()
    if any(item.is_dir() for item in entries):
        raise RuntimeError("model pack contains unexpected directory entries")
    names = [item.filename for item in entries]
    if len(names) != len(set(names)):
        raise RuntimeError("model pack contains duplicate paths")
    for name in names:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
            raise RuntimeError(f"model pack contains unsafe path: {name}")
    return names


def create_model_pack(model_dir: Path, output_path: Path) -> Path:
    """Create a deterministic offline pack from an exactly verified payload."""
    model_dir = Path(model_dir).resolve()
    valid, reason = validate_manifest(model_dir)
    if not valid:
        raise RuntimeError(f"cannot pack invalid embedding model: {reason}")
    manifest = read_manifest(model_dir)
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in [MANIFEST_NAME, *manifest["files"]]:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                with (model_dir / name).open("rb") as source, archive.open(info, "w") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return output_path


def import_model_pack(
    pack_path: Path,
    manifest_path: Path,
    *,
    cache_root: Path | None = None,
) -> Path:
    """Verify and atomically import an offline pack into the shared cache."""
    manifest_path = Path(manifest_path).resolve()
    manifest, reason = _canonical_manifest(manifest_path.parent)
    if manifest is None:
        raise RuntimeError(reason)
    target_dir = cache_dir_for_manifest(manifest_path, cache_root)
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target_dir.parent / f".{target_dir.name}.lock"
    with _cache_publish_lock(lock_path):
        valid, _ = validate_manifest(target_dir, manifest["model_id"])
        if valid:
            return target_dir
        if target_dir.exists():
            raise RuntimeError(f"embedding model cache path already exists but is invalid: {target_dir}")
        stage_root = Path(
            tempfile.mkdtemp(prefix=f".{target_dir.name}.stage-", dir=target_dir.parent)
        )
        stage_dir = stage_root / "model"
        stage_dir.mkdir()
        try:
            with zipfile.ZipFile(pack_path, "r") as archive:
                names = _safe_pack_members(archive)
                expected_names = {MANIFEST_NAME, *manifest["files"]}
                if set(names) != expected_names:
                    raise RuntimeError("model pack file inventory does not match canonical manifest")
                if archive.read(MANIFEST_NAME) != manifest_path.read_bytes():
                    raise RuntimeError("model pack manifest does not match canonical manifest")
                for name in names:
                    destination = stage_dir / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
            _verify_stage(stage_dir, manifest["model_id"])
            _publish_stage(stage_dir, target_dir)
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)
    return target_dir


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
    parser = argparse.ArgumentParser(
        description="Verify, hydrate, create, or import the pinned Engram FP32 embedding model payload."
    )
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--ensure", action="store_true")
    parser.add_argument("--ensure-cache", action="store_true")
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--validate-metadata", action="store_true")
    parser.add_argument("--refresh-manifest", action="store_true")
    parser.add_argument("--revision")
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--legacy-model-dir", action="append", default=[], type=Path)
    parser.add_argument("--create-pack", type=Path)
    parser.add_argument("--import-pack", type=Path)
    args = parser.parse_args(argv)
    try:
        model_dir = args.model_dir or bundled_manifest_path().parent
        manifest_path = model_dir / MANIFEST_NAME
        validation_dir = model_dir
        if args.refresh_manifest:
            if not args.revision:
                raise RuntimeError("--refresh-manifest requires --revision")
            refresh_manifest(model_dir, args.model_id, args.revision)
            print("refreshed")
        elif args.ensure:
            print(ensure_model(model_dir, args.model_id, args.allow_download))
        elif args.import_pack:
            resolved = import_model_pack(
                args.import_pack,
                manifest_path,
                cache_root=args.cache_root,
            )
            validation_dir = resolved
            print(str(resolved))
        elif args.ensure_cache:
            resolved = ensure_cached_model(
                manifest_path,
                cache_root=args.cache_root,
                allow_download=args.allow_download,
                migration_sources=args.legacy_model_dir,
                expected_model_id=args.model_id,
            )
            validation_dir = resolved
            print(str(resolved))
        elif args.create_pack:
            source = model_dir
            if not validate_manifest(source, args.model_id)[0]:
                source = ensure_cached_model(
                    manifest_path,
                    cache_root=args.cache_root,
                    allow_download=args.allow_download,
                    migration_sources=args.legacy_model_dir,
                    expected_model_id=args.model_id,
                )
            validation_dir = source
            print(str(create_model_pack(source, args.create_pack)))

        if args.validate_metadata:
            valid, reason = validate_manifest_metadata(manifest_path, args.model_id)
        elif args.ensure_cache or args.import_pack:
            valid, reason = validate_manifest(validation_dir, args.model_id)
        else:
            valid, reason = validate_manifest(validation_dir, args.model_id)
        print(json.dumps({"valid": valid, "reason": reason}))
        return 0 if valid else 1
    except Exception as exc:
        print(json.dumps({"valid": False, "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(_main())
