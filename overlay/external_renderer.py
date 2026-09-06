"""Connected renderer registry and ID-only selection persistence for Event API v2."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .event_api import connected_renderer_snapshot


@dataclass(frozen=True)
class InstalledRenderer:
    id: str
    name: str
    supported_modes: tuple[str, ...]


@dataclass(frozen=True)
class RendererDiagnostic:
    path: Path
    reason: str


def discover_renderers(home: Path | None = None) -> tuple[list[InstalledRenderer], list[RendererDiagnostic]]:
    del home
    renderers: dict[str, InstalledRenderer] = {}
    for item in connected_renderer_snapshot():
        renderer_id = str(item.get("id") or "")
        if not renderer_id:
            continue
        modes = tuple(mode for mode in item.get("supported_modes", ()) if mode in {"observer", "replace"})
        current = renderers.get(renderer_id)
        if current is None or "replace" in modes:
            renderers[renderer_id] = InstalledRenderer(renderer_id, str(item.get("name") or renderer_id), modes or ("observer",))
    return sorted(renderers.values(), key=lambda item: item.id.lower()), []


def apply_renderer_selection(user_cfg: dict[str, Any], renderer: InstalledRenderer | None, mode: str | None = None) -> bool:
    """Persist only renderer identity and mode while preserving unrelated YAML."""
    chosen_mode = (mode or "observer").lower()
    if renderer is not None and (chosen_mode not in {"observer", "replace"} or chosen_mode not in renderer.supported_modes):
        return False
    overlay = user_cfg.get("overlay")
    if renderer is None:
        if isinstance(overlay, dict): overlay.pop("external_renderer", None)
        return True
    if not isinstance(overlay, dict):
        overlay = {}; user_cfg["overlay"] = overlay
    overlay["external_renderer"] = {"selected_renderer_id": renderer.id, "mode": chosen_mode}
    return True


def legacy_renderer_diagnostic(cfg: dict[str, Any]) -> str:
    ext = ((cfg.get("overlay") or {}).get("external_renderer") if isinstance(cfg.get("overlay"), dict) else None)
    return "기존 command 기반 외부 오버레이 설정은 안전을 위해 실행되지 않습니다. 독립 renderer를 시작한 뒤 다시 선택하세요." if isinstance(ext, dict) and ext.get("command") else ""
