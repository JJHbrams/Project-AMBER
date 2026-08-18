"""Small character reaction stickers derived only from public bubble events."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import tkinter as tk
import time

from PIL import Image, ImageTk

from overlay.bubble.bubble_window import BubbleWindow
from overlay.character_assets import ReactionPackResolution, resolve_reaction_pack
from overlay.config import resolve_path

SHEET_COLUMNS = 6
SHEET_ROWS = 4
CELL_WIDTH = 434
CELL_HEIGHT = 408
CHROMA_TOLERANCE = 24
PUBLIC_EVENT_FIELDS = ("kind", "text", "tool_name", "tool_output", "is_error")
_SHOW_KINDS = {"thought", "tool_use", "tool_result", "turn_end", "result", "error"}


def is_memory_tool_name(tool_name: object) -> bool:
    """Return whether a public tool name represents Engram memory activity."""
    tool = str(tool_name or "").lower()
    return any(token in tool for token in ("mcp__engram__", "engram/", "memory", "kg_"))


@dataclass(frozen=True)
class Reaction:
    state: str
    index: int
    immediate: bool = False


_REACTIONS = {
    "thought": Reaction("thought", 1),
    "deep_thought": Reaction("deep_thought", 20),
    "tool_use": Reaction("tool_use", 3),
    "success": Reaction("success", 2),
    "error": Reaction("error", 17, True),
    "wait": Reaction("wait", 12),
    "blocked": Reaction("blocked", 14, True),
}


def public_event(event: object) -> dict[str, object]:
    """Copy only fields intentionally exposed by the bubble event contract."""
    if not isinstance(event, dict):
        return {}
    return {field: event.get(field) for field in PUBLIC_EVENT_FIELDS if field in event}


def _event_text(event: dict[str, object]) -> str:
    return " ".join(str(event.get(key) or "") for key in ("text", "tool_name", "tool_output")).lower()


def classify_reaction(event: object, allow_text_keywords: bool = True) -> Reaction | None:
    """Return a deterministic badge mapping without inspecting private event data."""
    safe = public_event(event)
    kind = str(safe.get("kind") or "").lower()
    if kind not in _SHOW_KINDS:
        return None
    text = _event_text(safe) if allow_text_keywords else ""
    if allow_text_keywords:
        if any(word in text for word in ("blocked", "denied", "permission", "private", "거절", "권한", "비공개", "차단")):
            return _REACTIONS["blocked"]
        if any(word in text for word in ("error", "failed", "failure", "conflict", "오류", "실패", "충돌")):
            return _REACTIONS["error"]
        if any(word in text for word in ("wait", "waiting", "retry", "대기", "재시도")):
            return _REACTIONS["wait"]
        if kind == "thought" and any(word in text for word in ("deep", "long", "extended", "깊", "오래", "장고")):
            return _REACTIONS["deep_thought"]
    if bool(safe.get("is_error")):
        return _REACTIONS["error"]
    if kind == "thought":
        return _REACTIONS["thought"]
    if kind == "tool_use":
        return _REACTIONS["tool_use"]
    if kind in {"tool_result", "turn_end", "result"}:
        return _REACTIONS["success"]
    if kind == "error":
        return _REACTIONS["error"]
    return None


def validate_sheet(image: Image.Image, columns: int = SHEET_COLUMNS, rows: int = SHEET_ROWS, cell_width: int = CELL_WIDTH, cell_height: int = CELL_HEIGHT) -> bool:
    return all(isinstance(value, int) and value > 0 for value in (columns, rows, cell_width, cell_height)) and image.size == (cell_width * columns, cell_height * rows)


def crop_sprite(image: Image.Image, index: int, columns: int = SHEET_COLUMNS, rows: int = SHEET_ROWS, cell_width: int = CELL_WIDTH, cell_height: int = CELL_HEIGHT) -> Image.Image:
    if not validate_sheet(image, columns, rows, cell_width, cell_height) or not 0 <= index < columns * rows:
        raise ValueError("invalid reaction sprite sheet")
    col, row = index % columns, index // columns
    return image.crop((col * cell_width, row * cell_height, (col + 1) * cell_width, (row + 1) * cell_height))


def key_chroma(image: Image.Image, chroma_key: str = "#00FF00", tolerance: int = CHROMA_TOLERANCE) -> Image.Image:
    value = chroma_key.strip().lstrip("#")
    try:
        key = tuple(int(value[offset:offset + 2], 16) for offset in (0, 2, 4))
        if len(value) != 6:
            raise ValueError
    except (TypeError, ValueError):
        key = (0, 255, 0)
    rgba = image.convert("RGBA")
    rgba.putdata([
        (red, green, blue, 0 if max(abs(red - key[0]), abs(green - key[1]), abs(blue - key[2])) <= tolerance else alpha)
        for red, green, blue, alpha in rgba.getdata()
    ])
    return rgba


def is_engram_character(name: object) -> bool:
    return Path(str(name or "")).stem.lower() == "engram"


def is_reaction_pack_applicable(character_name: object, pack_id: object, apply_to_custom: object) -> bool:
    return bool(apply_to_custom) or Path(str(character_name or "")).stem.lower() == str(pack_id or "").lower()


def reaction_character_identity(character_cfg: object) -> object:
    if not isinstance(character_cfg, dict):
        return ""
    return character_cfg.get("name") or character_cfg.get("set")


class ReactionBadgeController:
    """Tk owner for a transient sprite badge; failure to load assets disables it."""

    def __init__(self, root: tk.Tk, get_char_rect: Callable[[], tuple[int, int, int, int]], character_cfg: object):
        self._root = root
        self._get_char_rect = get_char_rect
        character_cfg = character_cfg if isinstance(character_cfg, dict) else {}
        cfg = character_cfg.get("reactions") if isinstance(character_cfg.get("reactions"), dict) else {}
        self._cfg = cfg
        self._pack_id = str(cfg.get("pack") or "").strip()
        self._pack: ReactionPackResolution = resolve_reaction_pack(self._pack_id)
        self._badge = BubbleWindow(root)
        self._sheet: Image.Image | None = None
        self._cache: dict[tuple[int, int], ImageTk.PhotoImage] = {}
        self._last_state: str | None = None
        self._last_event_at = 0.0
        self.enabled = bool(cfg.get("enabled", False)) and is_reaction_pack_applicable(
            reaction_character_identity(character_cfg), self._pack_id or "engram", cfg.get("apply_to_custom", False)
        )
        if self.enabled:
            self._load_sheet()

    def _load_sheet(self) -> None:
        try:
            path = self._pack.sprite_sheet if self._pack.source != "disabled" else resolve_path(str(self._cfg.get("sprite_sheet") or ""))
            with Image.open(Path(path)) as source:
                sheet = source.convert("RGBA")
            columns = self._pack.columns or int(self._cfg.get("columns", SHEET_COLUMNS))
            rows = self._pack.rows or int(self._cfg.get("rows", SHEET_ROWS))
            cell_width = self._pack.cell_width or CELL_WIDTH
            cell_height = self._pack.cell_height or CELL_HEIGHT
            if not validate_sheet(sheet, columns, rows, cell_width, cell_height):
                raise ValueError("unexpected sheet grid")
            self._sheet = sheet
        except Exception:
            self.enabled = False
            self._sheet = None

    def _image_for(self, index: int, char_width: int) -> ImageTk.PhotoImage | None:
        if self._sheet is None:
            return None
        width = max(1, round(char_width * float(self._cfg.get("scale_ratio", 0.38))))
        cache_key = (index, width)
        if cache_key not in self._cache:
            columns = self._pack.columns or int(self._cfg.get("columns", SHEET_COLUMNS))
            rows = self._pack.rows or int(self._cfg.get("rows", SHEET_ROWS))
            cell_width = self._pack.cell_width or CELL_WIDTH
            cell_height = self._pack.cell_height or CELL_HEIGHT
            chroma_key = self._pack.chroma_key if self._pack.source != "disabled" else str(self._cfg.get("chroma_key", "#00FF00"))
            sprite = key_chroma(crop_sprite(self._sheet, index, columns, rows, cell_width, cell_height), chroma_key)
            height = max(1, round(sprite.height * width / sprite.width))
            self._cache[cache_key] = ImageTk.PhotoImage(sprite.resize((width, height), Image.LANCZOS))
        return self._cache[cache_key]

    def show_event(self, event: object) -> Reaction | None:
        reaction = classify_reaction(event, bool(self._cfg.get("allow_text_keywords", True)))
        if not self.enabled or reaction is None:
            return reaction
        now = time.monotonic() * 1000
        if reaction.state == self._last_state and not reaction.immediate:
            if now - self._last_event_at >= int(self._cfg.get("debounce_ms", 450)):
                self._render(reaction)
            self._last_event_at = now
            self._badge.schedule_dismiss(int(self._cfg.get("dwell_ms", 2400)))
            return reaction
        self._last_state = reaction.state
        self._last_event_at = now
        self._render(reaction)
        self._badge.schedule_dismiss(int(self._cfg.get("dwell_ms", 2400)))
        return reaction

    def _render(self, reaction: Reaction) -> None:
        char_x, char_y, char_w, _char_h = self._get_char_rect()
        index = (self._pack.mapping or {}).get(reaction.state, reaction.index)
        image = self._image_for(index, char_w)
        if image is None:
            return
        canvas = self._badge.ensure()
        canvas.delete("all")
        canvas.configure(width=image.width(), height=image.height())
        canvas.create_image(0, 0, anchor="nw", image=image)
        # Keep a direct canvas reference as an additional guard against Tk image GC.
        canvas.image = image
        x = char_x + char_w - image.width() // 2
        y = char_y - image.height() // 3
        x = max(0, min(x, self._root.winfo_screenwidth() - image.width()))
        y = max(0, min(y, self._root.winfo_screenheight() - image.height()))
        self._badge.place(x, y, image.width(), image.height())

    def refresh_position(self) -> None:
        if self.enabled and self._last_state is not None and self._badge.is_visible():
            reaction = _REACTIONS.get(self._last_state)
            if reaction is not None:
                self._render(reaction)

    def hide(self) -> None:
        self._last_state = None
        self._last_event_at = 0.0
        self._badge.hide()
