"""Bounded child-agent activity, separate from parent turn completion."""
import hashlib
import re
import time
from collections import Counter
from .tool_semantics import CATEGORIES, tool_category

DELEGATION_TOOLS = {'claude': frozenset({'Agent', 'Task'}),
                    'codex': frozenset({'spawn_agent', 'wait'})}
CHILD_EVENTS = frozenset({'SubagentStart', 'SubagentStop', 'PreToolUse', 'PostToolUse',
                         'PostToolUseFailure', 'PermissionRequest', 'Stop', 'StopFailure'})
CHILD_ACTIVITY_TTL_SECONDS = 3600


def digest_identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
        return None
    return hashlib.sha256(value.encode()).hexdigest()


def validate_summary(value):
    if not isinstance(value, dict) or set(value) != {'total', 'counts'}:
        return None
    if type(value['total']) is not int or not 0 <= value['total'] <= 64:
        return None
    counts = value['counts']
    if not isinstance(counts, dict) or set(counts) - CATEGORIES:
        return None
    if any(type(v) is not int or not 0 < v <= 64 for v in counts.values()) or sum(counts.values()) > value['total']:
        return None
    return {'total': value['total'], 'counts': dict(counts)}


class ChildActivity:
    def __init__(self, *, clock=time.monotonic, ttl=CHILD_ACTIVITY_TTL_SECONDS, capacity=256):
        self.clock, self.ttl, self.capacity = clock, ttl, capacity
        self.parents = {}

    def forget(self, parent):
        self.parents.pop(parent, None)

    def retire(self, parent):
        record = self.parents.get(parent)
        if record:
            record['retired'].update(record['children'])
            record['children'].clear()

    def known(self, parent, agent_id):
        self.summary(parent)
        return digest_identity(agent_id) in self.parents.get(parent, {}).get('children', {})

    def update(self, parent, provider, event, agent_id, tool_name=None, tool_use_id=None):
        child = digest_identity(agent_id)
        if child is None or event not in CHILD_EVENTS:
            return False
        if provider == 'codex' and event not in ('SubagentStart', 'SubagentStop'):
            return False
        if tool_name is not None and (not isinstance(tool_name, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', tool_name)):
            return False
        tool = digest_identity(tool_use_id)
        if tool_use_id not in (None, '') and tool is None:
            return False
        if parent not in self.parents:
            if len(self.parents) >= self.capacity or event != 'SubagentStart':
                return False
            self.parents[parent] = {'children': {}, 'retired': set()}
        record = self.parents[parent]
        children = record['children']
        self.summary(parent)
        if event == 'SubagentStart':
            if child in record['retired'] or len(record['retired']) >= 256:
                return False
            if child not in children:
                if len(children) >= 64:
                    return False
                children[child] = {'tools': {}, 'stamp': self.clock()}
        elif child not in children:
            return False  # Unknown and late callbacks never create an agent.
        elif event in ('SubagentStop', 'Stop', 'StopFailure'):
            children.pop(child)
            record['retired'].add(child)
        else:
            current = children[child]
            current['stamp'] = self.clock()
            if event == 'PreToolUse' and tool:
                if len(current['tools']) >= 64 and tool not in current['tools']:
                    return False
                current['tools'][tool] = tool_category(tool_name)
            elif event in ('PostToolUse', 'PostToolUseFailure'):
                current['tools'].pop(tool, None)
            elif event == 'PermissionRequest':
                current['tools'].clear()
        return True

    def summary(self, parent):
        record = self.parents.get(parent)
        if not record:
            return {'total': 0, 'counts': {}}
        for key, child in list(record['children'].items()):
            if self.clock() - child['stamp'] >= self.ttl:
                record['children'].pop(key)
                record['retired'].add(key)
        categories = [next(reversed(child['tools'].values()), None) for child in record['children'].values()]
        return {'total': len(categories), 'counts': dict(Counter(c for c in categories if c))}
