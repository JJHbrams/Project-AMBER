"""Codex-native, connection-local lifecycle reduction.

Schema: https://learn.chatgpt.com/docs/hooks. Codex turn_id is an opaque
string, not Claude's prompt UUID. No session ID or transcript is accepted.
"""
import hashlib
import re

from .claude_lifecycle import LifecycleTracker, TurnState

EVENT_STATES = {
    'UserPromptSubmit': 'working', 'PreToolUse': 'working',
    'PostToolUse': 'working', 'PermissionRequest': 'needs_input',
    'Stop': 'ready', 'Interrupt': 'ready',
}
SELF_TOOL = 'engram_report_codex_event'
TITLE_CONTEXT = (
    'The current Engram connection has no registered title. A prior connection '
    'does not register a title here. Before answering the current user request, '
    'call engram_report_session_title with a safe 2-8 word public task label; '
    'reuse the prior safe title if unchanged. Discover the tool only if needed. '
    'Never send prompt text, paths, credentials or private content. Do not repeat '
    'memory bootstrap or call lifecycle reporting tools yourself; native hooks report state.'
)


def validate_event(event, turn_id, tool_name=None, tool_use_id=None):
    if type(event) is not str or event not in EVENT_STATES:
        return None, 'invalid_event'
    if not isinstance(turn_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', turn_id):
        return None, 'missing_or_invalid_turn'
    if tool_name not in (None, ''):
        if not isinstance(tool_name, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', tool_name):
            return None, 'invalid_tool_name'
        if any(tool_name == own or tool_name.endswith('__' + own)
               for own in (SELF_TOOL, 'engram_report_claude_event')):
            return None, 'self_report_ignored'
    if tool_use_id not in (None, '') and (
        not isinstance(tool_use_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', tool_use_id)
    ):
        return None, 'invalid_tool_identity'
    # IDs and tool names never leave the reducer in raw form.
    from .tool_semantics import tool_category
    category = tool_category(tool_name)
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest() if value else None
    return {'event': event, 'turn': digest(turn_id), 'tool': digest(tool_use_id),
            'name': digest(tool_name), 'category': category}, None


def fold(state: TurnState, event, turn, tool=None, tool_name=None):
    """Serial connection fence. It does not claim undocumented root-agent identity."""
    from .permission_fence import clear_permissions, complete_permission, request_permission
    if turn in state.retired:
        return None, 'stale_turn'
    if state.turn == turn and state.state == 'ready' and event == 'PostToolUse':
        return None, 'completed_turn'
    if state.turn != turn:
        if state.turn is not None and event != 'UserPromptSubmit' and not (
            event == 'PreToolUse' and state.state in ('unknown', 'ready')
        ):
            return None, 'unestablished_turn'
        if state.turn and state.turn not in state.retired:
            if len(state.retired) >= 256:
                return None, 'turn_capacity'
            state.retired.add(state.turn)
        state.turn = turn
        state.title_requested = False
        clear_permissions(state)
        state.inflight.clear()
        state.unresolved_permission = False
    if event == 'PreToolUse' and tool:
        if tool not in state.inflight and len(state.inflight) >= 64:
            return None, 'tool_capacity'
        state.inflight[tool] = tool_name
    if event == 'PermissionRequest':
        error = request_permission(state, tool, tool_name)
        if error:
            return None, error
    elif event == 'PostToolUse':
        complete_permission(state, tool, tool_name)
    elif event in ('Stop', 'Interrupt'):
        if event == 'Interrupt':
            if len(state.retired) >= 256:
                return None, 'turn_capacity'
            state.retired.add(turn)
        # Stop can be followed by another hook's continuation. Only fresh
        # pre-tool/prompt evidence may resume it; a late post cannot do so.
        clear_permissions(state)
        state.inflight.clear()
        state.unresolved_permission = False
    state.state = ('needs_input' if state.pending or state.unresolved_permission
                   else EVENT_STATES[event])
    return state.state, None
