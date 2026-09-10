"""Native Bolttagu view: no transport, process, window or timer ownership.

Ported from engram-overlay d795f5a; provenance accompanies bundled assets.
CharacterOverlay owns the presentation and drives this view on its own tick.
"""
from __future__ import annotations

import json
import time
import tkinter as tk
from pathlib import Path
from typing import Callable

from PIL import Image, ImageTk

from .bolttagu_mapping import (
    ASSET_DIR, CLIPS, OPTIONS, STATE_POSES, CATEGORY_POSES, HINT_ONESHOTS,
    IDLE_POSE, SCALE_RANGE, TRANSPARENT, Recipe, load_mapping, pose_for,
    scaled_cell, facing_mirrored, finished as _finished, frames_at as _frames_at,
)

class BolttaguAnimator:
    """Track the active hint, any one-shot override, and the idle sub-loops."""

    def __init__(
        self,
        *,
        started_ms: int = 0,
        intro: str | None = "enter",
        seed: int = 0,
        hints: dict[str, str] | None = None,
        categories: dict[str, str] | None = None,
        oneshots: dict[str, str] | None = None,
    ) -> None:
        if intro is not None and intro not in CLIPS:
            raise ValueError(f"unknown intro clip: {intro}")
        self.hints = STATE_POSES if hints is None else hints
        self.categories = CATEGORY_POSES if categories is None else categories
        self.oneshots = HINT_ONESHOTS if oneshots is None else oneshots
        self.display_hint = "idle"
        self.tool_category: str | None = None
        self.state_started_ms = started_ms
        self.oneshot = intro
        self.oneshot_started_ms = started_ms
        # A farewell is terminal: nothing follows it on screen, so it holds its
        # last frame instead of snapping back to idle before the window closes.
        self.oneshot_holds = False
        # Feeds the pseudo-random blink hold; two renderers on one screen need not
        # blink in lockstep.
        self.seed = seed

    @property
    def pose(self) -> str:
        return pose_for(self.display_hint, self.tool_category, self.hints, self.categories)

    def play_lifecycle(self, clip: str, now_ms: int, *, hold_last: bool = False) -> int:
        """Start a show/hide transition, overriding any hint one-shot in flight."""
        if clip not in CLIPS:
            raise ValueError(f"unknown lifecycle clip: {clip}")
        self.oneshot = clip
        self.oneshot_started_ms = now_ms
        self.oneshot_holds = hold_last
        return CLIPS[clip].total_ms

    def apply_hint(self, hint: str, now_ms: int, category: str | None = None) -> None:
        resolved = hint if hint in self.hints else "idle"
        if (resolved, category) == (self.display_hint, self.tool_category):
            return
        self.display_hint = resolved
        self.tool_category = category
        self.state_started_ms = now_ms
        self.oneshot_holds = False
        oneshot = self.oneshots.get(resolved)
        if oneshot is not None:
            self.oneshot = oneshot
            self.oneshot_started_ms = now_ms

    def resolve(self, now_ms: int) -> Recipe:
        """Layers to draw for this instant, retiring a finished one-shot on the way.

        Both a transient flourish and the resting pose are just options on the
        shared timeline; only which one is asked for differs.
        """
        if self.oneshot is not None:
            option = OPTIONS[self.oneshot]
            elapsed = now_ms - self.oneshot_started_ms
            if not _finished(option, elapsed):
                return _frames_at(option, elapsed, self.seed)
            if self.oneshot_holds:
                return _frames_at(option, option.total_ms, self.seed)
            self.oneshot = None
        # A non-looping pose runs out and stands on its last frame.
        return _frames_at(OPTIONS[self.pose], now_ms - self.state_started_ms, self.seed)


def load_atlas(asset_dir: Path = ASSET_DIR) -> tuple[dict[str, tuple[Image.Image, ...]], tuple[int, int]]:
    """Slice the bundled sheets into per-cell RGBA frames."""
    metadata = json.loads((asset_dir / "atlas.json").read_text(encoding="utf-8"))
    cell_width, cell_height = (int(value) for value in metadata["cell"])
    if not (1 <= cell_width <= 2048 and 1 <= cell_height <= 2048):
        raise ValueError("invalid atlas cell size")
    if not isinstance(metadata["sheets"], dict) or not 1 <= len(metadata["sheets"]) <= 32:
        raise ValueError("invalid atlas sheet count")
    sheets: dict[str, tuple[Image.Image, ...]] = {}
    for file_name, frames in metadata["sheets"].items():
        key = Path(file_name).stem.removeprefix("bolttagu-")
        if Path(file_name).name != file_name or not file_name.endswith(".png"):
            raise ValueError("invalid atlas sheet name")
        if not isinstance(frames, list) or not 1 <= len(frames) <= 32:
            raise ValueError("invalid atlas frame count")
        with Image.open(asset_dir / file_name) as raw:
            sheet = raw.convert("RGBA")
        expected = (cell_width * len(frames), cell_height)
        if sheet.size != expected:
            raise ValueError(f"{file_name} must be {expected[0]}x{expected[1]}, got {sheet.size}")
        sheets[key] = tuple(
            sheet.crop((index * cell_width, 0, (index + 1) * cell_width, cell_height))
            for index in range(len(frames))
        )
    for option in OPTIONS.values():
        for layer in option.layers:
            if layer.sheet not in sheets or max(layer.cells) >= len(sheets[layer.sheet]):
                raise ValueError("atlas cannot draw a bundled option")
    return sheets, (cell_width, cell_height)


class Bolttagu2dView:
    background = TRANSPARENT
    transparent_color = TRANSPARENT

    def __init__(
        self,
        *,
        scale: float = 1.0,
        face_pointer: bool = True,
        launcher_managed: bool = False,
        show_floor: bool = False,
        seed: int = 0,
        mapping_path: Path | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        mapping = load_mapping(mapping_path, log=log)
        self.mapping = mapping
        sheets, cell = load_atlas()
        self.cell = cell
        self.scale = scale
        # Resize the finished frame rather than every cell, so memory stays flat
        # at any scale and only the <=12 redraws per second pay for it.
        self.width, self.height = scaled_cell(cell, scale)
        # Just the ground. The spilled coffee belongs to the poses that spill it,
        # so a baked-in floor variant would draw it under every other pose too.
        self.floor = sheets["floor"][0] if show_floor else None
        self.sheets = sheets
        self.face_pointer = face_pointer
        # When Engram's launcher owns presentation, the arrival bow belongs to
        # overlay.show rather than to process start.
        self.animator = BolttaguAnimator(
            started_ms=self._now_ms(),
            intro=None if launcher_managed else "enter",
            seed=seed,
            hints=mapping.hints,
            categories=mapping.categories,
            oneshots=mapping.oneshots,
        )
        self.mirrored = False
        self._pointer_frozen = False
        self.canvas: tk.Canvas | None = None
        self.image_id: int | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.drawn: tuple[Recipe, bool] | None = None

    def compose(self, recipe: Recipe, mirrored: bool) -> Image.Image:
        """Flatten one recipe. Cheap enough to redo per visible frame change."""
        base = self.floor
        frame = self.sheets[recipe[0][0]][recipe[0][1]]
        image = Image.alpha_composite(base, frame) if base is not None else frame.copy()
        for sheet, cell in recipe[1:]:
            image = Image.alpha_composite(image, self.sheets[sheet][cell])
        if mirrored:
            image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if (self.width, self.height) != self.cell:
            image = image.resize((self.width, self.height), Image.Resampling.LANCZOS)
        return image

    def mount(self, canvas: tk.Canvas) -> None:
        self.canvas = canvas
        target = (self.animator.resolve(self._now_ms()), self.mirrored)
        self.photo = ImageTk.PhotoImage(self.compose(*target), master=canvas)
        self.image_id = canvas.create_image(0, 0, image=self.photo, anchor="nw")
        self.drawn = target

    def apply_hint(self, hint: str, category: str | None = None) -> None:
        self.animator.apply_hint(hint, self._now_ms(), category)

    def render_frame(self, pointer_x: int, window_x: int, *, force: bool = False) -> Image.Image | None:
        """CharacterOverlay pulls a frame; this view owns no widget or Tk timer."""
        if self.face_pointer and not self._pointer_frozen:
            self.mirrored = facing_mirrored(pointer_x, window_x, self.width, current=self.mirrored)
        target = (self.animator.resolve(self._now_ms()), self.mirrored)
        if not force and target == self.drawn:
            return None
        image = self.compose(*target)
        self.drawn = target
        return image

    def resize(self, requested_width: int, requested_height: int) -> tuple[int, int]:
        """Apply host physical height within the safe scale range without stretching art."""
        del requested_width
        scale = min(SCALE_RANGE[1], max(SCALE_RANGE[0], float(requested_height) / self.cell[1]))
        self.width, self.height = scaled_cell(self.cell, scale)
        self.drawn = None
        return self.width, self.height

    def begin_enter(self) -> int:
        self._pointer_frozen = False
        return self.animator.play_lifecycle(self.mapping.lifecycle["show"], self._now_ms())

    def begin_exit(self) -> int:
        now_ms = self._now_ms()
        # Freeze orientation before starting or drawing the farewell so a late
        # pointer sample cannot flip any of its frames.
        self._pointer_frozen = True
        # Holding the final frame keeps the character gone until the window closes.
        hold_ms = self.animator.play_lifecycle(
            self.mapping.lifecycle["hide"], now_ms, hold_last=True
        )
        self._redraw((self.animator.resolve(now_ms), self.mirrored))
        return hold_ms

    def tick(self, pointer_x: int, pointer_y: int, window_x: int, window_y: int) -> None:
        if self.face_pointer and not self._pointer_frozen:
            self.mirrored = facing_mirrored(pointer_x, window_x, self.width, current=self.mirrored)
        self._redraw((self.animator.resolve(self._now_ms()), self.mirrored))

    def _redraw(self, target: tuple[Recipe, bool]) -> None:
        if self.canvas is None or self.image_id is None or target == self.drawn:
            return
        self.photo = ImageTk.PhotoImage(self.compose(*target), master=self.canvas)
        self.canvas.itemconfigure(self.image_id, image=self.photo)
        self.drawn = target

    @staticmethod
    def _now_ms() -> int:
        return int(time.monotonic() * 1000)
