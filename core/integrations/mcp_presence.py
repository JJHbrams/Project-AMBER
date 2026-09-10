"""Ephemeral MCP presence, independent from STM identity and conversation content."""

from collections import OrderedDict
import hashlib
import hmac
import json
import logging
from pathlib import Path
from queue import Empty, SimpleQueue
import re
import secrets
import threading
import time
import uuid
import weakref
from functools import wraps
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _serialize_titles(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with self._title_lock:
            return function(self, *args, **kwargs)
    return wrapped


class PresenceReporter:
    def __init__(self, discovery=None, *, timeout=0.2, capacity=256):
        self.discovery = (
            Path(discovery)
            if discovery
            else Path.home() / ".engram/overlay-state-api-v1.json"
        )
        self.timeout, self.capacity = timeout, capacity
        self._pending = OrderedDict()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread = None
        self._salt = secrets.token_bytes(32)
        self._sessions = weakref.WeakKeyDictionary()
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        from .claude_lifecycle import LifecycleTracker
        self._claude_lifecycle = LifecycleTracker(capacity=capacity)
        self._codex_lifecycle = LifecycleTracker(capacity=capacity)
        from .session_title_cache import SessionTitleCache
        import os
        self._title_cache = SessionTitleCache(os.environ.get('ENGRAM_SMOKE_DB_DIR') or self.discovery.parent)
        self._title_lock = threading.RLock()
        self._native_titles = OrderedDict()
        self._binding_owners = {}
        self._transport_owners = {}
        self._transport_bindings = {}
        self._owner_members = {}
        self._detached_transports = OrderedDict()
        self._pending_alias_retirements = OrderedDict()
        self._child_transports = OrderedDict()
        self._finalized_transports = SimpleQueue()
        from .child_activity import ChildActivity
        self._children = ChildActivity(capacity=capacity)
        self._parent_activity = {}

    def _decorate_parent_activity(self, identifier, provider, entry, checked, state, tool_name):
        from .tool_semantics import semantic_packet
        from .child_activity import DELEGATION_TOOLS
        packet = semantic_packet(entry, checked, state)
        if identifier not in self._parent_activity and len(self._parent_activity) >= self.capacity:
            return packet
        record = self._parent_activity.setdefault(identifier, {'tools': {}})
        record['turn'] = checked['turn']
        record['updated'] = time.monotonic()
        record['tools'] = {key: value for key, value in record['tools'].items() if key in entry.inflight}
        if checked['event'] == 'PreToolUse' and checked['tool']:
            record['tools'][checked['tool']] = tool_name in DELEGATION_TOOLS[provider]
        if state != 'working':
            record['tools'].clear()
        parent_category = packet['active_category']
        if state == 'working':
            direct = [category for key, category in entry.semantic_categories.items()
                      if not record['tools'].get(key, False)]
            if direct:
                parent_category = packet['active_category'] = direct[-1]
                if checked['event'] == 'PreToolUse' and record['tools'].get(checked['tool']):
                    packet['events'] = []
            elif record['tools'] and all(record['tools'].values()):
                parent_category = None  # Waiting alone is generic parent work.
                packet['active_category'] = None
                counts = self._children.summary(identifier)['counts']
                if counts:
                    packet['active_category'] = next(reversed(counts))
                    if checked['event'] == 'PreToolUse':
                        packet['events'] = []
        # Never retain a child-derived display category as parent-owned state.
        record.update(state=state, category=parent_category)
        return packet

    def _report_child_event(self, context, provider, event, agent_id, native_session_id, tool_name=None, tool_use_id=None, turn_id=None):
        try:
            binding = self._native_binding(context, provider, native_session_id)
        except (ValueError, AttributeError):
            return {'accepted': False, 'reason': 'invalid_native_identity'}
        if binding is None:
            return {'accepted': False, 'reason': 'parent_identity_missing'}
        identity = self.context_identity(context)
        if identity is None or identity[1]:
            return {'accepted': False, 'reason': 'child_context_unavailable'}
        if not self._check_binding(identity[0], binding):
            return {'accepted': False, 'reason': 'native_identity_conflict'}
        candidates = [key for key in (self._binding_owners.get(binding),)
                      if key is not None and key in self._parent_activity]
        if len(candidates) != 1:
            return {'accepted': False, 'reason': 'parent_identity_ambiguous_or_missing'}
        parent = candidates[0]
        tracker = self._claude_lifecycle if provider == 'claude' else self._codex_lifecycle
        with tracker.transaction(parent) as entry:
            if entry is None or entry.ended:
                return {'accepted': False, 'reason': 'parent_not_active'}
            activity = self._parent_activity[parent]
            parent_state = entry.state
            if parent_state == 'working' and time.monotonic() - activity['updated'] >= 120:
                parent_state = 'unknown'  # A child cannot renew expired parent work.
            from .child_activity import digest_identity
            child_turn = digest_identity(turn_id)
            if provider == 'claude':
                try:
                    child_turn = hashlib.sha256(str(uuid.UUID(turn_id)).encode()).hexdigest()
                except (ValueError, TypeError, AttributeError):
                    child_turn = None
            if event == 'SubagentStart':
                if child_turn != entry.turn or parent_state != 'working':
                    return {'accepted': False, 'reason': 'stale_child_turn'}
            elif not self._children.known(parent, agent_id):
                return {'accepted': False, 'reason': 'child_event_unverified_or_stale'}
            if not self._children.update(parent, provider, event, agent_id, tool_name, tool_use_id):
                return {'accepted': False, 'reason': 'child_event_unverified_or_stale'}
            child_transport = identity[0] != parent
            self._bind_owner(identity[0], binding, parent,
                             transfer_project=not child_transport)
            if child_transport:
                self._child_transports[identity[0]] = True
                self._child_transports.move_to_end(identity[0])
                while len(self._child_transports) > self.capacity:
                    self._child_transports.popitem(last=False)
            summary = self._children.summary(parent)
            active = activity['category'] if parent_state == 'working' else None
            delegated = activity['tools'] and all(activity['tools'].values())
            if delegated and parent_state == 'working' and summary['counts']:
                active = next(reversed(summary['counts']))
            entry.semantic_seq += 1
            packet = {'seq': entry.semantic_seq, 'events': [], 'active_category': active}
            if event == 'PreToolUse' and delegated and active and parent_state == 'working':
                packet['events'] = [{'type': 'tool.started', 'category': active}]
            try:
                delivered = self._send({'provider': 'mcp', 'session_id': parent, 'state': parent_state,
                    'agent_name': 'Claude' if provider == 'claude' else 'Codex',
                    'subagent_count': summary['total'], 'child_activity': summary,
                    'child_update': True, 'semantic': packet},
                    endpoint='/state', lifecycle=provider)
            except Exception:
                delivered = False
            return {'accepted': bool(delivered), 'reason': 'delivered' if delivered else 'overlay_unavailable'}

    def _title_record(self, identifier):
        if identifier not in self._native_titles and len(self._native_titles) >= self.capacity:
            return {'binding': None, 'title': None, 'project': None, 'project_source': 0}  # Never evict a live identity fence.
        record = self._native_titles.setdefault(identifier, {'binding': None, 'title': None,
                                                             'project': None, 'project_source': 0})
        self._native_titles.move_to_end(identifier)
        return record

    def _native_binding(self, context, provider, native_id):
        from .session_title_cache import valid_identity
        if native_id is None:
            return None
        if not valid_identity(native_id):
            raise ValueError('invalid_native_identity')
        request = context.request_context.request
        principal = request.scope.get('engram.remote_principal', 'local') if request is not None else 'local'
        if not isinstance(principal, str) or len(principal) > 256:
            raise ValueError('invalid_native_principal')
        return provider, hashlib.sha256((principal + '\0' + native_id).encode()).hexdigest()

    def _restore_title(self, identifier, binding, title_status):
        with self._title_lock:
            record = self._title_record(identifier)
            if binding is not None:
                record['binding'] = binding
            binding = record['binding']
            # A stable native identity is required for durable cross-connection
            # lookup, not for a title already reported by this exact transport.
            title = record['title']
            if binding:
                provider, digest = binding
                title = self._title_cache.access(provider, 'scoped', digest, title) or title
            if title and title_status.get('missing'):
                try:
                    restored = self._send({'provider': 'mcp', 'session_id': identifier, 'title': title}, endpoint='/state/title')
                except Exception:
                    restored = False
                if restored:
                    record['title'] = title
                    title_status['missing'] = False

    def _restore_project(self, identifier):
        """Presence is allowed to recreate a host row after overlay restart."""
        record = self._native_titles.get(identifier)
        project = record and record.get('project')
        if project:
            try:
                self._send({'provider': 'mcp', 'session_id': identifier, 'project_name': project},
                           endpoint='/state/project')
            except Exception:
                pass

    def _check_binding(self, identifier, binding, *, provider=None):
        with self._title_lock:
            if identifier in self._detached_transports:
                return False
            if (binding is not None and identifier not in self._transport_bindings
                    and (len(self._transport_bindings) >= self.capacity
                         or len(self._pending_alias_retirements) >= self.capacity)):
                return False
            old = self._transport_bindings.get(identifier)
            if old is not None and provider is not None and old[0] != provider:
                return False
            return binding is None or old is None or old == binding

    def _owner_for(self, transport, binding=None):
        return self._transport_owners.get(transport, self._binding_owners.get(binding, transport))

    def _bind_owner(self, transport, binding, owner, *, transfer_project=True):
        """Commit only validated, accepted native evidence under title lock."""
        if binding is None:
            return
        if self._transport_owners.get(transport) == owner:
            self._retire_aliases()
            return
        self._binding_owners[binding] = owner
        self._transport_bindings[transport] = binding
        self._transport_owners[transport] = owner
        self._owner_members.setdefault(owner, set()).add(transport)
        if owner != transport:
            previous = self._native_titles.pop(transport, None)
            current = self._title_record(owner)
            if not current['title'] and previous:
                current['title'] = previous['title']
            # The established canonical row wins a conflict.  This is separate
            # from later, explicit source-ranked updates on that canonical row.
            if (transfer_project and previous and previous.get('project')
                    and (not current.get('project')
                         or previous.get('project_source', 0) > current.get('project_source', 0))):
                current['project'] = previous['project']
                current['project_source'] = previous.get('project_source', 0)
            self._claude_lifecycle.forget(transport)
            self._codex_lifecycle.forget(transport)
            self._children.forget(transport)
            self._parent_activity.pop(transport, None)
            # Retire only an alias row, not the native lifecycle owner.
            self._pending_alias_retirements[transport] = None
            self._retire_aliases()

    def _retire_aliases(self):
        # Retain failed deletions for the next worker/native delivery. At most
        # four bounded IO attempts per pass keep this recovery queue fair.
        for alias in list(self._pending_alias_retirements)[:4]:
            try:
                if self._send({'provider': 'mcp', 'session_id': alias, 'ended': True}):
                    self._pending_alias_retirements.pop(alias, None)
                    continue
            except Exception:
                pass
            self._pending_alias_retirements.move_to_end(alias)

    def _forget_owner(self, owner):
        self._native_titles.pop(owner, None)
        self._children.forget(owner)
        self._parent_activity.pop(owner, None)
        self._claude_lifecycle.forget(owner)
        self._codex_lifecycle.forget(owner)

    def _detach_transport(self, transport):
        self._child_transports.pop(transport, None)
        self._detached_transports[transport] = True
        self._detached_transports.move_to_end(transport)
        while len(self._detached_transports) > self.capacity:
            self._detached_transports.popitem(last=False)
        owner = self._transport_owners.pop(transport, transport)
        binding = self._transport_bindings.pop(transport, None)
        with self._lock:
            self._pending.pop(('mcp', transport), None)
        members = self._owner_members.get(owner)
        if members is not None:
            members.discard(transport)
            if members:
                return None
            self._owner_members.pop(owner, None)
        if binding is not None:
            self._binding_owners.pop(binding, None)
        self._forget_owner(owner)
        return owner

    def identity(self, transport_id):
        return hmac.new(
            self._salt, str(transport_id).encode(), hashlib.sha256
        ).hexdigest()[:32]

    def _ensure_worker_locked(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True, name='mcp-presence')
            self._thread.start()

    def submit(self, session_id, *, bubble_owner=False, ended=False):
        if not isinstance(session_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{16,128}", session_id
        ):
            return
        if ended and not bubble_owner:
            with self._title_lock:
                owner = self._detach_transport(session_id)
                if owner is None:
                    return
                session_id = owner
        with self._title_lock:
            if not ended and (session_id in self._detached_transports
                    or self._claude_lifecycle.ended(self._owner_for(session_id))):
                return
        payload = {
            "provider": "claude" if bubble_owner else "mcp",
            "session_id": session_id,
            "bubble_owner": bool(bubble_owner),
            "ended": bool(ended),
        }
        with self._lock:
            key = (payload["provider"], session_id)
            self._pending[key] = payload
            self._pending.move_to_end(key)
            while len(self._pending) > self.capacity:
                self._pending.popitem(last=False)
            self._ensure_worker_locked()
        self._wake.set()

    def context_identity(self, context):
        """Resolve the same ephemeral identity for presence and explicit state."""
        try:
            request = context.request_context.request
            headers = request.headers if request is not None else {}
            owner = headers.get("x-engram-bubble-owner", "")
            if owner and request.scope.get("engram.presence.local", False):
                return owner, True
            # Only the HTTP ownership guard may identify an HTTP transport.
            # SSE/stdio can carry arbitrary headers but must use their own
            # ServerSession fallback, never borrow another principal's HTTP ID.
            transport = (
                request.scope.get("engram.presence.transport_id")
                if request is not None
                else None
            )
            if transport:
                return self.identity(transport), False
            session = context.session
            with self._lock:
                identifier = self._sessions.get(session)
                if identifier is None:
                    identifier = uuid.uuid4().hex
                    self._sessions[session] = identifier
                    # GC may run while either reporter lock is already held.
                    # SimpleQueue.put is reentrant; finalizers never acquire our
                    # locks or perform network IO. The worker owns detach.
                    weakref.finalize(session, self._finalized_transports.put, identifier)
                    self._ensure_worker_locked()
            return identifier, False
        except Exception:
            # Missing request context, absent overlay, and legacy transports must
            # never prevent a normal MCP tool response.
            return None

    def report_context(self, context):
        identity = self.context_identity(context)
        if identity:
            identifier, bubble_owner = identity
            if bubble_owner:
                self.submit(identifier, bubble_owner=True)
            else:
                self.submit(identifier)

    @_serialize_titles
    def report_state(self, context, state, label=None, subagent_count=None):
        """Explicit metadata-only state; never infers work from arbitrary calls."""
        from overlay.state_api import validate_payload

        identity = self.context_identity(context)
        if identity is None:
            return {"accepted": False, "reason": "context_unavailable"}
        identifier, bubble_owner = identity
        if bubble_owner:
            return {"accepted": False, "reason": "bubble_controller_owned"}
        if identifier in self._detached_transports:
            return {"accepted": False, "reason": "session_ended"}
        identifier = self._owner_for(identifier)
        if self._claude_lifecycle.ended(identifier):
            return {"accepted": False, "reason": "session_ended"}
        payload = {"provider": "mcp", "session_id": identifier, "state": state}
        if label is not None:
            payload["label"] = label
        if subagent_count is not None:
            payload["subagent_count"] = subagent_count
        checked, error = validate_payload(payload)
        if error:
            return {"accepted": False, "reason": "invalid_metadata"}
        try:
            delivered = self._send(checked, endpoint="/state")
        except Exception:
            delivered = False
        return {
            "accepted": bool(delivered),
            "reason": "delivered" if delivered else "overlay_unavailable",
        }

    @_serialize_titles
    def report_title(self, context, title):
        """Deliver an explicit title to this connection's already-live row only."""
        from overlay.state_api import validate_title_payload

        identity = self.context_identity(context)
        if identity is None:
            return {"accepted": False, "reason": "context_unavailable"}
        identifier, bubble_owner = identity
        if identifier in self._detached_transports:
            return {"accepted": False, "reason": "session_ended"}
        if not bubble_owner:
            identifier = self._owner_for(identifier)
        # The normal tool wrapper has already queued this presence report, but
        # title reporting immediately after bootstrap must not race that worker.
        if self._claude_lifecycle.ended(identifier):
            return {"accepted": False, "reason": "session_ended"}
        # This carries no state or caller-supplied identity.
        presence = {
            "provider": "claude" if bubble_owner else "mcp",
            "session_id": identifier,
            "bubble_owner": bool(bubble_owner),
            "ended": False,
        }
        payload = {
            "provider": "claude" if bubble_owner else "mcp",
            "session_id": identifier,
            "title": title,
        }
        checked, error = validate_title_payload(payload)
        if error:
            return {"accepted": False, "reason": "invalid_metadata"}
        try:
            present = self._send(presence)
            delivered = bool(present) and self._send(checked, endpoint="/state/title")
        except Exception:
            delivered = False
        if delivered and not bubble_owner:
            with self._title_lock:
                record = self._title_record(identifier)
                record['title'] = checked['title']
                if record['binding']:
                    provider, digest = record['binding']
                    self._title_cache.access(provider, 'scoped', digest, checked['title'])
        return {"accepted": bool(delivered),
                "reason": "delivered" if delivered else "overlay_unavailable"}

    @_serialize_titles
    def report_project(self, context, project_name=None, cwd=None, *, source=None):
        from overlay.state_api import project_display_name
        name = project_display_name(project_name,cwd)
        if name is None:
            return {"accepted":False,"reason":"invalid_metadata"}
        identity = self.context_identity(context)
        if identity is None:
            return {"accepted":False,"reason":"context_unavailable"}
        identifier,bubble_owner = identity
        if identifier in self._detached_transports:
            return {"accepted": False, "reason": "session_ended"}
        transport = identifier
        if not bubble_owner:
            identifier = self._owner_for(identifier)
        if self._claude_lifecycle.ended(identifier):
            return {"accepted": False, "reason": "session_ended"}
        provider = "claude" if bubble_owner else "mcp"
        if source is None:
            source = 3 if project_name is not None else 1
        if not isinstance(source, int) or source not in (1, 2, 3):
            return {"accepted":False,"reason":"invalid_metadata"}
        if transport in self._child_transports:
            # A registered child shares its parent row for lifecycle routing but
            # never owns parent project metadata, whether automatic or explicit.
            return {"accepted":True,"reason":"child_transport_owned"}
        if source == 2 and not bubble_owner and transport != identifier:
            # Native alias members route lifecycle to the canonical owner, but
            # only that owner may refresh automatic client-root metadata.
            return {"accepted":True,"reason":"alias_transport_owned"}
        record = None
        if not bubble_owner:
            record = self._title_record(identifier)
            # Lower-quality automatic metadata cannot replace an explicit name.
            if record.get('project') and source < record.get('project_source', 0):
                return {"accepted":True,"reason":"retained_higher_quality"}
        try:
            present = self._send({"provider":provider,"session_id":identifier,
                                  "bubble_owner":bool(bubble_owner),"ended":False})
            delivered = bool(present) and self._send({"provider":provider,"session_id":identifier,
                "project_name":name},endpoint="/state/project")
        except Exception:
            delivered = False
        if delivered and record is not None:
            record['project'] = name
            record['project_source'] = source
        return {"accepted":bool(delivered),"reason":"delivered" if delivered else "overlay_unavailable"}

    @_serialize_titles
    def report_claude_event(self, context, event, turn_id=None, agent_id=None, tool_name=None, tool_use_id=None, native_session_id=None):
        from .claude_lifecycle import TITLE_CONTEXT, fold, validate_event
        if event in ('SubagentStart', 'SubagentStop') or agent_id not in (None, ''):
            if native_session_id is not None:
                return self._report_child_event(context, 'claude', event, agent_id, native_session_id, tool_name, tool_use_id, turn_id=turn_id)
        checked, reason = validate_event(event, turn_id, agent_id, tool_name, tool_use_id)
        if reason:
            return {"accepted": False, "reason": reason}
        identity = self.context_identity(context)
        if identity is None:
            return {"accepted": False, "reason": "context_unavailable"}
        identifier, bubble_owner = identity
        if bubble_owner:
            return {"accepted": False, "reason": "bubble_controller_owned"}
        try:
            binding = self._native_binding(context, 'claude', native_session_id)
        except (ValueError, AttributeError):
            return {'accepted': False, 'reason': 'invalid_native_identity'}
        transport = identifier
        if not self._check_binding(transport, binding, provider='claude'):
            return {'accepted': False, 'reason': 'native_identity_conflict'}
        identifier = self._owner_for(transport, binding)
        with self._claude_lifecycle.transaction(identifier) as entry:
            if entry is None:
                return {"accepted": False, "reason": "lifecycle_busy_or_full"}
            state, reason = fold(entry, checked["event"], checked["turn"], checked["tool"], checked["name"])
            if reason:
                return {"accepted": False, "reason": reason}
            self._bind_owner(transport, binding, identifier)
            title_status = {}
            try:
                if state == "ended":
                    self._children.forget(identifier)
                    self._parent_activity.pop(identifier, None)
                    # Remove queued presence before retiring this exact connection.
                    with self._lock:
                        self._pending.pop(("mcp", identifier), None)
                    delivered = self._send({"provider": "mcp", "session_id": identifier,
                                            "ended": True}, lifecycle=True)
                else:
                    packet = self._decorate_parent_activity(identifier, 'claude', entry, checked, state, tool_name)
                    delivered = self._send({"provider": "mcp", "session_id": identifier,
                                            "state": state, "agent_name": "Claude",
                                            'subagent_count': self._children.summary(identifier)['total'],
                                            'child_activity': self._children.summary(identifier),
                                            "semantic": packet},
                                           endpoint="/state", lifecycle=True,
                                           title_status=title_status)
            except Exception:
                delivered = False
            result = {"accepted": bool(delivered),
                      "reason": "delivered" if delivered else "overlay_unavailable"}
            if delivered and state != 'ended':
                self._restore_title(identifier, binding, title_status)
                self._restore_project(identifier)
            if (delivered and title_status.get("missing") and not entry.title_requested
                    and event in ("UserPromptSubmit", "PreToolUse")):
                entry.title_requested = True
                result["hookSpecificOutput"] = {"hookEventName": event,
                                                "additionalContext": TITLE_CONTEXT}
            return result

    @_serialize_titles
    def report_codex_event(self, context, event, turn_id, tool_name=None, tool_use_id=None, native_session_id=None, agent_id=None):
        from .codex_lifecycle import TITLE_CONTEXT, fold, validate_event
        if event in ('SubagentStart', 'SubagentStop'):
            return self._report_child_event(context, 'codex', event, agent_id, native_session_id, turn_id=turn_id)
        if agent_id not in (None, ''):
            return {'accepted': False, 'reason': 'unsupported_agent_identity'}
        checked, reason = validate_event(event, turn_id, tool_name, tool_use_id)
        if reason:
            return {"accepted": False, "reason": reason}
        identity = self.context_identity(context)
        if identity is None:
            return {"accepted": False, "reason": "context_unavailable"}
        identifier, bubble_owner = identity
        if bubble_owner:
            return {"accepted": False, "reason": "bubble_controller_owned"}
        try:
            binding = self._native_binding(context, 'codex', native_session_id)
        except (ValueError, AttributeError):
            return {'accepted': False, 'reason': 'invalid_native_identity'}
        transport = identifier
        if not self._check_binding(transport, binding, provider='codex'):
            return {'accepted': False, 'reason': 'native_identity_conflict'}
        identifier = self._owner_for(transport, binding)
        with self._codex_lifecycle.transaction(identifier) as entry:
            if entry is None:
                return {"accepted": False, "reason": "lifecycle_busy_or_full"}
            state, reason = fold(entry, checked['event'], checked['turn'], checked['tool'], checked['name'])
            if reason:
                return {"accepted": False, "reason": reason}
            self._bind_owner(transport, binding, identifier)
            title_status = {}
            try:
                packet = self._decorate_parent_activity(identifier, 'codex', entry, checked, state, tool_name)
                delivered = self._send({"provider": "mcp", "session_id": identifier,
                                        "state": state, "agent_name": "Codex",
                                        'subagent_count': self._children.summary(identifier)['total'],
                                        'child_activity': self._children.summary(identifier),
                                        "semantic": packet},
                                       endpoint="/state", lifecycle="codex", title_status=title_status)
            except Exception:
                delivered = False
            result = {"accepted": bool(delivered),
                      "reason": "delivered" if delivered else "overlay_unavailable"}
            if delivered:
                self._restore_title(identifier, binding, title_status)
                self._restore_project(identifier)
            if (delivered and title_status.get('missing') and not entry.title_requested
                    and event in ('UserPromptSubmit', 'PreToolUse')):
                entry.title_requested = True
                result['hookSpecificOutput'] = {'hookEventName': event, 'additionalContext': TITLE_CONTEXT}
            return result

    def _send(self, payload, *, endpoint="/state/presence", lifecycle=False, title_status=None):
        if not self.discovery.is_file() or self.discovery.stat().st_size > 4096:
            return
        info = json.loads(self.discovery.read_text(encoding="utf-8"))
        if (
            info.get("schema_version") != 1
            or info.get("host") != "127.0.0.1"
            or type(info.get("port")) is not int
            or not 1 <= info["port"] <= 65535
        ):
            return
        token = info.get("token")
        if not isinstance(token, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{32,128}", token
        ):
            return
        request = Request(
            f"http://127.0.0.1:{info['port']}{endpoint}",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
                **({"X-Engram-Lifecycle": "codex" if lifecycle == "codex" else "claude"} if lifecycle else {}),
            },
        )
        with self._opener.open(request, timeout=self.timeout) as response:
            # Headers suffice: no unbounded or slow response-body read.
            if title_status is not None:
                title_status["missing"] = response.headers.get("X-Engram-Title-Missing") == "1"
            return response.status == 200

    def _run(self):
        while not self._stopping.is_set():
            self._wake.wait(0.5)
            with self._lock:
                if not self._pending:
                    self._wake.clear()
                    payload = None
                else:
                    _, payload = self._pending.popitem(last=False)
            try:
                # Serialize presence+restore with explicit title and lifecycle
                # reports so an older remembered title cannot race a new one.
                with self._title_lock:
                    for _ in range(self.capacity):
                        try:
                            finalized = self._finalized_transports.get_nowait()
                        except Empty:
                            break
                        if finalized in self._transport_owners or finalized in self._native_titles:
                            self.submit(finalized, ended=True)
                        else:
                            # Identity lookup alone never published a row and
                            # must not cause outbound cleanup as a GC side effect.
                            self._forget_owner(finalized)
                    self._retire_aliases()
                    if payload is None:
                        continue
                    identifier = payload['session_id']
                    if not payload.get('ended') and identifier in self._detached_transports:
                        continue
                    if not payload.get('ended') and not payload.get('bubble_owner'):
                        identifier = self._owner_for(identifier)
                        payload = dict(payload, session_id=identifier)
                    if not payload.get("ended") and self._claude_lifecycle.ended(identifier):
                        continue
                    title_status = {}
                    delivered = self._send(payload, title_status=title_status)
                    if (delivered and title_status.get('missing')
                            and not payload.get('ended') and not payload.get('bubble_owner')
                            and identifier in self._native_titles):
                        self._restore_title(identifier, None, title_status)
                    if delivered and not payload.get('ended') and not payload.get('bubble_owner'):
                        self._restore_project(identifier)
            except Exception:
                pass

    def stop(self):
        self._stopping.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=self.timeout + 0.1)


class PresenceMiddleware:
    """Bind HTTP transport sessions to their authenticated listener principal."""

    def __init__(self, app, reporter, *, path="/mcp", capacity=4096, ttl=1800):
        self.app, self.reporter, self.path = app, reporter, path
        self.capacity, self.ttl = capacity, ttl
        self._owners = OrderedDict()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path", "").rstrip(
            "/"
        ) != self.path.rstrip("/"):
            return await self.app(scope, receive, send)
        scope = dict(scope)
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        server = scope.get("server") or ("", 0)
        principal = (server[1], scope.get("engram.remote_principal", "local"))
        scope["engram.presence.local"] = principal[1] == "local"
        now = time.monotonic()
        expired = [
            sid for sid, (_, stamp) in self._owners.items() if now - stamp >= self.ttl
        ]
        for sid in expired:
            self._owners.pop(sid, None)
            self.reporter.submit(self.reporter.identity(sid), ended=True)
        sid = headers.get("mcp-session-id")
        if sid:
            record = self._owners.get(sid)
            if record is None or record[0] != principal:
                body = b'{"error":"invalid MCP session owner"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 404 if record is None else 403,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                return await send({"type": "http.response.body", "body": body})
            self._owners[sid] = (principal, now)
            self._owners.move_to_end(sid)
            scope["engram.presence.transport_id"] = sid

        async def guarded_send(message):
            if message.get("type") == "http.response.start":
                response_headers = dict(message.get("headers", []))
                issued = response_headers.get(b"mcp-session-id")
                if issued and message.get("status", 500) < 400:
                    issued = issued.decode("ascii")
                    self._owners[issued] = (principal, now)
                    self._owners.move_to_end(issued)
                    while len(self._owners) > self.capacity:
                        self._owners.popitem(last=False)
                    bubble = (
                        headers.get("x-engram-bubble-owner")
                        if scope["engram.presence.local"]
                        else None
                    )
                    self.reporter.submit(
                        bubble or self.reporter.identity(issued),
                        bubble_owner=bool(bubble),
                    )
                if (
                    sid
                    and scope.get("method") == "DELETE"
                    and message.get("status", 500) < 400
                ):
                    self._owners.pop(sid, None)
                    if not (
                        scope["engram.presence.local"]
                        and headers.get("x-engram-bubble-owner")
                    ):
                        self.reporter.submit(self.reporter.identity(sid), ended=True)
            await send(message)

        return await self.app(scope, receive, guarded_send)


class TransportIdentityFilter(logging.Filter):
    """Do not expose SDK transport session handles in lifecycle logs."""

    def filter(self, record):
        record.msg = re.sub(r"\b[0-9a-fA-F]{32}\b", "[session]", record.getMessage())
        record.args = ()
        return True
