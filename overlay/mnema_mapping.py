"""Mnema's editable event-to-sprite mapping schema."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .bolttagu_mapping import Row, Section, SpriteMap, _check, resolve, single
from .native_engram_icon import ICON_CLIPS, ICON_OPTIONS

ASSET_DIR = Path(__file__).resolve().parents[1] / "resource" / "character" / "mnema"
OVERLAY_ID = "mnema-2d"
SPRITE_PREFIX = "mnema-"

HINTS = {
    "idle": "idle", "default": "idle", "input": "listening", "generating": "speaking",
    "thought": "waiting", "search": "searching", "memory": "searching", "success": "success",
    "hover": "alert", "click": "alert", "error": "wondering", "provider_error": "error",
}
CATEGORIES = {
    "write": "writing", "execute": "executing", "read": "searching",
    "communication": "speaking", "other": "tool_use",
}
ONESHOTS = {"success": "success"}
LIFECYCLE = {"show": "enter", "hide": "exit"}

HINT_NOTES = {
    "idle": "유휴", "default": "기본", "input": "사용자 입력 제출", "generating": "응답 생성 · 도구 실행",
    "thought": "생각 중", "search": "검색 도구", "memory": "기억 도구", "success": "턴 완료",
    "hover": "포인터 올림", "click": "클릭", "error": "도구 실패", "provider_error": "provider 실패",
}
CATEGORY_NOTES = {
    "write": "write · edit · patch · delete", "execute": "shell · exec · build · test · run",
    "read": "read · open", "communication": "mail · message · discord", "other": "그 외",
}
HINT_ORDER = tuple(HINTS)


def sprite_map() -> SpriteMap:
    metadata = json.loads((ASSET_DIR / "atlas.json").read_text(encoding="utf-8"))
    sheets = {
        Path(name).stem.removeprefix(SPRITE_PREFIX): (name, len(frames), len(frames))
        for name, frames in metadata["sheets"].items()
    }
    poses = tuple(sorted(ICON_OPTIONS))
    return SpriteMap(
        overlay_id=OVERLAY_ID, name="므네마", cell=tuple(metadata["cell"]), asset_dir=ASSET_DIR,
        sheets=sheets, options=ICON_OPTIONS,
        sections=(
            Section("hints", "display hint", tuple(Row(key, HINT_NOTES[key], (value,)) for key, value in HINTS.items()), poses,
                    note="상태에 머무는 동안의 동작입니다. 므네마 atlas의 15개 상태만 선택할 수 있습니다."),
            Section("oneshots", "1회 재생", tuple(Row(key, HINT_NOTES[key], (ONESHOTS[key],) if key in ONESHOTS else ()) for key in HINT_ORDER),
                    tuple(sorted(key for key, option in ICON_OPTIONS.items() if not option.loops)),
                    note="신호에 진입할 때 지속 동작 위로 한 번 재생합니다.", allow_empty=True),
            Section("categories", "도구 범주 — generating 세분", tuple(Row(key, CATEGORY_NOTES[key], (value,)) for key, value in CATEGORIES.items()), poses,
                    note="generating 상태에서 도구 종류별 동작입니다. 비우면 generating 설정을 따릅니다.", allow_empty=True),
            Section("lifecycle", "런처 전환", (Row("show", "overlay.show · 런처로 펼칠 때", (LIFECYCLE["show"],)), Row("hide", "overlay.hide · 런처로 접을 때", (LIFECYCLE["hide"],))),
                    tuple(sorted(ICON_CLIPS)), note="display hint와 무관하게 항상 우선합니다."),
        ),
    )


@dataclass(frozen=True)
class Mapping:
    hints: dict[str, str]
    categories: dict[str, str]
    oneshots: dict[str, str]
    lifecycle: dict[str, str]


def load_mapping(path: Path | None = None, *, log: Callable[[str], None] | None = None) -> Mapping:
    resolved = resolve(sprite_map(), path, log=log)
    return Mapping(hints=single(resolved, "hints"), categories=single(resolved, "categories"), oneshots=single(resolved, "oneshots"), lifecycle=single(resolved, "lifecycle"))


def validate_mapping_document(document: object) -> None:
    if not isinstance(document, dict):
        raise ValueError("매핑 최상위는 JSON 객체여야 합니다.")
    if "version" in document and (type(document["version"]) is not int or document["version"] != 1):
        raise ValueError("지원하는 매핑 version은 1입니다.")
    if document.get("overlay_id") not in (None, OVERLAY_ID):
        raise ValueError("다른 캐릭터용 매핑입니다.")
    sections = {section.key: section for section in sprite_map().sections}
    for name, entries in document.items():
        if name in {"version", "overlay_id"}:
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


def validate_mapping_file(path: Path) -> None:
    if path.stat().st_size > 65536:
        raise ValueError("매핑 파일은 64 KiB 이하여야 합니다.")
    validate_mapping_document(json.loads(path.read_text(encoding="utf-8-sig")))


def pose_for(hint: str, category: str | None, hints: dict[str, str] | None = None, categories: dict[str, str] | None = None) -> str:
    hints = HINTS if hints is None else hints
    categories = CATEGORIES if categories is None else categories
    resolved = hint if hint in hints else "idle"
    return categories[category] if resolved == "generating" and category in categories else hints[resolved]
