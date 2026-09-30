"""Shared source/frozen runtime contract for the Engram overlay entrypoint.

This check deliberately avoids opening listeners, creating UI windows, or loading the
embedding model.  It proves that both runtimes can import the same canonical modules,
load the effective overlay configuration, and resolve required bundled resources.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from core.install.versioning import resolve_version


def _under(path: Path, base: Path) -> bool:
    try:
        Path(os.path.normcase(str(path.resolve()))).relative_to(
            Path(os.path.normcase(str(base.resolve())))
        )
    except ValueError:
        return False
    return True


def _module_origins(modules: dict[str, Any]) -> dict[str, str]:
    """``__file__`` of the core modules; frozen code must come from ``sys._MEIPASS``."""
    origins = {name: str(getattr(module, "__file__", "") or "") for name, module in modules.items()}
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", ""))
        for name, origin in origins.items():
            if not origin or not _under(Path(origin), base):
                raise RuntimeError(
                    f"frozen module {name} loaded from outside the bundle: {origin or '<none>'}"
                )
    return origins


def _build_manifest_facts() -> tuple[str, str]:
    """``(build_kind, app_payload_digest)`` from ``<exe dir>/build-manifest.json``."""
    if not getattr(sys, "frozen", False):
        return "", ""
    executable_dir = Path(sys.executable).resolve().parent
    try:
        manifest = json.loads((executable_dir / "build-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    files = (manifest.get("app") or {}).get("payload_files") or []
    digest = ""
    if files:
        digest = _payload_digest(Path(getattr(sys, "_MEIPASS", executable_dir)), files)
    return str(manifest.get("build_kind", "")), digest


def _payload_digest(contents: Path, files: list[str]) -> str:
    """Same bytes as ``app_payload.digest_files``; kept dependency-light on purpose.

    The frozen artifact only bundles stdlib modules PyInstaller saw at the full
    build, so the runtime must not import the (heavier) build-side helper.
    """
    import hashlib

    digest = hashlib.sha256()
    for dest in sorted(set(files)):
        path = Path(contents) / dest
        value = "MISSING"
        if path.is_file():
            file_hash = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    file_hash.update(chunk)
            value = file_hash.hexdigest()
        digest.update(f"{dest}\0{value}\n".encode("utf-8"))
    return digest.hexdigest()


def evaluate_runtime_contract() -> dict[str, Any]:
    if not getattr(sys, "frozen", False):
        source_root = Path(__file__).resolve().parents[2]
        kg_path = str(source_root / "scripts" / "kg")
        if kg_path not in sys.path:
            sys.path.insert(0, kg_path)

    from mcp.server.fastmcp import FastMCP

    if not callable(FastMCP):
        raise RuntimeError("mcp.server.fastmcp.FastMCP is not importable")

    import kg_watcher  # noqa: F401
    import mcp_server  # noqa: F401
    import overlay.main as overlay_main
    from overlay.config import load_cfg, resolve_path

    cfg = load_cfg(strict=True, create_user_config=False)
    if not isinstance(cfg, dict):
        raise RuntimeError("overlay configuration did not load as a mapping")

    required_resources = ("config/overlay.yaml", "resource/icon.png")
    resolved_resources: dict[str, str] = {}
    for relative in required_resources:
        resolved = resolve_path(relative).resolve()
        if not resolved.is_file():
            raise RuntimeError(f"required runtime resource missing: {relative} -> {resolved}")
        resolved_resources[relative] = str(resolved)

    overlay_cfg = cfg.get("overlay") if isinstance(cfg.get("overlay"), dict) else {}
    mcp_cfg = cfg.get("mcp") if isinstance(cfg.get("mcp"), dict) else {}
    dashboard_cfg = cfg.get("dashboard") if isinstance(cfg.get("dashboard"), dict) else {}
    frozen = bool(getattr(sys, "frozen", False))
    source_root = "" if frozen else str(Path(__file__).resolve().parents[2])
    version = resolve_version()
    from core.install.service_config import service_config_provenance
    import core as core_package

    module_origins = _module_origins(
        {"overlay.main": overlay_main, "core": core_package, "mcp_server": mcp_server}
    )
    build_kind, app_payload_digest = _build_manifest_facts()
    return {
        "contract_version": 1,
        "runtime": "frozen" if frozen else "source",
        "version": version.version,
        "version_build_source": version.build_source,
        "version_commit": version.commit,
        "entrypoint": "engram_overlay_entry.py",
        "pid": os.getpid(),
        "python": str(Path(sys.executable).resolve()),
        "source_root": source_root,
        "service_config": service_config_provenance(),
        "project_root": str(Path(overlay_main.PROJECT_ROOT).resolve()),
        "stm_port": int(overlay_cfg.get("stm_server_port", 17384)),
        "mcp_port": int(mcp_cfg.get("http_port", 17385)),
        "dashboard_enabled": bool(dashboard_cfg.get("enabled", True)),
        "dashboard_port": int(dashboard_cfg.get("port", 8501)),
        "selected_renderer_id": str((overlay_cfg.get('external_renderer') or {}).get('selected_renderer_id', '')),
        "resources": resolved_resources,
        "app_payload_digest": app_payload_digest,
        "module_origins": module_origins,
        "build_kind": build_kind,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the Engram runtime contract")
    parser.parse_args(argv)
    print(json.dumps(evaluate_runtime_contract(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
