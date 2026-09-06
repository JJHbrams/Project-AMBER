"""캐릭터 오버레이 창 — 투명 always-on-top, 드래그/클릭 가능."""

import ctypes
from ctypes import wintypes
import random
import re
import tkinter as tk
import time
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from PIL import Image, ImageTk
import yaml

from overlay.bubble import geometry as bubble_geometry

from overlay.character_assets import (
    USER_CHARACTER_SETS_DIR,
    USER_REACTION_PACKS_DIR,
    ReactionPackResolution,
    inline_reaction_pack,
    resolve_layered_timeline,
    resolve_bundled_character_source,
    resolve_character_set,
    resolve_legacy_asset,
    resolve_reaction_pack,
)
from overlay.reaction_badge import crop_sprite, is_memory_tool_name, key_chroma, public_event
from overlay.config import (
    _USER_CONFIG_PATH, resolve_editable_overlay_path, load_cfg,
    get_overlay_state, set_flip_horizontal, update_overlay_state,
    update_overlay_state_async,
)

USER_CONFIG_DIR = Path.home() / ".engram"
USER_OVERLAY_RESOURCE = USER_CONFIG_DIR / "overlay.png"
USER_CHARACTER_DIR = USER_CONFIG_DIR / "character"
RESOURCE_OVERLAY = resolve_editable_overlay_path("resource/overlay.png")
_CHROMA = "#010101"


def launcher_tooltip_position(lx: int, ly: int, width: int, height: int, work: tuple[int, int, int, int]) -> tuple[int, int]:
    """Choose a measured tooltip position without assuming a primary monitor."""
    candidates = ((lx + 58, ly + 8), (lx - width - 6, ly + 8), (lx + 26 - width // 2, ly - height - 6), (lx + 26 - width // 2, ly + 58))
    x, y = next(((cx, cy) for cx, cy in candidates if work[0] <= cx and cx + width <= work[2] and work[1] <= cy and cy + height <= work[3]), candidates[-1])
    return bubble_geometry.clamp_rect(x, y, width, height, work)


def presentation_menu_action(
    launcher_mode: bool, *, can_collapse: bool, can_hide_to_tray: bool,
) -> tuple[str, str] | None:
    """Return the one presentation action appropriate to the visible surface."""
    if launcher_mode and can_hide_to_tray:
        return "트레이로 숨기기", "hide_to_tray"
    if not launcher_mode and can_collapse:
        return "런처로 접기", "collapse"
    return None
_SMALL_MODEL_RE = re.compile(r"\b[0-4](?:\.\d+)?b\b", re.IGNORECASE)
log = logging.getLogger(__name__)


def _is_widget_descendant(widget, ancestor) -> bool:
    """Whether focus remains on a surface or one of its child widgets."""
    while widget is not None:
        if widget is ancestor:
            return True
        try:
            widget = widget.master
        except Exception:
            return False
    return False


def _clamp_menu_geometry(x: int, y: int, width: int, height: int, work_rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    cx, cy = bubble_geometry.clamp_rect(int(x), int(y), int(width), int(height), work_rect)
    return int(width), int(height), cx, cy


def _point_outside_rect(point: tuple[int, int], rect: tuple[int, int, int, int]) -> bool:
    x, y = point
    left, top, right, bottom = rect
    return not (left <= x < right and top <= y < bottom)


def _menu_entry_is_selected(kind: str, variable_value, *, value="", onvalue="1") -> bool:
    """Mirror Tk menu value matching without treating a non-empty StringVar as true."""
    current = "" if variable_value is None else str(variable_value)
    if kind == "radiobutton":
        return current == str(value)
    if kind == "checkbutton":
        return current == str(onvalue)
    return False


def _clamp_float(value, minimum: float, maximum: float, default: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, num))


def _clamp_int(value, minimum: int, maximum: int, default: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, num))


def clamp_overlay_position(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    """Keep a restored window within the work area nearest its saved position."""
    work = bubble_geometry.get_monitor_work_rect(x + width // 2, y + height // 2)
    return bubble_geometry.clamp_rect(x, y, width, height, work)


def _parse_chroma_key(value: object, default: tuple[int, int, int] = (1, 1, 1)) -> tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return default
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return default


def _key_chroma_background(image: Image.Image, chroma_key: tuple[int, int, int], tolerance: int = 20) -> Image.Image:
    """Return an RGBA effect image with near-chroma pixels made transparent."""
    rgba = image.convert("RGBA")
    pixels = list(rgba.getdata())
    keyed = [
        (red, green, blue, 0 if max(abs(red - chroma_key[0]), abs(green - chroma_key[1]), abs(blue - chroma_key[2])) <= tolerance else alpha)
        for red, green, blue, alpha in pixels
    ]
    rgba.putdata(keyed)
    return rgba


def _with_opacity(image: Image.Image, opacity: float) -> Image.Image:
    result = image.copy()
    alpha = result.getchannel("A").point(lambda value: int(value * max(0.0, min(1.0, opacity))))
    result.putalpha(alpha)
    return result


def _thicken_effect_pixels(image: Image.Image, thickness_px: int) -> Image.Image:
    """Expand colored RGBA effect pixels after downscaling without touching the base sprite."""
    thickness_px = _clamp_int(thickness_px, 1, 6, 1)
    if thickness_px == 1:
        return image

    radius = thickness_px - 1
    expanded = Image.new("RGBA", image.size)
    for offset_y in range(-radius, radius + 1):
        for offset_x in range(-radius, radius + 1):
            if offset_x * offset_x + offset_y * offset_y <= radius * radius:
                expanded.alpha_composite(image, (offset_x, offset_y))
    return expanded


def _render_effect_frame(
    base: Image.Image,
    effect: Image.Image | None,
    opacity: float = 0.0,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    offset_y: int = 0,
    offset_x: int = 0,
    effect_thickness_px: int = 1,
) -> Image.Image:
    """Compose a cached RGBA VFX layer without changing the character canvas size."""
    base = base.convert("RGBA")
    width, height = base.size
    transformed = Image.new("RGBA", base.size)
    scaled_size = (max(1, round(width * scale_x)), max(1, round(height * scale_y)))
    scaled = base.resize(scaled_size, Image.LANCZOS)
    transformed.alpha_composite(scaled, ((width - scaled.width) // 2 + offset_x, (height - scaled.height) // 2 + offset_y))
    if effect is not None and opacity > 0:
        # VFX is pixel art: LANCZOS turns its one-pixel lines into sub-pixel alpha at overlay size.
        layer = effect.resize(base.size, Image.NEAREST)
        layer = _thicken_effect_pixels(layer, effect_thickness_px)
        transformed.alpha_composite(_with_opacity(layer, opacity))
    return transformed


def _animation_state(click_started_ms: float | None, now_ms: float, click_frame_ms: int) -> str:
    if click_started_ms is not None and now_ms - click_started_ms < click_frame_ms:
        return "click"
    return "idle"


def apply_sprite_crop_y_offset(image: Image.Image, offset_px: int) -> Image.Image:
    """Remove only a declared top gutter before scaling the remaining complete sprite."""
    if offset_px <= 0:
        return image
    offset = min(offset_px, image.height - 1)
    return image.crop((0, offset, image.width, image.height))


def target_height_for_work_area(work_size: tuple[int, int], ratio: float) -> int:
    """Calculate the shared static/sprite height from one monitor work area."""
    width, height = work_size
    return max(120, int(min(width, height) * ratio))


def file_fingerprint(path: Path) -> tuple[int, int] | None:
    """Small, stable change token suitable for UI-thread polling."""
    try:
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def fingerprint_paths(paths: set[Path]) -> tuple[tuple[str, tuple[int, int] | None], ...]:
    return tuple(sorted((str(path), file_fingerprint(path)) for path in paths))


def bottom_anchored_geometry(x: int, y: int, old_height: int, width: int, height: int) -> str:
    """Resize a borderless overlay while keeping its bottom edge stationary."""
    return f"{width}x{height}+{x}+{y + old_height - height}"


def _discover_numbered_frames(base_name: str, search_dirs: tuple[Path, ...]) -> dict[int, Path]:
    indexed: dict[int, Path] = {}
    pattern = re.compile(rf"^{re.escape(base_name)}_(\d+)\.png$", re.IGNORECASE)
    for directory in search_dirs:
        if not directory.exists():
            continue
        for path in directory.glob("*.png"):
            m = pattern.match(path.name)
            if not m:
                continue
            try:
                idx = int(m.group(1))
            except ValueError:
                continue
            # 앞선 directory가 우선순위를 갖도록 같은 index는 최초 발견값 유지
            if idx in indexed:
                continue
            indexed[idx] = path
    return indexed


_SPRITE_WORK_STATES = {"idle", "generating", "thought", "search", "memory"}
_SPRITE_LOCKED_STATES = {"click", "input", "success", "error", "provider_error"}


def classify_sprite_event(event: object) -> str | None:
    """Map public bubble events to a sprite state without reading private payloads."""
    safe = public_event(event)
    kind = str(safe.get("kind") or "").lower()
    if kind == "thought":
        return "thought"
    if kind == "tool_use":
        tool = str(safe.get("tool_name") or "").lower()
        if is_memory_tool_name(tool):
            return "memory"
        if any(token in tool for token in ("search", "find", "open", "web", "browser", "fetch", "read", "glob", "list", "grep", "rg", "explore")):
            return "search"
        return "generating"
    if kind == "tool_result":
        return "error" if bool(safe.get("is_error")) else "generating"
    if kind in {"turn_end", "result"}:
        return "success"
    if kind == "error":
        return "provider_error"
    return None


@dataclass
class SpriteStateMachine:
    """Pure display/work state model for the sprite-grid character."""

    states: set[str]
    state: str = "idle"
    work_state: str = "idle"
    hovered: bool = False
    input_active: bool = False
    started_ms: float = 0.0
    epoch: int = 0

    def _available(self, state: str) -> str:
        if state in self.states:
            return state
        if "idle" in self.states:
            return "idle"
        return "default"

    def _set_display(self, state: str, now_ms: float) -> bool:
        state = self._available(state)
        if state == self.state:
            return False
        self.state = state
        self.started_ms = now_ms
        self.epoch += 1
        return True

    @property
    def locked(self) -> bool:
        return self.state in _SPRITE_LOCKED_STATES and not (
            self.input_active and self.state == self._available("input")
        )

    def set_work(self, state: str, now_ms: float) -> bool:
        if state not in _SPRITE_WORK_STATES or state not in self.states or self.locked:
            return False
        self.work_state = state
        target = "input" if self.input_active else ("hover" if self.hovered else state)
        return self._set_display(target, now_ms)

    def show_transient(self, state: str, now_ms: float) -> bool:
        if state not in _SPRITE_LOCKED_STATES or state not in self.states:
            return False
        if state == "click" and self.state == "click":
            self.started_ms = now_ms
            self.epoch += 1
            return True
        if self.locked:
            return False
        return self._set_display(state, now_ms)

    def set_hovered(self, value: bool, now_ms: float) -> bool:
        self.hovered = value
        if self.locked:
            return False
        target = "input" if self.input_active else ("hover" if value else self.work_state)
        return self._set_display(target, now_ms)

    def set_input_active(self, value: bool, now_ms: float) -> bool:
        value = bool(value)
        if value == self.input_active:
            return False
        self.input_active = value
        target = "input" if value else ("hover" if self.hovered else self.work_state)
        return self._set_display(target, now_ms)

    def handle_event(self, event: object, now_ms: float) -> bool:
        state = classify_sprite_event(event)
        if state is None:
            return False
        if state in {"thought", "search", "memory"} and state not in self.states:
            state = "generating"
        if state == "success":
            if self.locked:
                return False
            self.work_state = self._available("idle")
            if self.input_active:
                return self._set_display("input", now_ms)
            return self.show_transient(state, now_ms)
        if state in {"error", "provider_error"}:
            if self.locked:
                return False
            self.work_state = self._available("idle")
            if self.input_active:
                return self._set_display("input", now_ms)
            return self.show_transient(state, now_ms)
        return self.set_work(state, now_ms)

    def expire(self, now_ms: float, dwell_ms: int) -> bool:
        if not self.locked or now_ms - self.started_ms < dwell_ms:
            return False
        return self._set_display("hover" if self.hovered else self.work_state, now_ms)


def _shuffle_cycle_order(
    frames: tuple[int, ...],
    state: str,
    epoch: int,
    cycle: int,
    orders: dict[tuple[str, int, int], tuple[int, ...]],
    rng: random.Random,
) -> tuple[int, ...]:
    key = (state, epoch, cycle)
    existing = orders.get(key)
    if existing is not None:
        return existing

    previous = orders.get((state, epoch, cycle - 1)) if cycle > 0 else None
    shuffled = list(frames)
    rng.shuffle(shuffled)
    if previous and len(shuffled) > 1 and shuffled[0] == previous[-1]:
        shuffled[0], shuffled[1] = shuffled[1], shuffled[0]
    order = tuple(shuffled)
    orders[key] = order
    return order


def select_sprite_frame(
    spec: dict,
    state: str,
    epoch: int,
    elapsed_ms: float,
    choices: dict[tuple[str, int, int], int],
    rng: random.Random | object = random,
    shuffle_orders: dict[tuple[str, int, int], tuple[int, ...]] | None = None,
) -> tuple[int, int]:
    """Pick a frame once per state-time bucket, retaining it for every redraw in that bucket."""
    frames = tuple(spec["frames"])
    frame_ms = max(1, int(spec["frame_ms"]))
    bucket = int(max(0, elapsed_ms) // frame_ms)
    selection = spec.get("selection", "fixed")
    if selection == "sequence":
        return frames[bucket % len(frames)], bucket
    if selection == "sequence_once":
        return frames[min(bucket, len(frames) - 1)], bucket
    if selection == "random":
        key = (state, epoch, bucket)
        if key not in choices:
            choices[key] = rng.choice(frames)  # type: ignore[attr-defined]
        return choices[key], bucket
    if selection == "rotation":
        key = (state, epoch, bucket)
        if key not in choices:
            previous = choices.get((state, epoch, bucket - 1)) if bucket > 0 else None
            candidates = tuple(frame for frame in frames if len(frames) == 1 or frame != previous)
            choices[key] = rng.choice(candidates)  # type: ignore[attr-defined]
        return choices[key], bucket
    if selection == "shuffle":
        orders = shuffle_orders if shuffle_orders is not None else {}
        cycle, position = divmod(bucket, len(frames))
        return _shuffle_cycle_order(frames, state, epoch, cycle, orders, rng)[position], bucket
    return frames[0], bucket


class _CharacterProfile:
    def __init__(self, cfg: dict):
        overlay_cfg = cfg.get("overlay", {})
        character_cfg = overlay_cfg.get("character", {})
        seq_cfg = character_cfg.get("sequence", {})
        effects_cfg = character_cfg.get("effects", {})
        if not isinstance(effects_cfg, dict):
            effects_cfg = {}

        self.name = str(character_cfg.get("name", "")).strip()
        self.set_id = str(character_cfg.get("set", "")).strip()
        stored_source_mode = str(character_cfg.get("source_mode") or "").strip().lower()
        legacy_sequence = Path(self.name).is_dir() or resolve_bundled_character_source(self.name, "sequence") is not None
        self.source_mode = stored_source_mode or ("sequence" if legacy_sequence else "static")
        if self.source_mode not in {"static", "sequence", "sprite_grid"}:
            self.source_mode = "static"
        self.set_resolution = resolve_character_set(self.set_id)
        reactions_cfg = character_cfg.get("reactions", {})
        reactions_cfg = reactions_cfg if isinstance(reactions_cfg, dict) else {}
        self.reactions_cfg = reactions_cfg
        inline_grid = reactions_cfg.get("grid") if isinstance(reactions_cfg.get("grid"), dict) else {}
        manifest_pack = resolve_reaction_pack(reactions_cfg.get("pack", self.set_id))
        inline_pack = inline_reaction_pack(
            reactions_cfg.get("sprite_sheet"),
            inline_grid,
            reactions_cfg.get("chroma_key"),
            reactions_cfg.get("crop_y_offset_px", 0),
        )
        # The built-in/user manifest is authoritative for its own sheet.  Inline grid is
        # only for a different explicitly selected custom PNG.
        if manifest_pack.source != "disabled":
            configured_sheet = str(reactions_cfg.get("sprite_sheet") or "")
            same_builtin = configured_sheet.replace("\\", "/").endswith(f"reactions/{reactions_cfg.get('pack', self.set_id)}/states.png")
            self.reaction_pack = manifest_pack if same_builtin or inline_pack.source == "disabled" else inline_pack
        else:
            self.reaction_pack = inline_pack
        if "crop_y_offset_px" in reactions_cfg:
            try:
                configured_offset = int(reactions_cfg["crop_y_offset_px"])
            except (TypeError, ValueError):
                configured_offset = -1
            if not isinstance(reactions_cfg["crop_y_offset_px"], bool) and 0 <= configured_offset < self.reaction_pack.cell_height:
                self.reaction_pack = replace(self.reaction_pack, crop_y_offset_px=configured_offset)
        # source_mode is authoritative.  In particular, a stale static/sequence
        # name must not veto an explicitly selected sprite-grid pack.
        self.sprite_enabled = self.source_mode == "sprite_grid" and bool(reactions_cfg.get("enabled", False)) and self.reaction_pack.source != "disabled" and bool(self.reaction_pack.states)

        self.sequence_enabled = bool(seq_cfg.get("enabled", True))
        self.trigger_chance = _clamp_float(seq_cfg.get("trigger_chance", 0.12), 0.0, 1.0, 0.12)
        self.start_index = _clamp_int(seq_cfg.get("start_index", 1), 0, 999, 1)
        self.end_index = _clamp_int(seq_cfg.get("end_index", 2), 0, 999, 2)
        self.repeat_count = _clamp_int(seq_cfg.get("repeat_count", 3), 1, 20, 3)
        self.interval_min_sec = _clamp_float(seq_cfg.get("interval_min_sec", 0.2), 0.05, 10.0, 0.2)
        self.interval_max_sec = _clamp_float(seq_cfg.get("interval_max_sec", 3.0), self.interval_min_sec, 30.0, 3.0)
        self.idle_check_interval_sec = _clamp_float(seq_cfg.get("idle_check_interval_sec", 1.0), 0.1, 30.0, 1.0)

        self.effects_enabled = bool(effects_cfg.get("enabled", False))
        # Sequence animation keeps its historical body motion.  Only explicit
        # static mode becomes geometry-stable by default.
        self.legacy_body_motion = self.source_mode != "static" or bool(effects_cfg.get("legacy_body_motion", False))
        self.effects_chroma_key = _parse_chroma_key(effects_cfg.get("chroma_key", _CHROMA))
        self.effects_idle_interval_ms = _clamp_int(effects_cfg.get("idle_interval_ms", 2400), 800, 12000, 2400)
        self.effects_click_frame_ms = _clamp_int(effects_cfg.get("click_frame_ms", 420), 120, 2000, 420)
        idle_manifest = self.set_resolution.idle
        click_manifest = self.set_resolution.click
        # Pack manifests are self-contained; legacy config thickness is only for legacy assets.
        self.effects_idle_thickness_px = (
            idle_manifest.thickness_px
            if idle_manifest else _clamp_int(effects_cfg.get("idle_thickness_px", 2), 1, 6, 2)
        )
        self.effects_click_thickness_px = (
            click_manifest.thickness_px
            if click_manifest else _clamp_int(effects_cfg.get("click_thickness_px", 3), 1, 6, 3)
        )
        self.effects_idle_asset = idle_manifest.path if idle_manifest else resolve_legacy_asset(effects_cfg.get("idle_asset"))
        self.effects_click_asset = click_manifest.path if click_manifest else resolve_legacy_asset(effects_cfg.get("click_asset"))

        self.frames_by_index: dict[int, Path] = {}
        self.default_frame: Path = RESOURCE_OVERLAY
        self.has_numbered_frames = False

        self._discover_frames()
        self.builtin_engram_identity = self._is_bundled_engram_click_target()
        self.click_vfx_enabled = self.effects_enabled and self.builtin_engram_identity

    @staticmethod
    def _same_path(left: Path | None, right: Path | None) -> bool:
        if left is None or right is None:
            return False
        try:
            return left.resolve() == right.resolve()
        except OSError:
            return left == right

    def _is_bundled_engram_click_target(self) -> bool:
        if self.source_mode == "sprite_grid":
            pack_id = str(self.reactions_cfg.get("pack", self.set_id)).strip().lower()
            return self.sprite_enabled and self.reaction_pack.source == "bundled" and pack_id == "engram"

        bundled_set_image = self.set_resolution.base_image if self.set_resolution.source == "bundled" else None
        default_engram_set = resolve_character_set("engram")
        canonical_engram_image = default_engram_set.base_image if default_engram_set.source == "bundled" else None
        return self._same_path(self.default_frame, bundled_set_image) or self._same_path(self.default_frame, canonical_engram_image)

    @staticmethod
    def _fallback_frame() -> Path:
        return USER_OVERLAY_RESOURCE if USER_OVERLAY_RESOURCE.exists() else RESOURCE_OVERLAY

    def _use_sequence_directory(self, directory: Path) -> bool:
        numbered = _discover_numbered_frames(directory.name, (directory,))
        if not numbered:
            return False
        self.frames_by_index = numbered
        self.has_numbered_frames = True
        self.default_frame = numbered.get(0, numbered[min(numbered.keys())])
        return True

    def _discover_frames(self):
        # A valid reaction pack is the character itself, not a floating badge.
        if self.sprite_enabled:
            return
        # Sprite-grid selection never falls through to a stale static/sequence
        # path.  Keep a stable base only as a graceful invalid-pack fallback.
        if self.source_mode == "sprite_grid":
            self.default_frame = self.set_resolution.base_image or self._fallback_frame()
            return

        name_path = Path(self.name)
        if name_path.is_absolute():
            if self.source_mode == "static" and name_path.is_file() and name_path.suffix.lower() == ".png":
                self.default_frame = name_path
                return
            if self.source_mode == "sequence" and name_path.is_dir() and self._use_sequence_directory(name_path):
                return
            legacy_bundled = resolve_bundled_character_source(self.name, self.source_mode)
            if self.source_mode == "static" and legacy_bundled is not None:
                self.default_frame = legacy_bundled
                return
            if self.source_mode == "sequence" and legacy_bundled is not None and self._use_sequence_directory(legacy_bundled):
                return
            self.default_frame = self._fallback_frame()
            return

        if self.source_mode == "static":
            if self.set_resolution.base_image is not None and self.name in {"", self.set_id}:
                self.default_frame = self.set_resolution.base_image
                return
            candidates = []
            if self.name:
                candidates.append(USER_CHARACTER_DIR / f"{self.name}.png")
                resolved = resolve_bundled_character_source(self.name, "static")
                if resolved is not None:
                    candidates.append(resolved)
            for single in candidates:
                if single.is_file() and single.suffix.lower() == ".png":
                    self.default_frame = single
                    return
        elif self.source_mode == "sequence" and self.name:
            candidates = [USER_CHARACTER_DIR / self.name]
            resolved = resolve_bundled_character_source(self.name, "sequence")
            if resolved is not None:
                candidates.append(resolved)
            for directory in candidates:
                if directory.is_dir() and self._use_sequence_directory(directory):
                    return
        self.default_frame = self._fallback_frame()

    def build_sequence_paths(self) -> list[Path]:
        if not (self.sequence_enabled and self.has_numbered_frames and self.frames_by_index):
            return []

        if self.start_index <= self.end_index:
            order = list(range(self.start_index, self.end_index + 1))
        else:
            order = list(range(self.start_index, self.end_index - 1, -1))

        step_paths = [self.frames_by_index[i] for i in order if i in self.frames_by_index]
        if not step_paths:
            return []

        sequence: list[Path] = []
        for _ in range(self.repeat_count):
            sequence.extend(step_paths)
        return sequence


class CharacterOverlay:
    def __init__(
        self,
        root: tk.Tk,
        on_activate: Callable[[], None],
        on_set_provider: Callable[[str], None] | None = None,
        on_get_provider: Callable[[], str] | None = None,
        on_quit: Callable[[], None] | None = None,
        on_set_provider_model: Callable[[str, "str | None"], None] | None = None,
        on_get_ollama_models: Callable[[], list] | None = None,
        on_get_ollama_model: Callable[[], str] | None = None,
        on_reload_ollama_models: Callable[[], None] | None = None,
        on_settings: Callable[[], None] | None = None,
        on_restart: Callable[[], None] | None = None,
        on_history: Callable[[], None] | None = None,
        on_pointer_event: Callable[[str, dict], None] | None = None,
        on_collapse: Callable[[], None] | None = None,
        on_expand: Callable[[], None] | None = None,
        on_hide_launcher: Callable[[], None] | None = None,
        is_launcher_mode: Callable[[], bool] | None = None,
    ):
        self.root = root
        self.on_activate = on_activate
        self.on_set_provider = on_set_provider
        self.on_get_provider = on_get_provider
        self.on_quit = on_quit
        self.on_set_provider_model = on_set_provider_model
        self.on_get_ollama_models = on_get_ollama_models
        self.on_get_ollama_model = on_get_ollama_model
        self.on_reload_ollama_models = on_reload_ollama_models
        self.on_settings = on_settings
        self.on_restart = on_restart
        self.on_history = on_history
        self.on_pointer_event = on_pointer_event
        self.on_collapse = on_collapse
        self.on_expand = on_expand
        self.on_hide_launcher = on_hide_launcher
        self.is_launcher_mode = is_launcher_mode
        self._launcher_canvas = None
        self._launcher_tooltip = None
        self._launcher_press = None
        self._launcher_moved = False
        self._full_rect = None
        self._launcher_expand_anchor: tuple[int, int] | None = None

        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", _CHROMA)
        self.root.configure(bg=_CHROMA)

        self._cfg = load_cfg()
        self._profile = _CharacterProfile(self._cfg)
        self._work_size = (self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        self._current_source = self._profile.default_frame
        self._sequence_queue: list[Path] = []
        self._animation_after_id: str | None = None
        self._click_started_ms: float | None = None
        self._effect_images: dict[str, Image.Image] = {}
        self._context_menu_open = False
        self._flip_h = bool(self._cfg["overlay"].get("flip_horizontal", False))
        self._sprite_sheet: Image.Image | None = None
        self._sprite_cache: dict[tuple[int, int, bool, int], Image.Image] = {}
        self._sprite_choices: dict[tuple[str, int, int], int] = {}
        self._sprite_shuffle_orders: dict[tuple[str, int, int], tuple[int, ...]] = {}
        self._sprite_selection_epoch = -1
        self._sprite_rng = random.Random(time.time_ns())
        initial_ms = time.monotonic() * 1000
        self._sprite_model = SpriteStateMachine(
            set(self._profile.reaction_pack.states or {}),
            started_ms=initial_ms,
        )
        if self._profile.sprite_enabled:
            self._load_sprite_sheet()

        self._preload_effect_images()
        self._load_image(source_path=self._current_source)
        if not self._restore_position():
            self._place_default()
        self._bind_events()
        self._build_context_menu()
        self.root.deiconify()
        self._keep_topmost()
        self._schedule_animation_tick(initial=True)
        self._watch_after_id: str | None = None
        self._watch_signature = fingerprint_paths(self._character_watch_paths())
        self._watch_pending_signature = None
        self._watch_pending_at = 0.0
        self._schedule_config_watch()

    @staticmethod
    def _decode_image(path: Path) -> Image.Image:
        with Image.open(path) as raw:
            raw.verify()
        with Image.open(path) as raw:
            image = raw.convert("RGBA")
        if image.width < 1 or image.height < 1:
            raise ValueError(f"invalid image dimensions: {path}")
        return image

    def _load_sprite_sheet_for(self, profile: _CharacterProfile) -> Image.Image | None:
        if not profile.sprite_enabled:
            return None
        pack = profile.reaction_pack
        if pack.sprite_sheet is None:
            raise ValueError("sprite pack has no sheet")
        sheet = self._decode_image(pack.sprite_sheet)
        if sheet.size != (pack.columns * pack.cell_width, pack.rows * pack.cell_height):
            raise ValueError("sprite grid mismatch")
        return sheet

    def _effect_images_for(self, profile: _CharacterProfile) -> dict[str, Image.Image]:
        images: dict[str, Image.Image] = {}
        if not profile.effects_enabled:
            return images
        assets = [("twinkle", profile.effects_idle_asset)]
        if profile.click_vfx_enabled:
            assets.append(("sparkle_burst", profile.effects_click_asset))
        for name, path in assets:
            if path is not None:
                images[name] = _key_chroma_background(self._decode_image(path), profile.effects_chroma_key)
        return images

    def _character_watch_paths_for(self, profile: _CharacterProfile) -> set[Path]:
        paths = {resolve_editable_overlay_path("config/overlay.yaml"), _USER_CONFIG_PATH}
        # Keep the configured user-pack candidates in the watch set even when
        # their manifest is currently missing or invalid and resolution fell
        # back to the bundled pack.  Creating/fixing that manifest must trigger
        # a retry, while a half-written file must never replace last-good state.
        for root, value in (
            (USER_CHARACTER_SETS_DIR, profile.set_id),
            (USER_REACTION_PACKS_DIR, profile.reactions_cfg.get("pack", profile.set_id)),
        ):
            pack_id = str(value or "").strip()
            if pack_id and re.fullmatch(r"[A-Za-z0-9_-]+", pack_id):
                paths.add(root / pack_id / "manifest.yaml")
        for path in (profile.default_frame, profile.effects_idle_asset, profile.effects_click_asset):
            if path is not None:
                paths.add(path)
        if profile.set_resolution.base_image is not None:
            paths.add(profile.set_resolution.base_image.parent / "manifest.yaml")
        pack = profile.reaction_pack
        if pack.sprite_sheet is not None:
            paths.add(pack.sprite_sheet)
            paths.add(pack.sprite_sheet.parent / "manifest.yaml")
        return paths

    def _character_watch_paths(self) -> set[Path]:
        return self._character_watch_paths_for(self._profile)

    @staticmethod
    def _validate_yaml_mappings(paths: set[Path]) -> None:
        for path in paths:
            if path.suffix.lower() not in {".yaml", ".yml"} or not path.exists():
                continue
            try:
                parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                raise ValueError(f"invalid YAML: {path}") from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"YAML mapping required: {path}")

    def _schedule_config_watch(self) -> None:
        try:
            self._watch_after_id = self.root.after(500, self._poll_config_watch)
        except tk.TclError:
            self._watch_after_id = None

    def _poll_config_watch(self) -> None:
        self._watch_after_id = None
        try:
            if not self.root.winfo_exists():
                return
            signature = fingerprint_paths(self._character_watch_paths())
            if signature != self._watch_signature:
                now = time.monotonic()
                if signature != self._watch_pending_signature:
                    self._watch_pending_signature = signature
                    self._watch_pending_at = now
                elif now - self._watch_pending_at >= 0.3:
                    # Record this version even if rejected: another completed write
                    # gets a new fingerprint and will be retried without log spam.
                    self._watch_signature = signature
                    self._watch_pending_signature = None
                    self.reload_config()
        except Exception:
            log.exception("[overlay] character config watch failed")
        finally:
            self._schedule_config_watch()

    def cancel_config_watch(self) -> None:
        if self._watch_after_id is not None:
            try:
                self.root.after_cancel(self._watch_after_id)
            except tk.TclError:
                pass
        self._watch_after_id = None

    def reload_config(self) -> bool:
        """Validate all new character inputs, then replace them as one UI-thread update."""
        try:
            cfg = load_cfg(strict=True)
            profile = _CharacterProfile(cfg)
            # Validate both sets.  This prevents a half-written active user
            # manifest from silently falling back to a bundled pack.
            self._validate_yaml_mappings(self._character_watch_paths())
            self._validate_yaml_mappings(self._character_watch_paths_for(profile))
            reactions = cfg.get("overlay", {}).get("character", {}).get("reactions", {})
            if (
                str(cfg.get("overlay", {}).get("character", {}).get("source_mode", "")).lower() == "sprite_grid"
                and isinstance(reactions, dict) and reactions.get("enabled")
                and not profile.sprite_enabled
            ):
                raise ValueError("enabled sprite-grid configuration did not resolve a valid reaction pack")
            sheet = self._load_sprite_sheet_for(profile)
            effects = self._effect_images_for(profile)
            if not profile.sprite_enabled:
                self._decode_image(profile.default_frame)
        except Exception as exc:
            log.warning("[overlay] character reload rejected; keeping last good assets: %s", exc)
            return False

        old_model = self._sprite_model
        now_ms = time.monotonic() * 1000
        model = SpriteStateMachine(set(profile.reaction_pack.states or {}), started_ms=old_model.started_ms)
        model.hovered = old_model.hovered
        model.input_active = old_model.input_active
        model.work_state = model._available(old_model.work_state)
        desired = "input" if model.input_active else (
            old_model.state if old_model.state in model.states else ("hover" if model.hovered else model.work_state)
        )
        model.state = model._available(desired)
        model.epoch = old_model.epoch + 1
        if model.state != old_model.state:
            model.started_ms = now_ms

        old_height = self._img_h
        # A successful settings save is authoritative: retaining the previous
        # static/sequence path here would make source selection appear ignored.
        current_source = profile.default_frame
        self._cfg, self._profile, self._sprite_sheet = cfg, profile, sheet
        self._effect_images = effects
        self._sprite_cache = {}
        self._sprite_choices = {}
        self._sprite_shuffle_orders = {}
        self._sprite_selection_epoch = -1
        self._sprite_model = model
        self._flip_h = bool(cfg.get("overlay", {}).get("flip_horizontal", self._flip_h))
        self._sequence_queue.clear()
        self._click_started_ms = None
        self._load_image(work_size=self._work_size, source_path=current_source)
        self._resize_window_to_image(old_height)
        self._watch_signature = fingerprint_paths(self._character_watch_paths())
        self._watch_pending_signature = None
        self._schedule_animation_in(0)
        log.info("[overlay] character config reloaded")
        return True

    def _preload_effect_images(self) -> None:
        """Load and chroma-key VFX once; unavailable assets simply disable that layer."""
        try:
            self._effect_images = self._effect_images_for(self._profile)
        except Exception:
            self._log_overlay_exception()

    def _load_sprite_sheet(self) -> None:
        """Load the sheet once; invalid dimensions make the pack fall back to static."""
        try:
            self._sprite_sheet = self._load_sprite_sheet_for(self._profile)
        except Exception:
            self._profile.sprite_enabled = False
            self._sprite_sheet = None

    def _state_spec(self, state: str) -> dict:
        states = self._profile.reaction_pack.states or {}
        return states.get(state) or states.get("default") or {"frames": (0,), "selection": "fixed", "transform": "none", "vfx": "none", "frame_ms": 600, "dwell_ms": 600}

    def set_sprite_state(self, state: str, *, transient: bool = False) -> None:
        if not self._profile.sprite_enabled:
            return
        now_ms = time.monotonic() * 1000
        changed = (
            self._sprite_model.show_transient(state, now_ms)
            if transient or state in _SPRITE_LOCKED_STATES
            else self._sprite_model.set_work(state, now_ms)
        )
        if changed:
            self._schedule_animation_in(0)

    def handle_bubble_event(self, event: object) -> None:
        if self._profile.sprite_enabled and self._sprite_model.handle_event(event, time.monotonic() * 1000):
            self._schedule_animation_in(0)

    def set_input_active(self, active: bool) -> None:
        if self._profile.sprite_enabled and self._sprite_model.set_input_active(active, time.monotonic() * 1000):
            self._schedule_animation_in(0)

    def _sprite_image(self, index: int, target_h: int, flip: bool) -> Image.Image:
        p = self._profile.reaction_pack
        crop_offset = p.crop_y_offset_px
        key = (index, target_h, flip, crop_offset)
        if key not in self._sprite_cache:
            cell = crop_sprite(self._sprite_sheet, index, p.columns, p.rows, p.cell_width, p.cell_height)  # type: ignore[arg-type]
            sprite = apply_sprite_crop_y_offset(key_chroma(cell, p.chroma_key), crop_offset)
            width = max(1, round(sprite.width * target_h / sprite.height))
            sprite = sprite.resize((width, target_h), Image.LANCZOS)
            if flip: sprite = sprite.transpose(Image.FLIP_LEFT_RIGHT)
            self._sprite_cache[key] = sprite
        return self._sprite_cache[key]

    def _sprite_layer_image(self, indices: tuple[int, ...], target_h: int, flip: bool) -> Image.Image:
        """Composite declared grid layers on one stable, bottom-aligned canvas."""
        images = tuple(self._sprite_image(index, target_h, flip) for index in indices)
        if len(images) == 1:
            return images[0]
        width = max(image.width for image in images)
        composed = Image.new("RGBA", (width, target_h), (0, 0, 0, 0))
        for image in images:
            composed.alpha_composite(image, ((width - image.width) // 2, target_h - image.height))
        return composed

    def _load_image(self, work_size: tuple[int, int] | None = None, source_path: Path | None = None):
        cfg = self._cfg["overlay"]
        if source_path is not None:
            self._current_source = source_path

        active_source = self._current_source if self._current_source.exists() else RESOURCE_OVERLAY
        if work_size:
            sw, sh = work_size
        else:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
        self._work_size = (sw, sh)

        # 짧은 축 기준 스케일링 (landscape→높이, portrait→너비)
        target_h = target_height_for_work_area(self._work_size, cfg["char_height_ratio"])

        if self._profile.sprite_enabled and self._sprite_sheet is not None:
            spec = self._state_spec(self._sprite_model.state)
            img = self._sprite_image(spec["frames"][0], target_h, self._flip_h)
            target_w = img.width
            self._img_w, self._img_h = target_w, target_h
            self._base_image = img
            self._render_current_image()
            return
        try:
            with Image.open(active_source) as raw:
                img = raw.convert("RGBA")
        except Exception:
            with Image.open(RESOURCE_OVERLAY) as raw:
                img = raw.convert("RGBA")

        scale = target_h / img.height
        target_w = int(img.width * scale)
        img = img.resize((target_w, target_h), Image.LANCZOS)
        if self._flip_h:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        self._img_w, self._img_h = target_w, target_h
        self._base_image = img
        self._render_current_image()

    def _render_current_image(
        self,
        effect: Image.Image | None = None,
        opacity: float = 0.0,
        scale_x: float = 1.0,
        scale_y: float = 1.0,
        offset_y: int = 0,
        offset_x: int = 0,
        effect_thickness_px: int = 1,
    ) -> None:
        img = _render_effect_frame(
            self._base_image,
            effect,
            opacity,
            scale_x,
            scale_y,
            offset_y,
            offset_x,
            effect_thickness_px,
        )

        r, g, b, a = img.split()
        canvas = Image.new("RGB", img.size, (1, 1, 1))
        canvas.paste(Image.merge("RGB", (r, g, b)), mask=a)
        self._photo = ImageTk.PhotoImage(canvas)

        if hasattr(self, "_label") and self._label.winfo_exists():
            self._label.configure(image=self._photo)
            self._label.image = self._photo
            return

        self._label = tk.Label(
            self.root,
            image=self._photo,
            bg=_CHROMA,
            cursor="hand2",
            bd=0,
            highlightthickness=0,
        )
        self._label.pack()

    def _place_default(self):
        cfg = self._cfg["overlay"]
        sh = self.root.winfo_screenheight()
        x = cfg["char_margin_x"]
        y = sh - self._img_h - cfg["char_margin_y"]
        self.root.geometry(f"{self._img_w}x{self._img_h}+{x}+{y}")

    def _restore_position(self) -> bool:
        """Restore a previous drag location, clamped to a currently visible screen."""
        saved = get_overlay_state().get("overlay_window", {})
        if not isinstance(saved, dict):
            return False
        try:
            x, y = int(saved["x"]), int(saved["y"])
        except (KeyError, TypeError, ValueError):
            return False
        x, y = clamp_overlay_position(x, y, self._img_w, self._img_h)
        self.root.geometry(f"{self._img_w}x{self._img_h}+{x}+{y}")
        return True

    def _save_position(self) -> None:
        x, y = self.root.winfo_x(), self.root.winfo_y()
        def update(state: dict) -> None:
            work = bubble_geometry.get_monitor_work_rect(x + self._img_w // 2, y + self._img_h // 2)
            previous = state.get("overlay_window")
            record = dict(previous) if isinstance(previous, dict) else {}
            record.update({"x": int(x), "y": int(y), "work_area": list(work)})
            state["overlay_window"] = record
        update_overlay_state_async(update)

    def hide_for_external_renderer(self) -> None:
        """Hide only the bundled window after a replacement renderer handshake."""
        self._external_rect = self.get_phys_rect()
        self.root.withdraw()

    def restore_bundled_renderer(self) -> None:
        self._external_rect = None
        self.root.deiconify()
        self.root.attributes("-topmost", True)

    def show_launcher(self) -> None:
        """Replace the bundled art with a compact host-owned signal pebble."""
        if self._launcher_canvas is not None:
            saved = (get_overlay_state().get("launcher_window") or {})
            x, y = int(saved.get("x", self.root.winfo_x())), int(saved.get("y", self.root.winfo_y()))
            work = bubble_geometry.get_monitor_work_rect(x + 26, y + 26)
            x, y = bubble_geometry.clamp_rect(x, y, 52, 52, work)
            self.root.geometry(f"52x52+{x}+{y}")
            return
        self._full_rect = self.get_phys_rect()
        canvas = tk.Canvas(self.root, width=52, height=52, bg=_CHROMA, highlightthickness=0, bd=0)
        # Keep this entirely canvas-native: it stays sharp on high-DPI screens
        # and does not add a separate launcher bitmap to package or reload.
        canvas.create_oval(5, 7, 49, 51, fill="#241642", outline="", tags="launcher-shadow")
        ring = canvas.create_oval(3, 3, 49, 49, fill="#5b3db7", outline="#ffffff", width=2, tags="launcher-face")
        canvas.create_oval(14, 16, 39, 34, fill="#ffffff", outline="", tags="launcher-glyph")
        canvas.create_polygon(19, 31, 18, 38, 25, 33, fill="#ffffff", outline="", tags="launcher-glyph")
        for dot_x in (21, 26, 31):
            canvas.create_oval(dot_x, 23, dot_x + 3, 26, fill="#5b3db7", outline="", tags="launcher-glyph")
        canvas.configure(takefocus=True)
        canvas.bind("<ButtonPress-1>", self._on_launcher_press)
        canvas.bind("<B1-Motion>", self._on_launcher_drag)
        canvas.bind("<ButtonRelease-1>", self._on_launcher_release)
        canvas.bind("<Button-3>", self._on_launcher_context_menu)
        canvas.bind("<Return>", self._activate_launcher)
        canvas.bind("<space>", self._activate_launcher)
        canvas.bind("<FocusIn>", lambda _event: canvas.itemconfigure(ring, outline="#ffff00", width=4))
        canvas.bind("<FocusOut>", lambda _event: canvas.itemconfigure(ring, outline="#ffffff", width=3))
        canvas.bind("<Enter>", lambda _event: (canvas.configure(cursor="hand2"), self._set_launcher_hover(True), self._show_launcher_tooltip()))
        canvas.bind("<Leave>", lambda _event: (canvas.configure(cursor=""), self._set_launcher_hover(False), self._hide_launcher_tooltip()))
        self._label.pack_forget()
        canvas.pack()
        self._launcher_canvas = canvas
        saved = (get_overlay_state().get("launcher_window") or {})
        x, y = int(saved.get("x", self._full_rect[0])), int(saved.get("y", self._full_rect[1]))
        work = bubble_geometry.get_monitor_work_rect(x, y)
        x, y = bubble_geometry.clamp_rect(x, y, 52, 52, work)
        self.root.geometry(f"52x52+{x}+{y}")
        update_overlay_state_async(lambda state: state.update({"launcher_window": {"x": x, "y": y, "width": 52, "height": 52}}))

    def show_full(self) -> None:
        if self._launcher_canvas is None:
            return
        self._launcher_canvas.destroy()
        self._launcher_canvas = None
        self._hide_launcher_tooltip()
        self._label.pack()
        if self._full_rect is not None:
            x, y, w, h = self.launcher_full_target()
            work = bubble_geometry.get_monitor_work_rect(x + w // 2, y + h // 2)
            self.root.geometry(f"{w}x{h}+{x}+{y}")
            def update(state: dict) -> None:
                previous = state.get("overlay_window")
                record = dict(previous) if isinstance(previous, dict) else {}
                record.update({"x": x, "y": y, "width": w, "height": h, "work_area": list(work)})
                state["overlay_window"] = record
            update_overlay_state(update)

    def _activate_launcher(self, _event=None):
        callback = self.on_expand or self.on_activate
        if callback is not None:
            callback()
        return "break"

    def _on_launcher_press(self, event) -> None:
        self._launcher_press = (event.x_root, event.y_root)
        self._launcher_moved = False

    def _on_launcher_drag(self, event) -> None:
        if self._launcher_press is None:
            return
        old_x, old_y = self._launcher_press
        dx, dy = event.x_root - old_x, event.y_root - old_y
        if abs(dx) > 4 or abs(dy) > 4:
            self._launcher_moved = True
        x, y = self.root.winfo_x() + dx, self.root.winfo_y() + dy
        self.root.geometry(f"+{x}+{y}")
        self._launcher_press = (event.x_root, event.y_root)

    def _on_launcher_release(self, _event) -> None:
        if not self._launcher_moved:
            self._activate_launcher()
            return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        work = bubble_geometry.get_monitor_work_rect(x + 26, y + 26)
        x, y = bubble_geometry.clamp_rect(x, y, 52, 52, work)
        self.root.geometry(f"52x52+{x}+{y}")
        # Launcher motion must not resize/reload the full artwork or replace
        # the full-window position retained for the next expansion.
        update_overlay_state_async(lambda state: state.update({"launcher_window": {"x": x, "y": y, "width": 52, "height": 52, "work_area": list(work)}}))
        self._launcher_press = None

    def _on_launcher_context_menu(self, event):
        self._hide_launcher_tooltip()
        self.external_context_menu(int(event.x_root), int(event.y_root))
        return "break"

    def _show_launcher_tooltip(self) -> None:
        if self._launcher_tooltip is not None or self._launcher_canvas is None:
            return
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text="Engram 열기", bg="#111827", fg="#ffffff", padx=6, pady=3).pack()
        tip.update_idletasks()
        width, height = tip.winfo_reqwidth(), tip.winfo_reqheight()
        lx, ly = self.root.winfo_x(), self.root.winfo_y()
        work = bubble_geometry.get_monitor_work_rect(lx + 26, ly + 26)
        x, y = launcher_tooltip_position(lx, ly, width, height, work)
        tip.geometry(f"{width}x{height}+{x}+{y}")
        self._launcher_tooltip = tip

    def _set_launcher_hover(self, hovered: bool) -> None:
        canvas = self._launcher_canvas
        if canvas is None:
            return
        canvas.itemconfigure("launcher-face", fill="#7659cf" if hovered else "#5b3db7")
        # A one-pixel lift reads as a button response without changing the
        # host window rectangle used by drag/persistence.
        offset = -1 if hovered else 1
        canvas.move("launcher-face", 0, offset)
        canvas.move("launcher-glyph", 0, offset)

    def _hide_launcher_tooltip(self) -> None:
        if getattr(self, "_launcher_tooltip", None) is not None:
            self._launcher_tooltip.destroy()
            self._launcher_tooltip = None

    def get_presentation_rect(self, mode: str) -> tuple[int, int, int, int]:
        """Return the independently persisted target rectangle for a presentation."""
        saved = get_overlay_state()
        if mode == "launcher":
            record = saved.get("launcher_window") or {}
            fallback = self.get_phys_rect()
            return (int(record.get("x", fallback[0])), int(record.get("y", fallback[1])),
                    int(record.get("width", 52)), int(record.get("height", 52)))
        record = saved.get("overlay_window") or {}
        fallback = self.get_bundled_phys_rect()
        return (int(record.get("x", fallback[0])), int(record.get("y", fallback[1])),
                int(record.get("width", fallback[2])), int(record.get("height", fallback[3])))

    def launcher_full_target(self, external_size: tuple[int, int] | None = None) -> tuple[int, int, int, int]:
        """Fresh full target from launcher plus the user's preferred drag offset.

        First capable-renderer launch has no acknowledged rect, so bundled size
        is the safe fallback until the renderer reports its actual geometry.
        """
        lx, ly = self.root.winfo_x(), self.root.winfo_y()
        if external_size is not None:
            width, height = max(1, int(external_size[0])), max(1, int(external_size[1]))
        else:
            try:
                _x, _y, width, height = self.get_bundled_phys_rect()
            except AttributeError:
                _x, _y, width, height = self._full_rect
        offset_x, offset_y = self._saved_launcher_offset()
        preferred_x = lx + 26 - width // 2 + offset_x
        preferred_y = ly + 26 - height + offset_y
        # The preferred full layout may intentionally live on a different
        # monitor from its launcher. Resolve/clamp against that full rectangle,
        # while get_monitor_work_rect retains nearest-monitor fallback if the
        # previously used monitor disappeared.
        work = bubble_geometry.get_monitor_work_rect(
            preferred_x + width // 2,
            preferred_y + height // 2,
        )
        x, y = bubble_geometry.clamp_rect(
            preferred_x,
            preferred_y,
            width,
            height,
            work,
        )
        return x, y, width, height

    def _saved_launcher_offset(self) -> tuple[int, int]:
        state = get_overlay_state()
        record = state.get("overlay_window") if isinstance(state, dict) else None
        if not isinstance(record, dict):
            return 0, 0
        offset_x = record.get("launcher_offset_x")
        offset_y = record.get("launcher_offset_y")
        if (
            not isinstance(offset_x, int) or isinstance(offset_x, bool)
            or not isinstance(offset_y, int) or isinstance(offset_y, bool)
            or not -1_000_000 <= offset_x <= 1_000_000
            or not -1_000_000 <= offset_y <= 1_000_000
        ):
            return 0, 0
        return offset_x, offset_y

    def remember_launcher_relative_offset(self, x: int, y: int, width: int, height: int) -> tuple[int, int]:
        """Persist full placement relative to launcher without moving launcher."""
        anchor = getattr(self, "_launcher_expand_anchor", None)
        if anchor is None:
            state = get_overlay_state()
            launcher = state.get("launcher_window") if isinstance(state, dict) else None
            try:
                anchor = (int(launcher["x"]), int(launcher["y"]))
            except (KeyError, TypeError, ValueError):
                # First-run full drag with no launcher state defines the
                # current placement as nominal rather than inventing an offset.
                anchor = (
                    int(x) + int(width) // 2 - 26,
                    int(y) + int(height) - 26,
                )
        lx, ly = anchor
        nominal_x = lx + 26 - int(width) // 2
        nominal_y = ly + 26 - int(height)
        offset_x = max(-1_000_000, min(1_000_000, int(x) - nominal_x))
        offset_y = max(-1_000_000, min(1_000_000, int(y) - nominal_y))

        def update(state: dict) -> None:
            previous = state.get("overlay_window")
            record = dict(previous) if isinstance(previous, dict) else {}
            record.update({"launcher_offset_x": offset_x, "launcher_offset_y": offset_y})
            state["overlay_window"] = record

        update_overlay_state_async(update)
        return offset_x, offset_y

    def capture_launcher_expand_anchor(self) -> tuple[int, int]:
        """Remember the physical launcher position before a clamped full open."""
        self._launcher_expand_anchor = (int(self.root.winfo_x()), int(self.root.winfo_y()))
        return self._launcher_expand_anchor

    def snapshot_launcher_anchor(self) -> tuple[int, int]:
        """Persist the independent launcher anchor, never derive it from a full drag."""
        if getattr(self, "_launcher_expand_anchor", None) is not None:
            lx, ly = self._launcher_expand_anchor
            work = bubble_geometry.get_monitor_work_rect(lx + 26, ly + 26)
            lx, ly = bubble_geometry.clamp_rect(lx, ly, 52, 52, work)
        else:
            saved = get_overlay_state()
            record = saved.get("launcher_window") if isinstance(saved, dict) else None
            try:
                lx, ly = int(record["x"]), int(record["y"])
            except (KeyError, TypeError, ValueError):
                # First-run migration only: without a launcher record or an
                # expand capture, seed it once from the current full window.
                x, y, width, height = self.get_phys_rect()
                work = bubble_geometry.get_monitor_work_rect(x + width // 2, y + height // 2)
                lx, ly = bubble_geometry.clamp_rect(
                    x + width // 2 - 26, y + height - 26, 52, 52, work
                )
            else:
                work = bubble_geometry.get_monitor_work_rect(lx + 26, ly + 26)
                lx, ly = bubble_geometry.clamp_rect(lx, ly, 52, 52, work)
        update_overlay_state_async(lambda state: state.update({"launcher_window": {"x": lx, "y": ly, "width": 52, "height": 52, "work_area": list(work)}}))
        self._launcher_expand_anchor = None
        return lx, ly

    def apply_external_geometry(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        *,
        preserve_position: bool = False,
        persist_position: bool = True,
        presentation_mode: str = "full",
    ) -> tuple[int, int, int, int]:
        """Sync replacement geometry while keeping persisted startup x/y authoritative.

        The renderer's very first geometry is its own window bootstrap, not a
        user move. In that one case Engram adopts its dimensions but retains
        the bundled/persisted position. During drag_move, persist_position is
        false so only the in-memory anchor changes; drag_end writes the final
        position once.
        """
        width, height = max(1, int(width)), max(1, int(height))
        if preserve_position and self._external_rect is not None:
            x, y = self._external_rect[:2]
        x, y = clamp_overlay_position(int(x), int(y), width, height)
        self._external_rect = (x, y, width, height)
        if preserve_position or not persist_position:
            return self._external_rect
        work = bubble_geometry.get_monitor_work_rect(x + width // 2, y + height // 2)

        def update(state: dict) -> None:
            key = "launcher_window" if presentation_mode == "launcher" else "overlay_window"
            previous = state.get(key)
            record = dict(previous) if isinstance(previous, dict) else {}
            record.update({"x": x, "y": y, "width": width, "height": height, "work_area": list(work)})
            state[key] = record

        update_overlay_state_async(update)
        return self._external_rect

    def external_activate(self) -> None:
        self._invoke_activate()

    def external_context_menu(self, x: int, y: int) -> None:
        """Render the complete existing Tk menu model in a nonmodal host surface."""
        self._dismiss_context_menu()
        try:
            # The host canvas survives while withdrawn, so the menu must not
            # retain a full-overlay action when the launcher is later shown.
            self._build_context_menu()
            self._flip_var.set(self._flip_h)
            self._rebuild_provider_menu()
            menu = tk.Toplevel(self.root)
            menu.overrideredirect(True)
            menu.attributes("-topmost", True)
            self._context_menu_open = True
            self._external_context_surface = menu
            holder = tk.Frame(menu, bg="#252a35", bd=1, relief="solid")
            holder.pack(fill="both", expand=True)

            def render(model, trail=()):
                for child in holder.winfo_children():
                    child.destroy()
                if trail:
                    tk.Button(holder, text="‹ 뒤로", command=lambda: render(trail[-1], trail[:-1]), anchor="w").pack(fill="x")
                end = model.index("end")
                for index in range(0 if end is None else end + 1):
                    kind = model.type(index)
                    if kind == "separator":
                        tk.Frame(holder, height=1, bg="#4b5568").pack(fill="x", pady=3); continue
                    label = model.entrycget(index, "label")
                    state = model.entrycget(index, "state")
                    if kind in {"checkbutton", "radiobutton"}:
                        variable = model.entrycget(index, "variable")
                        try:
                            value = model.entrycget(index, "value") if kind == "radiobutton" else ""
                            onvalue = model.entrycget(index, "onvalue") if kind == "checkbutton" else "1"
                            selected = _menu_entry_is_selected(
                                kind,
                                self.root.getvar(variable),
                                value=value,
                                onvalue=onvalue,
                            )
                        except Exception:
                            selected = False
                        label = ("✓ " if selected else "   ") + label
                    if kind == "cascade":
                        path = model.entrycget(index, "menu")
                        child = model.nametowidget(path)
                        command = lambda m=child, t=trail + (model,): render(m, t)
                        label += "  ›"
                    else:
                        command = lambda m=model, i=index: (self._dismiss_context_menu(), m.invoke(i))
                    tk.Button(holder, text=label, command=command, state=state, anchor="w", relief="flat",
                              bg="#252a35", fg="#edf1f7", activebackground="#3a4354",
                              activeforeground="#ffffff", padx=12).pack(fill="x")

            render(self._context_menu)
            menu.bind("<Escape>", lambda _event: self._dismiss_context_menu())
            def focus_out(_event):
                menu.after_idle(lambda: self._dismiss_context_menu()
                                if not _is_widget_descendant(menu.focus_displayof(), menu) else None)
            menu.bind("<FocusOut>", focus_out)
            menu.update_idletasks()
            work = bubble_geometry.get_monitor_work_rect(int(x), int(y))
            width, height, px, py = _clamp_menu_geometry(x, y, menu.winfo_reqwidth(), menu.winfo_reqheight(), work)
            menu.geometry(f"{width}x{height}+{px}+{py}")
            menu.focus_force()
            self._start_context_menu_outside_poll(menu, (px, py, px + width, py + height))
        except Exception:
            self._dismiss_context_menu()

    @staticmethod
    def _left_button_pressed() -> bool:
        """Read local button state without installing a system-wide observer."""
        try:
            return bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
        except Exception:
            return False

    def _start_context_menu_outside_poll(self, surface, rect) -> None:
        """Dismiss on the first outside left-button edge using Tk-thread polling."""
        generation = int(getattr(self, "_context_menu_poll_generation", 0)) + 1
        self._context_menu_poll_generation = generation
        self._context_menu_left_down = self._left_button_pressed()

        def poll() -> None:
            if generation != getattr(self, "_context_menu_poll_generation", None):
                return
            if not self._context_menu_open or getattr(self, "_external_context_surface", None) is not surface:
                return
            down = self._left_button_pressed()
            previous = bool(getattr(self, "_context_menu_left_down", False))
            self._context_menu_left_down = down
            if down and not previous and _point_outside_rect(self._pointer_screen_position() or rect[:2], rect):
                self._dismiss_context_menu()
                return
            try:
                self._context_menu_poll_after = self.root.after(20, poll)
            except Exception:
                self._dismiss_context_menu()

        self._context_menu_poll_after = self.root.after(20, poll)

    def _bind_events(self):
        self._press_x = 0
        self._press_y = 0
        self._moved = False

        self._label.bind("<ButtonPress-1>", self._on_press)
        self._label.bind("<B1-Motion>", self._on_drag)
        self._label.bind("<ButtonRelease-1>", self._on_release)
        self._label.bind("<Button-3>", self._on_context_menu_event)
        self._label.bind("<Enter>", lambda _event: self._set_hovered(True))
        self._label.bind("<Leave>", lambda _event: self._set_hovered(False))
        self.root.bind("<Button-3>", self._on_context_menu_event)

    def _on_context_menu_event(self, event):
        self._emit_pointer_event("right_click", {"screen_x": event.x_root, "screen_y": event.y_root})
        self._show_context_menu(event)
        # label/root 이중 바인딩 이벤트 전파를 막아 메뉴 중복 post를 방지한다.
        return "break"

    def _build_context_menu(self):
        self._provider_var = tk.StringVar(value=self._get_provider_value())
        self._claude_model_var = tk.StringVar()
        self._ollama_model_var = tk.StringVar()
        self._flip_var = tk.BooleanVar(value=self._flip_h)

        self._context_menu = tk.Menu(self.root, tearoff=0)
        self._context_menu.add_command(label="채팅 열기/닫기", command=self._invoke_activate)
        self._context_menu.add_command(label="대화 기록 보기", command=self._invoke_history)

        self._provider_menu = tk.Menu(self._context_menu, tearoff=0)
        self._claude_submenu = tk.Menu(self._provider_menu, tearoff=0)
        self._ollama_submenu = tk.Menu(self._provider_menu, tearoff=0)

        self._context_menu.add_cascade(label="CLI 공급자", menu=self._provider_menu)
        self._context_menu.add_checkbutton(label="좌우 반전", variable=self._flip_var, command=self._toggle_flip)
        self._context_menu.add_separator()
        self._context_menu.add_command(label="설정", command=self._invoke_settings)
        try:
            launcher_mode = bool(self.is_launcher_mode and self.is_launcher_mode())
        except Exception:
            launcher_mode = False
        action = presentation_menu_action(
            launcher_mode,
            can_collapse=self.on_collapse is not None,
            can_hide_to_tray=self.on_hide_launcher is not None,
        )
        if action == ("런처로 접기", "collapse"):
            self._context_menu.add_command(label=action[0], command=self.on_collapse)
        elif action == ("트레이로 숨기기", "hide_to_tray"):
            self._context_menu.add_command(label=action[0], command=self._invoke_hide_launcher)
        self._context_menu.add_command(label="재시작", command=self._invoke_restart)

    def _get_ollama_model_value(self) -> str:
        if self.on_get_ollama_model is None:
            return ""
        try:
            return str(self.on_get_ollama_model() or "")
        except Exception:
            return ""

    def _get_ollama_models_value(self) -> list:
        if self.on_get_ollama_models is None:
            return []
        try:
            return list(self.on_get_ollama_models() or [])
        except Exception:
            return []

    @staticmethod
    def _is_ollama_routing(model: str) -> bool:
        m = (model or "").lower().strip()
        claude_aliases = {"default", "best", "sonnet", "opus", "haiku", "opusplan", "sonnet[1m]", "opus[1m]"}
        return bool(m) and not m.startswith("claude-") and m not in claude_aliases

    def _rebuild_provider_menu(self):
        """팝업 직전에 현재 상태를 반영해 provider 서브메뉴를 재구성한다."""
        self._provider_menu.delete(0, "end")
        self._claude_submenu.delete(0, "end")
        self._ollama_submenu.delete(0, "end")

        current_provider = self._get_provider_value()
        current_model = self._get_ollama_model_value()
        models = self._get_ollama_models_value()

        # ── Provider model submenus ────────────────────────────────
        self._provider_var.set(current_provider)
        from overlay.cli_capabilities import models as provider_models
        cfg_cli = (load_cfg().get("cli") or {})
        for provider, label in (("copilot", "Copilot CLI"), ("antigravity", "Antigravity"), ("codex", "Codex CLI")):
            submenu = tk.Menu(self._provider_menu, tearoff=0)
            selected = str(cfg_cli.get({"copilot":"copilot_model", "antigravity":"antigravity_model", "codex":"codex_model"}[provider]) or "")
            choices = provider_models(provider, cfg_cli, models)
            if choices:
                var = tk.StringVar(value=selected if current_provider == provider else "")
                for model in choices:
                    submenu.add_radiobutton(label=model, value=model, variable=var, command=lambda p=provider, m=model: self._select_provider_model(p, m))
            else:
                submenu.add_command(label="기본값", command=lambda p=provider: self._select_provider_model(p, None))
            self._provider_menu.add_cascade(label=f"{'✓' if current_provider == provider else ' '} {label}", menu=submenu)
        self._provider_menu.add_separator()

        # ── Claude Code 서브메뉴 ────────────────────────────────────
        if current_provider in {"claude-code", "claude-code-ollama"}:
            direct_model = str(cfg_cli.get("claude_model") or "direct")
            self._claude_model_var.set(current_model if current_provider == "claude-code-ollama" else direct_model)
        else:
            self._claude_model_var.set("")

        self._claude_submenu.add_checkbutton(
            label="claude (직접)",
            onvalue="direct",
            offvalue="",
            variable=self._claude_model_var,
            command=lambda: self._select_provider_model("claude-code", ""),
        )
        for alias in provider_models("claude-code", cfg_cli, models):
            self._claude_submenu.add_checkbutton(
                label=f"claude: {alias}",
                onvalue=alias,
                offvalue="",
                variable=self._claude_model_var,
                command=lambda mod=alias: self._select_provider_model("claude-code", mod),
            )
        self._claude_submenu.add_separator()
        if models:
            for _m in models:
                self._claude_submenu.add_checkbutton(
                    label=f"ollama: {_m}",
                    onvalue=_m,
                    offvalue="",
                    variable=self._claude_model_var,
                    command=lambda mod=_m: self._select_provider_model("claude-code-ollama", mod),
                )
        else:
            self._claude_submenu.add_command(label="(Ollama 모델 없음)", state="disabled")
        self._claude_submenu.add_separator()
        self._claude_submenu.add_command(label="Ollama 새로고침", command=self._invoke_reload_ollama_models)

        claude_label = f"{'✓' if current_provider in {'claude-code', 'claude-code-ollama'} else ' '} Claude Code"
        self._provider_menu.add_cascade(label=claude_label, menu=self._claude_submenu)
        self._provider_menu.add_separator()

        # ── Ollama 서브메뉴 ─────────────────────────────────────────
        if current_provider == "ollama":
            self._ollama_model_var.set(current_model)
        else:
            self._ollama_model_var.set("")

        if models:
            for _m in models:
                self._ollama_submenu.add_checkbutton(
                    label=_m,
                    onvalue=_m,
                    offvalue="",
                    variable=self._ollama_model_var,
                    command=lambda mod=_m: self._select_provider_model("ollama", mod),
                )
        else:
            self._ollama_submenu.add_command(label="(Ollama 모델 없음)", state="disabled")
        self._ollama_submenu.add_separator()
        self._ollama_submenu.add_command(label="새로고침", command=self._invoke_reload_ollama_models)

        ollama_label = f"{'✓' if current_provider == 'ollama' else ' '} Ollama"
        self._provider_menu.add_cascade(label=ollama_label, menu=self._ollama_submenu)

    def _toggle_flip(self):
        self.set_flip(not self._flip_h)

    def set_flip(self, value: bool):
        """좌우 반전 상태를 갱신하고(영속화 포함) 즉시 재렌더링한다."""
        value = bool(value)
        if value == self._flip_h:
            return
        self._flip_h = value
        set_flip_horizontal(value)
        self._set_frame(self._current_source)

    def _show_context_menu(self, event):
        self.external_context_menu(int(event.x_root), int(event.y_root))

    @staticmethod
    def _pointer_screen_position() -> "tuple[int, int] | None":
        try:
            point = wintypes.POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                return int(point.x), int(point.y)
        except Exception:
            pass
        return None

    def _dismiss_context_menu(self):
        log.debug("[overlay] context menu dismiss requested")
        surface = getattr(self, "_external_context_surface", None)
        self._context_menu_poll_generation = int(getattr(self, "_context_menu_poll_generation", 0)) + 1
        after_id = getattr(self, "_context_menu_poll_after", None)
        self._context_menu_poll_after = None
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._external_context_surface = None
        if surface is not None:
            try:
                if surface.winfo_exists():
                    surface.destroy()
            except Exception:
                pass
        def _close_once():
            for menu in (getattr(self, "_provider_menu", None), getattr(self, "_context_menu", None)):
                if menu is None:
                    continue
                try:
                    menu.unpost()
                except Exception:
                    pass
                try:
                    menu.grab_release()
                except Exception:
                    pass

        _close_once()
        # 서브메뉴 command 콜백 중에는 즉시 unpost가 누락될 수 있어 지연 닫기를 추가한다.
        try:
            self.root.after(0, _close_once)
            self.root.after(25, _close_once)
        except Exception:
            pass
        self._context_menu_open = False
        try:
            self.root.attributes("-topmost", True)
            self.root.lift()
        except Exception:
            pass

    def _get_provider_value(self) -> str:
        if self.on_get_provider is None:
            return "copilot"
        try:
            return str(self.on_get_provider())
        except Exception:
            self._log_overlay_exception()
            return "copilot"

    def _select_provider_model(self, provider: str, model: "str | None"):
        self._dismiss_context_menu()
        if model and _SMALL_MODEL_RE.search(model):
            import tkinter.messagebox as mb

            proceed = mb.askyesno(
                "\u26a0\ufe0f 소형 모델 경고",
                f"'{model}'은(는) 소형 모델입니다.\n\n"
                "engram의 복잡한 시스템 프롬프트(정체성 + 페르소나 + 지침 + 기억)를\n"
                "처리하기에 파라미터가 부족해 지시 무시, 도구 호출 실패 등이\n"
                "발생할 수 있습니다. (권장: 7B 이상)\n\n"
                "계속 진행하시겠습니까?",
                parent=self.root,
            )
            if not proceed:
                return
        self._provider_var.set(provider)
        if self.on_set_provider_model is not None:
            try:
                self.on_set_provider_model(provider, model)
            except Exception:
                self._log_overlay_exception()
        elif self.on_set_provider is not None:
            try:
                self.on_set_provider(provider)
            except Exception:
                self._log_overlay_exception()

    def _select_provider(self, provider: str):
        """하위 호환 — on_set_provider_model 없을 때 fallback."""
        self._select_provider_model(provider, None)

    def _invoke_activate(self):
        self._dismiss_context_menu()
        try:
            self.on_activate()
        except Exception:
            self._log_overlay_exception()

    def _invoke_settings(self):
        self._dismiss_context_menu()
        if self.on_settings is None:
            return
        try:
            self.on_settings()
        except Exception:
            self._log_overlay_exception()

    def _invoke_history(self):
        self._dismiss_context_menu()
        if self.on_history is None:
            return
        try:
            self.on_history()
        except Exception:
            self._log_overlay_exception()

    def _invoke_restart(self):
        self._dismiss_context_menu()
        if self.on_restart is None:
            return
        try:
            self.on_restart()
        except Exception:
            self._log_overlay_exception()

    def _invoke_hide_launcher(self):
        self._dismiss_context_menu()
        if self.on_hide_launcher is None:
            return
        try:
            self.on_hide_launcher()
        except Exception:
            self._log_overlay_exception()

    def _invoke_quit(self):
        self._dismiss_context_menu()
        if self.on_quit is None:
            return
        try:
            self.on_quit()
        except Exception:
            self._log_overlay_exception()

    def _invoke_reload_ollama_models(self):
        self._dismiss_context_menu()
        if self.on_reload_ollama_models is not None:
            try:
                self.on_reload_ollama_models()
            except Exception:
                self._log_overlay_exception()

    def _log_overlay_exception(self):
        import logging
        import traceback

        logging.basicConfig(filename=str(Path.home() / ".engram" / "overlay_error.log"), level=logging.ERROR)
        logging.error(traceback.format_exc())

    def _on_press(self, event):
        self._press_x = event.x_root
        self._press_y = event.y_root
        self._moved = False

    def _set_hovered(self, value: bool) -> None:
        self._emit_pointer_event("pointer_enter" if value else "pointer_leave", {})
        if self._profile.sprite_enabled and self._sprite_model.set_hovered(value, time.monotonic() * 1000):
            self._schedule_animation_in(0)

    def _on_drag(self, event):
        dx = event.x_root - self._press_x
        dy = event.y_root - self._press_y
        if abs(dx) > 4 or abs(dy) > 4:
            self._moved = True
        x = self.root.winfo_x() + dx
        y = self.root.winfo_y() + dy
        self._press_x = event.x_root
        self._press_y = event.y_root
        self.root.geometry(f"+{x}+{y}")
        self._emit_pointer_event("drag_move", {"screen_x": x, "screen_y": y})

    def _emit_pointer_event(self, action: str, payload: dict) -> None:
        if self.on_pointer_event is None:
            return
        try:
            self.on_pointer_event(action, payload)
        except Exception:
            log.debug("[overlay] pointer event callback skipped", exc_info=True)

    def _keep_topmost(self):
        """주기적으로 창을 맨 위로 올려 작업표시줄 등에 가리지 않게 유지."""
        if not self._context_menu_open:
            self.root.lift()
            self.root.attributes("-topmost", True)
        self.root.after(500, self._keep_topmost)

    def _set_frame(self, source_path: Path):
        self._reload_image_for_current_monitor(source_path=source_path)

    def _schedule_animation_tick(self, initial: bool = False):
        delay = 200 if initial else max(100, int(self._profile.idle_check_interval_sec * 1000))
        self._schedule_animation_in(delay)

    def _schedule_animation_in(self, delay_ms: int) -> None:
        if self._animation_after_id is not None:
            try:
                self.root.after_cancel(self._animation_after_id)
            except tk.TclError:
                return
        try:
            self._animation_after_id = self.root.after(max(0, delay_ms), self._animation_tick)
        except tk.TclError:
            self._animation_after_id = None

    def _render_idle_effect(self, now_ms: float) -> None:
        interval = self._profile.effects_idle_interval_ms
        phase = (now_ms % interval) / interval
        pulse = (1.0 - abs(2.0 * phase - 1.0))
        legacy_motion = bool(getattr(self._profile, "legacy_body_motion", False))
        bob = round(-2 * pulse) if legacy_motion else 0
        self._render_current_image(
            effect=self._effect_images.get("twinkle"),
            opacity=0.18 + 0.26 * pulse,
            scale_x=1.0 - 0.008 * pulse if legacy_motion else 1.0,
            scale_y=1.0 + 0.012 * pulse if legacy_motion else 1.0,
            offset_y=bob,
            effect_thickness_px=self._profile.effects_idle_thickness_px,
        )

    def _render_click_effect(self, progress: float) -> None:
        pulse = 1.0 - abs(2.0 * progress - 1.0)
        legacy_motion = bool(getattr(self._profile, "legacy_body_motion", False))
        shake = round(2 * (1.0 - progress) * (1 if int(progress * 20) % 2 else -1)) if legacy_motion else 0
        self._render_current_image(
            effect=self._effect_images.get("sparkle_burst"),
            opacity=min(1.0, 0.25 + 0.9 * pulse),
            scale_x=1.0 - 0.055 * pulse if legacy_motion else 1.0,
            scale_y=1.0 + 0.055 * pulse if legacy_motion else 1.0,
            offset_x=shake,
            offset_y=round(-2 * pulse) if legacy_motion else 0,
            effect_thickness_px=self._profile.effects_click_thickness_px,
        )

    def _start_click_action(self) -> None:
        if self._profile.sprite_enabled:
            self.set_sprite_state("click")
            return
        if not self._profile.click_vfx_enabled or "sparkle_burst" not in self._effect_images:
            return
        # 클릭은 번호 프레임을 포함한 어떤 idle보다 항상 우선이며 재클릭은 0프레임부터다.
        self._sequence_queue.clear()
        self._click_started_ms = time.monotonic() * 1000
        if self._current_source != self._profile.default_frame:
            self._set_frame(self._profile.default_frame)
        self._schedule_animation_in(0)

    def _animation_tick(self):
        self._animation_after_id = None
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return

        now_ms = time.monotonic() * 1000
        if self._profile.sprite_enabled and self._sprite_sheet is not None:
            spec = self._state_spec(self._sprite_model.state)
            elapsed = now_ms - self._sprite_model.started_ms
            dwell = max(1, int(spec.get("dwell_ms", spec["frame_ms"])))
            if self._sprite_model.expire(now_ms, dwell):
                spec = self._state_spec(self._sprite_model.state)
                elapsed = 0
            if self._sprite_selection_epoch != self._sprite_model.epoch:
                self._sprite_choices.clear()
                self._sprite_shuffle_orders.clear()
                self._sprite_selection_epoch = self._sprite_model.epoch
            timeline_enabled = any(key in spec for key in ("durations_ms", "loop", "hold_ms", "layers"))
            if timeline_enabled:
                position = resolve_layered_timeline(
                    spec, elapsed, seed=f"{self._sprite_model.state}:{self._sprite_model.epoch}"
                )
                indices = position.frames
                bucket = position.frame_index
            else:
                index, bucket = select_sprite_frame(
                    spec,
                    self._sprite_model.state,
                    self._sprite_model.epoch,
                    elapsed,
                    self._sprite_choices,
                    self._sprite_rng,
                    self._sprite_shuffle_orders,
                )
                indices = (index,)
            if len(self._sprite_choices) > 128:
                self._sprite_choices.clear()
            if len(self._sprite_shuffle_orders) > 8:
                current_cycle = bucket // len(spec["frames"])
                self._sprite_shuffle_orders = {
                    key: order
                    for key, order in self._sprite_shuffle_orders.items()
                    if key[0] == self._sprite_model.state
                    and key[1] == self._sprite_model.epoch
                    and key[2] >= current_cycle - 1
                }
            transform = spec.get("transform", "none")
            flip_key = (f"flip:{self._sprite_model.state}", self._sprite_model.epoch, bucket)
            idle_flip = False
            if transform == "breathe_mirror":
                if flip_key not in self._sprite_choices:
                    self._sprite_choices[flip_key] = random.choice((0, 1))
                idle_flip = bool(self._sprite_choices[flip_key])
            hover_flip = transform == "hflip_squash" and bucket % 2 == 1
            flip = self._flip_h ^ idle_flip ^ hover_flip
            target_h = target_height_for_work_area(self._work_size, self._cfg["overlay"]["char_height_ratio"])
            old_width, old_height = self._img_w, self._img_h
            self._base_image = self._sprite_layer_image(indices, target_h, flip)
            self._img_w, self._img_h = self._base_image.size
            phase = (elapsed % max(1, spec["frame_ms"])) / max(1, spec["frame_ms"])
            sx, sy = (1.0, 1.0)
            pulse = 1.0 - abs(2.0 * phase - 1.0)
            if transform == "breathe_mirror":
                sx, sy = (0.98 + 0.02 * pulse, 1.02 - 0.02 * pulse)
            elif transform == "hflip_squash":
                sx, sy = (1.0 + 0.025 * pulse, 1.0 - 0.06 * pulse)
            wants_click_vfx = spec.get("vfx") == "sparkle_burst"
            effect = self._effect_images.get(spec.get("vfx", "none")) if (not wants_click_vfx or self._profile.click_vfx_enabled) else None
            thickness = self._profile.effects_click_thickness_px if spec.get("vfx") == "sparkle_burst" else self._profile.effects_idle_thickness_px
            self._render_current_image(effect=effect, opacity=.55 if effect else 0, scale_x=sx, scale_y=sy, effect_thickness_px=thickness)
            if (old_width, old_height) != (self._img_w, self._img_h):
                self._resize_window_to_image(old_height)
            self._schedule_animation_in(min(80, max(30, int(spec["frame_ms"]/4))))
            return
        if _animation_state(self._click_started_ms, now_ms, self._profile.effects_click_frame_ms) == "click":
            progress = (now_ms - self._click_started_ms) / self._profile.effects_click_frame_ms  # type: ignore[operator]
            self._render_click_effect(max(0.0, min(1.0, progress)))
            self._schedule_animation_in(33)
            return
        if self._click_started_ms is not None:
            self._click_started_ms = None
            if self._current_source != self._profile.default_frame:
                self._set_frame(self._profile.default_frame)

        if self._sequence_queue:
            next_frame = self._sequence_queue.pop(0)
            self._set_frame(next_frame)
            if self._sequence_queue:
                sec = random.uniform(self._profile.interval_min_sec, self._profile.interval_max_sec)
                self._schedule_animation_in(max(50, int(sec * 1000)))
                return

            if self._current_source != self._profile.default_frame:
                self._set_frame(self._profile.default_frame)
            self._schedule_animation_tick()
            return

        if self._current_source != self._profile.default_frame:
            self._set_frame(self._profile.default_frame)

        if random.random() <= self._profile.trigger_chance:
            sequence = self._profile.build_sequence_paths()
            if sequence:
                self._sequence_queue = sequence
                self._schedule_animation_in(0)
                return

        if "twinkle" in self._effect_images:
            self._render_idle_effect(now_ms)
            self._schedule_animation_in(100)
            return

        self._schedule_animation_tick()

    def _reload_image_for_current_monitor(self, source_path: Path | None = None):
        """드래그 후 현재 모니터 해상도 기준으로 캐릭터 이미지 재계산."""
        import win32api

        cx = self.root.winfo_x() + self._img_w // 2
        cy = self.root.winfo_y() + self._img_h // 2
        try:
            hmon = win32api.MonitorFromPoint((cx, cy), 2)
            mon_info = win32api.GetMonitorInfo(hmon)
            wl, wt, wr, wb = mon_info["Work"]
            work_size = (wr - wl, wb - wt)
        except Exception:
            work_size = (self.root.winfo_screenwidth(), self.root.winfo_screenheight())

        old_h = self._img_h
        self._load_image(work_size=work_size, source_path=source_path)
        self._resize_window_to_image(old_h)

    def _resize_window_to_image(self, old_height: int) -> None:
        """Keep the image label and borderless Tk window in the same dimensions."""
        x = self.root.winfo_x()
        y = self.root.winfo_y()
        bottom_anchored_y = y + old_height - self._img_h
        x, bottom_anchored_y = clamp_overlay_position(x, bottom_anchored_y, self._img_w, self._img_h)
        self.root.geometry(f"{self._img_w}x{self._img_h}+{x}+{bottom_anchored_y}")

    def _on_release(self, event):
        if self._moved:
            self._reload_image_for_current_monitor()
            self._save_position()
            self.remember_launcher_relative_offset(
                self.root.winfo_x(), self.root.winfo_y(), self._img_w, self._img_h
            )
            self._emit_pointer_event(
                "drag_end", {"screen_x": self.root.winfo_x(), "screen_y": self.root.winfo_y()}
            )
        else:
            self._start_click_action()
            self._emit_pointer_event("left_click", {"screen_x": event.x_root, "screen_y": event.y_root})
            self._invoke_activate()

    def get_phys_rect(self):
        """tkinter 논리 좌표 반환 — wt --pos 와 동일한 좌표계."""
        external_rect = getattr(self, "_external_rect", None)
        if external_rect is not None:
            return external_rect
        return (
            self.root.winfo_x(),
            self.root.winfo_y(),
            self._img_w,
            self._img_h,
        )

    def get_bundled_phys_rect(self):
        """Return the visible bundled window rect, never a replacement rect.

        Observer renderers are separate windows; their transient geometry must
        never become this window's persisted position.
        """
        return (
            self.root.winfo_x(),
            self.root.winfo_y(),
            self._img_w,
            self._img_h,
        )
