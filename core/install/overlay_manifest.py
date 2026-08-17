"""Build manifest generation and validation for the frozen overlay."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

from core.install.model_manifest import validate_manifest


MANIFEST_NAME = "build-manifest.json"
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "temp",
    "tmp",
}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_allowed(root: Path, path: Path) -> bool:
    relative_parts = path.relative_to(root).parts
    return not any(
        part in EXCLUDED_PARTS or part.endswith((".pyc", ".pyo"))
        for part in relative_parts
    )


def input_files(root: Path) -> list[Path]:
    files: set[Path] = set()
    for name in ("overlay", "core", "discord_bot", "scripts/kg"):
        directory = root / name
        if directory.is_dir():
            files.update(path for path in directory.rglob("*") if path.is_file())
    for name in (
        "mcp_server.py",
        "engram_overlay_entry.py",
        "engram-overlay.spec",
        "installer/pyi_rth_engram_tk.py",
        "requirements.txt",
        "environment.yml",
    ):
        path = root / name
        if path.is_file():
            files.add(path)
    # Only files embedded by engram-overlay.spec belong to this artifact.
    # Local user overrides and installer/client configuration must not force a
    # 1+ GiB frozen rebuild.
    for name in ("config/overlay.yaml", "config/config.yaml"):
        path = root / name
        if path.is_file():
            files.add(path)
    for name in ("resource/icon.png", "resource/overlay.png", "resource/embedding-model/manifest.json"):
        path = root / name
        if path.is_file():
            files.add(path)
    character_dir = root / "resource/character"
    if character_dir.is_dir():
        files.update(path for path in character_dir.rglob("*") if path.is_file())
    return sorted(path for path in files if _is_allowed(root, path))


def input_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _hash_file(path)
        for path in input_files(root)
    }


def environment_metadata() -> dict[str, Any]:
    packages: dict[str, str] = {}
    for distribution in (
        "pyinstaller",
        "pyinstaller-hooks-contrib",
        "mcp",
        "sentence-transformers",
        "torch",
        "transformers",
        "streamlit",
        "pandas",
        "pyarrow",
        "scipy",
        "scikit-learn",
        "numpy",
        "pillow",
        "kuzu",
        "tkinterweb",
        "discord.py",
    ):
        try:
            packages[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            packages[distribution] = ""
    fastmcp_import = False
    fastmcp_error = ""
    try:
        from mcp.server.fastmcp import FastMCP

        fastmcp_import = callable(FastMCP)
    except Exception as exc:
        fastmcp_error = f"{type(exc).__name__}: {exc}"
    return {
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "packages": packages,
        "fastmcp_import": fastmcp_import,
        "fastmcp_error": fastmcp_error,
    }


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def make_manifest(
    root: Path,
    model_manifest_path: Path,
    mode: str,
) -> dict[str, Any]:
    model_manifest = _read_json(model_manifest_path)
    inputs = input_hashes(root)
    if not inputs:
        raise ValueError("overlay build manifest cannot be written without inputs")
    return {
        "schema_version": 1,
        "mode": mode,
        "environment": environment_metadata(),
        "inputs": inputs,
        "embedding_model": {
            "manifest_sha256": _hash_file(model_manifest_path),
            "manifest": model_manifest,
        },
    }


def write_manifest(
    root: Path,
    artifact_dir: Path,
    model_manifest_path: Path,
    mode: str,
) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / MANIFEST_NAME
    path.write_text(
        json.dumps(
            make_manifest(root, model_manifest_path, mode),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def validate_build(
    root: Path,
    artifact_dir: Path,
    model_manifest_path: Path,
) -> tuple[bool, str]:
    build_manifest_path = artifact_dir / MANIFEST_NAME
    executable = artifact_dir / "engram-overlay.exe"
    dashboard_executable = artifact_dir / "engram-dashboard.exe"
    if (
        not executable.is_file()
        or not dashboard_executable.is_file()
        or not build_manifest_path.is_file()
    ):
        return False, "overlay/dashboard executable or build manifest missing"
    valid_model, model_reason = validate_manifest(
        model_manifest_path.parent,
        expected_model_id=None,
    )
    if not valid_model:
        return False, model_reason
    try:
        manifest = _read_json(build_manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, f"build manifest unreadable: {exc}"
    if manifest.get("schema_version") != 1:
        return False, "unsupported build manifest schema"
    manifest_inputs = manifest.get("inputs")
    if not isinstance(manifest_inputs, dict) or not manifest_inputs:
        return False, "overlay build manifest has no inputs"
    current_environment = environment_metadata()
    if manifest.get("environment") != current_environment:
        return False, "Python or package environment changed"
    if manifest_inputs != input_hashes(root):
        return False, "overlay build inputs changed"
    model_section = manifest.get("embedding_model")
    if not isinstance(model_section, dict):
        return False, "embedding model build metadata missing"
    if model_section.get("manifest_sha256") != _hash_file(model_manifest_path):
        return False, "embedding model manifest changed"
    if model_section.get("manifest") != _read_json(model_manifest_path):
        return False, "embedded model metadata changed"
    return True, "valid"


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--model-manifest", required=True, type=Path)
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args(argv)
    if args.write:
        write_manifest(args.root, args.artifact, args.model_manifest, args.mode)
        print(json.dumps({"valid": True, "action": "written"}))
        return 0
    valid, reason = validate_build(args.root, args.artifact, args.model_manifest)
    print(json.dumps({"valid": valid, "reason": reason}))
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(_main())
