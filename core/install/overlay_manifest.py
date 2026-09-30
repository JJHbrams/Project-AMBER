"""Build manifest generation and validation for the frozen overlay."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

from core.install import app_payload
from core.install.model_manifest import validate_manifest_metadata
from core.install.versioning import resolve_version, write_snapshot


MANIFEST_NAME = "build-manifest.json"
SCHEMA_VERSION = 2
BUILD_KINDS = ("full", "fast-patch")
EXECUTABLES = ("engram-overlay.exe", "engram-dashboard.exe")
BRIDGE_EXECUTABLE = "engram-mcp-bridge.exe"
# Files PyInstaller bakes into the executables or collects as non-app payload.
# Any change here needs the full PyInstaller build.
ENGINE_FILES = (
    "requirements.txt",
    "environment.yml",
    "engram_overlay_entry.py",
    "resource/icon.ico",
)
ENGINE_PATTERNS = ("*.spec", "installer/pyi_rth_*.py", "native-bubble-shell/*")
SMOKE_POLICY = {
    "version": 1,
    "full": "all roles run",
    "fast-patch": (
        "runtime-contract always; embedding/smoke/dashboard only when the changed "
        "files fall in the import closure of their seeds; select-all on "
        "config/*.yaml or core/entrypoint.py"
    ),
    "seeds": {role: list(patterns) for role, patterns in app_payload.SMOKE_SEEDS.items()},
}
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
    "target",
    "gen",
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
    for name in ("overlay", "core", "discord_bot", "scripts/kg", "native-bubble-shell"):
        directory = root / name
        if directory.is_dir():
            files.update(path for path in directory.rglob("*") if path.is_file())
    for name in (
        "mcp_server.py",
        "engram_overlay_entry.py",
        "engram-overlay.spec",
        "engram-mcp-bridge.spec",
        "scripts/engram_mcp_bridge.py",
        "resource/icon.ico",
        "installer/pyi_rth_engram_tk.py",
        "requirements.txt",
        "environment.yml",
        "VERSION",
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

def _read_manifest(artifact_dir: Path) -> dict[str, Any] | None:
    try:
        data = _read_json(Path(artifact_dir) / MANIFEST_NAME)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Temp file + ``os.replace``: the target may be hardlinked to another artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def is_engine_input(relative: str) -> bool:
    return relative in ENGINE_FILES or any(
        fnmatch.fnmatchcase(relative, pattern) for pattern in ENGINE_PATTERNS
    )


def split_inputs(hashes: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    engine = {key: value for key, value in hashes.items() if is_engine_input(key)}
    app = {key: value for key, value in hashes.items() if key not in engine}
    return engine, app


def engine_id(engine_inputs: dict[str, str], environment: dict[str, Any]) -> str:
    payload = json.dumps(
        {"inputs": engine_inputs, "environment": environment},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_model_manifest(root: Path, path: Path) -> Path:
    if not path.is_absolute() and not path.exists() and (root / path).exists():
        return root / path
    return path


def _diff(baseline: dict[str, str], current: dict[str, str]) -> list[str]:
    return sorted(
        key for key in set(baseline) | set(current) if baseline.get(key) != current.get(key)
    )


def _toc_from_build_dir(root: Path) -> dict[str, Any]:
    path = root / "build" / app_payload.TOC_NAME
    return app_payload.load_toc(path) if path.is_file() else app_payload.empty_toc()


def _pe_versions(artifact_dir: Path) -> dict[str, str]:
    try:
        from core.install.pe_version import read_version
    except Exception:
        return {}
    found = {}
    for name in EXECUTABLES:
        value = read_version(artifact_dir / name)
        if value:
            found[name] = value
    return found


def make_manifest(
    root: Path,
    model_manifest_path: Path,
    mode: str,
    *,
    kind: str = "full",
    artifact_dir: Path | None = None,
    toc: dict[str, Any] | None = None,
    smoke: dict[str, str] | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if kind not in BUILD_KINDS:
        raise ValueError(f"unknown build kind: {kind}")
    root = Path(root)
    model_manifest = _read_json(model_manifest_path)
    inputs = input_hashes(root)
    if not inputs:
        raise ValueError("overlay build manifest cannot be written without inputs")
    version = resolve_version(root)
    from core.install.service_lifecycle import repository_fingerprint

    environment = environment_metadata()
    engine_inputs, app_inputs = split_inputs(inputs)
    toc = app_payload.load_toc(toc) if toc is not None else _toc_from_build_dir(root)
    files = app_payload.payload_files(toc)
    closure = app_payload.bridge_closure(root, toc)
    digest = ""
    bridge_sha = ""
    pe_versions: dict[str, str] = {}
    if artifact_dir is not None:
        artifact_dir = Path(artifact_dir)
        if files:
            digest = app_payload.digest_files(app_payload.internal_dir(artifact_dir), files)
        bridge_exe = artifact_dir / BRIDGE_EXECUTABLE
        if bridge_exe.is_file():
            bridge_sha = _hash_file(bridge_exe)
        pe_versions = _pe_versions(artifact_dir)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "build_kind": kind,
        "version": version.to_dict(),
        "source_repository": repository_fingerprint(root),
        "environment": environment,
        "inputs": inputs,
        "engine": {
            "id": engine_id(engine_inputs, environment),
            "inputs": engine_inputs,
        },
        "app": {
            "inputs": app_inputs,
            "toc": toc,
            "payload_files": files,
            "payload_digest": digest,
            "imports": app_payload.all_external_imports(root, toc),
        },
        "bridge": {
            "closure_hash": app_payload.bridge_closure_hash(closure),
            "modules": sorted(closure),
            "sha256": bridge_sha,
        },
        "smoke": dict(smoke or {}),
        "smoke_policy": SMOKE_POLICY,
        "pe_version": pe_versions,
        "embedding_model": {
            "manifest_sha256": _hash_file(model_manifest_path),
            "manifest": model_manifest,
        },
    }
    if previous is not None:
        manifest["patched_from"] = {
            "version": previous.get("version"),
            "build_kind": previous.get("build_kind"),
            "payload_digest": (previous.get("app") or {}).get("payload_digest", ""),
        }
    return manifest


def write_manifest(
    root: Path,
    artifact_dir: Path,
    model_manifest_path: Path,
    mode: str,
    kind: str = "full",
    smoke: dict[str, str] | None = None,
) -> Path:
    root = Path(root)
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / MANIFEST_NAME
    toc = None
    previous = None
    if kind == "fast-patch":
        previous = _read_manifest(artifact_dir)
        if previous is None or previous.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("fast-patch manifest needs a schema 2 baseline manifest in the artifact")
        baseline = previous["app"]["inputs"]
        changed = _diff(baseline, split_inputs(input_hashes(root))[1])
        toc = app_payload.resolve_toc(root, previous["app"]["toc"], changed)[0]
    _write_json_atomic(
        path,
        make_manifest(
            root,
            model_manifest_path,
            mode,
            kind=kind,
            artifact_dir=artifact_dir,
            toc=toc,
            smoke=smoke,
            previous=previous,
        ),
    )
    return path


def _valid_smoke_status(status: str) -> bool:
    return status in {"pass", "fail"} or (
        status.startswith("skipped:") and len(status) > len("skipped:")
    )


def record_smoke(artifact_dir: Path, results: dict[str, str]) -> Path:
    """Merge smoke results into the artifact manifest (atomic replace)."""
    for role, status in results.items():
        if not role or not _valid_smoke_status(status):
            raise ValueError(f"invalid smoke result {role}={status}")
    path = Path(artifact_dir) / MANIFEST_NAME
    manifest = _read_manifest(Path(artifact_dir))
    if manifest is None:
        raise ValueError(f"build manifest unreadable: {path}")
    smoke = dict(manifest.get("smoke") or {})
    smoke.update(results)
    manifest["smoke"] = smoke
    _write_json_atomic(path, manifest)
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
    valid_model, model_reason = validate_manifest_metadata(
        model_manifest_path,
        expected_model_id=None,
    )
    if not valid_model:
        return False, model_reason
    try:
        manifest = _read_json(build_manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return False, f"build manifest unreadable: {exc}"
    if manifest.get("schema_version") != SCHEMA_VERSION:
        return False, "unsupported build manifest schema (full build required)"
    if manifest.get("version") != resolve_version(root).to_dict():
        return False, "overlay build version changed"
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
    app_section = manifest.get("app") or {}
    files = app_section.get("payload_files") or []
    recorded = app_section.get("payload_digest") or ""
    if files and recorded:
        actual = app_payload.digest_files(app_payload.internal_dir(artifact_dir), files)
        if actual != recorded:
            return False, "app payload drifted from build manifest"
    target = str((manifest.get("version") or {}).get("version", ""))
    for name, stamped in _pe_versions(artifact_dir).items():
        if stamped != target:
            return False, f"{name} PE version {stamped} does not match build {target}"
    return True, "valid"


def _full_smoke() -> dict[str, bool]:
    return {
        "runtime-contract": True,
        "embedding": True,
        "smoke": True,
        "dashboard": True,
        "embedding_in_smoke": True,
    }


def _plan(root: Path, artifact_dir: Path, model_manifest_path: Path) -> dict[str, Any]:
    reasons: list[str] = []
    plan: dict[str, Any] = {
        "build": "full",
        "reasons": reasons,
        "changed_files": [],
        "smoke": _full_smoke(),
        "bridge_rebuild": True,
        "restamp": True,
        "compile": [],
        "copy": [],
        "remove": [],
        "added_modules": [],
        "unresolved_imports": [],
        "version": {"artifact": None, "source": None},
    }
    current = input_hashes(root)
    current_version = resolve_version(root).to_dict()
    plan["version"]["source"] = current_version.get("version")
    manifest = _read_manifest(artifact_dir)
    executables_present = all((artifact_dir / name).is_file() for name in EXECUTABLES)
    if manifest is None or not executables_present:
        reasons.append("overlay/dashboard executable or build manifest missing")
        plan["changed_files"] = sorted(current)
        return plan
    plan["version"]["artifact"] = (manifest.get("version") or {}).get("version")
    baseline_inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), dict) else {}
    plan["changed_files"] = _diff(baseline_inputs, current)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        reasons.append(
            f"build manifest schema {manifest.get('schema_version')!r} predates the fast path; full build required"
        )
        return plan

    valid_model, model_reason = validate_manifest_metadata(model_manifest_path, expected_model_id=None)
    if not valid_model:
        reasons.append(f"embedding model manifest invalid: {model_reason}")
    environment = environment_metadata()
    if manifest.get("environment") != environment:
        reasons.append("Python or package environment changed")
    current_engine, current_app = split_inputs(current)
    engine_section = manifest.get("engine") or {}
    engine_changed = _diff(engine_section.get("inputs") or {}, current_engine)
    for name in engine_changed:
        reasons.append(f"engine input changed: {name}")
    if not engine_changed and engine_section.get("id") != engine_id(current_engine, environment):
        reasons.append("engine identity changed")

    app_section = manifest.get("app") or {}
    try:
        toc = app_payload.load_toc(app_section.get("toc"))
    except ValueError as exc:
        toc = app_payload.empty_toc()
        reasons.append(f"app TOC unusable: {exc}")
    if not toc["pure"]:
        reasons.append("app TOC missing or empty in build manifest")
    files = app_payload.payload_files(toc)
    recorded_digest = app_section.get("payload_digest") or ""
    if files and recorded_digest:
        actual = app_payload.digest_files(app_payload.internal_dir(artifact_dir), files)
        if actual != recorded_digest:
            reasons.append("artifact app payload drifted from build manifest")
    if reasons:
        return plan

    app_changed = _diff(app_section.get("inputs") or {}, current_app)
    try:
        new_toc, added, removed = app_payload.resolve_toc(root, toc, app_changed)
        checked = sorted(set(app_changed) | set(added))
        new_imports = app_payload.external_imports(
            root, new_toc, checked, app_section.get("imports") or {}
        )
    except SyntaxError as exc:
        reasons.append(f"source does not compile: {exc.filename}:{exc.lineno}: {exc.msg}")
        return plan
    unresolved = app_payload.unresolved_imports(
        artifact_dir, [name for names in new_imports.values() for name in names]
    )
    plan["unresolved_imports"] = unresolved
    for name in unresolved:
        reasons.append(f"import not present in artifact: {name}")
    if reasons:
        return plan

    pure_sources = {entry["source"] for entry in new_toc["pure"]}
    data_sources = {entry["source"] for entry in new_toc["datas"]}
    version_changed = manifest.get("version") != current_version
    to_apply = sorted(
        rel
        for rel in set(app_changed) | set(added)
        if (root / rel).is_file() and (rel in pure_sources or rel in data_sources)
    )
    if version_changed and app_payload.VERSION_SNAPSHOT_REL in data_sources:
        to_apply = sorted(set(to_apply) | {app_payload.VERSION_SNAPSHOT_REL})
    target_version = str(current_version.get("version"))
    stamped = _pe_versions(artifact_dir)
    restamp = version_changed or any(value != target_version for value in stamped.values())
    closure_hash = app_payload.bridge_closure_hash(app_payload.bridge_closure(root, new_toc))
    bridge_section = manifest.get("bridge") or {}
    bridge_rebuild = closure_hash != bridge_section.get("closure_hash")
    recorded_bridge = bridge_section.get("sha256") or ""
    if recorded_bridge:
        bridge_exe = artifact_dir / BRIDGE_EXECUTABLE
        if not bridge_exe.is_file() or _hash_file(bridge_exe) != recorded_bridge:
            bridge_rebuild = True

    plan.update(
        {
            "compile": [rel for rel in to_apply if rel in pure_sources],
            "copy": [rel for rel in to_apply if rel in data_sources],
            "remove": removed,
            "added_modules": sorted(rel for rel in added if rel in pure_sources),
            "restamp": restamp,
            "bridge_rebuild": bridge_rebuild,
        }
    )
    if version_changed:
        reasons.append(f"version changed: {plan['version']['artifact']} -> {target_version}")
    elif restamp:
        reasons.append("exe PE version does not match the build version")
    if app_changed:
        reasons.append(f"app files changed: {len(app_changed)}")
    if removed:
        reasons.append(f"app files removed: {len(removed)}")
    if bridge_rebuild:
        reasons.append("bridge closure or executable changed")

    if not (app_changed or removed or version_changed or restamp or bridge_rebuild):
        plan["build"] = "reuse"
        plan["reasons"] = ["artifact matches the working tree"]
        plan["smoke"] = app_payload.select_smokes(root, new_toc, [])
        return plan

    plan["build"] = "fast"
    plan["smoke"] = app_payload.select_smokes(
        root, new_toc, sorted(set(plan["compile"]) | set(plan["copy"]) | set(removed))
    )
    return plan


def make_plan(root: Path, artifact_dir: Path, model_manifest_path: Path) -> dict[str, Any]:
    """Deterministic build plan.  The baseline is the artifact manifest, never git."""
    root = Path(root)
    artifact_dir = Path(artifact_dir)
    model_manifest_path = _resolve_model_manifest(root, Path(model_manifest_path))
    try:
        return _plan(root, artifact_dir, model_manifest_path)
    except Exception as exc:  # planning failure must never look like a reusable artifact
        plan = {
            "build": "full",
            "reasons": [f"plan failed: {type(exc).__name__}: {exc}"],
            "changed_files": [],
            "smoke": _full_smoke(),
            "bridge_rebuild": True,
            "restamp": True,
            "compile": [],
            "copy": [],
            "remove": [],
            "added_modules": [],
            "unresolved_imports": [],
            "version": {"artifact": None, "source": None},
        }
        return plan


def apply_fast(root: Path, stage: Path, model_manifest_path: Path) -> dict[str, Any]:
    """Apply a ``fast`` plan to ``stage`` (a hardlink clone) without PyInstaller."""
    root = Path(root)
    stage = Path(stage)
    model_manifest_path = _resolve_model_manifest(root, Path(model_manifest_path))
    plan = make_plan(root, stage, model_manifest_path)
    if plan["build"] == "full":
        raise RuntimeError("full build required: " + "; ".join(plan["reasons"]))
    result: dict[str, Any] = {"plan": plan, "applied": {"compiled": [], "copied": [], "removed": []}, "restamped": []}
    if plan["build"] == "reuse":
        return result
    manifest = _read_manifest(stage) or {}
    toc = app_payload.load_toc((manifest.get("app") or {}).get("toc"))
    baseline = (manifest.get("app") or {}).get("inputs") or {}
    new_toc = app_payload.resolve_toc(
        root, toc, _diff(baseline, split_inputs(input_hashes(root))[1])
    )[0]
    if plan["restamp"]:
        linked = [name for name in EXECUTABLES if (stage / name).stat().st_nlink > 1]
        if linked:
            # A running image (or any hardlink peer) cannot be replaced on Windows.
            raise RuntimeError(
                "stage executables must be real copies, not hardlinks: " + ", ".join(linked)
            )
    snapshot = root / app_payload.VERSION_SNAPSHOT_REL
    current_version = resolve_version(root)
    if plan["version"]["artifact"] != current_version.version or not snapshot.is_file():
        write_snapshot(snapshot, current_version)
    result["applied"] = app_payload.compile_payload(
        stage, root, new_toc, plan["compile"] + plan["copy"], plan["remove"]
    )
    if plan["restamp"]:
        from core.install.pe_version import restamp

        for name in EXECUTABLES:
            restamp(stage / name, current_version.version)
            result["restamped"].append(name)
    return result


def _parse_smoke_results(entries: list[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    for entry in entries:
        role, separator, status = entry.partition("=")
        if not separator:
            raise ValueError(f"--record-smoke expects role=status, got {entry!r}")
        results[role.strip()] = status.strip()
    return results


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--model-manifest", required=True, type=Path)
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--kind", choices=BUILD_KINDS, default="full")
    parser.add_argument("--record-smoke", action="append", default=[], metavar="ROLE=STATUS")
    args = parser.parse_args(argv)
    if args.plan:
        plan = make_plan(args.root, args.artifact, args.model_manifest)
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.apply:
        try:
            outcome = apply_fast(args.root, args.artifact, args.model_manifest)
        except Exception as exc:
            print(json.dumps({"applied": False, "error": f"{type(exc).__name__}: {exc}"}))
            return 1
        print(json.dumps(outcome, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    smoke = _parse_smoke_results(args.record_smoke)
    if args.write:
        write_manifest(
            args.root, args.artifact, args.model_manifest, args.mode, args.kind, smoke
        )
        print(json.dumps({"valid": True, "action": "written", "build_kind": args.kind}))
        return 0
    if smoke:
        record_smoke(args.artifact, smoke)
        print(json.dumps({"valid": True, "action": "smoke-recorded", "smoke": smoke}))
        return 0
    valid, reason = validate_build(args.root, args.artifact, args.model_manifest)
    print(json.dumps({"valid": valid, "reason": reason}))
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(_main())
