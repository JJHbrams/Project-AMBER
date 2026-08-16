from __future__ import annotations

import copy
import json
import re
from typing import Any, Iterable

from core.context.guard_execution import normalize_action_metadata


TRIGGER_KEYWORDS: dict[str, list[str]] = {
    "wiki": [
        "wiki", "문서", "노트", "작성", "기록", "저장", "vault",
        "kg_add", "kg_update", "kg_read", "위키", "정리",
    ],
    "code": [
        "코드", "수정", "구현", "버그", "디버깅", "리팩토링", "파일",
        "함수", "클래스", "모듈", "import", "fix", "refactor", "작성",
        "빌드", "테스트",
    ],
    "git": [
        "git", "커밋", "commit", "브랜치", "branch", "push", "merge",
        "pr", "풀리퀘", "rebase", "checkout",
    ],
    "reflection": [
        "reflect", "/reflect", "반성", "세션", "close_session",
        "피드백", "종료", "정리", "끝", "수고",
    ],
}

VALID_ENFORCEMENT_LEVELS = {"advisory", "workflow", "blocking"}
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")


def _normalize_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        parsed: Any
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = [part.strip() for part in value.split(",") if part.strip()]
        value = parsed
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def normalize_enforcement_level(
    value: Any,
    workflow_skill_id: str = "",
    guard_id: str = "",
) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in VALID_ENFORCEMENT_LEVELS:
        return normalized
    if str(guard_id or "").strip():
        return "blocking"
    if str(workflow_skill_id or "").strip():
        return "workflow"
    return "advisory"


def legacy_trigger_data_from_trigger_type(trigger_type: Any) -> dict[str, Any]:
    normalized = str(trigger_type or "").strip().lower()
    if not normalized or normalized == "always":
        return {"match": "always"}
    return {"legacy_trigger_types": [normalized]}


def normalize_trigger_data(value: Any, trigger_type: Any = "always") -> dict[str, Any]:
    parsed = value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            parsed = {}
        else:
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = {}

    if not isinstance(parsed, dict) or not parsed:
        return legacy_trigger_data_from_trigger_type(trigger_type)

    result: dict[str, Any] = {}
    match_mode = str(parsed.get("match") or "").strip().lower()
    if match_mode == "always":
        result["match"] = "always"

    list_fields = (
        "actions_any",
        "action_keywords_any",
        "action_modes_any",
        "action_tags_any",
        "query_keywords_any",
        "callers",
        "scope_keys_any",
        "scope_prefixes_any",
        "project_keys_any",
        "legacy_trigger_types",
    )
    for field in list_fields:
        items = normalize_string_list(parsed.get(field))
        if items:
            result[field] = items

    render_in_prompt = _normalize_optional_bool(parsed.get("render_in_prompt"))
    if render_in_prompt is not None:
        result["render_in_prompt"] = render_in_prompt

    if "legacy_trigger_types" not in result:
        normalized_trigger = str(trigger_type or "").strip().lower()
        if normalized_trigger and normalized_trigger != "always":
            result["legacy_trigger_types"] = [normalized_trigger]

    return result


def coerce_directive_record(raw: dict[str, Any]) -> dict[str, Any]:
    directive = dict(raw)
    workflow_skill_id = str(directive.get("workflow_skill_id") or "").strip()
    guard_id = str(directive.get("guard_id") or "").strip()
    try:
        priority = int(directive.get("priority", 0))
    except (TypeError, ValueError):
        priority = 0

    directive["key"] = str(directive.get("key") or "").strip()
    directive["content"] = str(directive.get("content") or "")
    directive["source"] = str(directive.get("source") or "unknown").strip() or "unknown"
    directive["scope"] = str(directive.get("scope") or "all").strip() or "all"
    directive["priority"] = priority
    directive["active"] = 1 if bool(directive.get("active", 1)) else 0
    directive["trigger_type"] = str(directive.get("trigger_type") or "always").strip().lower() or "always"
    directive["workflow_skill_id"] = workflow_skill_id
    directive["guard_id"] = guard_id
    directive["enforcement_level"] = normalize_enforcement_level(
        directive.get("enforcement_level"),
        workflow_skill_id=workflow_skill_id,
        guard_id=guard_id,
    )
    directive["trigger_data"] = normalize_trigger_data(
        directive.get("trigger_data"),
        directive["trigger_type"],
    )
    directive["legacy_migration_markers"] = normalize_string_list(
        directive.get("legacy_migration_markers")
    )
    return directive


def active_legacy_triggers(user_query: str = "", action: str = "") -> set[str]:
    combined = " ".join(part for part in (user_query, action) if part).lower()
    if not combined:
        return set()
    active = set()
    for trigger, keywords in TRIGGER_KEYWORDS.items():
        if any(keyword in combined for keyword in keywords):
            active.add(trigger)
    return active


def _extract_tokens(text: str) -> set[str]:
    if not text:
        return set()
    return {token.lower() for token in _TOKEN_RE.findall(text.lower()) if token}


def _scope_matches(scope: str, caller: str) -> bool:
    return scope == "all" or scope == caller


def _directive_sort_key(directive: dict[str, Any]) -> tuple[int, str, str]:
    return (-int(directive.get("priority", 0)), str(directive.get("created_at") or ""), str(directive.get("key") or ""))


def _string_list_matches_prefix(prefixes: list[str], value: str) -> bool:
    if not prefixes:
        return True
    if not value:
        return False
    return any(value.startswith(prefix) for prefix in prefixes)


def _string_list_matches_exact(values: list[str], current: str) -> bool:
    if not values:
        return True
    if not current:
        return False
    return current in values


def _directive_matches_context(
    directive: dict[str, Any],
    caller: str,
    user_query: str,
    action: str,
    scope_key: str,
    project_key: str,
    action_metadata: dict[str, Any] | None = None,
) -> bool:
    if not directive.get("active", 1):
        return False
    if not _scope_matches(str(directive.get("scope") or "all"), caller):
        return False

    trigger_data = normalize_trigger_data(
        directive.get("trigger_data"),
        directive.get("trigger_type"),
    )
    callers = normalize_string_list(trigger_data.get("callers"))
    if callers and "all" not in callers and caller not in callers:
        return False

    scope_keys = normalize_string_list(trigger_data.get("scope_keys_any"))
    if scope_keys and not _string_list_matches_exact(scope_keys, scope_key):
        return False

    scope_prefixes = normalize_string_list(trigger_data.get("scope_prefixes_any"))
    if scope_prefixes and not _string_list_matches_prefix(scope_prefixes, scope_key):
        return False

    project_keys = normalize_string_list(trigger_data.get("project_keys_any"))
    if project_keys and not _string_list_matches_exact(project_keys, project_key):
        return False

    normalized_action_metadata = normalize_action_metadata(action_metadata)

    action_modes = {item.lower() for item in normalize_string_list(trigger_data.get("action_modes_any"))}
    if action_modes:
        current_mode = str(normalized_action_metadata.get("mode") or "").lower()
        if not current_mode or current_mode not in action_modes:
            return False

    action_tags = {item.lower() for item in normalize_string_list(trigger_data.get("action_tags_any"))}
    if action_tags:
        current_tags = {
            str(item).lower()
            for item in normalize_string_list(normalized_action_metadata.get("tags"))
        }
        if not current_tags or not (action_tags & current_tags):
            return False

    if trigger_data.get("match") == "always":
        return True

    action_text = str(action or "").lower()
    user_query_text = str(user_query or "").lower()
    action_tokens = _extract_tokens(action_text)
    legacy_triggers = active_legacy_triggers(user_query=user_query_text, action=action_text)

    actions_any = {item.lower() for item in normalize_string_list(trigger_data.get("actions_any"))}
    if actions_any and (actions_any & action_tokens):
        return True

    action_keywords_any = [item.lower() for item in normalize_string_list(trigger_data.get("action_keywords_any"))]
    if action_keywords_any and any(keyword in action_text for keyword in action_keywords_any):
        return True

    query_keywords_any = [item.lower() for item in normalize_string_list(trigger_data.get("query_keywords_any"))]
    if query_keywords_any and any(keyword in user_query_text for keyword in query_keywords_any):
        return True

    legacy_trigger_types = {item.lower() for item in normalize_string_list(trigger_data.get("legacy_trigger_types"))}
    if legacy_trigger_types and (legacy_trigger_types & legacy_triggers):
        return True

    return False


def evaluate_directive_policy(
    directives: Iterable[dict[str, Any]],
    *,
    caller: str = "all",
    user_query: str = "",
    action: str = "",
    scope_key: str = "",
    project_key: str = "",
    action_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = [
        coerce_directive_record(dict(directive))
        for directive in directives
    ]
    ordered = sorted(normalized, key=_directive_sort_key)

    matched_directives: list[dict[str, Any]] = []
    required_workflows: list[dict[str, Any]] = []
    blocking_guards: list[dict[str, Any]] = []
    advisory_notes: list[dict[str, Any]] = []
    seen_workflows: set[str] = set()
    seen_guards: set[str] = set()
    seen_advisories: set[str] = set()

    for directive in ordered:
        if not _directive_matches_context(
            directive,
            caller=caller,
            user_query=user_query,
            action=action,
            scope_key=scope_key,
            project_key=project_key,
            action_metadata=action_metadata,
        ):
            continue

        matched_directives.append(copy.deepcopy(directive))
        directive_key = str(directive.get("key") or "")
        enforcement_level = normalize_enforcement_level(
            directive.get("enforcement_level"),
            workflow_skill_id=str(directive.get("workflow_skill_id") or ""),
            guard_id=str(directive.get("guard_id") or ""),
        )

        if enforcement_level == "blocking":
            guard_id = str(directive.get("guard_id") or directive_key).strip() or directive_key
            if guard_id not in seen_guards:
                seen_guards.add(guard_id)
                blocking_guards.append(
                    {
                        "directive_key": directive_key,
                        "guard_id": guard_id,
                        "content": directive.get("content", ""),
                    }
                )
            continue

        if enforcement_level == "workflow":
            workflow_skill_id = str(directive.get("workflow_skill_id") or directive_key).strip() or directive_key
            if workflow_skill_id not in seen_workflows:
                seen_workflows.add(workflow_skill_id)
                required_workflows.append(
                    {
                        "directive_key": directive_key,
                        "workflow_skill_id": workflow_skill_id,
                        "content": directive.get("content", ""),
                    }
                )
            continue

        if directive_key not in seen_advisories:
            seen_advisories.add(directive_key)
            advisory_notes.append(
                {
                    "directive_key": directive_key,
                    "content": directive.get("content", ""),
                }
            )

    decision = "allow"
    if blocking_guards:
        decision = "blocked"
    elif required_workflows:
        decision = "workflow_required"

    return {
        "decision": decision,
        "context": {
            "caller": caller,
            "user_query": user_query,
            "action": action,
            "scope_key": scope_key,
            "project_key": project_key,
        },
        "matched_directives": matched_directives,
        "required_workflows": required_workflows,
        "blocking_guards": blocking_guards,
        "advisory_notes": advisory_notes,
    }
