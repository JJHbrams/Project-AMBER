"""The approval-gated public registration path for persistent directives."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from typing import Any

from core.common.sanitizer import sanitize
from core.context.directive_policy import coerce_directive_record, normalize_trigger_data
from core.context.guard_execution import registered_guard_ids
from core.storage.db import get_connection

APPROVAL_TTL_SECONDS = 15 * 60
WORKFLOW_SKILLS = (
    ("engram-task-workflow", "Required repository-change workflow."),
    ("engram-wiki-workflow", "Required Wiki write/update workflow."),
    ("engram-close-session", "Explicit session close/reflection workflow."),
    ("engram-new-session", "Start a fresh Engram session workflow."),
)
SCHEMA = {
    "scope": {
        "required": True,
        "choices": [
            {"id": "all", "description": "Apply to every supported agent."},
            {"id": "copilot-cli", "description": "Apply only to Copilot CLI."},
            {"id": "claude-code", "description": "Apply only to Claude Code."},
        ],
    },
    "trigger_type": {
        "required": True,
        "choices": [
            {"id": "always", "description": "Match every request."},
            {"id": "wiki", "description": "Match Wiki/document work."},
            {"id": "code", "description": "Match code, build, or test work."},
            {"id": "git", "description": "Match Git work."},
            {"id": "reflection", "description": "Match session reflection or close work."},
            {"id": "conditional", "description": "Match only supplied structured conditions."},
        ],
    },
    "enforcement_level": {
        "required": True,
        "choices": [
            {"id": "advisory", "description": "Show guidance only."},
            {"id": "workflow", "description": "Require the selected workflow skill."},
            {"id": "blocking", "description": "Run the selected blocking guard."},
        ],
    },
    "active": {"required": True, "choices": [{"id": True, "description": "Enable immediately."}, {"id": False, "description": "Save disabled."}]},
    "workflow_skill_id": {"required_when": {"enforcement_level": "workflow"}, "choices": []},
    "guard_id": {"required_when": {"enforcement_level": "blocking"}, "choices": []},
    "trigger_data": {"required_when": {"trigger_type": "conditional"}},
}


class RegistrationError(ValueError):
    pass


def registration_schema() -> dict[str, Any]:
    """Return server-owned choices. Clients must render these rather than invent enums."""
    schema = json.loads(json.dumps(SCHEMA))
    schema["workflow_skill_id"]["choices"] = [
        {"id": skill_id, "description": description}
        for skill_id, description in WORKFLOW_SKILLS
    ]
    schema["guard_id"]["choices"] = [
        {"id": guard_id, "description": "Registered executable policy guard."}
        for guard_id in registered_guard_ids()
    ]
    schema["fields"] = ["key", "content", "source", "scope", "priority", "active", "trigger_type", "enforcement_level", "trigger_data", "workflow_skill_id", "guard_id"]
    return schema


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _parse_json(value: Any, name: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise RegistrationError(f"{name} must be valid JSON") from exc
    return value


def _meaningful_conditions(trigger_data: dict[str, Any]) -> bool:
    return any(key != "legacy_trigger_types" and bool(value) for key, value in trigger_data.items())


def canonicalize(payload: dict[str, Any]) -> dict[str, Any]:
    raw = dict(payload)
    raw["key"] = str(raw.get("key") or "").strip()
    raw["content"] = sanitize(str(raw.get("content") or ""), max_length=1500).strip()
    raw["source"] = str(raw.get("source") or "user").strip() or "user"
    raw["scope"] = str(raw.get("scope") or "").strip().lower()
    raw["trigger_type"] = str(raw.get("trigger_type") or "").strip().lower()
    raw["enforcement_level"] = str(raw.get("enforcement_level") or "").strip().lower()
    raw["workflow_skill_id"] = str(raw.get("workflow_skill_id") or "").strip()
    raw["guard_id"] = str(raw.get("guard_id") or "").strip()
    raw["active"] = raw.get("active", True)
    try:
        raw["priority"] = int(raw.get("priority", 0))
    except (TypeError, ValueError) as exc:
        raise RegistrationError("priority must be an integer") from exc
    raw["trigger_data"] = _parse_json(raw.get("trigger_data") or {}, "trigger_data")
    if not isinstance(raw["trigger_data"], dict):
        raise RegistrationError("trigger_data must be an object")
    schema = registration_schema()
    allowed = {field: {choice["id"] for choice in definition.get("choices", [])}
               for field, definition in schema.items() if isinstance(definition, dict) and definition.get("choices")}
    if not raw["key"] or not raw["content"]:
        raise RegistrationError("key and content are required")
    for field in ("scope", "trigger_type", "enforcement_level"):
        if raw[field] not in allowed[field]:
            raise RegistrationError(f"invalid {field}: {raw[field]!r}; use registration schema choices")
    if not isinstance(raw["active"], bool):
        raise RegistrationError("active must be a boolean")
    if raw["enforcement_level"] == "workflow" and raw["workflow_skill_id"] not in allowed["workflow_skill_id"]:
        raise RegistrationError("workflow enforcement requires a workflow_skill_id from the registration schema")
    if raw["enforcement_level"] != "workflow" and raw["workflow_skill_id"]:
        raise RegistrationError("workflow_skill_id is only allowed for workflow enforcement")
    if raw["enforcement_level"] == "blocking":
        if raw["guard_id"] not in registered_guard_ids():
            raise RegistrationError("blocking enforcement requires a recognized guard_id")
    elif raw["guard_id"]:
        raise RegistrationError("guard_id is only allowed for blocking enforcement")
    normalized_trigger = normalize_trigger_data(raw["trigger_data"], raw["trigger_type"])
    if raw["trigger_type"] == "conditional" and not _meaningful_conditions(normalized_trigger):
        raise RegistrationError("conditional trigger requires meaningful structured conditions")
    # A structured condition must not silently turn into an always match.
    if raw["trigger_type"] != "always" and raw["trigger_data"] and not _meaningful_conditions(normalized_trigger):
        raise RegistrationError("structured trigger requires meaningful conditions")
    raw["trigger_data"] = normalized_trigger
    raw["legacy_migration_markers"] = []
    return coerce_directive_record(raw)


def _target_snapshot(conn, key: str) -> str:
    row = conn.execute("SELECT key, content, source, scope, priority, active, trigger_type, enforcement_level, trigger_data, workflow_skill_id, guard_id, legacy_migration_markers FROM directives WHERE key = ?", (key,)).fetchone()
    return _digest(dict(row)) if row else ""


def _require_owner(actor: str, session_id: str) -> tuple[str, str]:
    actor = str(actor or "").strip()
    session_id = str(session_id or "").strip()
    if not actor or not session_id:
        raise RegistrationError("actor and session_id are required for directive registration")
    return actor, session_id


def _owned_draft(conn, draft_id: str, actor: str, session_id: str):
    actor, session_id = _require_owner(actor, session_id)
    row = conn.execute("SELECT * FROM directive_registration_drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    if row is None:
        raise RegistrationError("registration draft was not found")
    if not secrets.compare_digest(str(row["actor"]), actor) or not secrets.compare_digest(str(row["session_id"]), session_id):
        raise RegistrationError("draft actor/session does not match its owner")
    return row


def begin_registration(actor: str, session_id: str, initial: dict[str, Any] | None = None) -> dict[str, Any]:
    actor, session_id = _require_owner(actor, session_id)
    initial = dict(initial or {})
    draft_id = uuid.uuid4().hex
    now = int(time.time())
    conn = get_connection()
    try:
        with conn:
            conn.execute("INSERT INTO directive_registration_drafts (draft_id, payload, status, actor, session_id, created_at, updated_at) VALUES (?, ?, 'draft', ?, ?, ?, ?)", (draft_id, _canonical_json(initial), actor, session_id, now, now))
    finally:
        conn.close()
    return {"status": "draft_started", "draft_id": draft_id, "schema": registration_schema(), "payload": initial}


def complete_registration(draft_id: str, actor: str, session_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    conn = get_connection()
    try:
        row = _owned_draft(conn, draft_id, actor, session_id)
        if row["status"] != "draft":
            raise RegistrationError("draft is not available for completion")
        payload = json.loads(row["payload"])
        payload.update(fields)
        directive = canonicalize(payload)
        target_digest = _target_snapshot(conn, directive["key"])
        digest = _digest(directive)
        with conn:
            conn.execute("UPDATE directive_registration_drafts SET payload = ?, digest = ?, target_digest = ?, updated_at = ? WHERE draft_id = ?", (_canonical_json(directive), digest, target_digest, int(time.time()), draft_id))
        return {"status": "draft_completed", "draft_id": draft_id, "directive": directive, "digest": digest}
    finally:
        conn.close()


def preview_registration(draft_id: str, actor: str, session_id: str) -> dict[str, Any]:
    conn = get_connection()
    try:
        row = _owned_draft(conn, draft_id, actor, session_id)
        if not row["digest"] or row["status"] != "draft":
            raise RegistrationError("complete the draft before preview")
        directive = json.loads(row["payload"])
        effect = "overwrite existing directive" if row["target_digest"] else "create new directive"
        behavior = f"{directive['scope']} scope; {directive['trigger_type']} trigger; {directive['enforcement_level']} enforcement"
        if directive["workflow_skill_id"]:
            behavior += f" using workflow {directive['workflow_skill_id']}"
        if directive["guard_id"]:
            behavior += f" using guard {directive['guard_id']}"
        return {"status": "preview", "draft_id": draft_id, "directive": directive, "effective_behavior": behavior, "effect": effect, "digest": row["digest"], "approval_required": True}
    finally:
        conn.close()


def approve_registration(draft_id: str, actor: str, session_id: str, digest: str, approved: bool) -> dict[str, Any]:
    if approved is not True:
        raise RegistrationError("explicit approved=true is required")
    conn = get_connection()
    try:
        row = _owned_draft(conn, draft_id, actor, session_id)
        if row["status"] != "draft" or not row["digest"] or not secrets.compare_digest(str(row["digest"]), str(digest)):
            raise RegistrationError("approval digest does not match the current draft")
        token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + APPROVAL_TTL_SECONDS
        with conn:
            conn.execute("UPDATE directive_registration_drafts SET status = 'approved', approval_digest = ?, approval_token_hash = ?, approval_expires_at = ?, updated_at = ? WHERE draft_id = ?", (digest, _digest(token), expires_at, int(time.time()), draft_id))
        return {"status": "approved", "draft_id": draft_id, "digest": digest, "approval_token": token, "expires_at": expires_at}
    finally:
        conn.close()


def commit_registration(draft_id: str, actor: str, session_id: str, digest: str, approval_token: str) -> dict[str, Any]:
    conn = get_connection()
    try:
        actor, session_id = _require_owner(actor, session_id)
        conn.execute("BEGIN IMMEDIATE")
        row = _owned_draft(conn, draft_id, actor, session_id)
        if row["status"] != "approved":
            raise RegistrationError("draft is not approved or has already been committed")
        if int(row["approval_expires_at"] or 0) < int(time.time()):
            raise RegistrationError("approval has expired")
        if not secrets.compare_digest(str(row["digest"]), str(digest)) or not secrets.compare_digest(str(row["approval_digest"]), str(digest)) or not secrets.compare_digest(str(row["approval_token_hash"]), _digest(approval_token)):
            raise RegistrationError("approval is not bound to this exact draft digest")
        directive = json.loads(row["payload"])
        if _target_snapshot(conn, directive["key"]) != str(row["target_digest"] or ""):
            raise RegistrationError("target changed since preview; start a new registration draft")
        conn.execute("INSERT INTO directives (key, content, source, scope, priority, active, trigger_type, enforcement_level, trigger_data, workflow_skill_id, guard_id, legacy_migration_markers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET content=excluded.content, source=excluded.source, scope=excluded.scope, priority=excluded.priority, active=excluded.active, trigger_type=excluded.trigger_type, enforcement_level=excluded.enforcement_level, trigger_data=excluded.trigger_data, workflow_skill_id=excluded.workflow_skill_id, guard_id=excluded.guard_id, legacy_migration_markers=excluded.legacy_migration_markers, updated_at=datetime('now','localtime')", (directive["key"], directive["content"], directive["source"], directive["scope"], directive["priority"], directive["active"], directive["trigger_type"], directive["enforcement_level"], _canonical_json(directive["trigger_data"]), directive["workflow_skill_id"], directive["guard_id"], "[]"))
        conn.execute("UPDATE directive_registration_drafts SET status = 'committed', approval_token_hash = '', updated_at = ? WHERE draft_id = ?", (int(time.time()), draft_id))
        conn.commit()
        return {"status": "directive_committed", "draft_id": draft_id, "digest": digest, "directive": directive}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
