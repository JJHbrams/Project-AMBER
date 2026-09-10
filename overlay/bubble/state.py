"""Metadata-only state folding for one locally owned bubble lifetime."""

import threading
import uuid

from overlay.state_api import validate_payload


class BubbleStateController:
    def __init__(self, registry, session_id=None, *, resume_session_id=None, saved_title=None):
        self.registry = registry
        self.session_id = session_id or uuid.uuid4().hex
        self.env_overrides = {'ENGRAM_BUBBLE_SESSION_ID': self.session_id}
        self._lock = threading.RLock()
        self._active = True
        self._base_state = 'unknown'
        self._pending = set()
        self._confirmed_provider_id = None
        self._expected_resume_id = resume_session_id
        from overlay.state_api import validate_bubble_title_metadata
        self._saved_candidate = validate_bubble_title_metadata(saved_title)
        self._attempt_started = False
        self._manual_touched = False
        self._saved_title_signature = None
        self._terminal_title_checkpoint = None
        self._publish()

    @property
    def active(self):
        with self._lock:
            return self._active

    def prepare_attempt(self, resume_session_id):
        with self._lock:
            if not self._active:
                return
            if self._attempt_started:
                current = self.registry.title_metadata(f'claude:{self.session_id}')
                candidate = (current if self._confirmed_provider_id == resume_session_id and resume_session_id
                             else self._saved_candidate if self._expected_resume_id == resume_session_id and resume_session_id
                             else None)
                self._rotate_owner()
                self._saved_candidate = candidate
            elif resume_session_id != self._expected_resume_id:
                self._saved_candidate = None
            self._expected_resume_id = resume_session_id
            self._confirmed_provider_id = None
            self._attempt_started = True

    def _rotate_owner(self):
        old = next((r for r in self.registry.snapshot() if r['key'] == f'claude:{self.session_id}'), None)
        self.registry.remove('claude', self.session_id)
        self.session_id = uuid.uuid4().hex
        self.env_overrides = {'ENGRAM_BUBBLE_SESSION_ID': self.session_id}
        self._pending.clear()
        self._manual_touched = False
        self._saved_title_signature = None
        self._publish()
        if old and old.get('project_name'):
            self.registry.set_project_name('claude', self.session_id, old['project_name'])

    def _publish(self):
        if self._active:
            self.registry.claim({'provider': 'claude', 'session_id': self.session_id,
                                 'state': 'needs_input' if self._pending else self._base_state,
                                 'is_bubble': True})

    def event(self, kind, is_error=False):
        with self._lock:
            if kind in ('submit', 'speech', 'thought', 'tool_use', 'tool_result', 'retry'):
                self._base_state = 'blocked' if kind == 'tool_result' and is_error else 'working'
            elif kind == 'turn_end':
                self._base_state = 'blocked' if is_error else 'ready'
            elif kind == 'error':
                self._base_state = 'blocked'
            self._publish()

    def approval_requested(self, request_id):
        with self._lock:
            if self._active:
                self._pending.add(request_id)
                self._publish()

    def approval_settled(self, request_id):
        with self._lock:
            self._pending.discard(request_id)
            self._publish()

    def bind_provider_session(self, session_id):
        payload, error = validate_payload({'provider': 'claude', 'session_id': session_id, 'state': 'unknown'})
        if error:
            return
        with self._lock:
            if self._active:
                if self._confirmed_provider_id and self._confirmed_provider_id != session_id:
                    self._rotate_owner()
                    self._saved_candidate = None
                key = f'claude:{self.session_id}'
                if self._confirmed_provider_id is None:
                    current = self.registry.title_metadata(key)
                    if self._expected_resume_id == session_id and self._saved_candidate:
                        restored = dict(self._saved_candidate)
                        if current and current.get('producer') is not None:
                            restored['producer'] = current['producer']
                        if self._manual_touched:
                            restored['manual'] = current['manual'] if current else None
                        self.registry.restore_title_metadata(key, restored)
                    elif self._expected_resume_id and self._expected_resume_id != session_id:
                        self.registry.restore_title_metadata(key, {'producer':None, 'manual':None})
                    self._saved_candidate = None
                self._confirmed_provider_id = session_id
                self.registry.bind_alias('claude', payload['session_id'], self.session_id)

    def missing_title(self):
        # Host-owned bubble has a fixed effective title, independent of saved metadata.
        return False

    def title_checkpoint(self):
        with self._lock:
            if not self._active or not self._confirmed_provider_id:
                return None
            metadata = self.registry.title_metadata(f'claude:{self.session_id}')
            if metadata is None:
                return None
            signature = (self.session_id, self._confirmed_provider_id, metadata['producer'], metadata['manual'])
            if signature == self._saved_title_signature:
                return None
            return signature, metadata

    def accept_title_checkpoint(self, checkpoint, enqueue):
        """Serialize queue acceptance against retirement; never perform disk I/O."""
        with self._lock:
            signature, metadata = checkpoint
            if signature[:2] != (self.session_id, self._confirmed_provider_id):
                return False
            if self._active:
                if (signature == self._saved_title_signature
                        or self.registry.title_metadata(f'claude:{self.session_id}') != metadata):
                    return False
            elif checkpoint is not self._terminal_title_checkpoint:
                return False
            if not enqueue():
                return False
            self._saved_title_signature = signature
            self._terminal_title_checkpoint = None
            return True

    def heartbeat(self):
        with self._lock:
            self._publish()

    def set_label(self, label):
        """Explicit local metadata only; no inference from conversation text."""
        with self._lock:
            changed = self._active and self.registry.set_label(f'claude:{self.session_id}', label)
            if changed:
                self._manual_touched = True
            return changed

    def set_project(self, *, cwd=None, project_name=None):
        from overlay.state_api import project_display_name
        name = project_display_name(project_name,cwd)
        with self._lock:
            return bool(self._active and name and self.registry.set_project_name('claude',self.session_id,name))

    def retire(self):
        with self._lock:
            if not self._active:
                return None
            self._active = False
            self._pending.clear()
            metadata = self.registry.retire_title_owner('claude', self.session_id)
            if self._confirmed_provider_id and metadata is not None:
                signature = (self.session_id, self._confirmed_provider_id,
                             metadata['producer'], metadata['manual'])
                if signature != self._saved_title_signature:
                    self._terminal_title_checkpoint = (signature, metadata)
                    return self._terminal_title_checkpoint
            return None
