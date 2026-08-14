"""Offline embedding model manifest creation and validation."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import shutil
import sys
from pathlib import Path
from typing import Any


MANIFEST_NAME = "manifest.json"
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
        relative = path.relative_to(model_dir).as_posix()
        result[relative] = _hash_file(path)
    return result


def _inventory_hash(file_hashes: dict[str, str]) -> str:
    payload = "\n".join(f"{name}\0{file_hashes[name]}" for name in sorted(file_hashes))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sentence_transformers_version() -> str:
    try:
        return importlib.metadata.version("sentence-transformers")
    except importlib.metadata.PackageNotFoundError:
        return ""


def _cached_revision(model_id: str, file_hashes: dict[str, str]) -> str:
    parts = _normalise_model_id(model_id).split("/")
    if len(parts) == 2:
        cache_root = (
            Path.home()
            / ".cache"
            / "huggingface"
            / "hub"
            / f"models--{parts[0]}--{parts[1]}"
            / "snapshots"
        )
        snapshots = sorted(cache_root.glob("*"))
        if snapshots:
            return snapshots[-1].name
    return f"local-export:{_inventory_hash(file_hashes)}"


def create_manifest(
    model_dir: Path,
    model_id: str = DEFAULT_MODEL_ID,
    sentence_transformers_version: str | None = None,
    resolved_revision: str | None = None,
) -> dict[str, Any]:
    hashes = _file_hashes(model_dir)
    if not hashes:
        raise ValueError(f"embedding model has no exported files: {model_dir}")
    model_id = _normalise_model_id(model_id)
    return {
        "schema_version": 1,
        "model_id": model_id,
        "resolved_revision": (
            resolved_revision
            or _cached_revision(model_id, hashes)
        ),
        "sentence_transformers_version": (
            sentence_transformers_version or _sentence_transformers_version()
        ),
        "files": hashes,
    }


def read_manifest(model_dir: Path) -> dict[str, Any]:
    path = model_dir / MANIFEST_NAME
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(
    model_dir: Path,
    expected_model_id: str | None = None,
) -> tuple[bool, str]:
    path = model_dir / MANIFEST_NAME
    if not path.is_file():
        return False, f"manifest missing: {path}"
    try:
        manifest = read_manifest(model_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, f"manifest unreadable: {exc}"

    if manifest.get("schema_version") != 1:
        return False, "unsupported embedding manifest schema"
    actual_model_id = _normalise_model_id(manifest.get("model_id", ""))
    if expected_model_id and actual_model_id != _normalise_model_id(expected_model_id):
        return False, f"model id mismatch: {actual_model_id}"
    if not str(manifest.get("resolved_revision", "")).strip():
        return False, "resolved revision missing"
    if not str(manifest.get("sentence_transformers_version", "")).strip():
        return False, "sentence-transformers version missing"

    expected_files = manifest.get("files")
    if not isinstance(expected_files, dict) or not expected_files:
        return False, "model file hashes missing"
    actual_files = _file_hashes(model_dir)
    if set(expected_files) != set(actual_files):
        return False, "model file list changed"
    for name, expected_hash in expected_files.items():
        if str(expected_hash).lower() != actual_files[name].lower():
            return False, f"model file hash mismatch: {name}"
    return True, "valid"


def _write_manifest(model_dir: Path, model_id: str) -> None:
    manifest = create_manifest(model_dir, model_id=model_id)
    (model_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _clear_export_dir(model_dir: Path) -> None:
    for path in model_dir.iterdir():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def ensure_model(
    model_dir: Path,
    model_id: str = DEFAULT_MODEL_ID,
    allow_download: bool = False,
) -> str:
    model_dir = model_dir.resolve()
    model_dir.mkdir(parents=True, exist_ok=True)
    valid, reason = validate_manifest(model_dir, expected_model_id=model_id)
    if valid:
        return "reused"
    if any(model_dir.iterdir()):
        if not allow_download:
            raise RuntimeError(reason)
        # 같은 차원의 구형 모델은 파일 모양만으로 판별할 수 없다. 스탬프가
        # 없거나 불일치하면 기존 export를 신뢰하지 않고 요청 모델로 교체한다.
        shutil.rmtree(model_dir)
        model_dir.mkdir(parents=True, exist_ok=True)

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError("sentence-transformers is required to export the model") from exc

    _clear_export_dir(model_dir)
    try:
        model = SentenceTransformer(model_id, local_files_only=True, device="cpu")
    except Exception:
        if not allow_download:
            raise RuntimeError(
                f"embedding model is not cached offline: {model_id}"
            )
        model = SentenceTransformer(model_id, device="cpu")
    model.save(str(model_dir))
    _write_manifest(model_dir, model_id)
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
    args = parser.parse_args(argv)
    try:
        if args.ensure:
            print(ensure_model(args.model_dir, args.model_id, args.allow_download))
        valid, reason = validate_manifest(args.model_dir, args.model_id)
        print(json.dumps({"valid": valid, "reason": reason}))
        return 0 if valid else 1
    except Exception as exc:
        print(json.dumps({"valid": False, "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(_main())
