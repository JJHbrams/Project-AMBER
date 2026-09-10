"""In-memory session-stack state; deliberately independent from STM storage."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable
from core.integrations.child_activity import CHILD_ACTIVITY_TTL_SECONDS


IDLE_TIMEOUT_SECONDS = 3600.0
NATIVE_WORKING_LEASE_SECONDS = 120.0
CLAUDE_WORKING_LEASE_SECONDS = NATIVE_WORKING_LEASE_SECONDS  # compatibility
NATIVE_EVENT_MAX_AGE_SECONDS = 2.0
BUBBLE_DISPLAY_TITLE = '오버레이 세션'


@dataclass
class SessionState:
    key: str
    provider: str
    session_id: str
    state: str
    first_seen: float
    state_since: float
    last_seen: float
    label: str | None = None
    subagent_count: int | None = None
    is_bubble: bool | None = None
    project_name: str | None = None
    agent_name: str | None = None


class SessionStateRegistry:
    """Thread-safe, non-persistent registry with an injectable monotonic clock."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 idle_timeout: float = IDLE_TIMEOUT_SECONDS):
        self._clock = clock
        self._idle_timeout = idle_timeout
        self._items: dict[str, SessionState] = {}
        self._lock = threading.RLock()
        self._aliases: dict[str, str] = {}
        self._owned: set[str] = set()
        # Producer labels are ephemeral session metadata.  A local rename is a
        # separate preference: producers may refresh their title without
        # silently undoing what the user chose to call the session.
        self._producer_labels: dict[str, str | None] = {}
        self._user_labels: dict[str, str] = {}
        self._lifecycle_deadlines: dict[str, float] = {}
        self._lifecycle_ended: dict[str, float] = {}
        self._native_semantics = {}
        self._child_activity = {}
        self._child_activity_deadlines = {}

    def _public_item(self, item):
        row = asdict(item)
        owned_bubble = item.key in self._owned and item.is_bubble is True
        row['is_bubble'] = True if owned_bubble else None
        if owned_bubble:
            row['label'] = BUBBLE_DISPLAY_TITLE
        if item.key in self._child_activity:
            child = self._child_activity[item.key]
            row['child_activity'] = {'total': child['total'], 'counts': dict(child['counts'])}
        category = self._native_semantics.get(item.key, {}).get('active_category')
        if item.state == 'working' and category:
            row['activity_category'] = category
        return row

    def upsert(self, payload: dict, *, owned: bool = False, _native=False) -> dict:
        now = self._clock()
        provider, session_id, state = payload["provider"], payload["session_id"], payload["state"]
        key = f"{provider}:{session_id}"
        with self._lock:
            self._prune_locked(now)
            key = self._aliases.get(key, key)
            self._lifecycle_deadlines.pop(key, None)
            if not _native and key in self._native_semantics:
                self._native_semantics[key]['active_category'] = None
                self._native_semantics[key]['queue'].clear()
            item = self._items.get(key)
            if item is not None and key in self._owned and not owned:
                item.last_seen = now
                return self._public_item(item)
            if item is None:
                item = SessionState(key=key, provider=provider, session_id=session_id, state=state,
                                    first_seen=now, state_since=now, last_seen=now,
                                    label=payload.get("label"), subagent_count=payload.get("subagent_count"),
                                    is_bubble=payload.get("is_bubble"), project_name=payload.get("project_name"),
                                    agent_name=payload.get("agent_name"))
                self._items[key] = item
                if "label" in payload:
                    self._producer_labels[key] = item.label
            else:
                if item.state != state:
                    item.state, item.state_since = state, now
                item.last_seen = now
                if "label" in payload:
                    self._producer_labels[key] = payload["label"]
                    if key not in self._user_labels:
                        item.label = payload["label"]
                for name in ("subagent_count", "is_bubble", "project_name", "agent_name"):
                    if name in payload:
                        setattr(item, name, payload[name])
            return self._public_item(item)

    def claim(self, payload: dict) -> dict:
        with self._lock:
            result = self.upsert(payload, owned=True)
            self._owned.add(result['key'])
            return self._public_item(self._items[result['key']])

    def upsert_lifecycle(self, payload, *, semantic=None):
        """Private hook working lease; ordinary presence never renews it."""
        with self._lock:
            self._prune_locked(self._clock())
            key = f"{payload['provider']}:{payload['session_id']}"
            if key in self._lifecycle_ended:
                return None
            previous = self._native_semantics.get(key)
            if semantic is not None and previous and semantic['seq'] <= previous['seq']:
                return None  # Reject state and semantics together, not state first.
            payload = dict(payload)
            child_update = payload.pop('_child_update', False)
            previous_deadline = self._lifecycle_deadlines.get(key)
            if child_update:
                existing = self._items.get(key)
                if existing is None or key in self._owned:
                    return None  # Children never create or claim a parent.
                payload['state'] = existing.state
                if existing.state != 'working' and semantic is not None:
                    semantic = {**semantic, 'active_category': None, 'events': []}
            children = payload.pop('child_activity', None)
            row = self.upsert(payload, _native=True)
            if children is not None:
                self._child_activity[key] = children
                if children['total']:
                    self._child_activity_deadlines[key] = self._clock() + CHILD_ACTIVITY_TTL_SECONDS
                else:
                    self._child_activity_deadlines.pop(key, None)
            if semantic is not None:
                record = previous or {'queue': deque(maxlen=64)}
                record.update(seq=semantic['seq'], active_category=semantic['active_category'])
                record['queue'].append((self._clock(), [dict(event) for event in semantic['events']]))
                self._native_semantics[key] = record
            if child_update:
                if previous_deadline is not None:
                    self._lifecycle_deadlines[row['key']] = previous_deadline
            elif row["state"] == "working":
                self._lifecycle_deadlines[row["key"]] = self._clock() + NATIVE_WORKING_LEASE_SECONDS
            return row

    def consume_native_semantics(self, selected_key, expected_state, *, selection_changed=False):
        """Tk-only drain: discard unselected history, preserve selected order."""
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            for key, record in self._native_semantics.items():
                if key != selected_key:
                    record['queue'].clear()
            row = self._items.get(selected_key)
            record = self._native_semantics.get(selected_key)
            if row is None or record is None or row.state != expected_state:
                return None
            events = []
            for created, batch in record['queue']:
                if not selection_changed and now - created <= NATIVE_EVENT_MAX_AGE_SECONDS:
                    events.extend({**event, '_expires_at': created + NATIVE_EVENT_MAX_AGE_SECONDS}
                                  for event in batch)
            record['queue'].clear()
            return {'seq': record['seq'], 'active_category': record['active_category'], 'events': events}

    def end_lifecycle(self, provider, session_id):
        with self._lock:
            key = f"{provider}:{session_id}"
            self.remove(provider, session_id)
            self._lifecycle_ended[key] = self._clock()
            if len(self._lifecycle_ended) > 4096:
                self._lifecycle_ended.pop(next(iter(self._lifecycle_ended)))

    def set_label(self, key: str, label: str | None) -> bool:
        """Local user edit of existing live metadata, never presence or state."""
        from overlay.state_api import validate_payload
        with self._lock:
            self._prune_locked(self._clock())
            key = self._aliases.get(key, key)
            item = self._items.get(key)
            if item is None or (key in self._owned and item.is_bubble is True):
                return False
            payload, error = validate_payload({'provider': item.provider,
                'session_id': item.session_id, 'state': item.state, 'label': label})
            if error:
                return False
            value = payload.get('label')
            if value is None:
                self._user_labels.pop(key, None)
                item.label = self._producer_labels.get(key)
            else:
                self._user_labels[key] = value
                item.label = value
            return True

    def set_producer_label(self, provider: str, session_id: str, label: str) -> bool:
        """Accept a safe title without changing state, clocks, or ownership."""
        from overlay.state_api import validate_title_payload
        with self._lock:
            self._prune_locked(self._clock())
            payload, error = validate_title_payload(
                {"provider": provider, "session_id": session_id, "title": label}
            )
            if error:
                return False
            key = self._aliases.get(f"{payload['provider']}:{payload['session_id']}",
                                    f"{payload['provider']}:{payload['session_id']}")
            item = self._items.get(key)
            if item is None:
                return False
            self._producer_labels[key] = payload['title']
            if key not in self._user_labels:
                item.label = payload['title']
            return True

    def presence(self, payload: dict) -> dict | None:
        """Presence never changes a known state or resurrects a retired bubble."""
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            key = f"{payload['provider']}:{payload['session_id']}"
            key = self._aliases.get(key, key)
            if key in self._lifecycle_ended:
                return None
            item = self._items.get(key)
            if payload.get('bubble_owner'):
                if key not in self._owned or item is None:
                    return None
                if not payload.get('ended'):
                    item.last_seen = now
                return self._public_item(item)
            if payload.get('ended'):
                if item is not None and key not in self._owned:
                    self.remove(item.provider, item.session_id)
                return None
            if item is not None:
                item.last_seen = now
                return self._public_item(item)
            return self.upsert({'provider': payload['provider'], 'session_id': payload['session_id'], 'state': 'unknown'})

    def set_project_name(self, provider: str, session_id: str, project_name: str) -> bool:
        from overlay.state_api import validate_project_payload
        payload,error = validate_project_payload({"provider":provider,"session_id":session_id,"project_name":project_name})
        if error:
            return False
        with self._lock:
            self._prune_locked(self._clock())
            key = f"{provider}:{session_id}"
            item = self._items.get(self._aliases.get(key,key))
            if item is None:
                return False
            item.project_name = payload["project_name"]
            return True

    def title_metadata(self, key):
        """Atomic title-only snapshot; no callbacks or IO under the registry lock."""
        with self._lock:
            if key not in self._items:
                return None
            return {"producer": self._producer_labels.get(key),
                    "manual": self._user_labels.get(key)}

    def retire_title_owner(self, provider, session_id):
        with self._lock:
            metadata = self.title_metadata(f'{provider}:{session_id}')
            self.remove(provider, session_id)
            return metadata

    def restore_title_metadata(self, key, metadata):
        from overlay.state_api import validate_bubble_title_metadata
        checked = validate_bubble_title_metadata(metadata)
        if checked is None:
            return False
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return False
            self._producer_labels[key] = checked['producer']
            if checked['manual'] is None:
                self._user_labels.pop(key, None)
            else:
                self._user_labels[key] = checked['manual']
            item.label = checked['manual'] if checked['manual'] is not None else checked['producer']
            return True

    def bind_alias(self, provider: str, session_id: str, owner_id: str) -> None:
        with self._lock:
            owner = f'{provider}:{owner_id}'
            alias = f'{provider}:{session_id}'
            if owner not in self._owned or alias == owner or alias in self._owned:
                return
            # A provider may have reported before its SDK init message arrived.
            self._items.pop(alias, None)
            self._producer_labels.pop(alias, None)
            self._user_labels.pop(alias, None)
            self._aliases[alias] = owner

    def remove(self, provider: str, session_id: str) -> None:
        with self._lock:
            key = self._aliases.get(f'{provider}:{session_id}', f'{provider}:{session_id}')
            self._items.pop(key, None)
            self._owned.discard(key)
            self._producer_labels.pop(key, None)
            self._user_labels.pop(key, None)
            self._lifecycle_deadlines.pop(key, None)
            self._native_semantics.pop(key, None)
            self._child_activity.pop(key, None)
            self._child_activity_deadlines.pop(key, None)
            self._aliases = {alias: owner for alias, owner in self._aliases.items() if owner != key}

    def snapshot(self) -> list[dict]:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            return [self._public_item(item) for item in sorted(self._items.values(), key=lambda item: item.first_seen)]

    def _prune_locked(self, now: float) -> None:
        for key, deadline in list(self._child_activity_deadlines.items()):
            if now >= deadline:
                item = self._items.get(key)
                if item is not None:
                    item.subagent_count = 0
                self._child_activity.pop(key, None)
                self._child_activity_deadlines.pop(key, None)
        for key, deadline in list(self._lifecycle_deadlines.items()):
            if now >= deadline:
                item = self._items.get(key)
                if item is not None and item.state == "working":
                    item.state, item.state_since = "unknown", now
                    item.subagent_count = 0
                self._child_activity.pop(key, None)
                self._child_activity_deadlines.pop(key, None)
                self._lifecycle_deadlines.pop(key, None)
                if key in self._native_semantics:
                    self._native_semantics[key]['active_category'] = None
                    self._native_semantics[key]['queue'].clear()
        for key, ended_at in list(self._lifecycle_ended.items()):
            if now - ended_at >= self._idle_timeout:
                self._lifecycle_ended.pop(key, None)
        expired = [key for key, item in self._items.items() if now - item.last_seen >= self._idle_timeout]
        for key in expired:
            self.remove(self._items[key].provider, self._items[key].session_id)
