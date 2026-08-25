"""Discovery and safe persistence helpers for installed overlay renderers.

Renderer manifests are deliberately data-only.  The Settings UI never accepts a
command from the user: it may select only a validated manifest below the user's
``~/.engram/overlays`` directory.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
from typing import Any

import yaml

log = logging.getLogger(__name__)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MODES = frozenset({"observer", "replace"})


@dataclass(frozen=True)
class InstalledRenderer:
    id: str
    name: str
    command: tuple[str, ...]
    supported_modes: tuple[str, ...]
    manifest_path: Path


@dataclass(frozen=True)
class RendererDiagnostic:
    path: Path
    reason: str


def overlays_root(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".engram" / "overlays"


def _safe_child(root: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def load_renderer_manifest(manifest_path: Path) -> InstalledRenderer:
    """Validate one v1 manifest and return its canonical command.

    The executable's relative path is resolved before persistence so later
    process startup does not depend on Engram's current working directory.
    """
    manifest_path = manifest_path.resolve()
    renderer_dir = manifest_path.parent
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("manifest must be a mapping")
    if raw.get("schema_version") != 1:
        raise ValueError("schema_version must be exactly 1")
    renderer_id = raw.get("id")
    if not isinstance(renderer_id, str) or not _ID.fullmatch(renderer_id):
        raise ValueError("id must be a safe renderer identifier")
    if renderer_id != renderer_dir.name:
        raise ValueError("id must match its containing directory")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    command = raw.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x.strip() for x in command):
        raise ValueError("command must be a non-empty argv string list")
    executable = Path(command[0]).expanduser()
    if not executable.is_absolute():
        executable = (renderer_dir / executable).resolve()
        if not _safe_child(renderer_dir, executable):
            raise ValueError("relative executable must remain within manifest directory")
    if not executable.is_file():
        raise ValueError("renderer executable is unavailable")
    modes = raw.get("supported_modes", ["observer"])
    if not isinstance(modes, list) or not modes or not all(isinstance(x, str) for x in modes):
        raise ValueError("supported_modes must be a non-empty string list")
    normalized_modes = tuple(x.lower() for x in modes)
    if len(set(normalized_modes)) != len(normalized_modes):
        raise ValueError("supported_modes must not contain duplicates")
    if any(x not in _MODES for x in normalized_modes):
        raise ValueError("supported_modes contains an unsupported mode")
    return InstalledRenderer(renderer_id, name.strip(), (str(executable), *command[1:]), normalized_modes, manifest_path)


def discover_renderers(home: Path | None = None) -> tuple[list[InstalledRenderer], list[RendererDiagnostic]]:
    root = overlays_root(home)
    if not root.is_dir():
        return [], []
    renderers: list[InstalledRenderer] = []
    diagnostics: list[RendererDiagnostic] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir() or not _ID.fullmatch(child.name):
            continue
        manifest = child / "manifest.yaml"
        if not manifest.is_file():
            diagnostics.append(RendererDiagnostic(manifest, "manifest.yaml is missing"))
            continue
        try:
            renderers.append(load_renderer_manifest(manifest))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            diagnostics.append(RendererDiagnostic(manifest, str(exc)))
            log.warning("[overlay] external renderer manifest unavailable: %s: %s", manifest, exc)
    return renderers, diagnostics


def apply_renderer_selection(user_cfg: dict[str, Any], renderer: InstalledRenderer | None, mode: str | None = None) -> bool:
    """Merge the selected renderer without disturbing unrelated user settings.

    ``False`` means an invalid selection was refused and ``user_cfg`` is left
    untouched.  ``None`` selects the built-in renderer.
    """
    chosen_mode = (mode or "observer").lower()
    if renderer is not None and chosen_mode not in renderer.supported_modes:
        return False
    overlay = user_cfg.get("overlay")
    if renderer is None:
        if isinstance(overlay, dict):
            overlay.pop("external_renderer", None)
        return True
    if not isinstance(overlay, dict):
        overlay = {}
        user_cfg["overlay"] = overlay
    overlay["external_renderer"] = {"mode": chosen_mode, "command": list(renderer.command)}
    return True
