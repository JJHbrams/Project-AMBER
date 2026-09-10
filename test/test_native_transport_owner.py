"""Real isolated state HTTP: multiple MCP contexts, one verified native owner."""
import json
import gc
import concurrent.futures
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.integrations.mcp_presence import PresenceReporter
from overlay.stm_server import STMServer


def context(transport, principal='local'):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
        headers={}, scope={'engram.presence.transport_id': transport,
                           'engram.remote_principal': principal})))


class NativeTransportOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        guard = patch.dict(os.environ, {'ENGRAM_SMOKE_DB_DIR': self.temp.name})
        guard.start()
        self.addCleanup(guard.stop)
        self.discovery = Path(self.temp.name) / 'state.json'
        self.server = STMServer(port=0, state_discovery_path=self.discovery)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.reporter = PresenceReporter(self.discovery, timeout=2)
        self.addCleanup(self.reporter.stop)
        self.a, self.b = context('transport-a'), context('transport-b')
        self.turn = '00000000-0000-0000-0000-000000000001'

    def emit(self, ctx, event, *, provider='codex', native='native-synthetic', **kwargs):
        return getattr(self.reporter, 'report_' + provider + '_event')(
            ctx, event, self.turn, native_session_id=native, **kwargs)

    def rows(self):
        return self.server._state_registry.snapshot()

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('isolated worker condition timeout')

    def test_permission_a_matching_post_stop_b_single_owner_both_providers(self):
        for provider in ('claude', 'codex'):
            with self.subTest(provider=provider):
                a, b = context(provider + '-a'), context(provider + '-b')
                self.assertTrue(self.emit(a, 'PermissionRequest', provider=provider, tool_name='Read')['accepted'])
                owner = self.reporter.context_identity(a)[0]
                self.assertTrue(self.reporter.report_title(a, 'Safe shared title')['accepted'])
                # B's ordinary MCP bootstrap may already have made an alias row.
                self.assertTrue(self.reporter.report_title(b, 'Safe shared title')['accepted'])
                self.assertTrue(self.emit(b, 'PostToolUse', provider=provider, tool_name='Read', tool_use_id='call-a')['accepted'])
                matching = [r for r in self.rows() if r['agent_name'] == provider.title()]
                self.assertEqual(len(matching), 1)
                self.assertEqual(matching[0]['session_id'], owner)
                self.assertEqual(matching[0]['state'], 'working')
                self.assertTrue(self.emit(b, 'Stop', provider=provider)['accepted'])
                self.assertEqual(next(r for r in self.rows() if r['session_id'] == owner)['state'], 'ready')
                self.assertEqual(len(self.rows()), 1 if provider == 'claude' else 2)

    def test_bindings_not_titles_determine_identity_and_conflict_before_route(self):
        for ctx, provider, native in ((self.a, 'codex', 'one'), (self.b, 'codex', 'two'),
                                      (context('third'), 'claude', 'one'),
                                      (context('remote', 'other'), 'codex', 'one')):
            self.assertTrue(self.emit(ctx, 'UserPromptSubmit', provider=provider, native=native)['accepted'])
            self.reporter.report_title(ctx, 'Identical safe title')
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(self.emit(self.a, 'Stop', native='two')['reason'], 'native_identity_conflict')
        self.assertEqual(self.emit(self.a, 'Stop', provider='claude', native=None)['reason'], 'native_identity_conflict')
        self.assertTrue(all(r['state'] == 'working' for r in self.rows()))

    def test_concurrent_pending_and_stale_turn_fences_share_one_owner(self):
        self.emit(self.a, 'UserPromptSubmit')
        self.emit(self.b, 'PreToolUse', tool_name='Other', tool_use_id='unrelated')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda name: self.emit(self.a, 'PermissionRequest', tool_name=name),
                                    ('Read', 'Write')))
        self.assertTrue(all(result['accepted'] for result in results))
        self.emit(self.b, 'PostToolUse', tool_name='Read', tool_use_id='read')
        self.assertEqual(self.rows()[0]['state'], 'needs_input')
        self.emit(self.b, 'PostToolUse', tool_name='Write', tool_use_id='write')
        self.assertEqual(self.rows()[0]['state'], 'working')
        self.emit(self.b, 'Stop')
        old_turn = self.turn
        self.turn = '00000000-0000-0000-0000-000000000002'
        self.emit(self.b, 'UserPromptSubmit')
        result = self.reporter.report_codex_event(self.a, 'Stop', old_turn,
                                                  native_session_id='native-synthetic')
        self.assertEqual(result['reason'], 'stale_turn')
        self.assertEqual(self.rows()[0]['state'], 'working')

    def test_queued_presence_and_all_metadata_route_without_alias_resurrection(self):
        self.emit(self.a, 'UserPromptSubmit')
        with self.reporter._title_lock:
            self.reporter.report_context(self.b)  # Queue before binding, send after binding.
            self.assertTrue(self.emit(self.b, 'PreToolUse', tool_name='Read', tool_use_id='a')['accepted'])
        self.assertTrue(self.reporter.report_title(self.b, 'Canonical safe title')['accepted'])
        self.assertTrue(self.reporter.report_project(self.b, project_name='Synthetic project')['accepted'])
        self.assertTrue(self.reporter.report_state(self.b, 'working')['accepted'])
        sent = threading.Event()
        original = self.reporter._send
        def send(*args, **kwargs):
            result = original(*args, **kwargs)
            sent.set()
            return result
        with patch.object(self.reporter, '_send', side_effect=send):
            self.reporter.report_context(self.b)
            self.assertTrue(sent.wait(3))
            with self.reporter._title_lock:
                pass
        self.assertEqual(len(self.rows()), 1)
        row = self.rows()[0]
        self.assertEqual(row['label'], 'Canonical safe title')
        self.assertEqual(row['project_name'], 'Synthetic project')
        self.assertEqual(row['session_id'], self.reporter.context_identity(self.a)[0])
        self.assertNotIn('native-synthetic', json.dumps(row))

    def test_owner_detach_keeps_other_member_pending_last_detach_cleans(self):
        self.emit(self.a, 'PermissionRequest', tool_name='Read')
        self.emit(self.b, 'PreToolUse', tool_name='Other', tool_use_id='other')
        self.reporter.submit(self.reporter.context_identity(self.a)[0], ended=True)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['state'], 'needs_input')
        self.assertTrue(self.emit(self.b, 'PostToolUse', tool_name='Read', tool_use_id='read')['accepted'])
        self.assertEqual(self.rows()[0]['state'], 'working')
        self.reporter.submit(self.reporter.context_identity(self.b)[0], ended=True)
        self.wait(lambda: not self.rows())
        self.assertFalse(self.reporter._binding_owners)
        self.assertFalse(self.reporter._owner_members)
        self.assertFalse(self.reporter._transport_bindings)

    def test_child_events_use_existing_canonical_parent_not_new_root(self):
        self.emit(self.a, 'UserPromptSubmit')
        self.assertTrue(self.reporter.report_project(self.a, project_name='Parent explicit', source=3)['accepted'])
        self.emit(self.b, 'PreToolUse', tool_name='spawn_agent', tool_use_id='spawn')
        child = context('child-channel')
        self.assertTrue(self.emit(child, 'SubagentStart', agent_id='child-a')['accepted'])
        self.assertEqual(self.reporter.report_project(child, project_name='Child automatic', source=2)['reason'],
                         'child_transport_owned')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['subagent_count'], 1)
        self.assertEqual(self.rows()[0]['project_name'], 'Parent explicit')
        self.assertTrue(self.emit(child, 'SubagentStop', agent_id='child-a')['accepted'])
        self.assertEqual(self.rows()[0]['state'], 'working')
        self.assertEqual(self.emit(context('unknown-child'), 'SubagentStart', native='missing', agent_id='x')['reason'],
                         'parent_identity_ambiguous_or_missing')

    def test_same_parent_transport_child_event_keeps_automatic_root_eligible(self):
        self.emit(self.a, 'UserPromptSubmit')
        self.emit(self.a, 'PreToolUse', tool_name='spawn_agent', tool_use_id='spawn')
        self.assertTrue(self.emit(self.a, 'SubagentStart', agent_id='child-a')['accepted'])
        self.assertNotIn(self.reporter.context_identity(self.a)[0], self.reporter._child_transports)
        self.assertTrue(self.reporter.report_project(self.a, project_name='Advertised root', source=2)['accepted'])

    def test_true_session_end_retires_every_bound_member(self):
        self.emit(self.a, 'UserPromptSubmit', provider='claude')
        self.emit(self.b, 'PreToolUse', provider='claude', tool_name='Read', tool_use_id='read')
        self.assertTrue(self.emit(self.b, 'SessionEnd', provider='claude')['accepted'])
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.reporter.report_title(self.a, 'Cannot resurrect title')['accepted'])
        self.reporter.report_context(self.b)
        self.assertEqual(self.rows(), [])

    def test_legacy_owner_gc_detaches_without_forgetting_remaining_member(self):
        class Session:
            pass
        session = Session()
        legacy = SimpleNamespace(session=session, request_context=SimpleNamespace(request=None))
        self.emit(legacy, 'PermissionRequest', tool_name='Read')
        self.emit(self.b, 'PreToolUse', tool_name='Other', tool_use_id='other')
        owner = self.reporter.context_identity(legacy)[0]
        # Old synchronous finalizer would deadlock acquiring this non-reentrant
        # queue lock. The new callback only enqueues; worker drains afterward.
        with self.reporter._lock:
            del legacy, session
            gc.collect()
        self.wait(lambda: owner not in self.reporter._transport_owners)
        self.assertEqual(self.rows()[0]['state'], 'needs_input')
        self.assertTrue(self.emit(self.b, 'PostToolUse', tool_name='Read', tool_use_id='read')['accepted'])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['state'], 'working')

    def test_failed_alias_retirement_is_retried_not_forgotten(self):
        self.emit(self.a, 'UserPromptSubmit')
        self.reporter.report_title(self.b, 'Alias safe title')
        alias = self.reporter.context_identity(self.b)[0]
        original = self.reporter._send
        def fail_alias(payload, **kwargs):
            if payload.get('ended') and payload['session_id'] == alias:
                return False
            return original(payload, **kwargs)
        with patch.object(self.reporter, '_send', side_effect=fail_alias):
            self.emit(self.b, 'PreToolUse', tool_name='Read', tool_use_id='read')
        self.assertIn(alias, self.reporter._pending_alias_retirements)
        self.reporter.report_context(self.b)
        self.wait(lambda: len(self.rows()) == 1)
        self.assertFalse(self.reporter._pending_alias_retirements)
