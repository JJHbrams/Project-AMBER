"""Native Engram icon character driven by the established overlay event semantics."""
from __future__ import annotations

from pathlib import Path

from .bolttagu_mapping import (
    BLINK_INTERVAL_MS,
    BLINK_SEQUENCE,
    CATEGORY_POSES,
    ENTER_DURATIONS_MS,
    EVENT_DURATIONS_MS,
    EXIT_DURATIONS_MS,
    EYE_CELLS,
    HINT_ONESHOTS,
    IDLE_POSE,
    LOOPING_EVENTS,
    Layer,
    Option,
    Clip,
    Recipe,
    STATE_POSES,
    STEAM_CELLS,
    STEAM_FRAME_MS,
    TRICKCAL_EVENT_DURATIONS_MS,
    TRICKCAL_LOOPING_EVENTS,
)
from .native_bolttagu import Bolttagu2dView

ASSET_DIR = Path(__file__).resolve().parents[1] / "resource" / "character" / "engram-icon"
SPRITE_PREFIX = "engram-icon-"

ENGRAM_ICON_STATE_STRIPS = (
    "idle", "waiting", "listening", "speaking", "searching", "wondering", "writing",
    "tool_use", "executing", "success", "error", "enter", "exit", "alert", "steam",
)


def _event_clip(state: str, *, loop: bool) -> Clip:
    return Clip(state, (0, 1, 2), TRICKCAL_EVENT_DURATIONS_MS[state], loop)


ICON_CLIPS = {
    "alert": Clip("alert", (0,), (1000,), True),
    "enter": Clip("enter", (0, 1, 2), ENTER_DURATIONS_MS, False),
    "exit": Clip("exit", (0, 1, 2), EXIT_DURATIONS_MS, False),
    "success": _event_clip("success", loop=False),
    **{state: _event_clip(state, loop=True) for state in TRICKCAL_LOOPING_EVENTS},
}

ICON_OPTIONS = {
    IDLE_POSE: Option(
        IDLE_POSE,
        (
            Layer("idle", tuple(EYE_CELLS[name] for name in ("open", "half", "closed", "half")),
                  (0,) + tuple(ms for _, ms in BLINK_SEQUENCE), hold_ms=BLINK_INTERVAL_MS),
            Layer("steam", tuple(range(STEAM_CELLS)), (STEAM_FRAME_MS,) * STEAM_CELLS),
        ),
    ),
    **{
        name: Option(name, (Layer(clip.sheet, clip.cells, clip.durations_ms, clip.loop),))
        for name, clip in ICON_CLIPS.items()
    },
}

ICON_HINTS = {
    key: value.removeprefix("trickcal-")
    for key, value in STATE_POSES.items()
}
ICON_CATEGORIES = {
    key: value.removeprefix("trickcal-")
    for key, value in CATEGORY_POSES.items()
}
ICON_ONESHOTS = {
    key: value.removeprefix("trickcal-")
    for key, value in HINT_ONESHOTS.items()
}
ICON_LIFECYCLE = {"show": "enter", "hide": "exit"}


class EngramIconView(Bolttagu2dView):
    def __init__(self, **kwargs) -> None:
        super().__init__(
            asset_dir=ASSET_DIR,
            sprite_prefix=SPRITE_PREFIX,
            required_options=ICON_OPTIONS,
            clips=ICON_CLIPS,
            hints=ICON_HINTS,
            categories=ICON_CATEGORIES,
            oneshots=ICON_ONESHOTS,
            lifecycle=ICON_LIFECYCLE,
            **kwargs,
        )
