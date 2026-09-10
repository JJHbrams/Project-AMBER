"""Finite, content-free tool semantics shared by bubble and native hooks."""

CATEGORIES = frozenset({'memory', 'search', 'read', 'write', 'execute', 'communication', 'other'})
EVENTS = frozenset({'generation.started', 'tool.started', 'tool.completed',
                    'tool.failed', 'generation.completed', 'provider.failed'})


def category_hint(category):
    return 'memory' if category == 'read' else category if category in ('search', 'memory') else 'generating'


def tool_category(name: object) -> str:
    value = str(name or '').lower()
    if any(x in value for x in ('memory', 'kg_', 'recall')): return 'memory'
    if any(x in value for x in ('search', 'find', 'web', 'browser', 'fetch', 'grep', 'glob', 'list')): return 'search'
    if any(x in value for x in ('read', 'open')): return 'read'
    if any(x in value for x in ('write', 'edit', 'patch', 'delete')): return 'write'
    if any(x in value for x in ('shell', 'bash', 'exec', 'build', 'test', 'run', 'task', 'agent')): return 'execute'
    if any(x in value for x in ('mail', 'message', 'discord', 'slack')): return 'communication'
    return 'other'


def semantic_packet(entry, checked, state):
    """Called only after accepted reduction under the same connection lock."""
    if entry.semantic_turn != checked['turn']:
        entry.semantic_categories.clear()
        entry.semantic_turn = checked['turn']
    event, tool = checked['event'], checked['tool']
    category = checked.get('category') or 'other'
    events = []
    if event == 'PreToolUse':
        if tool is not None:
            entry.semantic_categories[tool] = category
        events.append({'type': 'tool.started', 'category': category})
    elif event in ('PostToolUse', 'PostToolUseFailure'):
        entry.semantic_categories.pop(tool, None)
        events.append({'type': 'tool.failed' if event == 'PostToolUseFailure' else 'tool.completed', 'category': None})
    elif event == 'UserPromptSubmit':
        events.append({'type': 'generation.started', 'category': None})
    elif event == 'Stop':
        events.append({'type': 'generation.completed', 'category': None})
    elif event == 'StopFailure':
        events.append({'type': 'provider.failed', 'category': None})
    entry.semantic_categories = {key: value for key, value in entry.semantic_categories.items()
                                 if key in entry.inflight}
    active = next(reversed(entry.semantic_categories.values()), None)
    if state == 'working' and event == 'PreToolUse':
        active = category
    if state != 'working':
        active = None
        if state == 'needs_input':
            events = []  # Approval input always wins over concurrent work.
        else:
            entry.semantic_categories.clear()
    elif event in ('PostToolUse', 'PostToolUseFailure') and active:
        events.append({'type': 'tool.started', 'category': active})
    entry.semantic_seq += 1
    return {'seq': entry.semantic_seq, 'events': events, 'active_category': active}


def validate_semantic(value):
    if not isinstance(value, dict) or set(value) != {'seq', 'events', 'active_category'}:
        return None
    if type(value['seq']) is not int or not 0 < value['seq'] < 2**53:
        return None
    if value['active_category'] is not None and (type(value['active_category']) is not str
                                               or value['active_category'] not in CATEGORIES):
        return None
    events = value['events']
    if not isinstance(events, list) or len(events) > 2:
        return None
    for event in events:
        if not isinstance(event, dict) or set(event) != {'type', 'category'}:
            return None
        if type(event['type']) is not str or event['type'] not in EVENTS:
            return None
        category = event['category']
        if event['type'] == 'tool.started':
            if type(category) is not str or category not in CATEGORIES:
                return None
        elif category is not None:
            return None
    return {'seq': value['seq'], 'events': [dict(event) for event in events],
            'active_category': value['active_category']}
