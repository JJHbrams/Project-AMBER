"""Approval correlation and real isolated HTTP regression coverage."""
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.integrations import claude_lifecycle, codex_lifecycle
from core.integrations.mcp_presence import PresenceReporter
from overlay.stm_server import STMServer


class PermissionRecoveryTests(unittest.TestCase):
    def test_candidate_ids_and_names_cannot_be_substituted(self):
        for module in (claude_lifecycle, codex_lifecycle):
            state = module.TurnState()
            emit = lambda event, tool=None, name=None: module.fold(state, event, 'turn', tool, name)
            emit('PreToolUse', 'a', 'read')
            emit('PreToolUse', 'b', 'read')
            emit('PermissionRequest', name='read')
            self.assertEqual(emit('PostToolUse', 'other', 'read')[0], 'needs_input')
            self.assertEqual(emit('PostToolUse', 'a', 'write')[0], 'needs_input')
            self.assertEqual(emit('PostToolUse', 'a', 'read')[0], 'needs_input')
            emit('PreToolUse', 'c', 'write')
            emit('PermissionRequest', name='write')
            self.assertEqual(emit('PostToolUse', 'b', 'read')[0], 'needs_input')
            self.assertEqual(emit('PostToolUse', 'c', 'wrong')[0], 'needs_input')
            self.assertEqual(emit('PostToolUse', 'c', 'write')[0], 'working')

    def test_unidentified_approval_is_not_guessed_or_timed_out(self):
        for module in (claude_lifecycle, codex_lifecycle):
            clock = [0]
            tracker = module.LifecycleTracker(clock=lambda: clock[0])
            with tracker.transaction('owned') as state:
                module.fold(state, 'PermissionRequest', 'turn')
            clock[0] = 999999
            with tracker.transaction('owned') as state:
                self.assertEqual(module.fold(state, 'PostToolUse', 'turn', 'a', 'read')[0], 'needs_input')
                self.assertEqual(module.fold(state, 'Stop', 'turn')[0], 'ready')

    def test_reconnect_first_named_approval_requires_named_post(self):
        for module in (claude_lifecycle, codex_lifecycle):
            state = module.TurnState()
            module.fold(state, 'PermissionRequest', 'turn', None, 'read')
            self.assertEqual(module.fold(state, 'PostToolUse', 'turn', 'a', 'write')[0], 'needs_input')
            self.assertEqual(module.fold(state, 'PostToolUse', 'turn', 'a')[0], 'needs_input')
            self.assertEqual(module.fold(state, 'PostToolUse', 'turn', 'a', 'read')[0], 'working')

    def test_real_http_recovers_claude_post_failure_and_codex_post(self):
        for provider, terminal in (('claude', 'PostToolUse'), ('claude', 'PostToolUseFailure'), ('codex', 'PostToolUse')):
            with self.subTest(provider=provider, terminal=terminal), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ENGRAM_SMOKE_DB_DIR': directory}):
                discovery = Path(directory) / 'state.json'
                server = STMServer(port=0, state_discovery_path=discovery)
                server.start()
                reporter = PresenceReporter(discovery, timeout=2)
                context = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
                    headers={}, scope={'engram.presence.transport_id': 'isolated-owner'})))
                emit = getattr(reporter, 'report_' + provider + '_event')
                turn = '00000000-0000-0000-0000-000000000001'
                try:
                    self.assertTrue(emit(context, 'PermissionRequest', turn, tool_name='Read')['accepted'])
                    self.assertEqual(server._state_registry.snapshot()[0]['state'], 'needs_input')
                    self.assertTrue(emit(context, terminal, turn, tool_name='Read', tool_use_id='call-a')['accepted'])
                    self.assertEqual(server._state_registry.snapshot()[0]['state'], 'working')
                    self.assertTrue(emit(context, 'Stop', turn)['accepted'])
                    self.assertEqual(server._state_registry.snapshot()[0]['state'], 'ready')
                finally:
                    reporter.stop()
                    server.stop()
