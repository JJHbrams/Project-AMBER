"""Tutorial state machine persisted in ~/.engram/tutorial.user.yaml."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

_ENGRAM_DIR = Path.home() / ".engram"
_RUNTIME_USER_CONFIG_PATH = _ENGRAM_DIR / "user.config.yaml"
_TUTORIAL_USER_PATH = _ENGRAM_DIR / "tutorial.user.yaml"
_USER_PERSONA_PATH = _ENGRAM_DIR / "persona.user.yaml"

_STEP_ORDER = [
    "persona_setup",
    "wiki_basic",
    "wiki_advanced",
    "session_continuity",
]

_DONE_STEP_SET = set(_STEP_ORDER)
_STEP_INDEX = {step: idx + 1 for idx, step in enumerate(_STEP_ORDER)}
_STEP_TOTAL = len(_STEP_ORDER)
_DECISION_STEP_SET = {"persona_setup", "wiki_basic", "wiki_advanced", "session_continuity"}
_PROCEEDABLE_STEPS = {"wiki_basic", "wiki_advanced", "session_continuity"}
_DEBUG_KEYWORDS: set[str] = set()


def _safe_load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def contains_tutorial_debug_keyword(*texts: Any) -> bool:
    """배포 빌드에서는 디버그 우회 키워드를 비활성화한다."""
    if not _DEBUG_KEYWORDS:
        return False
    for raw in texts:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        compact = " ".join(text.split())
        if compact in _DEBUG_KEYWORDS:
            return True
    return False


def _safe_write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cleanup_runtime_tutorial_field() -> None:
    runtime_cfg = _safe_load_yaml(_RUNTIME_USER_CONFIG_PATH)
    if "tutorial" not in runtime_cfg:
        return
    runtime_cfg.pop("tutorial", None)
    _safe_write_yaml(_RUNTIME_USER_CONFIG_PATH, runtime_cfg)


def _load_tutorial_doc() -> dict[str, Any]:
    tutorial_cfg = _safe_load_yaml(_TUTORIAL_USER_PATH)
    if tutorial_cfg:
        _cleanup_runtime_tutorial_field()
        return tutorial_cfg

    runtime_cfg = _safe_load_yaml(_RUNTIME_USER_CONFIG_PATH)
    legacy = runtime_cfg.get("tutorial")
    if isinstance(legacy, dict):
        _safe_write_yaml(_TUTORIAL_USER_PATH, legacy)
        runtime_cfg.pop("tutorial", None)
        _safe_write_yaml(_RUNTIME_USER_CONFIG_PATH, runtime_cfg)
        return legacy
    return {}


def _save_tutorial_doc(tutorial: dict[str, Any]) -> None:
    _safe_write_yaml(_TUTORIAL_USER_PATH, tutorial)
    _cleanup_runtime_tutorial_field()


def _is_identity_name_set(identity_name: str) -> bool:
    name = str(identity_name or "").strip()
    if not name:
        return False
    return name != "이름 없음"


def _is_set(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool([item for item in value if str(item).strip()])
    return True


def _has_user_persona_override() -> bool:
    persona = _safe_load_yaml(_USER_PERSONA_PATH)
    if not persona:
        return False
    for key in ("voice", "fewshot", "warmth", "formality", "humor", "directness"):
        if _is_set(persona.get(key)):
            return True
    for key in ("traits", "quirks", "values"):
        if _is_set(persona.get(key)):
            return True
    return False


def has_user_persona_override() -> bool:
    """persona.user.yaml에 유효 오버라이드 값이 있는지 반환한다."""
    return _has_user_persona_override()


def _normalize_completed_steps(raw_steps: Any) -> list[str]:
    if not isinstance(raw_steps, list):
        return []
    seen = set()
    normalized: list[str] = []
    for step in raw_steps:
        name = str(step).strip()
        if name in _DONE_STEP_SET and name not in seen:
            normalized.append(name)
            seen.add(name)
    return normalized


def _normalize_skipped_steps(raw_steps: Any) -> list[str]:
    if not isinstance(raw_steps, list):
        return []
    seen = set()
    normalized: list[str] = []
    for step in raw_steps:
        name = str(step).strip()
        if name in _DONE_STEP_SET and name not in seen:
            normalized.append(name)
            seen.add(name)
    return normalized


def _normalize_skip_log(raw_log: Any) -> list[dict[str, str]]:
    if not isinstance(raw_log, list):
        return []
    rows: list[dict[str, str]] = []
    for item in raw_log:
        if not isinstance(item, dict):
            continue
        step = str(item.get("step", "")).strip()
        if step not in _DONE_STEP_SET:
            continue
        rows.append(
            {
                "step": step,
                "reason": str(item.get("reason", "") or ""),
                "source": str(item.get("source", "") or ""),
                "at": str(item.get("at", "") or ""),
            }
        )
    return rows


def _normalize_step_proceeded(raw: Any) -> dict[str, bool]:
    data = raw if isinstance(raw, dict) else {}
    return {
        "wiki_basic": bool(data.get("wiki_basic", False)),
        "wiki_advanced": bool(data.get("wiki_advanced", False)),
        "session_continuity": bool(data.get("session_continuity", False)),
    }


def _coerce_nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _summary_valid(text: str, min_chars: int = 20) -> bool:
    return len(str(text or "").strip()) >= min_chars


def _wiki_docs_dir_hint() -> str:
    try:
        from core.config.runtime_config import get_db_root_dir

        root_dir = str(get_db_root_dir() or "").strip()
        if root_dir:
            return str((Path(root_dir) / "docs").resolve())
    except Exception:
        pass

    runtime_cfg = _safe_load_yaml(_RUNTIME_USER_CONFIG_PATH)
    db_cfg = runtime_cfg.get("db", {}) if isinstance(runtime_cfg.get("db"), dict) else {}
    fallback_root = str(db_cfg.get("root_dir", "") or "").strip()
    if fallback_root:
        return str((Path(fallback_root) / "docs").resolve())

    return str((Path.home() / ".engram" / "docs").resolve())


def _normalize_wiki_basic_review(raw: Any) -> dict[str, Any]:
    review = raw if isinstance(raw, dict) else {}
    summary = str(review.get("understanding_summary", "") or "")
    summary_ok = _summary_valid(summary)
    normalized = {
        "report_identifier": str(review.get("report_identifier", "") or ""),
        "report_node_id": str(review.get("report_node_id", "") or ""),
        "report_title": str(review.get("report_title", "") or ""),
        "artifact_ok": bool(review.get("artifact_ok", False)),
        "user_confirmed": bool(review.get("user_confirmed", False)),
        "understanding_summary": summary,
        "summary_ok": bool(review.get("summary_ok", summary_ok)),
        "verified": bool(review.get("verified", False)),
        "checked_at": str(review.get("checked_at", "") or ""),
    }
    normalized["summary_ok"] = summary_ok
    normalized["verified"] = (
        normalized["artifact_ok"]
        and normalized["user_confirmed"]
        and normalized["summary_ok"]
    )
    return normalized


def _normalize_wiki_advanced_review(raw: Any) -> dict[str, Any]:
    review = raw if isinstance(raw, dict) else {}
    summary = str(review.get("instruction_summary", "") or "")
    summary_ok = _summary_valid(summary)
    normalized = {
        "project_identifier": str(review.get("project_identifier", "") or ""),
        "project_node_id": str(review.get("project_node_id", "") or ""),
        "project_title": str(review.get("project_title", "") or ""),
        "artifact_ok": bool(review.get("artifact_ok", False)),
        "user_confirmed": bool(review.get("user_confirmed", False)),
        "instruction_summary": summary,
        "summary_ok": bool(review.get("summary_ok", summary_ok)),
        "verified": bool(review.get("verified", False)),
        "checked_at": str(review.get("checked_at", "") or ""),
    }
    normalized["summary_ok"] = summary_ok
    normalized["verified"] = (
        normalized["artifact_ok"]
        and normalized["user_confirmed"]
        and normalized["summary_ok"]
    )
    return normalized


def _normalize_session_continuity_review(raw: Any) -> dict[str, Any]:
    review = raw if isinstance(raw, dict) else {}
    summary = str(review.get("continuity_summary", "") or "")
    summary_ok = _summary_valid(summary)
    normalized = {
        "memory_query": str(review.get("memory_query", "") or ""),
        "memory_hit": bool(review.get("memory_hit", False)),
        "user_confirmed": bool(review.get("user_confirmed", False)),
        "continuity_summary": summary,
        "summary_ok": bool(review.get("summary_ok", summary_ok)),
        "phase_ready": bool(review.get("phase_ready", False)),
        "current_session_saved": bool(review.get("current_session_saved", False)),
        "current_session_saved_at": str(review.get("current_session_saved_at", "") or ""),
        "saved_session_id": str(review.get("saved_session_id", "") or ""),
        "saved_scope_key": str(review.get("saved_scope_key", "") or ""),
        "awaiting_next_session_check": bool(review.get("awaiting_next_session_check", False)),
        "next_session_checked": bool(review.get("next_session_checked", False)),
        "next_session_checked_at": str(review.get("next_session_checked_at", "") or ""),
        "checked_session_id": str(review.get("checked_session_id", "") or ""),
        "verified": bool(review.get("verified", False)),
        "checked_at": str(review.get("checked_at", "") or ""),
    }
    normalized["summary_ok"] = summary_ok
    normalized["phase_ready"] = bool(normalized["awaiting_next_session_check"])
    normalized["verified"] = (
        normalized["phase_ready"]
        and normalized["memory_hit"]
        and normalized["user_confirmed"]
        and normalized["summary_ok"]
    )
    return normalized


def _next_step(done_steps_in_order: list[str]) -> str:
    done_set = set(done_steps_in_order)
    for step in _STEP_ORDER:
        if step not in done_set:
            return step
    return "completed"


def _effective_done_order(completed_steps: list[str], skipped_steps: list[str]) -> list[str]:
    done_set = set(completed_steps) | set(skipped_steps)
    return [step for step in _STEP_ORDER if step in done_set]


def _apply_progress_state(state: dict[str, Any]) -> dict[str, Any]:
    completed_steps = _normalize_completed_steps(state.get("completed_steps", []))
    skipped_steps = _normalize_skipped_steps(state.get("skipped_steps", []))
    effective_done = _effective_done_order(completed_steps, skipped_steps)
    next_step = _next_step(effective_done)

    if next_step == "completed":
        status = "completed"
    elif effective_done:
        status = "in_progress"
    else:
        status = "pending"

    state["completed_steps"] = completed_steps
    state["skipped_steps"] = skipped_steps
    state["current_step"] = next_step
    state["status"] = status
    return state


def _ensure_tutorial_doc(tutorial_raw: dict[str, Any]) -> dict[str, Any]:
    tutorial = tutorial_raw if isinstance(tutorial_raw, dict) else {}
    state_raw = tutorial.get("state")
    state = state_raw if isinstance(state_raw, dict) else {}

    version_raw = tutorial.get("version", 1)
    try:
        version = int(version_raw)
    except (TypeError, ValueError):
        version = 1

    tutorial["version"] = max(1, version)
    tutorial["continuity_test_enabled"] = bool(tutorial.get("continuity_test_enabled", True))

    normalized_state: dict[str, Any] = {
        "status": str(state.get("status", "pending")).strip() or "pending",
        "current_step": str(state.get("current_step", "persona_setup")).strip() or "persona_setup",
        "completed_steps": _normalize_completed_steps(state.get("completed_steps", [])),
        "skipped_steps": _normalize_skipped_steps(state.get("skipped_steps", [])),
        "skip_log": _normalize_skip_log(state.get("skip_log", [])),
        "consecutive_skip_count": _coerce_nonnegative_int(state.get("consecutive_skip_count", 0)),
        "last_completed_at": str(state.get("last_completed_at", "") or ""),
        "last_mark_source": str(state.get("last_mark_source", "") or ""),
        "last_resumed_step": str(state.get("last_resumed_step", "") or ""),
        "last_resumed_at": str(state.get("last_resumed_at", "") or ""),
        "started_at": str(state.get("started_at", "") or ""),
        "reset_requested": bool(state.get("reset_requested", False)),
        "reset_reason": str(state.get("reset_reason", "") or ""),
        "reset_at": str(state.get("reset_at", "") or ""),
        "last_auto_update_at": str(state.get("last_auto_update_at", "") or ""),
        "step_proceeded": _normalize_step_proceeded(state.get("step_proceeded")),
        "wiki_basic_review": _normalize_wiki_basic_review(state.get("wiki_basic_review")),
        "wiki_advanced_review": _normalize_wiki_advanced_review(state.get("wiki_advanced_review")),
        "session_continuity_review": _normalize_session_continuity_review(state.get("session_continuity_review")),
    }
    normalized_state = _apply_progress_state(normalized_state)
    tutorial["state"] = normalized_state
    return tutorial


def _recalculate_state(
    tutorial: dict[str, Any],
    *,
    identity_name: str = "",
    persona_override_exists: bool | None = None,
) -> tuple[dict[str, Any], bool]:
    state = tutorial["state"]
    completed_steps = _normalize_completed_steps(state.get("completed_steps", []))
    skipped_steps = _normalize_skipped_steps(state.get("skipped_steps", []))
    changed = False

    # persona_setup은 자동 완료하지 않는다.
    # 사용자가 설정 창 저장(또는 명시적 complete_tutorial_step)으로 완료해야 한다.
    _ = identity_name
    _ = persona_override_exists

    prev_completed = _normalize_completed_steps(state.get("completed_steps", []))
    prev_skipped = _normalize_skipped_steps(state.get("skipped_steps", []))
    prev_current = str(state.get("current_step", "")).strip()
    prev_status = str(state.get("status", "")).strip()

    state["completed_steps"] = completed_steps
    state["skipped_steps"] = skipped_steps
    state = _apply_progress_state(state)

    if prev_completed != state["completed_steps"]:
        changed = True
    if prev_skipped != state["skipped_steps"]:
        changed = True
    if prev_current != state.get("current_step", ""):
        changed = True
    if prev_status != state.get("status", ""):
        changed = True
    if changed:
        state["last_auto_update_at"] = _now_iso()
        if state.get("current_step") != "persona_setup":
            state["reset_requested"] = False

    tutorial["state"] = state
    return tutorial, changed


def refresh_tutorial_progress(
    *,
    identity_name: str = "",
    persona_override_exists: bool | None = None,
) -> dict[str, Any]:
    tutorial = _ensure_tutorial_doc(_load_tutorial_doc())
    tutorial, changed = _recalculate_state(
        tutorial,
        identity_name=identity_name,
        persona_override_exists=persona_override_exists,
    )
    if changed:
        _save_tutorial_doc(tutorial)
    return tutorial


def reset_tutorial_state(reason: str = "manual_from_settings") -> dict[str, Any]:
    tutorial = _ensure_tutorial_doc(_load_tutorial_doc())
    state_raw = tutorial.get("state")
    prev_state = state_raw if isinstance(state_raw, dict) else {}

    version_raw = tutorial.get("version", 1)
    try:
        tutorial_version = int(version_raw)
    except (TypeError, ValueError):
        tutorial_version = 1
    tutorial["version"] = max(1, tutorial_version)
    tutorial["continuity_test_enabled"] = bool(tutorial.get("continuity_test_enabled", True))
    if prev_state:
        tutorial["last_state_before_reset"] = prev_state
    tutorial["state"] = {
        "status": "pending",
        "current_step": "persona_setup",
        "completed_steps": [],
        "skipped_steps": [],
        "skip_log": [],
        "consecutive_skip_count": 0,
        "last_completed_at": "",
        "last_mark_source": "",
        "last_resumed_step": "",
        "last_resumed_at": "",
        "started_at": "",
        "reset_requested": True,
        "reset_reason": str(reason or "manual_reset"),
        "reset_at": _now_iso(),
        "last_auto_update_at": "",
        "step_proceeded": _normalize_step_proceeded({}),
        "wiki_basic_review": _normalize_wiki_basic_review({}),
        "wiki_advanced_review": _normalize_wiki_advanced_review({}),
        "session_continuity_review": _normalize_session_continuity_review({}),
    }
    tutorial = _ensure_tutorial_doc(tutorial)
    _save_tutorial_doc(tutorial)
    return tutorial


def complete_tutorial_step(step: str, *, source: str = "manual") -> dict[str, Any]:
    target_step = str(step or "").strip()
    if target_step not in _DONE_STEP_SET:
        raise ValueError(f"invalid tutorial step: {target_step}")

    tutorial = _ensure_tutorial_doc(_load_tutorial_doc())
    state = tutorial["state"]

    completed = _normalize_completed_steps(state.get("completed_steps", []))
    skipped = _normalize_skipped_steps(state.get("skipped_steps", []))
    if target_step not in completed:
        completed.append(target_step)
    if target_step in skipped:
        skipped.remove(target_step)

    state["completed_steps"] = completed
    state["skipped_steps"] = skipped
    step_proceeded = _normalize_step_proceeded(state.get("step_proceeded"))
    if target_step in step_proceeded:
        step_proceeded[target_step] = False
    state["step_proceeded"] = step_proceeded
    state["consecutive_skip_count"] = 0
    state["last_completed_at"] = _now_iso()
    state["last_mark_source"] = str(source or "manual")
    state["reset_requested"] = False
    state = _apply_progress_state(state)
    tutorial["state"] = state

    _save_tutorial_doc(tutorial)
    return tutorial


def skip_tutorial_step(step: str, *, reason: str = "", source: str = "manual_skip") -> dict[str, Any]:
    """단계를 건너뛴다. skipped_steps에 기록하고 다음 단계로 진행한다."""
    target_step = str(step or "").strip()
    if target_step not in _DONE_STEP_SET:
        raise ValueError(f"invalid tutorial step: {target_step}")

    tutorial = _ensure_tutorial_doc(_load_tutorial_doc())
    state = tutorial["state"]

    skipped = _normalize_skipped_steps(state.get("skipped_steps", []))
    if target_step not in skipped:
        skipped.append(target_step)
    state["skipped_steps"] = skipped
    step_proceeded = _normalize_step_proceeded(state.get("step_proceeded"))
    if target_step in step_proceeded:
        step_proceeded[target_step] = False
    state["step_proceeded"] = step_proceeded
    state["consecutive_skip_count"] = _coerce_nonnegative_int(
        state.get("consecutive_skip_count", 0)
    ) + 1

    skip_log = _normalize_skip_log(state.get("skip_log", []))
    skip_log.append(
        {
            "step": target_step,
            "reason": str(reason or "").strip(),
            "source": str(source or "manual_skip"),
            "at": _now_iso(),
        }
    )
    state["skip_log"] = skip_log[-30:]
    state["last_mark_source"] = str(source or "manual_skip")
    state["reset_requested"] = False
    state = _apply_progress_state(state)
    tutorial["state"] = state

    _save_tutorial_doc(tutorial)
    return tutorial


def proceed_tutorial_step(step: str, *, source: str = "manual_proceed") -> dict[str, Any]:
    """단계 진행을 명시한다. (완료 처리 아님, 실행 가이드 모드 전환용)"""
    target_step = str(step or "").strip()
    if target_step not in _PROCEEDABLE_STEPS:
        raise ValueError(f"step does not support proceed mode: {target_step}")

    tutorial = _ensure_tutorial_doc(_load_tutorial_doc())
    state = tutorial["state"]
    current_step = str(state.get("current_step", "")).strip()
    if current_step != target_step:
        raise ValueError(f"step is not current: {target_step}")

    step_proceeded = _normalize_step_proceeded(state.get("step_proceeded"))
    step_proceeded[target_step] = True
    state["step_proceeded"] = step_proceeded
    state["consecutive_skip_count"] = 0
    state["last_mark_source"] = str(source or "manual_proceed")
    state["reset_requested"] = False
    tutorial["state"] = state

    _save_tutorial_doc(tutorial)
    return tutorial


def mark_session_continuity_saved(
    *,
    source: str = "close_session",
    session_id: int | str | None = None,
    scope_key: str = "",
) -> dict[str, Any]:
    """4단계 1차 실습(현재 세션 요약 저장)을 완료로 표시한다.

    - current_step=session_continuity
    - step_proceeded.session_continuity=True
    인 경우에만 상태를 갱신한다.
    """
    tutorial = _ensure_tutorial_doc(_load_tutorial_doc())
    state = tutorial["state"]
    current_step = str(state.get("current_step", "")).strip()
    step_proceeded = _normalize_step_proceeded(state.get("step_proceeded"))

    if current_step != "session_continuity" or not step_proceeded["session_continuity"]:
        return tutorial

    review = _normalize_session_continuity_review(state.get("session_continuity_review"))
    review.update(
        {
            "memory_query": "",
            "memory_hit": False,
            "user_confirmed": False,
            "continuity_summary": "",
            "summary_ok": False,
            "phase_ready": True,
            "current_session_saved": True,
            "current_session_saved_at": _now_iso(),
            "saved_session_id": str(session_id or "").strip(),
            "saved_scope_key": str(scope_key or "").strip(),
            "awaiting_next_session_check": True,
            "next_session_checked": False,
            "next_session_checked_at": "",
            "checked_session_id": "",
            "verified": False,
            "checked_at": "",
        }
    )
    state["session_continuity_review"] = review
    state["last_mark_source"] = str(source or "close_session")
    state["reset_requested"] = False
    tutorial["state"] = state
    _save_tutorial_doc(tutorial)
    return tutorial


def resume_tutorial_step(step: str = "", *, source: str = "manual_resume") -> dict[str, Any]:
    """건너뛴 단계를 재개한다. step이 비어있으면 가장 앞의 skipped step을 재개."""
    tutorial = _ensure_tutorial_doc(_load_tutorial_doc())
    state = tutorial["state"]

    skipped = _normalize_skipped_steps(state.get("skipped_steps", []))
    if not skipped:
        raise ValueError("no skipped step to resume")

    target = str(step or "").strip()
    if not target:
        for candidate in _STEP_ORDER:
            if candidate in skipped:
                target = candidate
                break
    if target not in _DONE_STEP_SET:
        raise ValueError(f"invalid tutorial step: {target}")
    if target not in skipped:
        raise ValueError(f"step is not skipped: {target}")

    skipped.remove(target)
    state["skipped_steps"] = skipped
    completed = _normalize_completed_steps(state.get("completed_steps", []))
    if target in completed:
        state = _apply_progress_state(state)
    else:
        state["current_step"] = target
        state["status"] = "in_progress"
    step_proceeded = _normalize_step_proceeded(state.get("step_proceeded"))
    if target in step_proceeded:
        step_proceeded[target] = False
    state["step_proceeded"] = step_proceeded
    state["consecutive_skip_count"] = 0
    state["last_resumed_step"] = target
    state["last_resumed_at"] = _now_iso()
    state["last_mark_source"] = str(source or "manual_resume")
    tutorial["state"] = state

    _save_tutorial_doc(tutorial)
    return tutorial


def verify_wiki_basic_step(
    *,
    report_identifier: str,
    report_node_id: str,
    report_title: str,
    artifact_ok: bool,
    user_confirmed: bool,
    understanding_summary: str,
    source: str = "wiki_basic_verifier",
) -> dict[str, Any]:
    """wiki_basic 단계의 3중 조건을 기록/판정한다.

    완료 조건:
    1) 산출물 존재(artifact_ok)
    2) 사용자 확인(user_confirmed=True)
    3) 이해 요약 텍스트(최소 글자 수)
    """
    tutorial = _ensure_tutorial_doc(_load_tutorial_doc())
    state = tutorial["state"]
    review = _normalize_wiki_basic_review(state.get("wiki_basic_review"))

    summary_text = str(understanding_summary or "").strip()
    summary_ok = _summary_valid(summary_text)

    review.update(
        {
            "report_identifier": str(report_identifier or "").strip(),
            "report_node_id": str(report_node_id or "").strip(),
            "report_title": str(report_title or "").strip(),
            "artifact_ok": bool(artifact_ok),
            "user_confirmed": bool(user_confirmed),
            "understanding_summary": summary_text,
            "summary_ok": summary_ok,
            "checked_at": _now_iso(),
        }
    )
    review["verified"] = (
        review["artifact_ok"] and review["user_confirmed"] and review["summary_ok"]
    )
    state["wiki_basic_review"] = review

    if review["verified"]:
        completed = _normalize_completed_steps(state.get("completed_steps", []))
        skipped = _normalize_skipped_steps(state.get("skipped_steps", []))
        step_proceeded = _normalize_step_proceeded(state.get("step_proceeded"))
        if "wiki_basic" not in completed:
            completed.append("wiki_basic")
        if "wiki_basic" in skipped:
            skipped.remove("wiki_basic")
        step_proceeded["wiki_basic"] = False
        state["completed_steps"] = completed
        state["skipped_steps"] = skipped
        state["step_proceeded"] = step_proceeded
        state["consecutive_skip_count"] = 0
        state["last_completed_at"] = _now_iso()
        state["last_mark_source"] = str(source or "wiki_basic_verifier")
        state["reset_requested"] = False
        state = _apply_progress_state(state)

    tutorial["state"] = state
    _save_tutorial_doc(tutorial)
    return {
        "tutorial": tutorial,
        "verified": bool(review["verified"]),
        "checks": {
            "artifact_ok": bool(review["artifact_ok"]),
            "user_confirmed": bool(review["user_confirmed"]),
            "summary_ok": bool(review["summary_ok"]),
        },
    }


def verify_wiki_advanced_step(
    *,
    project_identifier: str,
    project_node_id: str,
    project_title: str,
    artifact_ok: bool,
    user_confirmed: bool,
    instruction_summary: str,
    source: str = "wiki_advanced_verifier",
) -> dict[str, Any]:
    """wiki_advanced 단계의 3중 조건을 기록/판정한다.

    완료 조건:
    1) 프로젝트 위키 산출물 존재(artifact_ok)
    2) 사용자 확인(user_confirmed=True)
    3) 지시 요약 텍스트(최소 글자 수)
    """
    tutorial = _ensure_tutorial_doc(_load_tutorial_doc())
    state = tutorial["state"]
    review = _normalize_wiki_advanced_review(state.get("wiki_advanced_review"))

    summary_text = str(instruction_summary or "").strip()
    summary_ok = _summary_valid(summary_text)

    review.update(
        {
            "project_identifier": str(project_identifier or "").strip(),
            "project_node_id": str(project_node_id or "").strip(),
            "project_title": str(project_title or "").strip(),
            "artifact_ok": bool(artifact_ok),
            "user_confirmed": bool(user_confirmed),
            "instruction_summary": summary_text,
            "summary_ok": summary_ok,
            "checked_at": _now_iso(),
        }
    )
    review["verified"] = (
        review["artifact_ok"] and review["user_confirmed"] and review["summary_ok"]
    )
    state["wiki_advanced_review"] = review

    if review["verified"]:
        completed = _normalize_completed_steps(state.get("completed_steps", []))
        skipped = _normalize_skipped_steps(state.get("skipped_steps", []))
        step_proceeded = _normalize_step_proceeded(state.get("step_proceeded"))
        if "wiki_advanced" not in completed:
            completed.append("wiki_advanced")
        if "wiki_advanced" in skipped:
            skipped.remove("wiki_advanced")
        step_proceeded["wiki_advanced"] = False
        state["completed_steps"] = completed
        state["skipped_steps"] = skipped
        state["step_proceeded"] = step_proceeded
        state["consecutive_skip_count"] = 0
        state["last_completed_at"] = _now_iso()
        state["last_mark_source"] = str(source or "wiki_advanced_verifier")
        state["reset_requested"] = False
        state = _apply_progress_state(state)

    tutorial["state"] = state
    _save_tutorial_doc(tutorial)
    return {
        "tutorial": tutorial,
        "verified": bool(review["verified"]),
        "checks": {
            "artifact_ok": bool(review["artifact_ok"]),
            "user_confirmed": bool(review["user_confirmed"]),
            "summary_ok": bool(review["summary_ok"]),
        },
    }


def verify_session_continuity_step(
    *,
    memory_query: str,
    memory_hit: bool,
    user_confirmed: bool,
    continuity_summary: str,
    checked_session_id: str = "",
    source: str = "session_continuity_verifier",
) -> dict[str, Any]:
    """session_continuity 단계의 3중 조건을 기록/판정한다.

    완료 조건:
    1) 튜토리얼 세션 요약이 memory에 존재(memory_hit)
    2) 사용자가 연속성 확인(user_confirmed=True)
    3) 연속성 체감 요약 텍스트(최소 글자 수)
    """
    tutorial = _ensure_tutorial_doc(_load_tutorial_doc())
    state = tutorial["state"]
    review = _normalize_session_continuity_review(state.get("session_continuity_review"))
    phase_ready = bool(review.get("awaiting_next_session_check", False))

    summary_text = str(continuity_summary or "").strip()
    summary_ok = _summary_valid(summary_text)

    review.update(
        {
            "memory_query": str(memory_query or "").strip(),
            "memory_hit": bool(memory_hit),
            "user_confirmed": bool(user_confirmed),
            "continuity_summary": summary_text,
            "summary_ok": summary_ok,
            "phase_ready": phase_ready,
            "next_session_checked": True,
            "next_session_checked_at": _now_iso(),
            "checked_session_id": str(checked_session_id or "").strip(),
            "checked_at": _now_iso(),
        }
    )
    review["verified"] = (
        review["phase_ready"] and
        review["memory_hit"] and review["user_confirmed"] and review["summary_ok"]
    )
    phase_ready_check = bool(review["phase_ready"])
    state["session_continuity_review"] = review

    if review["verified"]:
        completed = _normalize_completed_steps(state.get("completed_steps", []))
        skipped = _normalize_skipped_steps(state.get("skipped_steps", []))
        step_proceeded = _normalize_step_proceeded(state.get("step_proceeded"))
        if "session_continuity" not in completed:
            completed.append("session_continuity")
        if "session_continuity" in skipped:
            skipped.remove("session_continuity")
        step_proceeded["session_continuity"] = False
        state["completed_steps"] = completed
        state["skipped_steps"] = skipped
        state["step_proceeded"] = step_proceeded
        state["consecutive_skip_count"] = 0
        state["last_completed_at"] = _now_iso()
        state["last_mark_source"] = str(source or "session_continuity_verifier")
        state["reset_requested"] = False
        review["awaiting_next_session_check"] = False
        review["phase_ready"] = False
        state["session_continuity_review"] = review
        state = _apply_progress_state(state)

    tutorial["state"] = state
    _save_tutorial_doc(tutorial)
    return {
        "tutorial": tutorial,
        "verified": bool(review["verified"]),
        "checks": {
            "phase_ready": phase_ready_check,
            "memory_hit": bool(review["memory_hit"]),
            "user_confirmed": bool(review["user_confirmed"]),
            "summary_ok": bool(review["summary_ok"]),
        },
    }


def build_tutorial_runtime_payload(tutorial: dict[str, Any]) -> dict[str, Any]:
    """현재 튜토리얼 상태를 코드 기반 상호작용 페이로드로 변환한다."""
    tutorial_doc = _ensure_tutorial_doc(tutorial)
    state = tutorial_doc.get("state", {}) if isinstance(tutorial_doc, dict) else {}
    current_step = str(state.get("current_step", "")).strip()
    status = str(state.get("status", "pending")).strip() or "pending"
    completed_steps = _normalize_completed_steps(state.get("completed_steps", []))
    skipped_steps = _normalize_skipped_steps(state.get("skipped_steps", []))
    step_proceeded = _normalize_step_proceeded(state.get("step_proceeded"))
    progress_count = min(_STEP_TOTAL, len(set(completed_steps) | set(skipped_steps)))
    docs_dir_hint = _wiki_docs_dir_hint()

    if status == "completed" or current_step == "completed":
        return {
            "mode": "completed",
            "status": "completed",
            "current_step": "completed",
            "step_number": _STEP_TOTAL,
            "step_total": _STEP_TOTAL,
            "progress": {"done": _STEP_TOTAL, "total": _STEP_TOTAL},
            "prompt_to_user": "튜토리얼이 모두 완료됐어요. 다음 작업으로 넘어가면 됩니다.",
            "choices": [],
        }

    if current_step == "persona_setup":
        return {
            "mode": "decision",
            "status": status,
            "current_step": "persona_setup",
            "step_number": _STEP_INDEX["persona_setup"],
            "step_total": _STEP_TOTAL,
            "progress": {"done": progress_count, "total": _STEP_TOTAL},
            "prompt_to_user": (
                "1단계 페르소나 설정입니다. 설정 창 > 페르소나 탭에서 말투/성격/가치관을 저장하면 됩니다. "
                "진행할까요, 보류(스킵)할까요?"
            ),
            "choices": [
                {"id": "proceed", "label": "지금 1단계 진행"},
                {"id": "skip", "label": "이번 단계 보류(스킵)"},
            ],
            "await_user_choice": True,
            "choice_question": "선택지: 1) 지금 1단계 진행 2) 이번 단계 보류(스킵)",
            "next_tools": ["engram_skip_tutorial_step", "engram_complete_tutorial_step"],
        }

    if current_step == "wiki_basic":
        if not step_proceeded["wiki_basic"]:
            return {
                "mode": "decision",
                "status": status,
                "current_step": "wiki_basic",
                "step_number": _STEP_INDEX["wiki_basic"],
                "step_total": _STEP_TOTAL,
                "progress": {"done": progress_count, "total": _STEP_TOTAL},
                "title": "튜토리얼 2단계: 위키 기초 실습",
                "prompt_to_user": (
                    "이 단계는 engram로 자료조사와 보고서 작성을 자동화하고, "
                    "결과를 위키로 관리하는 흐름을 익히는 실습입니다. "
                    "키워드 기반 조사 요청 → 보고서 노트 생성 → 위키에서 결과 확인까지 경험합니다. "
                    "지금 2단계를 진행할까요, 보류(스킵)할까요?"
                ),
                "choices": [
                    {"id": "proceed", "label": "지금 2단계 진행"},
                    {"id": "skip", "label": "이번 단계 보류(스킵)"},
                ],
                "await_user_choice": True,
                "choice_question": "선택지: 1) 지금 2단계 진행 2) 이번 단계 보류(스킵)",
                "next_tools": ["engram_proceed_tutorial_step", "engram_skip_tutorial_step"],
            }
        return {
            "mode": "input",
            "status": status,
            "current_step": "wiki_basic",
            "step_number": _STEP_INDEX["wiki_basic"],
            "step_total": _STEP_TOTAL,
            "progress": {"done": progress_count, "total": _STEP_TOTAL},
            "title": "튜토리얼 2단계: 위키 기초 실습",
            "prompt_to_user": (
                "좋아요. 이제 실제 실습입니다. "
                "아래 문장을 engram에 직접 입력하면 자료조사와 보고서 정리가 자동으로 진행됩니다. "
                "완료 후에는 생성된 문서를 절대경로 위키 폴더에서 바로 확인해 주세요."
            ),
            "choices": [],
            "input_required": True,
            "input_example": "llm wiki 에 대해 조사하고 정리해줘",
            "input_question": (
                "아래 문장을 engram에 직접 입력해 주세요:\n"
                "\"llm wiki 에 대해 조사하고 정리해줘\"\n"
                f"입력 후 결과물은 이 절대경로에서 확인해 주세요: {docs_dir_hint}"
            ),
            "wiki_docs_dir": docs_dir_hint,
            "next_tools": ["engram_verify_tutorial_wiki_basic"],
        }

    if current_step == "wiki_advanced":
        if not step_proceeded["wiki_advanced"]:
            return {
                "mode": "decision",
                "status": status,
                "current_step": "wiki_advanced",
                "step_number": _STEP_INDEX["wiki_advanced"],
                "step_total": _STEP_TOTAL,
                "progress": {"done": progress_count, "total": _STEP_TOTAL},
                "title": "튜토리얼 3단계: 위키 심화 실습",
                "prompt_to_user": (
                    "이 단계는 조사 결과를 프로젝트 위키 구조로 확장하는 심화 실습입니다. "
                    "engram에 위키 작업을 지시해 핵심 개념/프로젝트 요약/링크를 체계적으로 정리하는 방법을 익힙니다. "
                    "지금 3단계를 진행할까요, 보류(스킵)할까요?"
                ),
                "choices": [
                    {"id": "proceed", "label": "지금 3단계 진행"},
                    {"id": "skip", "label": "이번 단계 보류(스킵)"},
                ],
                "await_user_choice": True,
                "choice_question": "선택지: 1) 지금 3단계 진행 2) 이번 단계 보류(스킵)",
                "next_tools": ["engram_proceed_tutorial_step", "engram_skip_tutorial_step"],
            }
        return {
            "mode": "input",
            "status": status,
            "current_step": "wiki_advanced",
            "step_number": _STEP_INDEX["wiki_advanced"],
            "step_total": _STEP_TOTAL,
            "progress": {"done": progress_count, "total": _STEP_TOTAL},
            "title": "튜토리얼 3단계: 위키 심화 실습",
            "prompt_to_user": (
                "좋아요. 이제 심화 실습을 진행합니다. "
                "engram 프로젝트 기준으로 위키 구성을 지시하고, 생성 결과를 절대경로 위키 폴더에서 확인하세요."
            ),
            "choices": [],
            "input_required": True,
            "input_example": "engram 프로젝트 위키를 간략히 구성해줘",
            "input_question": (
                "아래와 같이 engram에 지시해 주세요:\n"
                "\"engram 프로젝트 위키를 간략히 구성해줘\"\n"
                f"입력 후 결과물은 이 절대경로에서 확인해 주세요: {docs_dir_hint}"
            ),
            "wiki_docs_dir": docs_dir_hint,
            "next_tools": ["engram_verify_tutorial_wiki_advanced"],
        }

    if current_step == "session_continuity":
        if not step_proceeded["session_continuity"]:
            return {
                "mode": "decision",
                "status": status,
                "current_step": "session_continuity",
                "step_number": _STEP_INDEX["session_continuity"],
                "step_total": _STEP_TOTAL,
                "progress": {"done": progress_count, "total": _STEP_TOTAL},
                "title": "튜토리얼 4단계: 세션 연속성 실습",
                "prompt_to_user": (
                    "이 단계는 세션 내용을 메모리에 남기고, 다음 세션에서 이어서 회상하는 흐름을 익히는 실습입니다. "
                    "진행을 선택하면 먼저 \"세션 내용 정리해서 메모리에 저장해줘\"를 직접 입력해야 합니다. "
                    "지금 4단계를 진행할까요, 보류(스킵)할까요?"
                ),
                "choices": [
                    {"id": "proceed", "label": "지금 4단계 진행"},
                    {"id": "skip", "label": "이번 단계 보류(스킵)"},
                ],
                "await_user_choice": True,
                "choice_question": "선택지: 1) 지금 4단계 진행 2) 이번 단계 보류(스킵)",
                "first_input_example": "세션 내용 정리해서 메모리에 저장해줘",
                "first_input_question": (
                    "진행을 선택하면 먼저 아래 문장을 engram에 직접 입력해 주세요:\n"
                    "\"세션 내용 정리해서 메모리에 저장해줘\"\n"
                    "주의: 이 입력만으로는 완료되지 않으며, "
                    "반드시 현재 세션을 종료해야 메모리 연속성 실습이 확정됩니다."
                ),
                "next_tools": ["engram_proceed_tutorial_step", "engram_skip_tutorial_step"],
            }
        review = _normalize_session_continuity_review(state.get("session_continuity_review"))
        if bool(review.get("awaiting_next_session_check", False)):
            return {
                "mode": "input",
                "status": status,
                "current_step": "session_continuity",
                "step_number": _STEP_INDEX["session_continuity"],
                "step_total": _STEP_TOTAL,
                "progress": {"done": progress_count, "total": _STEP_TOTAL},
                "title": "튜토리얼 4단계: 세션 연속성 실습",
                "phase": "next_session_recall",
                "prompt_to_user": (
                    "좋아요. 1차 저장은 끝났습니다. "
                    "이제 새 세션에서 이전 세션 회상을 확인하는 2차 실습을 진행해 주세요."
                ),
                "choices": [],
                "input_required": True,
                "input_example": "이전세션에 어떤작업을 했는지 알려줘",
                "input_question": (
                    "새 세션을 열었다면 아래 문장을 engram에 입력해 회상을 확인해 주세요:\n"
                    "\"이전세션에 어떤작업을 했는지 알려줘\"\n"
                    "회상 결과를 확인한 뒤에 4단계 검증을 진행합니다."
                ),
                "current_session_prompt": "세션 내용 정리해서 메모리에 저장해줘",
                "next_session_prompt": "이전세션에 어떤작업을 했는지 알려줘",
                "next_tools": ["engram_verify_tutorial_session_continuity"],
            }
        return {
            "mode": "input",
            "status": status,
            "current_step": "session_continuity",
            "step_number": _STEP_INDEX["session_continuity"],
            "step_total": _STEP_TOTAL,
            "progress": {"done": progress_count, "total": _STEP_TOTAL},
            "title": "튜토리얼 4단계: 세션 연속성 실습",
            "phase": "save_and_close",
            "prompt_to_user": (
                "좋아요. 이제 마지막 실습 1차입니다. "
                "현재 세션을 메모리에 저장한 뒤, 이 세션을 닫고 새 세션으로 넘어갑니다. "
                "세션을 닫지 않으면 연속성 검증이 완료되지 않습니다."
            ),
            "choices": [],
            "input_required": True,
            "input_example": "세션 내용 정리해서 메모리에 저장해줘",
            "input_question": (
                "아래 문장을 engram에 직접 입력해 주세요:\n"
                "\"세션 내용 정리해서 메모리에 저장해줘\"\n"
                "저장 완료 후 현재 세션창을 반드시 닫고 새 세션을 열어 주세요.\n"
                "세션 종료를 명시적으로 하지 않으면 이번 내용은 연속성 실습의 메모리로 확정되지 않습니다."
            ),
            "current_session_prompt": "세션 내용 정리해서 메모리에 저장해줘",
            "next_session_prompt": "이전세션에 어떤작업을 했는지 알려줘",
            "next_tools": ["engram_close_session"],
        }

    return {
        "mode": "action",
        "status": status,
        "current_step": current_step or "unknown",
        "step_number": _STEP_INDEX.get(current_step, 0),
        "step_total": _STEP_TOTAL,
        "progress": {"done": progress_count, "total": _STEP_TOTAL},
        "prompt_to_user": "현재 튜토리얼 단계를 계속 진행하세요.",
        "choices": [],
    }


def get_tutorial_runtime(identity_name: str = "") -> dict[str, Any]:
    """자동 보정된 튜토리얼 상태를 코드 기반 상호작용 페이로드로 반환한다."""
    tutorial = get_tutorial_status(identity_name=identity_name)
    payload = build_tutorial_runtime_payload(tutorial)
    if payload.get("mode") == "decision":
        payload["decision_mode"] = "proceed_or_skip"
    return payload


def get_tutorial_status(identity_name: str = "") -> dict[str, Any]:
    """튜토리얼 상태를 읽어 보정 후 반환한다."""
    return refresh_tutorial_progress(identity_name=identity_name)

