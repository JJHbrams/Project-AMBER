"""Shared vocabulary for sprite overlays that let their mapping be chosen.

A sprite overlay decides which artwork each semantic signal draws. Which artwork
that should be is a visual judgement, so it belongs in a picker rather than in a
table of literals -- and every sprite overlay wants the same picker.

An overlay describes itself with a :class:`SpriteMap`: the sheets it draws from,
the options a signal may take, and the sections those choices are grouped into.
``resolve`` merges a user's ``mapping.json`` over the declared defaults and drops
anything that cannot be drawn, so a hand-edited file can never stop a renderer.

The description is deliberately declarative: ``scripts/build-sprite-preview.py``
renders every overlay from it without knowing anything about any of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MAPPING_FILE = "mapping.json"

Choice = tuple[str, ...]
Resolved = dict[str, dict[str, Choice]]


@dataclass(frozen=True)
class Layer:
    """Cells from one sheet on their own timeline, drawn in declaration order."""

    sheet: str
    cells: tuple[int, ...]
    durations_ms: tuple[int, ...]
    loop: bool = True
    # A range the first cell's duration is drawn from anew each cycle. Lets a
    # resting pose wait an unpredictable while before its brief action -- a blink,
    # say -- without the timeline stopping being a pure function of the clock.
    hold_ms: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if not self.cells:
            raise ValueError(f"layer on {self.sheet} has no cells")
        if len(self.cells) != len(self.durations_ms):
            raise ValueError(f"layer on {self.sheet} has mismatched cells and durations")
        rest = self.durations_ms[1:] if self.hold_ms else self.durations_ms
        if rest and min(rest) <= 0:
            raise ValueError(f"layer on {self.sheet} has a non-positive duration")
        if self.hold_ms:
            low, high = self.hold_ms
            if low <= 0 or high < low:
                raise ValueError(f"layer on {self.sheet} has an unusable hold range")

    @property
    def total_ms(self) -> int:
        """Nominal length, using the midpoint of a random hold."""
        if not self.hold_ms:
            return sum(self.durations_ms)
        low, high = self.hold_ms
        return (low + high) // 2 + sum(self.durations_ms[1:])


@dataclass(frozen=True)
class Option:
    """One choosable value and how it is drawn."""

    key: str
    layers: tuple[Layer, ...]
    note: str = ""

    @property
    def total_ms(self) -> int:
        return max(layer.total_ms for layer in self.layers)

    @property
    def loops(self) -> bool:
        return all(layer.loop for layer in self.layers)


@dataclass(frozen=True)
class Row:
    """One signal the overlay reacts to."""

    key: str
    note: str = ""
    default: Choice = ()


@dataclass(frozen=True)
class Section:
    """A group of signals that share a pool of options."""

    key: str
    title: str
    rows: tuple[Row, ...]
    options: tuple[str, ...]
    note: str = ""
    multi: bool = False
    allow_empty: bool = False
    # Keys the overlay recognises but deliberately does not offer, each with the
    # reason. Without this a deliberate omission is indistinguishable from a typo.
    refused: dict[str, str] = field(default_factory=dict)
    # Resolved and loadable, but not offered in the picker. For a choice that is
    # real but too narrow to be worth a column.
    hidden: bool = False

    @property
    def by_key(self) -> dict[str, Row]:
        return {row.key: row for row in self.rows}


@dataclass(frozen=True)
class SpriteMap:
    """Everything the picker and the loader need to know about one overlay."""

    overlay_id: str
    name: str
    cell: tuple[int, int]
    asset_dir: Path
    # name -> (file name, cell count, columns). A horizontal strip has
    # columns == count; a grid atlas wraps after that many cells.
    sheets: dict[str, tuple[str, int, int]]
    options: dict[str, Option]
    sections: tuple[Section, ...] = field(default_factory=tuple)

    def defaults(self) -> Resolved:
        return {s.key: {row.key: row.default for row in s.rows} for s in self.sections}


def _hold_at(layer: Layer, cycle: int, seed: int) -> int:
    """The first cell's duration for one cycle: reproducible, not memorised.

    A plain LCG keeps this identical in the preview page, so what is previewed is
    what is drawn rather than an approximation of it.
    """
    assert layer.hold_ms is not None
    low, high = layer.hold_ms
    noise = ((cycle * 9301 + seed * 49297 + 233280) % 233280) / 233280
    return low + int(noise * (high - low))


def cell_at(layer: Layer, elapsed_ms: int, seed: int = 0) -> int:
    """Which cell of this layer is showing, given the clock."""
    time = max(0, elapsed_ms)
    if layer.hold_ms is None:
        total = sum(layer.durations_ms)
        if layer.loop:
            time %= total
        elif time >= total:
            return layer.cells[-1]
        cursor = 0
        for cell, duration in zip(layer.cells, layer.durations_ms):
            cursor += duration
            if time < cursor:
                return cell
        return layer.cells[-1]

    # A held first cell makes each cycle a different length, so walk the cycles.
    tail = sum(layer.durations_ms[1:])
    cycle = 0
    while True:
        span = _hold_at(layer, cycle, seed) + tail
        if time < span or not layer.loop:
            break
        time -= span
        cycle += 1
    cursor = _hold_at(layer, cycle, seed)
    if time < cursor:
        return layer.cells[0]
    for cell, duration in zip(layer.cells[1:], layer.durations_ms[1:]):
        cursor += duration
        if time < cursor:
            return cell
    return layer.cells[-1] if not layer.loop else layer.cells[0]


def frames_at(option: Option, elapsed_ms: int, seed: int = 0) -> tuple[tuple[str, int], ...]:
    """Every (sheet, cell) this option draws right now, bottom layer first."""
    return tuple(
        (layer.sheet, cell_at(layer, elapsed_ms, seed + index))
        for index, layer in enumerate(option.layers)
    )


def finished(option: Option, elapsed_ms: int) -> bool:
    """Whether a non-looping option has run out. A looping one never has."""
    return not option.loops and elapsed_ms >= option.total_ms


class Rotation:
    """Pick one of several options per time bucket, avoiding an immediate repeat.

    A signal that offers several stills wants variety without flicker, so the
    choice is made once per bucket and remembered.
    """

    def __init__(self, rng: object) -> None:
        self.rng = rng
        self.picked: dict[tuple[str, int], str] = {}

    def clear(self) -> None:
        self.picked.clear()

    def pick(self, key: str, choice: Choice, bucket: int) -> str | None:
        if not choice:
            return None
        if len(choice) == 1:
            return choice[0]
        bucket = max(0, bucket)
        remembered = self.picked.get((key, bucket))
        if remembered is not None:
            return remembered
        previous = self.picked.get((key, bucket - 1))
        candidates = tuple(value for value in choice if value != previous) or choice
        self.picked[(key, bucket)] = self.rng.choice(candidates)  # type: ignore[attr-defined]
        return self.picked[(key, bucket)]


def _check(section: Section, row_key: str, value: object, options: set[str]) -> tuple[Choice, str]:
    """Return the accepted choice and an empty note, or the reason it was refused."""
    values: list[object] = list(value) if isinstance(value, (list, tuple)) else [value]
    if values == [None]:
        values = []
    if not section.multi and len(values) > 1:
        return (), f"{row_key!r} takes one value, got {len(values)}"
    if not values and not section.allow_empty:
        return (), f"{row_key!r} cannot be empty"
    for item in values:
        if not isinstance(item, str) or item not in options:
            return (), f"{row_key!r} cannot draw {item!r}"
    return tuple(values), ""  # type: ignore[arg-type]


def resolve(
    sprite_map: SpriteMap,
    path: Path | None = None,
    *,
    log: Callable[[str], None] | None = None,
) -> Resolved:
    """Merge a user mapping over the declared defaults, refusing what cannot be drawn."""
    resolved = sprite_map.defaults()
    if path is None:
        return resolved
    note = log or (lambda message: None)
    if not path.is_file():
        return resolved
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        note(f"{MAPPING_FILE} ignored: {exc}")
        return resolved
    if not isinstance(document, dict):
        note(f"{MAPPING_FILE} ignored: top level must be an object")
        return resolved

    sections = {section.key: section for section in sprite_map.sections}
    for key, entries in document.items():
        if key == "version":
            continue
        section = sections.get(key)
        if section is None:
            note(f"{MAPPING_FILE}: unknown section {key!r}")
            continue
        if not isinstance(entries, dict):
            note(f"{MAPPING_FILE}: {key} must be an object")
            continue
        rows, options = section.by_key, set(section.options)
        for row_key, value in entries.items():
            if row_key in section.refused:
                note(f"{MAPPING_FILE}: {key}: {section.refused[row_key]}")
                continue
            if row_key not in rows:
                note(f"{MAPPING_FILE}: {key}: unknown signal {row_key!r}")
                continue
            choice, refusal = _check(section, row_key, value, options)
            if refusal:
                note(f"{MAPPING_FILE}: {key}: {refusal}")
            else:
                resolved[key][row_key] = choice
    return resolved


def single(resolved: Resolved, section: str) -> dict[str, str]:
    """Flatten a single-select section, dropping rows left empty."""
    return {key: value[0] for key, value in resolved[section].items() if value}



from .config import resolve_path

TRANSPARENT = "#010203"
ASSET_DIR = resolve_path("resource/character/bolttagu")

Recipe = tuple[tuple[str, int], ...]

# Frame timings are the pack's own, from sprites.json. Every event set is three frames.
ENTER_DURATIONS_MS = (200, 300, 220)
EXIT_DURATIONS_MS = (220, 220, 260)
EVENT_DURATIONS_MS: dict[str, tuple[int, int, int]] = {
    "wondering": (320, 260, 320),
    "searching": (650, 500, 650),
    "writing": (320, 320, 420),
    "speaking": (220, 220, 260),
    "listening": (420, 420, 420),
    "waiting": (800, 800, 800),
    "success": (280, 360, 360),
    "error": (260, 300, 420),
}
LOOPING_EVENTS = ("wondering", "searching", "writing", "speaking", "listening", "waiting", "error")

# The trickcal retouch. It redraws every state of the original at the same timings and
# adds two activities of its own, so it is the same table plus those two.
TRICKCAL = "trickcal-"
TRICKCAL_EVENT_DURATIONS_MS: dict[str, tuple[int, int, int]] = {
    **EVENT_DURATIONS_MS,
    "executing": (360, 300, 480),
    "tool_use": (420, 360, 500),
}
TRICKCAL_LOOPING_EVENTS = LOOPING_EVENTS + ("executing", "tool_use")

# Option-name prefix per bundled set, and the timings each one draws at. The original
# set is unprefixed so every mapping written before the retouch keeps working.
SET_DURATIONS: dict[str, dict[str, tuple[int, int, int]]] = {
    "": EVENT_DURATIONS_MS,
    TRICKCAL: TRICKCAL_EVENT_DURATIONS_MS,
}
SET_LOOPING: dict[str, tuple[str, ...]] = {
    "": LOOPING_EVENTS,
    TRICKCAL: TRICKCAL_LOOPING_EVENTS,
}

# idle: steam is a 24-frame 10 fps loop, blink is a fixed 210 ms sequence that
# re-arms itself 2.5-6 s after it finishes.
STEAM_FRAME_MS = 100
STEAM_CELLS = 24
EYE_CELLS = {"open": 0, "half": 1, "closed": 2}
BLINK_SEQUENCE = (("half", 50), ("closed", 90), ("half", 70))
BLINK_INTERVAL_MS = (2500, 6000)

# Poses drawn with both hands up and no mug in them. The mug has to go somewhere,
# so these draw the tipped mug and its puddle at her feet; without it the coffee
# simply vanishes the moment she reacts to anything. It rides on the pose rather
# than on the floor, so it shows with the floor layer off and never appears under
# a pose that is still holding the mug.
SPILL_SHEET = "spill"
SPILL_POSES = ("alert", "wondering")

# The artwork is drawn with the long cheek and the mug toward the viewer's left,
# so the sprite already looks left and is mirrored only to turn right.
POINTER_DEADZONE_PX = 24

SCALE_RANGE = (0.2, 4.0)


@dataclass(frozen=True)
class Clip:
    """One animation drawn from a single horizontal sheet."""

    sheet: str
    cells: tuple[int, ...]
    durations_ms: tuple[int, ...]
    loop: bool

    def __post_init__(self) -> None:
        if not self.cells:
            raise ValueError(f"clip {self.sheet} has no cells")
        if len(self.cells) != len(self.durations_ms):
            raise ValueError(f"clip {self.sheet} cell and duration counts differ")
        if min(self.durations_ms) <= 0:
            raise ValueError(f"clip {self.sheet} has a non-positive duration")

    @property
    def total_ms(self) -> int:
        return sum(self.durations_ms)


def _event_clip(state: str, *, loop: bool, prefix: str = "") -> Clip:
    """One three-frame event set, drawn from the named set's own sheet and timings."""
    return Clip(
        sheet=prefix + state,
        cells=(0, 1, 2),
        durations_ms=SET_DURATIONS[prefix][state],
        loop=loop,
    )


def _set_clips(prefix: str) -> dict[str, Clip]:
    """Every clip one sprite set draws, keyed by the option name that selects it."""
    return {
        prefix + "alert": Clip(sheet=prefix + "alert", cells=(0,), durations_ms=(1000,), loop=True),
        prefix + "enter": Clip(
            sheet=prefix + "enter", cells=(0, 1, 2), durations_ms=ENTER_DURATIONS_MS, loop=False
        ),
        # The rear-view farewell, played when Engram's launcher collapses the character.
        prefix + "exit": Clip(
            sheet=prefix + "exit", cells=(0, 1, 2), durations_ms=EXIT_DURATIONS_MS, loop=False
        ),
        # The packs' only one-shot event: play once, then settle back to idle.
        prefix + "success": _event_clip("success", loop=False, prefix=prefix),
        **{
            prefix + state: _event_clip(state, loop=True, prefix=prefix)
            for state in SET_LOOPING[prefix]
        },
    }


CLIPS: dict[str, Clip] = {
    name: clip for prefix in SET_DURATIONS for name, clip in _set_clips(prefix).items()
}

# "idle" is the layered blink+steam pose; the rest name a looping clip above.
# The mapping follows the intent the pack states in its own event-map.json.
IDLE_POSE = "idle"
# One layered resting pose per set. They are options like any other, so a mapping
# may rest in the retouch while a flourish plays from the original, or the reverse.
IDLE_POSES = (IDLE_POSE, TRICKCAL + IDLE_POSE)
STATE_POSES: dict[str, str] = {
    "default": IDLE_POSE,
    "idle": IDLE_POSE,
    # The success one-shot plays over this pose and settles back into it.
    "success": IDLE_POSE,
    "hover": "alert",
    "click": "alert",
    "input": "listening",
    "generating": "speaking",
    "thought": "wondering",
    "search": "searching",
    # Consulting stored notes reads the same as consulting documents.
    "memory": "searching",
    "error": "error",
    "provider_error": "error",
}

# payload.category refines "generating", which is Engram's catch-all for every tool
# that is neither a search nor a memory lookup. Without this, editing a file and
# streaming an answer look identical.
# Engram publishes "search" and "memory" as display hints of their own, so those
# two categories never arrive alongside "generating" and can never reach the
# refinement below. Only the rest are configurable here; the hint table owns the
# other two. See event_api.event_for_bubble.
UNREACHABLE_CATEGORIES = frozenset({"search", "memory"})
REFINABLE_CATEGORIES = frozenset({'search', 'memory', 'write', 'execute', 'read', 'communication', 'other'}) - UNREACHABLE_CATEGORIES

CATEGORY_POSES: dict[str, str] = {
    "write": "writing",     # code, docs, artifacts: write/edit/patch/delete tools
    "execute": "waiting",   # shell, build, test, run: work to wait on
    "read": "searching",    # opening a document
}

# A hint that warrants a one-shot the moment the renderer enters it.
# A flourish played once on arrival before settling back into the hint's pose.
# Not offered in the picker: with non-looping poses selectable, the only thing it
# adds is returning to idle afterwards, which is one clip and one default. The
# mapping file still accepts an "oneshots" section.
HINT_ONESHOTS: dict[str, str] = {"success": "success"}

# Engram's launcher plays these for overlay.show and overlay.hide. A hint may use
# them too: a lifecycle transition is a one-shot override, so it still wins while
# it runs. Nothing here is off limits -- every bundled clip can be chosen.
LIFECYCLE_CLIPS = ("enter", "exit")

# What the launcher's own transitions play. overlay.show and overlay.hide are
# events in their own right, so which clip each one runs is chosen here rather
# than hardcoded at the call site.
LIFECYCLE_TRANSITIONS: dict[str, str] = {"show": "enter", "hide": "exit"}


def selectable_poses() -> list[str]:
    """Every pose a state can rest in.

    A clip that does not loop holds its last frame for as long as the state lasts.
    """
    return list(IDLE_POSES) + sorted(CLIPS)


def selectable_oneshots() -> list[str]:
    """Every clip a hint may play once on arrival before settling into its pose."""
    return sorted(name for name, clip in CLIPS.items() if not clip.loop)


OVERLAY_ID = "bolttagu-2d"


HINT_NOTES = {
    "idle": "유휴", "default": "기본", "input": "사용자 입력 제출",
    "generating": "응답 생성 · 도구 실행", "thought": "생각 중", "search": "검색 도구",
    "memory": "기억 도구", "success": "턴 완료", "hover": "포인터 올림",
    "click": "클릭", "error": "도구 실패", "provider_error": "provider 실패",
}
HINT_ORDER = (
    "idle", "default", "input", "generating", "thought",
    "search", "memory", "success", "hover", "click", "error", "provider_error",
)
CATEGORY_NOTES = {
    "write": "write · edit · patch · delete",
    "execute": "shell · exec · build · test · run",
    "read": "read · open",
    "communication": "mail · message · discord",
    "other": "그 외",
}
def _idle_option(prefix: str) -> Option:
    """The layered resting pose of one set: a random blink under its own steam."""
    return Option(
        prefix + IDLE_POSE,
        (
            # The eye rests open for an unpredictable while, then blinks.
            Layer(
                prefix + "idle",
                tuple(EYE_CELLS[name] for name in ("open", "half", "closed", "half")),
                (0,) + tuple(ms for _, ms in BLINK_SEQUENCE),
                hold_ms=BLINK_INTERVAL_MS,
            ),
            Layer(prefix + "steam", tuple(range(STEAM_CELLS)), (STEAM_FRAME_MS,) * STEAM_CELLS),
        ),
        note=("트릭칼풍 " if prefix else "") + "눈깜빡임 + 커피 김",
    )


def _options() -> dict[str, Option]:
    """Every drawable pose, described once for both the picker and the renderer."""
    options = {prefix + IDLE_POSE: _idle_option(prefix) for prefix in SET_DURATIONS}
    for name, clip in CLIPS.items():
        layers = (Layer(clip.sheet, clip.cells, clip.durations_ms, clip.loop),)
        note = "트릭칼풍" if name.startswith(TRICKCAL) else ""
        if name.removeprefix(TRICKCAL) in SPILL_POSES:
            # Underneath her: the puddle is on the floor at her feet. One cell held
            # for exactly as long as the pose, so the spill cannot lengthen it.
            layers = (Layer(SPILL_SHEET, (0,), (clip.total_ms,)),) + layers
            note = (note + " 쏟은 커피").strip()
        options[name] = Option(name, layers, note=note)
    return options


OPTIONS: dict[str, Option] = _options()


def sprite_map() -> SpriteMap:
    """Declarative description of this overlay for the shared picker and loader."""
    metadata = json.loads((ASSET_DIR / "atlas.json").read_text(encoding="utf-8"))
    sheets = {
        Path(name).stem.removeprefix("bolttagu-"): (name, len(frames), len(frames))
        for name, frames in metadata["sheets"].items()
    }
    poses = tuple(selectable_poses())
    return SpriteMap(
        overlay_id=OVERLAY_ID,
        name="Bolttagu",
        cell=tuple(metadata["cell"]),
        asset_dir=ASSET_DIR,
        sheets=sheets,
        options=OPTIONS,
        sections=(
            Section(
                "hints", "display hint",
                tuple(Row(k, HINT_NOTES.get(k, ""), (STATE_POSES[k],)) for k in HINT_ORDER),
                poses,
                note="그 상태에 머무는 동안 반복되는 동작. 반복하지 않는 클립은 마지막 프레임에서 멈춘다. "
                     "trickcal- 로 시작하는 항목은 같은 캐릭터의 트릭칼풍 리터치 셋이고, "
                     "두 셋은 같은 crop 으로 맞춰져 있어 신호마다 섞어 써도 캐릭터가 튀지 않는다.",
            ),
            Section(
                "oneshots", "1회 재생",
                tuple(Row(k, HINT_NOTES.get(k, ""),
                          (HINT_ONESHOTS[k],) if k in HINT_ONESHOTS else ())
                      for k in HINT_ORDER),
                tuple(selectable_oneshots()),
                note="신호에 진입할 때 지속 동작 위로 한 번 얹힌 뒤 가라앉는다.",
                allow_empty=True,
                hidden=True,
            ),
            Section(
                "categories", "도구 범주 — generating 세분",
                tuple(Row(k, CATEGORY_NOTES.get(k, ""),
                          (CATEGORY_POSES[k],) if k in CATEGORY_POSES else ())
                      for k in ("write", "execute", "read", "communication", "other")
                      if k in REFINABLE_CATEGORIES),
                poses,
                note="검색·기억 도구는 자기 display hint로 오므로 여기 없다. 비우면 generating 설정을 따른다.",
                allow_empty=True,
                refused={
                    key: f"category {key!r} arrives as its own display hint, "
                         f"so set the {key!r} hint instead"
                    for key in sorted(UNREACHABLE_CATEGORIES)
                },
            ),
            Section(
                "lifecycle", "런처 전환",
                (Row("show", "overlay.show · 런처로 펼칠 때", (LIFECYCLE_TRANSITIONS["show"],)),
                 Row("hide", "overlay.hide · 런처로 접을 때", (LIFECYCLE_TRANSITIONS["hide"],))),
                tuple(sorted(CLIPS)),
                note="display hint와 무관하게 항상 우선한다.",
            ),
        ),
    )


@dataclass(frozen=True)
class Mapping:
    """Which animation each signal draws, after any user override."""

    hints: dict[str, str]
    categories: dict[str, str]
    oneshots: dict[str, str]
    lifecycle: dict[str, str]


def _valid_pose(pose: object) -> bool:
    return isinstance(pose, str) and pose in selectable_poses()


def _valid_oneshot(clip: object) -> bool:
    return clip is None or (isinstance(clip, str) and clip in selectable_oneshots())


def load_mapping(
    path: Path | None = None, *, log: Callable[[str], None] | None = None
) -> Mapping:
    """Resolve this overlay's mapping through the shared loader."""
    resolved = resolve(sprite_map(), path, log=log)
    return Mapping(
        hints=single(resolved, "hints"),
        categories=single(resolved, "categories"),
        oneshots=single(resolved, "oneshots"),
        lifecycle=single(resolved, "lifecycle"),
    )


def validate_mapping_file(path: Path) -> None:
    """Strict explicit-import gate; tolerant runtime defaults remain unchanged."""
    if path.stat().st_size > 65536:
        raise ValueError("매핑 파일은 64 KiB 이하여야 합니다.")
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    validate_mapping_document(document)


def validate_mapping_document(document: object) -> None:
    if not isinstance(document, dict):
        raise ValueError("매핑 최상위는 JSON 객체여야 합니다.")
    if "version" in document and (type(document['version']) is not int or document['version'] != 1):
        raise ValueError("지원하는 매핑 version은 1입니다.")
    sections = {section.key: section for section in sprite_map().sections}
    for name, entries in document.items():
        if name == 'version':
            continue
        section = sections.get(name)
        if section is None or not isinstance(entries, dict):
            raise ValueError(f"지원하지 않는 매핑 섹션: {name}")
        for key, value in entries.items():
            if key not in section.by_key or key in section.refused:
                raise ValueError(f"지원하지 않는 매핑 신호: {name}.{key}")
            _, reason = _check(section, key, value, set(section.options))
            if reason:
                raise ValueError(reason)


def pose_for(
    hint: str,
    category: str | None,
    hints: dict[str, str] | None = None,
    categories: dict[str, str] | None = None,
) -> str:
    """Pose for one hint, refined by the tool category when Engram supplied one."""
    hints = STATE_POSES if hints is None else hints
    categories = CATEGORY_POSES if categories is None else categories
    resolved = hint if hint in hints else "idle"
    if resolved == "generating" and category in categories:
        return categories[category]
    return hints[resolved]


def clip_cell(clip: Clip, elapsed_ms: int) -> int | None:
    """Sheet cell for ``elapsed_ms`` into ``clip``, or None once a one-shot has finished."""
    if elapsed_ms < 0:
        elapsed_ms = 0
    if clip.loop:
        elapsed_ms %= clip.total_ms
    elif elapsed_ms >= clip.total_ms:
        return None
    cursor = 0
    for index, duration in enumerate(clip.durations_ms):
        cursor += duration
        if elapsed_ms < cursor:
            return clip.cells[index]
    return clip.cells[-1]


def scaled_cell(cell: tuple[int, int], scale: float) -> tuple[int, int]:
    """Window size for an atlas cell, or the cell itself at 1.0."""
    low, high = SCALE_RANGE
    if not low <= scale <= high:
        raise ValueError(f"scale must be between {low} and {high}, got {scale}")
    if scale == 1.0:
        return cell
    return (max(1, round(cell[0] * scale)), max(1, round(cell[1] * scale)))


def facing_mirrored(pointer_x: int, window_x: int, width: int, *, current: bool) -> bool:
    """Whether to mirror the sprite so it looks toward the pointer.

    A deadzone around the window centre keeps the sprite from flipping back and forth
    while the pointer hovers exactly on the seam.
    """
    offset = pointer_x - (window_x + width // 2)
    if abs(offset) <= POINTER_DEADZONE_PX:
        return current
    return offset > 0
