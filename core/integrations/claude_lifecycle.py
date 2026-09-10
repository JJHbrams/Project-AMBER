"""Bounded, connection-local Claude hook reduction; no provider resume identity."""

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import re
import threading
import time
import uuid

EVENT_STATES = {
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "PostToolUseFailure": "working",
    "PermissionRequest": "needs_input",
    "Stop": "ready",
    "StopFailure": "blocked",
    "SessionEnd": None,
}
SELF_TOOL = "engram_report_claude_event"
TITLE_CONTEXT = (
    "The current Engram connection has no registered title. A title delivered "
    "earlier belonged to a previous connection and is not registered here. Before "
    "answering the current user request, call engram_report_session_title now with "
    "a safe 2–8 word public task label. Reuse the prior safe title if the task is "
    "unchanged; otherwise summarize the current task. Discover the tool only if "
    "needed. Never send prompt text, paths, credentials or private content. "
    "Do not repeat memory bootstrap to register a title. "
    "Do not call engram_report_claude_event yourself; native hooks report state."
)


@dataclass
class TurnState:
    turn: str | None = None
    retired: set[str] = field(default_factory=set)
    state: str = "unknown"
    ended: bool = False
    title_requested: bool = False
    pending: set[str] = field(default_factory=set)
    permission_names: dict[str, str | None] = field(default_factory=dict)
    permission_candidates: dict[str, set[str] | None] = field(default_factory=dict)
    inflight: dict[str, str | None] = field(default_factory=dict)
    unresolved_permission: bool = False
    touched: float = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    semantic_seq: int = 0
    semantic_turn: str | None = None
    semantic_categories: dict[str, str] = field(default_factory=dict)


def validate_event(
    event, turn_id=None, agent_id=None, tool_name=None, tool_use_id=None
):
    if type(event) is not str or event not in EVENT_STATES:
        return None, "invalid_event"
    if agent_id not in (None, ""):
        # Missing substitutions are not proof that this is the root agent.
        return None, (
            "unsupported_agent_identity"
            if agent_id == "${agent_id}"
            else "subagent_ignored"
        )
    if tool_name not in (None, ""):
        if not isinstance(tool_name, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,160}", tool_name
        ):
            return None, "invalid_tool_name"
        if tool_name == SELF_TOOL or (
            tool_name.startswith("mcp__") and tool_name.endswith("__" + SELF_TOOL)
        ):
            return None, "self_report_ignored"
    from .tool_semantics import tool_category
    category = tool_category(tool_name)
    tool_key = None
    if tool_use_id not in (None, ""):
        if not isinstance(tool_use_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", tool_use_id
        ):
            return None, "invalid_tool_identity"
        tool_key = hashlib.sha256(tool_use_id.encode()).hexdigest()
    name_key = hashlib.sha256(tool_name.encode()).hexdigest() if tool_name else None
    if event == "SessionEnd":
        return {"event": event, "turn": None, "tool": tool_key, "name": name_key}, None
    try:
        if not isinstance(turn_id, str) or len(turn_id) != 36:
            raise ValueError()
        normalized = str(uuid.UUID(turn_id))
    except (ValueError, AttributeError):
        return None, "missing_or_invalid_turn"
    return {
        "event": event,
        "turn": hashlib.sha256(normalized.encode()).hexdigest(),
        "tool": tool_key,
        "name": name_key,
        "category": category,
    }, None


def fold(
    state: TurnState,
    event: str,
    turn: str | None,
    tool: str | None = None,
    tool_name: str | None = None,
):
    """Call under the connection lock, which also serializes delivery order."""
    from .permission_fence import clear_permissions, complete_permission, request_permission
    if state.ended:
        return None, "session_ended"
    if event == "SessionEnd":
        state.ended = True
        return "ended", None
    if turn in state.retired:
        return None, "stale_turn"
    if (state.turn == turn and state.state in ('ready', 'blocked')
            and event in ('PostToolUse', 'PostToolUseFailure')):
        return None, 'completed_turn'
    if state.turn != turn:
        # PreToolUse recovers a missed first prompt / a new prompt after a stop.
        # On a fresh connection, even a post-tool/terminal callback is current
        # producer evidence. It cannot affect another connection's identity.
        if (
            state.turn is not None
            and event != "UserPromptSubmit"
            and not (
                event == "PreToolUse" and state.state in ("unknown", "ready", "blocked")
            )
        ):
            return None, "unestablished_turn"
        if state.turn:
            if len(state.retired) >= 256:
                return None, "turn_capacity"  # Never forget a fence while connected.
            state.retired.add(state.turn)
        state.turn = turn
        state.title_requested = False
        clear_permissions(state)
        state.inflight.clear()
        state.unresolved_permission = False
    if event == "PreToolUse" and tool:
        if len(state.inflight) >= 64 and tool not in state.inflight:
            return None, "tool_capacity"
        state.inflight[tool] = tool_name
    if event == "PermissionRequest":
        error = request_permission(state, tool, tool_name)
        if error:
            return None, error
    elif event in ("PostToolUse", "PostToolUseFailure"):
        complete_permission(state, tool, tool_name)
    elif event in ("Stop", "StopFailure"):
        clear_permissions(state)
        state.inflight.clear()
        state.unresolved_permission = False
    # Stop is an attempt: a following real tool callback may resume this turn.
    state.state = (
        "needs_input"
        if state.pending or state.unresolved_permission
        else EVENT_STATES[event]
    )
    return state.state, None


class LifecycleTracker:
    def __init__(self, *, capacity=256, ttl=600, clock=time.monotonic):
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self._states = OrderedDict()
        self._lock = threading.Lock()

    def ended(self, identifier):
        with self._lock:
            state = self._states.get(identifier)
            return bool(state and state.ended)

    def forget(self, identifier):
        with self._lock:
            self._states.pop(identifier, None)

    @contextmanager
    def transaction(self, identifier):
        now = self.clock()
        with self._lock:
            # An idle but connected client can deliver a delayed callback.
            # Never forget turn fences on a timer; DELETE/owner expiry releases
            # entries explicitly. Capacity exhaustion fails closed.
            state = self._states.get(identifier)
            if state is None and len(self._states) < self.capacity:
                state = self._states[identifier] = TurnState(touched=now)
        acquired = state is not None and state.lock.acquire(timeout=0.3)
        try:
            if acquired:
                state.touched = now
            yield state if acquired else None
        finally:
            if acquired:
                state.lock.release()
