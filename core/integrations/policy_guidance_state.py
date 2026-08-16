from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


POLICY_GUIDANCE_DISABLED_PATH = Path.home() / ".engram" / "policy-guidance.disabled"


def sync_policy_guidance_disabled_marker(enabled: bool) -> dict[str, Any]:
    """Synchronize the cheap launcher-level OFF marker atomically."""
    path = POLICY_GUIDANCE_DISABLED_PATH
    try:
        if enabled:
            changed = path.exists()
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            changed = not path.exists()
            fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write("Policy guidance disabled by user.\n")
                os.replace(tmp_path, path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
        return {"ok": True, "changed": changed, "enabled": enabled, "path": str(path)}
    except OSError as exc:
        return {
            "ok": False,
            "changed": False,
            "enabled": enabled,
            "path": str(path),
            "error": str(exc),
        }
