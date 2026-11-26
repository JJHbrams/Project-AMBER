"""Ephemeral MCP presence, independent from STM identity and conversation content.

Threading model (why the MCP event loop can no longer stall on presence):

* Paths the event loop calls (submit, report_context, context_identity, identity,
  PresenceMiddleware) only validate the shape of their input and put it on a
  SimpleQueue or touch a memory-only identity map.  They never take _state_lock
  or any lock that overlay IO / sqlite / DPAPI can hold.
* report_* run on presence-lane worker threads.  They update in-memory records
  under _state_lock (memory only), record intent in the Outbox and then wait up
  to ack_wait for delivery *outside every lock*.  A timeout answers
  accepted=True / reason="queued".
* One sender thread ("mcp-presence-sender") owns all network IO: it drains the
  inbox, handles GC finalizers, snapshots one outbox op under the lock, sends it
  without the lock, then records the result under the lock.
* Title-cache sqlite reads happen outside every lock (once per binding, kept in
  memory); writes happen on a dedicated cache thread, latest value only.

Lock rule: inside _state_lock and inside a LifecycleTracker entry lock there is
no network, disk (sqlite, files, discovery) or DPAPI access.  Lock order is
entry lock -> _state_lock; the sender only ever takes _state_lock.
"""

from collections import OrderedDict
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import re
import secrets
import stat as _stat
import threading
import time
import uuid
import weakref
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from .presence_outbox import Outbox, Ticket

logger = logging.getLogger('engram.mcp.presence')

DEFAULT_ACK_WAIT_SECS = 0.12
_UNSENT = object()  # title/project not yet delivered to the current overlay row
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _new_record():
    return {'binding': None, 'title': None, 'project': None, 'project_source': 0,
            'title_seq': 0, 'title_pending': False, 'title_epoch': _UNSENT,
            'project_seq': 0, 'project_pending': False, 'project_pending_source': 0,
            'project_epoch': _UNSENT}


class PresenceReporter:
    def __init__(self, discovery=None, *, timeout=0.2, capacity=256, ack_wait=None):
        self.discovery = (
            Path(discovery)
            if discovery
            else Path.home() / ".engram/overlay-state-api-v1.json"
        )
        self.timeout, self.capacity = timeout, capacity
        self.ack_wait = self._configured_ack_wait() if ack_wait is None else max(0.0, float(ack_wait))
        self._salt = secrets.token_bytes(32)
        self._sessions = weakref.WeakKeyDictionary()
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        from .claude_lifecycle import LifecycleTracker
        self._claude_lifecycle = LifecycleTracker(capacity=capacity)
        self._codex_lifecycle = LifecycleTracker(capacity=capacity)
        from .session_title_cache import SessionTitleCache
        self._title_cache = SessionTitleCache(os.environ.get('ENGRAM_SMOKE_DB_DIR') or self.discovery.parent)
        # _state_lock: memory-only state.  _identity_lock: the legacy session map
        # (loop path).  _thread_lock: starting the worker threads.  None of them
        # is ever held across IO.
        self._state_lock = threading.RLock()
        self._identity_lock = threading.Lock()
        self._thread_lock = threading.Lock()
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
        self._outbox = Outbox(capacity)
        self._inbox = SimpleQueue()
        self._inbox_limit = capacity * 8 + 256
        self._binding_titles = OrderedDict()
        self._title_missing = OrderedDict()  # owners the overlay reported as title-less
        self._cache_writes = OrderedDict()
        self._cache_busy = False
        self._seq = 0
        self._epoch = None
        self._discovery_cache = None
        self._flush_waiters = []
        self._stats = {'sent': 0, 'failed': 0, 'superseded': 0, 'errors': 0}
        self._last_error_log = 0.0
        self._last_alive_log = time.monotonic()
        self._wake = threading.Event()
        self._cache_wake = threading.Event()
        self._stopping = threading.Event()
        self._paused = threading.Event()
        self._thread = None
        self._cache_thread = None

    @staticmethod
    def _configured_ack_wait():
        try:
            from core.config.runtime_config import get_cfg_value
            value = float(get_cfg_value('mcp.presence_ack_wait_secs', DEFAULT_ACK_WAIT_SECS))
            return min(max(value, 0.0), 2.0)
        except Exception:
            return DEFAULT_ACK_WAIT_SECS

    # -- sender lifecycle --------------------------------------------------
    def _ensure_worker(self):
        if self._thread is not None:
            return
        with self._thread_lock:
            if self._thread is None:
                thread = threading.Thread(target=self._run, daemon=True, name='mcp-presence-sender')
                thread.start()
                self._thread = thread
            if self._cache_thread is None:
                thread = threading.Thread(target=self._cache_run, daemon=True, name='mcp-presence-title-cache')
                thread.start()
                self._cache_thread = thread

    def _dispatch(self):
        self._ensure_worker()
        self._wake.set()

    def pause_sender(self):
        """Test/diagnostic hook: stop sending (intake keeps accepting) until resume_sender()."""
        self._paused.set()

    def resume_sender(self):
        self._paused.clear()
        self._wake.set()

    def flush(self, timeout=2.0):
        """Wait until everything recorded so far was delivered (or failed) and cached."""
        self._ensure_worker()
        done = threading.Event()
        with self._state_lock:
            self._flush_waiters.append(done)
        self._wake.set()
        deadline = time.monotonic() + timeout
        ok = done.wait(timeout)
        while ok and time.monotonic() < deadline:
            with self._state_lock:
                pending = bool(self._cache_writes) or self._cache_busy
            if not pending:
                return True
            threading.Event().wait(0.005)
        return False

    def stop(self):
        self._stopping.set()
        self._wake.set()
        self._cache_wake.set()
        for thread in (self._thread, self._cache_thread):
            if thread is not None and hasattr(thread, 'join'):
                thread.join(timeout=self.timeout + 0.1)

    def _run(self):
        wait = 2.0
        while not self._stopping.is_set():
            self._wake.wait(wait)
            self._wake.clear()
            if self._stopping.is_set():
                break
            if self._paused.is_set():
                wait = 0.02
                continue
            try:
                self._tick()
                wait = self._idle_wait()
            except Exception as error:  # one bad iteration must never kill the sender
                self._note_error(error)
                wait = 0.2
            self._maybe_log_alive()

    def _tick(self):
        self._process_inbox()
        try:
            self._refresh_epoch()
        except Exception as error:
            self._note_error(error)
        sent = 0
        while not self._stopping.is_set() and not self._paused.is_set():
            with self._state_lock:
                item = self._outbox.take(time.monotonic())
            if item is None:
                break
            self._deliver(*item)
            sent += 1
            if sent % 8 == 0:
                self._process_inbox()
        with self._state_lock:
            idle = self._inbox.empty() and self._outbox.pending(time.monotonic()) == 0
            if idle and self._flush_waiters:
                waiters, self._flush_waiters = self._flush_waiters, []
                for waiter in waiters:
                    waiter.set()

    def _idle_wait(self):
        with self._state_lock:
            now = time.monotonic()
            if self._outbox.pending(now) or not self._inbox.empty():
                return 0.0
            retry = self._outbox.next_retry(now)
        return 2.0 if retry is None else max(0.05, min(2.0, retry))

    def _note_error(self, error):
        self._stats['errors'] += 1
        now = time.monotonic()
        if now - self._last_error_log >= 30:
            self._last_error_log = now
            # Class name only: never payloads, tokens or identifiers.
            logger.warning('presence sender iteration failed (%s); errors=%d',
                           type(error).__name__, self._stats['errors'])

    def _maybe_log_alive(self):
        now = time.monotonic()
        if now - self._last_alive_log < 60:
            return
        self._last_alive_log = now
        with self._state_lock:
            queued = self._outbox.pending(now)
        logger.info('presence sender alive sent=%d failed=%d superseded=%d errors=%d queued=%d',
                    self._stats['sent'], self._stats['failed'], self._stats['superseded'],
                    self._stats['errors'], queued)

    # -- sender: intake ----------------------------------------------------
    def _process_inbox(self):
        for _ in range(max(self.capacity * 4, 64)):
            try:
                item = self._inbox.get_nowait()
            except Empty:
                break
            try:
                with self._state_lock:
                    self._handle_submit_locked(*item)
            except Exception as error:
                self._note_error(error)
        for _ in range(self.capacity):
            try:
                finalized = self._finalized_transports.get_nowait()
            except Empty:
                break
            try:
                with self._state_lock:
                    if finalized in self._transport_owners or finalized in self._native_titles:
                        self._handle_submit_locked(finalized, False, True)
                    else:
                        # Identity lookup alone never published a row and
                        # must not cause outbound cleanup as a GC side effect.
                        self._forget_owner(finalized)
            except Exception as error:
                self._note_error(error)

    def _handle_submit_locked(self, session_id, bubble_owner, ended):
        provider = 'claude' if bubble_owner else 'mcp'
        if ended:
            if not bubble_owner:
                owner = self._detach_transport(session_id)
                if owner is None:
                    return
                session_id = owner
            payload = {"provider": provider, "session_id": session_id,
                       "bubble_owner": bool(bubble_owner), "ended": True}
            self._outbox.put_ended((provider, session_id), payload)
            return
        if session_id in self._detached_transports:
            return
        if not bubble_owner:
            session_id = self._owner_for(session_id)
        if self._claude_lifecycle.ended(session_id):
            return
        payload = {"provider": provider, "session_id": session_id,
                   "bubble_owner": bool(bubble_owner), "ended": False}
        self._outbox.put_presence((provider, session_id), payload)

    def _refresh_epoch(self):
        """Detect an overlay restart (new instance_id) and re-publish live metadata."""
        info = self._discovery_info()  # disk read, outside every lock
        epoch = info.get('instance_id') if info else None
        if not isinstance(epoch, str):
            return
        with self._state_lock:
            previous, self._epoch = self._epoch, epoch
            if previous is not None and previous != epoch:
                self._resend_all_locked()

    def _resend_all_locked(self):
        for owner, record in list(self._native_titles.items()):
            if owner in self._detached_transports or self._claude_lifecycle.ended(owner):
                continue
            presence = self._presence_payload('mcp', owner, False)
            if record['title'] and not record['title_pending']:
                self._queue_title_locked(owner, record['title'], presence=presence)
            if record['project'] and not record['project_pending']:
                self._queue_project_locked(owner, restore=True, presence=presence)

    # -- sender: delivery --------------------------------------------------
    def _deliver(self, key, op):
        title_status = {} if op.want_status else None
        status = 'failed'
        try:
            status = self._transmit(op, title_status)
        except Exception as error:
            self._note_error(error)
        self._complete(key, op, status, title_status)

    def _transmit(self, op, title_status):
        kwargs = {}
        if op.endpoint != '/state/presence':
            kwargs['endpoint'] = op.endpoint
        if op.lifecycle:
            kwargs['lifecycle'] = op.lifecycle
        if title_status is not None:
            kwargs['title_status'] = title_status
        try:
            if op.presence is not None and not self._send(op.presence):
                return 'failed'
            return 'delivered' if self._send(op.payload, **kwargs) else 'failed'
        except HTTPError as error:
            # 409 means the overlay already holds something newer or ended: superseded, not an outage.
            return 'superseded' if error.code == 409 else 'failed'
        except Exception:
            return 'failed'

    def _complete(self, key, op, status, title_status):
        key_stat = 'sent' if status == 'delivered' else ('superseded' if status == 'superseded' else 'failed')
        self._stats[key_stat] += 1
        with self._state_lock:
            requeued = False
            try:
                self._record_result_locked(key, op, status, title_status)
            except Exception as error:
                self._note_error(error)
            finally:
                requeued = self._outbox.finish(key, op, status, now=time.monotonic())
            if op.kind == 'ended' and op.meta.get('alias') and not requeued:
                self._pending_alias_retirements.pop(key[1], None)

    def _record_result_locked(self, key, op, status, title_status):
        provider, owner = key
        delivered = status == 'delivered'
        if provider != 'mcp':
            return
        if op.kind in ('presence', 'lifecycle') and delivered and title_status is not None:
            self._note_title_status_locked(owner, bool(title_status.get('missing')))
        elif op.kind == 'title':
            self._title_result_locked(owner, op, delivered)
        elif op.kind == 'project':
            self._project_result_locked(owner, op, delivered)

    def _set_title_missing_locked(self, owner, missing):
        if not missing:
            self._title_missing.pop(owner, None)
            return
        self._title_missing[owner] = True
        self._title_missing.move_to_end(owner)
        while len(self._title_missing) > self.capacity:
            self._title_missing.popitem(last=False)

    def _note_title_status_locked(self, owner, missing):
        """Overlay header, applied asynchronously: restore a known title, else flag the owner.

        Presence for a connection without any title never allocates a title record."""
        if not missing:
            self._set_title_missing_locked(owner, False)
            return
        record = self._native_titles.get(owner)
        binding = record['binding'] if record else None
        local = (record['title'] if record else None) or (self._binding_titles.get(binding) if binding else None)
        if record is not None and local:
            self._set_title_missing_locked(owner, False)
            if not record['title_pending']:
                self._queue_title_locked(owner, local)
            if record['project'] and not record['project_pending']:
                self._queue_project_locked(owner, restore=True)
        else:
            self._set_title_missing_locked(owner, not (record and record['title_pending']))

    def _title_result_locked(self, owner, op, delivered):
        title = op.meta.get('title')
        record = self._native_titles.get(owner)
        if record is None:
            target = self._transport_owners.get(owner, owner)
            target_record = self._native_titles.get(target)
            if (delivered and title and target != owner and target_record is not None
                    and not target_record['title'] and not target_record['title_pending']):
                self._queue_title_locked(target, title)  # reached a retired alias row only
            return
        if op.seq != record['title_seq']:
            return  # a newer title is queued or in flight: only the latest is recorded
        record['title_pending'] = False
        if not delivered:
            return
        record['title'] = title
        record['title_epoch'] = self._epoch
        self._set_title_missing_locked(owner, False)
        if record['binding']:
            self._remember_binding_title_locked(record['binding'], title)

    def _project_result_locked(self, owner, op, delivered):
        record = self._native_titles.get(owner)
        if record is None or op.seq != record['project_seq']:
            return
        record['project_pending'] = False
        record['project_pending_source'] = 0
        if delivered:
            record['project'] = op.meta['name']
            record['project_source'] = op.meta['source']
            record['project_epoch'] = self._epoch

    # -- title cache (memory + dedicated writer thread) --------------------
    def _cached_title(self, binding):
        """Remembered title for a native binding: one sqlite lookup per binding, outside any lock."""
        if binding is None:
            return None
        with self._state_lock:
            if binding in self._binding_titles:
                return self._binding_titles[binding]
        title = self._title_cache.lookup(binding[0], 'scoped', binding[1])
        with self._state_lock:
            # A title delivered while we were reading is newer than what we read.
            current = self._binding_titles.setdefault(binding, title)
            while len(self._binding_titles) > self.capacity * 2:
                self._binding_titles.popitem(last=False)
            return current

    def _remember_binding_title_locked(self, binding, title):
        self._binding_titles[binding] = title
        self._binding_titles.move_to_end(binding)
        while len(self._binding_titles) > self.capacity * 2:
            self._binding_titles.popitem(last=False)
        self._cache_writes[binding] = (self._next_seq(), title)
        self._cache_writes.move_to_end(binding)
        while len(self._cache_writes) > self.capacity:
            self._cache_writes.popitem(last=False)
        self._cache_wake.set()

    def _cache_run(self):
        while not self._stopping.is_set():
            self._cache_wake.wait(2.0)
            self._cache_wake.clear()
            while not self._stopping.is_set():
                with self._state_lock:
                    if not self._cache_writes:
                        self._cache_busy = False
                        break
                    binding, (_, title) = self._cache_writes.popitem(last=False)
                    self._cache_busy = True
                try:
                    self._title_cache.store(binding[0], 'scoped', binding[1], title)
                except Exception as error:
                    self._note_error(error)

    # -- discovery / transport --------------------------------------------
    def _discovery_info(self):
        """Parsed discovery file; re-read only when (mtime_ns, size, inode) changed."""
        path = self.discovery
        try:
            st = path.stat()
        except OSError:
            self._discovery_cache = None
            return None
        if not _stat.S_ISREG(st.st_mode) or st.st_size > 4096:
            return None
        key = (st.st_mtime_ns, st.st_size, getattr(st, 'st_ino', 0))
        cached = self._discovery_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._discovery_cache = None
            return None
        valid = None
        if (isinstance(info, dict) and info.get("schema_version") == 1
                and info.get("host") == "127.0.0.1"
                and type(info.get("port")) is int and 1 <= info["port"] <= 65535
                and isinstance(info.get("token"), str) and _TOKEN.fullmatch(info["token"])):
            valid = {'port': info['port'], 'token': info['token'], 'instance_id': info.get('instance_id')}
        # A just-written file can be rewritten within the timestamp granularity.
        stable = time.time_ns() - st.st_mtime_ns > 2_000_000_000
        self._discovery_cache = (key, valid) if stable else None
        return valid

    def _send(self, payload, *, endpoint="/state/presence", lifecycle=False, title_status=None):
        info = self._discovery_info()
        if info is None:
            return
        request = Request(
            f"http://127.0.0.1:{info['port']}{endpoint}",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + info['token'],
                **({"X-Engram-Lifecycle": "codex" if lifecycle == "codex" else "claude"} if lifecycle else {}),
            },
        )
        with self._opener.open(request, timeout=self.timeout) as response:
            # Headers suffice: no unbounded or slow response-body read.
            if title_status is not None:
                title_status["missing"] = response.headers.get("X-Engram-Title-Missing") == "1"
            return response.status == 200

    # -- memory-only bookkeeping (call with _state_lock held) --------------
    def _next_seq(self):
        self._seq += 1
        return self._seq

    @staticmethod
    def _presence_payload(provider, identifier, bubble_owner):
        return {"provider": provider, "session_id": identifier,
                "bubble_owner": bool(bubble_owner), "ended": False}

    def _title_record(self, identifier):
        if identifier not in self._native_titles and len(self._native_titles) >= self.capacity:
            return _new_record()  # Never evict a live identity fence.
        record = self._native_titles.setdefault(identifier, _new_record())
        self._native_titles.move_to_end(identifier)
        return record

    def _queue_title_locked(self, owner, title, *, payload=None, ticket=None, presence=None):
        record = self._title_record(owner)
        seq = self._next_seq()
        record['title_seq'], record['title_pending'] = seq, True
        payload = payload or {'provider': 'mcp', 'session_id': owner, 'title': title}
        ok = self._outbox.put_title(('mcp', owner), payload, seq, presence=presence,
                                    ticket=ticket, meta={'title': title})
        if not ok:
            record['title_pending'] = False
        return ok

    def _queue_project_locked(self, owner, name=None, source=None, *, restore=False, ticket=None, presence=None):
        record = self._title_record(owner)
        if restore:
            name, source = record['project'], record['project_source']
        if not name:
            return False
        seq = self._next_seq()
        record['project_seq'], record['project_pending'], record['project_pending_source'] = seq, True, source
        ok = self._outbox.put_project(
            ('mcp', owner), {'provider': 'mcp', 'session_id': owner, 'project_name': name}, seq,
            presence=presence, ticket=ticket, meta={'name': name, 'source': source})
        if not ok:
            record['project_pending'], record['project_pending_source'] = False, 0
        return ok

    def _restore_locked(self, identifier, binding, cached):
        """Queue (never send) whatever the overlay row may be missing for this owner."""
        record = self._title_record(identifier)
        if binding is not None:
            record['binding'] = binding
        binding = record['binding']
        if binding and record['title'] and self._binding_titles.get(binding) != record['title']:
            self._remember_binding_title_locked(binding, record['title'])
        if not record['title']:
            remembered = cached or (self._binding_titles.get(binding) if binding else None)
            if remembered and not record['title_pending']:
                self._queue_title_locked(identifier, remembered)
        elif record['title_epoch'] != self._epoch and not record['title_pending']:
            self._queue_title_locked(identifier, record['title'])
        if (record['project'] and record['project_epoch'] != self._epoch
                and not record['project_pending']):
            self._queue_project_locked(identifier, restore=True)

    def _claim_title_request(self, identifier, turn, tracker):
        """Once per turn: ask the model for a title when none is known or on its way."""
        with self._state_lock:
            record = self._native_titles.get(identifier)
            wanted = (identifier in self._title_missing
                      and not (record and (record['title'] or record['title_pending'])))
        if not wanted:
            return False
        with tracker.transaction(identifier) as entry:
            if entry is None or entry.ended or entry.turn != turn or entry.title_requested:
                return False
            entry.title_requested = True
        return True

    def _outcome(self, ticket):
        """Wait for delivery outside every lock; a timeout is 'queued', not a failure."""
        ticket.wait(self.ack_wait)
        if ticket.status == 'delivered':
            return True, 'delivered'
        if ticket.status is None:
            return True, 'queued'
        return False, 'overlay_unavailable'

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

    def _check_binding(self, identifier, binding, *, provider=None):
        with self._state_lock:
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
        with self._state_lock:
            return self._transport_owners.get(transport, self._binding_owners.get(binding, transport))

    def _bind_owner(self, transport, binding, owner, *, transfer_project=True):
        """Commit only validated, accepted native evidence under the state lock."""
        if binding is None:
            return
        if self._transport_owners.get(transport) == owner:
            return
        self._binding_owners[binding] = owner
        self._transport_bindings[transport] = binding
        self._transport_owners[transport] = owner
        self._owner_members.setdefault(owner, set()).add(transport)
        if owner != transport:
            previous = self._native_titles.pop(transport, None)
            current = self._title_record(owner)
            if not current['title'] and previous and previous['title']:
                current['title'] = previous['title']
                current['title_epoch'] = _UNSENT  # the canonical row never received it
            # The established canonical row wins a conflict.  This is separate
            # from later, explicit source-ranked updates on that canonical row.
            if (transfer_project and previous and previous.get('project')
                    and (not current.get('project')
                         or previous.get('project_source', 0) > current.get('project_source', 0))):
                current['project'] = previous['project']
                current['project_source'] = previous.get('project_source', 0)
                current['project_epoch'] = _UNSENT
            if previous:
                self._migrate_pending_locked(transport, owner, previous, current, transfer_project)
            self._claude_lifecycle.forget(transport)
            self._codex_lifecycle.forget(transport)
            self._children.forget(transport)
            self._parent_activity.pop(transport, None)
            # Retire only an alias row, not the native lifecycle owner.  The
            # mark is retried by the sender until the overlay confirms it.
            self._pending_alias_retirements[transport] = None
            self._outbox.put_ended(('mcp', transport),
                                   {"provider": "mcp", "session_id": transport, "ended": True},
                                   retry=True, meta={'alias': True})
            self._wake.set()

    def _migrate_pending_locked(self, alias, owner, previous, current, transfer_project):
        """A title/project still queued for the alias row belongs to the canonical owner."""
        names = []
        if previous['title_pending']:
            names.append('title')
        if (transfer_project and previous['project_pending']
                and previous['project_pending_source'] > max(
                    current['project_source'] if current['project'] else 0,
                    current['project_pending_source'])):
            names.append('project')
        if not names:
            return
        moved = self._outbox.migrate_metadata(('mcp', alias), ('mcp', owner), owner, tuple(names))
        if 'title' in moved:
            current['title_pending'], current['title_seq'] = True, previous['title_seq']
        if 'project' in moved:
            current['project_pending'], current['project_seq'] = True, previous['project_seq']
            current['project_pending_source'] = previous['project_pending_source']

    def _forget_owner(self, owner):
        self._native_titles.pop(owner, None)
        self._title_missing.pop(owner, None)
        self._children.forget(owner)
        self._parent_activity.pop(owner, None)
        self._claude_lifecycle.forget(owner)
        self._codex_lifecycle.forget(owner)
        self._outbox.discard(('mcp', owner))

    def _detach_transport(self, transport):
        self._child_transports.pop(transport, None)
        self._detached_transports[transport] = True
        self._detached_transports.move_to_end(transport)
        while len(self._detached_transports) > self.capacity:
            self._detached_transports.popitem(last=False)
        owner = self._transport_owners.pop(transport, transport)
        binding = self._transport_bindings.pop(transport, None)
        self._outbox.discard_presence(('mcp', transport))
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

    # -- loop-safe entry points (no lock that IO can hold) -----------------
    def identity(self, transport_id):
        return hmac.new(
            self._salt, str(transport_id).encode(), hashlib.sha256
        ).hexdigest()[:32]

    def submit(self, session_id, *, bubble_owner=False, ended=False):
        if not isinstance(session_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{16,128}", session_id
        ):
            return
        if not ended and self._inbox.qsize() > self._inbox_limit:
            return  # shed presence only; an end mark is always kept
        self._inbox.put((session_id, bool(bubble_owner), bool(ended)))
        self._dispatch()

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
            with self._identity_lock:
                identifier = self._sessions.get(session)
                created = identifier is None
                if created:
                    identifier = uuid.uuid4().hex
                    self._sessions[session] = identifier
                    # GC may run while any lock is held. SimpleQueue.put is
                    # reentrant; finalizers never acquire our locks or perform
                    # IO. The sender owns detach.
                    weakref.finalize(session, self._finalized_transports.put, identifier)
            if created:
                self._ensure_worker()
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

    # -- explicit reports (presence-lane worker threads) -------------------
    def _blocked_locked(self, identifier, bubble_owner=False):
        """(owner id, rejection reason) for a connection that may no longer report."""
        if identifier in self._detached_transports:
            return identifier, 'session_ended'
        if not bubble_owner:
            identifier = self._owner_for(identifier)
        if self._claude_lifecycle.ended(identifier):
            return identifier, 'session_ended'
        return identifier, None

    def report_state(self, context, state, label=None, subagent_count=None):
        """Explicit metadata-only state; never infers work from arbitrary calls."""
        from overlay.state_api import validate_payload

        identity = self.context_identity(context)
        if identity is None:
            return {"accepted": False, "reason": "context_unavailable"}
        identifier, bubble_owner = identity
        if bubble_owner:
            return {"accepted": False, "reason": "bubble_controller_owned"}
        with self._state_lock:
            identifier, reason = self._blocked_locked(identifier)
        if reason:
            return {"accepted": False, "reason": reason}
        payload = {"provider": "mcp", "session_id": identifier, "state": state}
        if label is not None:
            payload["label"] = label
        if subagent_count is not None:
            payload["subagent_count"] = subagent_count
        checked, error = validate_payload(payload)
        if error:
            return {"accepted": False, "reason": "invalid_metadata"}
        ticket = Ticket()
        with self._state_lock:
            if self._blocked_locked(identifier, True)[1]:
                return {"accepted": False, "reason": "session_ended"}
            self._outbox.put_explicit_state(("mcp", identifier), checked, ticket=ticket)
        self._dispatch()
        accepted, reason = self._outcome(ticket)
        return {"accepted": accepted, "reason": reason}

    def report_title(self, context, title):
        """Deliver an explicit title to this connection's already-live row only."""
        from overlay.state_api import validate_title_payload

        identity = self.context_identity(context)
        if identity is None:
            return {"accepted": False, "reason": "context_unavailable"}
        identifier, bubble_owner = identity
        with self._state_lock:
            identifier, reason = self._blocked_locked(identifier, bubble_owner)
        if reason:
            return {"accepted": False, "reason": reason}
        provider = "claude" if bubble_owner else "mcp"
        payload = {"provider": provider, "session_id": identifier, "title": title}
        checked, error = validate_title_payload(payload)
        if error:
            return {"accepted": False, "reason": "invalid_metadata"}
        # This carries no state or caller-supplied identity.
        presence = self._presence_payload(provider, identifier, bubble_owner)
        ticket = Ticket()
        with self._state_lock:
            if self._blocked_locked(identifier, True)[1]:
                return {"accepted": False, "reason": "session_ended"}
            if bubble_owner:
                self._outbox.put_title((provider, identifier), checked, self._next_seq(),
                                       presence=presence, ticket=ticket)
            else:
                self._queue_title_locked(identifier, checked['title'], payload=checked,
                                         ticket=ticket, presence=presence)
        self._dispatch()
        accepted, reason = self._outcome(ticket)
        return {"accepted": accepted, "reason": reason}

    def report_project(self, context, project_name=None, cwd=None, *, source=None):
        from overlay.state_api import project_display_name
        name = project_display_name(project_name,cwd)
        if name is None:
            return {"accepted":False,"reason":"invalid_metadata"}
        identity = self.context_identity(context)
        if identity is None:
            return {"accepted":False,"reason":"context_unavailable"}
        identifier,bubble_owner = identity
        ticket = Ticket()
        with self._state_lock:
            transport = identifier
            identifier, reason = self._blocked_locked(identifier, bubble_owner)
            if reason:
                return {"accepted": False, "reason": reason}
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
            presence = self._presence_payload(provider, identifier, bubble_owner)
            if bubble_owner:
                self._outbox.put_project((provider, identifier),
                    {"provider":provider,"session_id":identifier,"project_name":name},
                    self._next_seq(), presence=presence, ticket=ticket)
            else:
                record = self._title_record(identifier)
                effective = max(record['project_source'] if record['project'] else 0,
                                record['project_pending_source'])
                # Lower-quality automatic metadata cannot replace an explicit name,
                # including one that is still waiting for delivery.
                if (record['project'] or record['project_pending']) and source < effective:
                    return {"accepted":True,"reason":"retained_higher_quality"}
                self._queue_project_locked(identifier, name, source, ticket=ticket, presence=presence)
        self._dispatch()
        accepted, reason = self._outcome(ticket)
        return {"accepted": accepted, "reason": reason}

    # -- native lifecycle reports ------------------------------------------
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
        with self._state_lock:
            candidates = [key for key in (self._binding_owners.get(binding),)
                          if key is not None and key in self._parent_activity]
        if len(candidates) != 1:
            return {'accepted': False, 'reason': 'parent_identity_ambiguous_or_missing'}
        parent = candidates[0]
        tracker = self._claude_lifecycle if provider == 'claude' else self._codex_lifecycle
        with tracker.transaction(parent) as entry:
            outcome = self._child_update_locked(tracker, entry, identity, binding, parent, provider,
                                                event, agent_id, tool_name, tool_use_id, turn_id)
        if isinstance(outcome, dict):
            return outcome
        accepted, reason = self._outcome(outcome)
        return {'accepted': accepted, 'reason': reason}

    def _child_update_locked(self, tracker, entry, identity, binding, parent, provider,
                             event, agent_id, tool_name, tool_use_id, turn_id):
        """Runs inside the parent entry lock; returns a rejection dict or a delivery Ticket."""
        if entry is None or entry.ended:
            return {'accepted': False, 'reason': 'parent_not_active'}
        with self._state_lock:
            activity = self._parent_activity.get(parent)
            if activity is None:
                return {'accepted': False, 'reason': 'parent_identity_ambiguous_or_missing'}
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
            ticket = Ticket()
            self._outbox.put_lifecycle_state(
                ('mcp', parent),
                {'provider': 'mcp', 'session_id': parent, 'state': parent_state,
                 'agent_name': 'Claude' if provider == 'claude' else 'Codex',
                 'subagent_count': summary['total'], 'child_activity': summary,
                 'child_update': True, 'semantic': packet},
                provider, ticket=ticket)
        self._dispatch()
        return ticket

    def _native_event(self, provider, event, checked, tool_name, transport, binding, fold, title_context):
        tracker = self._claude_lifecycle if provider == 'claude' else self._codex_lifecycle
        agent_name = 'Claude' if provider == 'claude' else 'Codex'
        lifecycle = True if provider == 'claude' else 'codex'
        cached = self._cached_title(binding)  # sqlite read, outside every lock
        identifier = self._owner_for(transport, binding)
        ticket = Ticket()
        ended = False
        with tracker.transaction(identifier) as entry:
            if entry is None:
                return {"accepted": False, "reason": "lifecycle_busy_or_full"}
            state, reason = fold(entry, checked["event"], checked["turn"], checked["tool"], checked["name"])
            if reason:
                return {"accepted": False, "reason": reason}
            with self._state_lock:
                self._bind_owner(transport, binding, identifier)
                if state == "ended":
                    ended = True
                    self._children.forget(identifier)
                    self._parent_activity.pop(identifier, None)
                    # Replaces any queued presence/state for this exact connection.
                    self._outbox.put_ended(("mcp", identifier),
                                           {"provider": "mcp", "session_id": identifier, "ended": True},
                                           lifecycle=True, ticket=ticket)
                else:
                    packet = self._decorate_parent_activity(identifier, provider, entry, checked, state, tool_name)
                    summary = self._children.summary(identifier)
                    self._outbox.put_lifecycle_state(
                        ("mcp", identifier),
                        {"provider": "mcp", "session_id": identifier,
                         "state": state, "agent_name": agent_name,
                         'subagent_count': summary['total'],
                         'child_activity': summary,
                         "semantic": packet},
                        lifecycle, ticket=ticket)
                    self._restore_locked(identifier, binding, cached)
            self._dispatch()
        accepted, reason = self._outcome(ticket)
        result = {"accepted": accepted, "reason": reason}
        if (accepted and not ended and event in ("UserPromptSubmit", "PreToolUse")
                and self._claim_title_request(identifier, checked['turn'], tracker)):
            result["hookSpecificOutput"] = {"hookEventName": event,
                                            "additionalContext": title_context}
        return result

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
        if not self._check_binding(identifier, binding, provider='claude'):
            return {'accepted': False, 'reason': 'native_identity_conflict'}
        return self._native_event('claude', event, checked, tool_name, identifier, binding, fold, TITLE_CONTEXT)

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
        if not self._check_binding(identifier, binding, provider='codex'):
            return {'accepted': False, 'reason': 'native_identity_conflict'}
        return self._native_event('codex', event, checked, tool_name, identifier, binding, fold, TITLE_CONTEXT)


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
