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

    cfg = load_cfg()
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
        "project_root": str(Path(overlay_main.PROJECT_ROOT).resolve()),
        "stm_port": int(overlay_cfg.get("stm_server_port", 17384)),
        "mcp_port": int(mcp_cfg.get("http_port", 17385)),
        "dashboard_enabled": bool(dashboard_cfg.get("enabled", True)),
        "dashboard_port": int(dashboard_cfg.get("port", 8501)),
        "resources": resolved_resources,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the Engram runtime contract")
    parser.parse_args(argv)
    print(json.dumps(evaluate_runtime_contract(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
