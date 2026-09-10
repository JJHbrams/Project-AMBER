"""Bounded approval correlation using already-hashed native metadata only."""


def request_permission(state, tool, name):
    if tool is None and name is None:
        state.unresolved_permission = True
        return None
    candidates = {key for key, value in state.inflight.items() if name is not None and value == name}
    if tool is None and len(candidates) == 1:
        tool = next(iter(candidates))
    key = tool if tool is not None else 'name:' + name
    if key not in state.pending and len(state.pending) >= 64:
        return 'approval_capacity'
    state.pending.add(key)
    if tool is not None:
        state.permission_names[key] = name or state.inflight.get(tool)
    else:
        # Freeze the known candidate IDs. An unrelated or later same-name call
        # cannot discharge approvals attributed to this candidate set.
        previous = state.permission_candidates.get(key)
        state.permission_candidates[key] = (previous | candidates if previous else candidates) or None
    return None


def complete_permission(state, tool, name):
    if tool is None:
        return
    expected = state.permission_names.get(tool) or state.inflight.get(tool)
    if expected is not None and name is not None and expected != name:
        return  # Contradictory name must not erase the known invocation fence.
    matched_name = name or expected
    state.pending.discard(tool)
    state.permission_names.pop(tool, None)
    state.inflight.pop(tool, None)
    for key, candidates in list(state.permission_candidates.items()):
        if matched_name is None or key != 'name:' + matched_name:
            continue
        if candidates is not None:
            if tool not in candidates:
                continue
            candidates.discard(tool)
            if candidates:
                continue
        # No-candidate reconnect-first approval needs a named, identified post;
        # candidate-bound approvals require every captured candidate to settle.
        state.pending.discard(key)
        state.permission_candidates.pop(key, None)


def clear_permissions(state):
    state.pending.clear()
    state.permission_names.clear()
    state.permission_candidates.clear()
    state.unresolved_permission = False
