"""
지침(Directives) 관리 모듈
세션 간 유지되는 운영 규칙을 저장하고 컨텍스트에 자동 주입한다.

source: 지침을 생성한 도구 ('copilot-cli', 'claude-code', 'user')
scope:  지침이 적용되는 대상 ('all', 'copilot-cli', 'claude-code')
trigger_type: 주입 조건 ('always' | 'wiki' | 'code' | 'git' | 'reflection')
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.common.sanitizer import sanitize
from core.config.runtime_config import get_cfg_value
from core.context.directive_policy import (
    TRIGGER_KEYWORDS,
    coerce_directive_record,
    evaluate_directive_policy,
    legacy_trigger_data_from_trigger_type,
    normalize_enforcement_level,
    normalize_string_list,
    normalize_trigger_data,
)
from core.context.guard_execution import (
    execute_guard,
    normalize_action_metadata,
    normalize_chore_intent,
    normalize_independent_task_context,
)
from core.storage.db import get_connection


_VALID_ENFORCEMENT_MODES = {"triggered", "hybrid", "always"}
_DIRECTIVE_COLUMNS = (
    "key, content, source, scope, priority, active, trigger_type, "
    "enforcement_level, trigger_data, workflow_skill_id, guard_id, "
    "legacy_migration_markers, created_at, updated_at"
)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _active_triggers(user_query: str) -> set[str]:
    if not user_query:
        return set()
    q = user_query.lower()
    active = set()
    for trigger, keywords in TRIGGER_KEYWORDS.items():
        if any(keyword in q for keyword in keywords):
            active.add(trigger)
    return active


def _directive_enforcement_mode() -> str:
    mode = str(get_cfg_value("directives.enforcement.mode", "triggered")).strip().lower()
    if mode in _VALID_ENFORCEMENT_MODES:
        return mode
    return "triggered"


def _directive_pin_top_n() -> int:
    raw = get_cfg_value("directives.enforcement.pin_top_n", 3)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 3


def _directive_max_items() -> int:
    raw = get_cfg_value("directives.enforcement.max_items", 8)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 8
    return 0 if value == 0 else max(1, value)


def _directive_pinned_keys() -> set[str]:
    raw = get_cfg_value("directives.enforcement.pinned_keys", [])
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        return set()
    return {str(value).strip() for value in values if str(value).strip()}


def _policy_audit_default_limit() -> int:
    raw = get_cfg_value("directives.policy.audit_default_limit", 20)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 20


def _policy_audit_max_limit() -> int:
    raw = get_cfg_value("directives.policy.audit_max_limit", 100)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 100


def _bounded_policy_audit_limit(limit: int | None = None) -> int:
    max_limit = _policy_audit_max_limit()
    if limit is None:
        return min(_policy_audit_default_limit(), max_limit)
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        return min(_policy_audit_default_limit(), max_limit)
    if parsed <= 0:
        return min(_policy_audit_default_limit(), max_limit)
    return min(parsed, max_limit)


def _row_to_directive(row: Any) -> Dict[str, Any]:
    return coerce_directive_record(dict(row))


def _fetch_directives(
    scope_filter: str = "all",
    include_inactive: bool = False,
    conn=None,
) -> List[Dict[str, Any]]:
    owns_connection = conn is None
    conn = conn or get_connection()
    try:
        if include_inactive:
            rows = conn.execute(
                f"SELECT {_DIRECTIVE_COLUMNS} FROM directives "
                "ORDER BY priority DESC, created_at ASC, key ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_DIRECTIVE_COLUMNS} FROM directives "
                "WHERE active = 1 AND (scope = 'all' OR scope = ?) "
                "ORDER BY priority DESC, created_at ASC, key ASC",
                (scope_filter,),
            ).fetchall()
        return [_row_to_directive(row) for row in rows]
    finally:
        if owns_connection:
            conn.close()


def _get_directive_by_key(conn, key: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(
        f"SELECT {_DIRECTIVE_COLUMNS} FROM directives WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_directive(row)


def _directive_policy_suffix(directive: Dict[str, Any]) -> str:
    level = normalize_enforcement_level(
        directive.get("enforcement_level"),
        workflow_skill_id=str(directive.get("workflow_skill_id") or ""),
        guard_id=str(directive.get("guard_id") or ""),
    )
    if level == "workflow":
        workflow_skill_id = str(directive.get("workflow_skill_id") or directive.get("key") or "").strip()
        if workflow_skill_id:
            return f" [workflow:{workflow_skill_id}]"
    if level == "blocking":
        guard_id = str(directive.get("guard_id") or directive.get("key") or "").strip()
        if guard_id:
            return f" [guard:{guard_id}]"
    return ""


def _audit_directive_snapshot(directive: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "key": directive.get("key", ""),
        "scope": directive.get("scope", "all"),
        "priority": directive.get("priority", 0),
        "trigger_type": directive.get("trigger_type", "always"),
        "enforcement_level": directive.get("enforcement_level", "advisory"),
        "workflow_skill_id": directive.get("workflow_skill_id", ""),
        "guard_id": directive.get("guard_id", ""),
        "content": directive.get("content", ""),
    }


def _directive_renders_in_prompt(directive: Dict[str, Any]) -> bool:
    trigger_data = normalize_trigger_data(
        directive.get("trigger_data"),
        directive.get("trigger_type"),
    )
    return trigger_data.get("render_in_prompt", True) is not False


def _should_regenerate_legacy_trigger_data(
    existing: Dict[str, Any],
    trigger_type: Optional[str],
    trigger_data: Optional[Dict[str, Any] | str],
) -> bool:
    if trigger_type is None or trigger_data is not None:
        return False

    current_trigger_type = str(existing.get("trigger_type") or "always").strip().lower() or "always"
    next_trigger_type = str(trigger_type or "").strip().lower() or "always"
    if next_trigger_type == current_trigger_type:
        return False

    current_trigger_data = normalize_trigger_data(
        existing.get("trigger_data"),
        current_trigger_type,
    )
    return current_trigger_data == legacy_trigger_data_from_trigger_type(current_trigger_type)


def add_directive(
    key: str,
    content: str,
    source: str = "unknown",
    scope: str = "all",
    priority: int = 0,
    trigger_type: str = "always",
    enforcement_level: Optional[str] = None,
    trigger_data: Optional[Dict[str, Any] | str] = None,
    workflow_skill_id: Optional[str] = None,
    guard_id: Optional[str] = None,
    legacy_migration_markers: Optional[List[str] | str] = None,
) -> dict:
    content = sanitize(content, max_length=1500)
    conn = get_connection()
    try:
        existing = _get_directive_by_key(conn, key)
        raw = dict(existing or {})
        raw.update(
            {
                "key": key,
                "content": content,
                "source": source,
                "scope": scope,
                "priority": priority,
                "active": 1,
                "trigger_type": trigger_type,
            }
        )
        if enforcement_level is not None:
            raw["enforcement_level"] = enforcement_level
        if trigger_data is not None:
            raw["trigger_data"] = trigger_data
        if workflow_skill_id is not None:
            raw["workflow_skill_id"] = workflow_skill_id
        if guard_id is not None:
            raw["guard_id"] = guard_id
        if legacy_migration_markers is not None:
            raw["legacy_migration_markers"] = legacy_migration_markers

        directive = coerce_directive_record(raw)

        with conn:
            conn.execute(
                """
                INSERT INTO directives (
                    key, content, source, scope, priority, active, trigger_type,
                    enforcement_level, trigger_data, workflow_skill_id, guard_id,
                    legacy_migration_markers
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    content      = excluded.content,
                    source       = excluded.source,
                    scope        = excluded.scope,
                    priority     = excluded.priority,
                    active       = 1,
                    trigger_type = excluded.trigger_type,
                    enforcement_level = excluded.enforcement_level,
                    trigger_data = excluded.trigger_data,
                    workflow_skill_id = excluded.workflow_skill_id,
                    guard_id     = excluded.guard_id,
                    legacy_migration_markers = excluded.legacy_migration_markers,
                    updated_at   = datetime('now','localtime')
                """,
                (
                    directive["key"],
                    directive["content"],
                    directive["source"],
                    directive["scope"],
                    directive["priority"],
                    directive["active"],
                    directive["trigger_type"],
                    directive["enforcement_level"],
                    _json_text(normalize_trigger_data(directive["trigger_data"], directive["trigger_type"])),
                    directive["workflow_skill_id"],
                    directive["guard_id"],
                    _json_text(normalize_string_list(directive["legacy_migration_markers"])),
                ),
            )
            stored = _get_directive_by_key(conn, key)
    finally:
        conn.close()
    return stored or directive


def provision_directive_trusted(**fields: Any) -> dict:
    """Compatibility-only provisioning primitive for installer/tests.

    Public callers must use ``core.context.directive_registration``; this name
    makes the deliberate bypass visible to bootstrap and migration code.
    """
    return add_directive(**fields)


def get_directive(key: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    try:
        return _get_directive_by_key(conn, key)
    finally:
        conn.close()


def get_directives(
    scope_filter: str = "all",
    include_inactive: bool = False,
    user_query: str = "",
    for_prompt: bool = False,
) -> List[Dict]:
    """지침 목록 조회.

    - scope_filter: 대상 필터 ('all', 'copilot-cli', 'claude-code')
    - include_inactive: 비활성 지침 포함 여부
    - user_query: 트리거 필터링용 쿼리. 비어 있으면 trigger_type='always' 인 것만 반환.
      쿼리가 있으면 'always' + 활성화된 trigger 유형 모두 포함.
    """
    directives = _fetch_directives(scope_filter=scope_filter, include_inactive=include_inactive)
    if for_prompt:
        directives = [
            directive
            for directive in directives
            if _directive_renders_in_prompt(directive)
        ]
    if include_inactive:
        return directives

    enforcement_mode = _directive_enforcement_mode()
    configured_pins = _directive_pinned_keys()

    if enforcement_mode == "always":
        result = directives
    else:
        active_triggers = _active_triggers(user_query)
        pinned_keys = set(configured_pins)
        if enforcement_mode == "hybrid":
            pin_top_n = _directive_pin_top_n()
            pinned_keys.update({
                str(d.get("key", "")).strip()
                for d in directives[:pin_top_n]
                if str(d.get("key", "")).strip()
            })

        result = []
        for directive in directives:
            trigger = directive.get("trigger_type", "always")
            key = str(directive.get("key", "")).strip()
            if trigger == "always" or trigger in active_triggers or key in pinned_keys:
                result.append(directive)

    max_items = _directive_max_items()
    if max_items > 0:
        pinned = [d for d in result if str(d.get("key", "")).strip() in configured_pins]
        slots = max(0, max_items - len(pinned))
        selected_keys = {str(d.get("key", "")).strip() for d in pinned}
        for directive in result:
            key = str(directive.get("key", "")).strip()
            if key in selected_keys:
                continue
            if slots <= 0:
                break
            selected_keys.add(key)
            slots -= 1
        result = [
            directive
            for directive in result
            if str(directive.get("key", "")).strip() in selected_keys
        ]

    return result


def update_directive(
    key: str,
    content: Optional[str] = None,
    scope: Optional[str] = None,
    priority: Optional[int] = None,
    active: Optional[bool] = None,
    trigger_type: Optional[str] = None,
    enforcement_level: Optional[str] = None,
    trigger_data: Optional[Dict[str, Any] | str] = None,
    workflow_skill_id: Optional[str] = None,
    guard_id: Optional[str] = None,
    legacy_migration_markers: Optional[List[str] | str] = None,
) -> bool:
    """지침 수정. 전달된 필드만 업데이트."""
    if all(
        value is None
        for value in (
            content,
            scope,
            priority,
            active,
            trigger_type,
            enforcement_level,
            trigger_data,
            workflow_skill_id,
            guard_id,
            legacy_migration_markers,
        )
    ):
        return False

    conn = get_connection()
    try:
        existing = _get_directive_by_key(conn, key)
        if existing is None:
            return False

        raw = dict(existing)
        regenerate_legacy_trigger_data = _should_regenerate_legacy_trigger_data(
            existing,
            trigger_type,
            trigger_data,
        )
        if content is not None:
            raw["content"] = content
        if scope is not None:
            raw["scope"] = scope
        if priority is not None:
            raw["priority"] = priority
        if active is not None:
            raw["active"] = 1 if active else 0
        if trigger_type is not None:
            raw["trigger_type"] = trigger_type
        if enforcement_level is not None:
            raw["enforcement_level"] = enforcement_level
        if trigger_data is not None:
            raw["trigger_data"] = trigger_data
        elif regenerate_legacy_trigger_data:
            raw["trigger_data"] = legacy_trigger_data_from_trigger_type(trigger_type)
        if workflow_skill_id is not None:
            raw["workflow_skill_id"] = workflow_skill_id
        if guard_id is not None:
            raw["guard_id"] = guard_id
        if legacy_migration_markers is not None:
            raw["legacy_migration_markers"] = legacy_migration_markers

        directive = coerce_directive_record(raw)
        with conn:
            cursor = conn.execute(
                """
                UPDATE directives
                   SET content = ?,
                       scope = ?,
                       priority = ?,
                       active = ?,
                       trigger_type = ?,
                       enforcement_level = ?,
                       trigger_data = ?,
                       workflow_skill_id = ?,
                       guard_id = ?,
                       legacy_migration_markers = ?,
                       source = 'user',
                       updated_at = datetime('now','localtime')
                 WHERE key = ?
                """,
                (
                    directive["content"],
                    directive["scope"],
                    directive["priority"],
                    directive["active"],
                    directive["trigger_type"],
                    directive["enforcement_level"],
                    _json_text(normalize_trigger_data(directive["trigger_data"], directive["trigger_type"])),
                    directive["workflow_skill_id"],
                    directive["guard_id"],
                    _json_text(normalize_string_list(directive["legacy_migration_markers"])),
                    key,
                ),
            )
        return cursor.rowcount > 0
    finally:
        conn.close()


def remove_directive(key: str) -> bool:
    """지침 완전 삭제."""
    conn = get_connection()
    with conn:
        cursor = conn.execute("DELETE FROM directives WHERE key = ?", (key,))
    conn.close()
    return cursor.rowcount > 0


def render_directives_prompt(caller: str = "all", user_query: str = "") -> str:
    """컨텍스트 주입용 지침 문자열 렌더링."""
    directives = get_directives(scope_filter=caller, user_query=user_query, for_prompt=True)
    if not directives:
        return ""
    enforcement_mode = _directive_enforcement_mode()
    header = "[지침]"
    lines = []
    if enforcement_mode in {"hybrid", "always"}:
        header = "[지침|강제]"
        lines.append(
            "아래 지침은 상위 운영 규칙이다. 충돌 시 지침을 우선하고, 실행 불가 시 이유를 먼저 설명할 것."
        )
    for directive in directives:
        scope_tag = f" [{directive['scope']}]" if directive["scope"] != "all" else ""
        policy_tag = _directive_policy_suffix(directive)
        lines.append(f"• {directive['content']}{scope_tag}{policy_tag}")
    return header + "\n" + "\n".join(lines)


def record_directive_policy_audit(
    caller: str,
    user_query: str,
    action: str,
    scope_key: str,
    project_key: str,
    cwd: str,
    action_metadata: Dict[str, Any],
    chore_intent: Dict[str, Any],
    independent_task_context: Dict[str, Any],
    execute_guards: bool,
    evaluation: Dict[str, Any],
) -> int:
    conn = get_connection()
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO directive_policy_audit (
                    caller, user_query, action, scope_key, project_key, cwd,
                    action_metadata, chore_intent, independent_task_context,
                    execute_guards, decision, final_status, matched_directives,
                    required_workflows, blocking_guards, executed_guard_results,
                    advisory_notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    caller,
                    user_query,
                    action,
                    scope_key,
                    project_key,
                    cwd,
                    _json_text(action_metadata),
                    _json_text(chore_intent),
                    _json_text(independent_task_context),
                    1 if execute_guards else 0,
                    evaluation.get("decision", "allow"),
                    evaluation.get("final_status", ""),
                    _json_text([
                        _audit_directive_snapshot(directive)
                        for directive in evaluation.get("matched_directives", [])
                    ]),
                    _json_text(evaluation.get("required_workflows", [])),
                    _json_text(evaluation.get("blocking_guards", [])),
                    _json_text(evaluation.get("executed_guard_results", [])),
                    _json_text(evaluation.get("advisory_notes", [])),
                ),
            )
        return int(cursor.lastrowid)
    finally:
        conn.close()


def list_directive_policy_audit(limit: int | None = None) -> List[Dict[str, Any]]:
    bounded = _bounded_policy_audit_limit(limit)
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, caller, user_query, action, scope_key, project_key, cwd,
                   action_metadata, chore_intent, independent_task_context,
                   execute_guards, decision, final_status, matched_directives,
                   required_workflows, blocking_guards, executed_guard_results,
                   advisory_notes, created_at
            FROM directive_policy_audit
            ORDER BY id DESC
            LIMIT ?
            """,
            (bounded,),
        ).fetchall()
    finally:
        conn.close()

    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "id": row["id"],
                "caller": row["caller"],
                "user_query": row["user_query"],
                "action": row["action"],
                "scope_key": row["scope_key"],
                "project_key": row["project_key"],
                "cwd": row["cwd"],
                "action_metadata": json.loads(row["action_metadata"] or "{}"),
                "chore_intent": json.loads(row["chore_intent"] or "{}"),
                "independent_task_context": json.loads(row["independent_task_context"] or "{}"),
                "execute_guards": bool(row["execute_guards"]),
                "decision": row["decision"],
                "final_status": row["final_status"],
                "matched_directives": json.loads(row["matched_directives"] or "[]"),
                "required_workflows": json.loads(row["required_workflows"] or "[]"),
                "blocking_guards": json.loads(row["blocking_guards"] or "[]"),
                "executed_guard_results": json.loads(row["executed_guard_results"] or "[]"),
                "advisory_notes": json.loads(row["advisory_notes"] or "[]"),
                "created_at": row["created_at"],
            }
        )
    return result


def preflight_directives(
    caller: str = "all",
    user_query: str = "",
    action: str = "",
    scope_key: str = "",
    project_key: str = "",
    persist_audit: bool = True,
    cwd: str = "",
    action_metadata: Dict[str, Any] | None = None,
    chore_intent: Dict[str, Any] | None = None,
    independent_task_context: Dict[str, Any] | None = None,
    execute_guards: bool = False,
    advisory_only: bool = False,
) -> Dict[str, Any]:
    normalized_action_metadata = normalize_action_metadata(action_metadata)
    normalized_chore_intent = normalize_chore_intent(chore_intent)
    normalized_independent_task_context = normalize_independent_task_context(independent_task_context)
    directives = _fetch_directives(scope_filter=caller, include_inactive=False)
    evaluation = evaluate_directive_policy(
        directives,
        caller=caller,
        user_query=user_query,
        action=action,
        scope_key=scope_key,
        project_key=project_key,
        action_metadata=normalized_action_metadata,
    )
    if execute_guards:
        executed_guard_results: list[dict[str, Any]] = []
        for blocking_guard in evaluation.get("blocking_guards", []):
            guard_result = execute_guard(
                str(blocking_guard.get("guard_id") or ""),
                cwd=cwd,
                action_metadata=normalized_action_metadata,
                chore_intent=normalized_chore_intent,
                independent_task_context=normalized_independent_task_context,
            )
            executed_guard_results.append(
                {
                    "directive_key": blocking_guard.get("directive_key", ""),
                    "guard_id": guard_result.get("guard_id", ""),
                    "status": guard_result.get("status", "error"),
                    "reason": guard_result.get("reason", ""),
                    "content": blocking_guard.get("content", ""),
                    "evidence": guard_result.get("evidence", {}),
                }
            )

        final_status = "allow"
        if any(result.get("status") == "error" for result in executed_guard_results):
            final_status = "error"
        elif any(result.get("status") == "fail" for result in executed_guard_results):
            final_status = "blocked"

        evaluation["executed_guard_results"] = executed_guard_results
        evaluation["final_status"] = final_status
        if final_status == "error":
            evaluation["decision"] = "error"
        elif final_status == "blocked":
            evaluation["decision"] = "blocked"
        elif evaluation.get("required_workflows"):
            evaluation["decision"] = "workflow_required"
        else:
            evaluation["decision"] = "allow"

    if advisory_only:
        policy_decision = str(evaluation.get("decision") or "allow")
        evaluation["advisory_only"] = True
        evaluation["policy_decision"] = policy_decision
        evaluation["would_block"] = policy_decision in {"blocked", "error", "workflow_required"}
        if policy_decision != "allow":
            evaluation["decision"] = "advisory"
            evaluation["final_status"] = "advisory"

    if persist_audit:
        evaluation["audit_id"] = record_directive_policy_audit(
            caller=caller,
            user_query=user_query,
            action=action,
            scope_key=scope_key,
            project_key=project_key,
            cwd=cwd,
            action_metadata=normalized_action_metadata,
            chore_intent=normalized_chore_intent,
            independent_task_context=normalized_independent_task_context,
            execute_guards=execute_guards,
            evaluation=evaluation,
        )
    return evaluation
