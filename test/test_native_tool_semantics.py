import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

from core.integrations import claude_lifecycle, codex_lifecycle
from core.integrations.tool_semantics import semantic_packet, validate_semantic, tool_category
from overlay.session_registry import SessionStateRegistry, NATIVE_WORKING_LEASE_SECONDS
from overlay.state_api import validate_payload, validate_lifecycle_payload
from overlay.stm_server import STMServer


def payload(key='a', state='working'):
    return {'provider': 'mcp', 'session_id': key, 'state': state, 'agent_name': 'Claude'}


def packet(seq, category=None, kind='tool.started'):
    return {'seq': seq, 'active_category': category,
            'events': [{'type': kind, 'category': category if kind == 'tool.started' else None}]}


class NativeToolSemanticsTests(unittest.TestCase):
    def test_classification_and_concurrent_categories_both_providers(self):
        self.assertEqual(tool_category('kg_search'), 'memory')
        self.assertEqual(tool_category('web_search'), 'search')
        self.assertEqual(tool_category('Bash'), 'execute')
        for module in (claude_lifecycle, codex_lifecycle):
            state = module.TurnState()
            turn = str(uuid.uuid4())
            def emit(event, name=None, call=None):
                args = (event, turn, None, name, call) if module is claude_lifecycle else (event, turn, name, call)
                checked, error = module.validate_event(*args)
                self.assertIsNone(error)
                result, error = module.fold(state, checked['event'], checked['turn'], checked['tool'], checked['name'])
                self.assertIsNone(error)
                return semantic_packet(state, checked, result)
            first = emit('PreToolUse', 'web_search', 'call_A')
            second = emit('PreToolUse', 'kg_search', 'call_B')
            completed = emit('PostToolUse', 'kg_search', 'call_B')
            self.assertEqual(first['active_category'], 'search')
            self.assertEqual(second['active_category'], 'memory')
            self.assertEqual(completed['events'], [{'type': 'tool.completed', 'category': None},
                                                  {'type': 'tool.started', 'category': 'search'}])
            waiting = emit('PermissionRequest', 'web_search')
            self.assertIsNone(waiting['active_category'])
            self.assertEqual(waiting['events'], [])
            stopped = emit('Stop')
            self.assertIsNone(stopped['active_category'])
            self.assertFalse(state.semantic_categories)
            for private in ('call_A', 'call_B', 'web_search', 'kg_search', turn):
                self.assertNotIn(private, json.dumps([first, second, completed, waiting, stopped]))
            checked, _ = (module.validate_event('PostToolUse', turn, None, 'web_search', 'call_A')
                          if module is claude_lifecycle else module.validate_event('PostToolUse', turn, 'web_search', 'call_A'))
            self.assertEqual(module.fold(state, checked['event'], checked['turn'], checked['tool'], checked['name'])[1], 'completed_turn')

    def test_private_envelope_validation_not_public_fields(self):
        body = {**payload(), 'semantic': packet(1, 'search')}
        self.assertIsNotNone(validate_payload(body)[1])
        checked, semantic, error = validate_lifecycle_payload(body, 'Claude')
        self.assertIsNone(error)
        self.assertNotIn('semantic', checked)
        for bad in ({'seq': True, 'events': [], 'active_category': None},
                    {'seq': 1, 'events': [], 'active_category': {'raw': 'secret'}},
                    {**packet(1, 'search'), 'raw_tool': 'secret'},
                    packet(1, 'not-allowed'),
                    {'seq': 1, 'events': [{'type': 'tool.started', 'category': 'search', 'raw': 'secret'}], 'active_category': 'search'}):
            self.assertIsNone(validate_semantic(bad))

    def test_failed_tool_restores_generic_generation_under_error_transient(self):
        state = claude_lifecycle.TurnState()
        turn = str(uuid.uuid4())
        for event in ('PreToolUse', 'PostToolUseFailure'):
            checked, error = claude_lifecycle.validate_event(event, turn, None, 'web_search', 'call')
            self.assertIsNone(error)
            result, error = claude_lifecycle.fold(state, checked['event'], checked['turn'], checked['tool'], checked['name'])
            self.assertIsNone(error)
            semantic = semantic_packet(state, checked, result)
        self.assertEqual(semantic['events'], [{'type': 'tool.failed', 'category': None}])
        self.assertIsNone(semantic['active_category'])
        app = self.make_host()
        app._session_registry.upsert_lifecycle(payload(), semantic=packet(1, 'search'))
        self.refresh(app)
        calls = Mock()
        calls.attach_mock(app._overlay_events.select_session_state, 'snapshot')
        calls.attach_mock(app._overlay_events.publish, 'publish')
        app._session_registry.upsert_lifecycle(payload(), semantic=semantic)
        self.refresh(app, now=.2)
        from unittest.mock import call
        self.assertEqual(calls.mock_calls, [call.snapshot('working'), call.publish('tool.failed', 'error', {})])
        app.character.set_sprite_state.assert_called_with('error', transient=True)

    def test_atomic_revision_bounded_queue_no_public_metadata_and_expiry(self):
        now = [0.0]
        registry = SessionStateRegistry(clock=lambda: now[0])
        for seq in range(1, 71):
            registry.upsert_lifecycle(payload(), semantic=packet(seq, 'search'))
        self.assertEqual(len(registry._native_semantics['mcp:a']['queue']), 64)
        self.assertIsNone(registry.upsert_lifecycle(payload(state='ready'), semantic=packet(69, None, 'generation.completed')))
        self.assertEqual(registry.snapshot()[0]['state'], 'working')
        self.assertNotIn('semantic', repr(registry.snapshot()))
        now[0] = 3
        self.assertEqual(registry.consume_native_semantics('mcp:a', 'working')['events'], [])
        now[0] = NATIVE_WORKING_LEASE_SECONDS + 1
        self.assertEqual(registry.snapshot()[0]['state'], 'unknown')
        self.assertIsNone(registry.consume_native_semantics('mcp:a', 'unknown')['active_category'])
        registry.remove('mcp', 'a')
        self.assertFalse(registry._native_semantics)

    def make_host(self):
        from overlay.main import OverlayApp
        app = object.__new__(OverlayApp)
        app.character = Mock()
        app._overlay_events = Mock()
        app._session_registry = SessionStateRegistry(clock=lambda: 0)
        app._native_semantic_key = 'mcp:a'
        return app

    def test_known_native_no_tool_uses_pose_not_reasoning_event(self):
        for agent in ('Claude', 'Codex', 'MCP'):
            app = self.make_host()
            row = app._session_registry.upsert_lifecycle(
                {**payload(), 'agent_name': agent}, semantic=packet(1, None, 'generation.started'))
            with patch('overlay.main.time.monotonic', return_value=0):
                app._refresh_native_session_semantics(row, 'working')
            hint = 'thought' if agent in ('Claude', 'Codex') else 'generating'
            app.character.set_sprite_state.assert_called_with(hint, transient=False)
            app._overlay_events.publish.assert_called_with('generation.started', hint, {})
            self.assertEqual(row['state'], 'working')
            app._native_semantic_key = 'mcp:other'
            app._refresh_native_session_semantics(row, 'working')
            app.character.set_sprite_state.assert_called_with(hint)
            self.assertFalse(any(call.args[0] == 'generation.thinking'
                                 for call in app._overlay_events.publish.call_args_list))
            app._refresh_native_session_semantics(row, 'needs_input')
            app.character.set_sprite_state.reset_mock()
            app._refresh_native_session_semantics(row, 'working')
            app.character.set_sprite_state.assert_called_with(hint)

    def refresh(self, app, key='a', state='working', now=0):
        with patch('overlay.main.time.monotonic', return_value=now):
            app._refresh_native_session_semantics({'key': 'mcp:' + key, 'state': state}, state)

    def test_selected_fast_pair_visible_dwell_and_terminal_preemption(self):
        app = self.make_host()
        registry = app._session_registry
        registry.upsert_lifecycle(payload(), semantic=packet(1, 'search'))
        registry.upsert_lifecycle(payload(), semantic=packet(2, None, 'tool.completed'))
        self.refresh(app)
        app._overlay_events.publish.assert_called_once_with('tool.started', 'search', {'category': 'search'})
        self.refresh(app, now=.1)
        self.assertEqual(app._overlay_events.publish.call_count, 1)
        self.refresh(app, now=.2)
        app._overlay_events.publish.assert_called_with('tool.completed', 'generating', {})
        registry.upsert_lifecycle(payload(), semantic=packet(3, 'memory'))
        self.refresh(app, now=.3)
        registry.upsert_lifecycle(payload(state='ready'), semantic=packet(4, None, 'generation.completed'))
        count = app._overlay_events.publish.call_count
        self.refresh(app, state='ready', now=.31)
        self.assertFalse(app._native_semantic_pending)
        self.assertEqual(app._overlay_events.publish.call_count, count)  # Normal state renders completion once.

    def test_unselected_events_noop_reselect_durable_only_and_permission_preempts(self):
        app = self.make_host()
        registry = app._session_registry
        registry.upsert_lifecycle(payload(), semantic=packet(1, 'search'))
        self.refresh(app)
        app._overlay_events.publish.reset_mock()
        registry.upsert_lifecycle(payload('b'), semantic=packet(1, 'memory'))
        self.refresh(app, now=.2)
        app._overlay_events.publish.assert_not_called()
        self.refresh(app, key='b', now=.3)
        app._overlay_events.publish.assert_called_once_with('tool.started', 'memory', {'category': 'memory'})
        registry.upsert_lifecycle(payload('b', 'needs_input'), semantic={'seq': 2, 'events': [], 'active_category': None})
        app._overlay_events.publish.reset_mock()
        self.refresh(app, key='b', state='needs_input', now=.31)
        app._overlay_events.publish.assert_not_called()
        self.assertFalse(app._native_semantic_pending)

    def test_host_retains_original_expiry_and_reselect_never_invents_completion(self):
        app = self.make_host()
        registry = app._session_registry
        registry.upsert_lifecycle(payload(), semantic=packet(1, 'search'))
        registry.upsert_lifecycle(payload(), semantic=packet(2, None, 'tool.completed'))
        self.refresh(app, now=1.9)
        app._overlay_events.publish.reset_mock()
        self.refresh(app, now=2.11)
        app._overlay_events.publish.assert_not_called()  # Original t=2 deadline, not drain+2.
        app._overlay_events.select_session_state.assert_called_with('working')
        registry.upsert_lifecycle(payload('b'), semantic=packet(1, None, 'tool.completed'))
        self.refresh(app, key='b', now=2.2)
        app._overlay_events.publish.assert_not_called()

    def test_actual_stm_envelope_rejects_stale_state_and_raw_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            server = STMServer(port=0, state_discovery_path=Path(directory) / 'state.json')
            server.start()
            try:
                info = json.loads(server._state_discovery_path.read_text())
                def post(body):
                    req = Request(f'http://127.0.0.1:{server.port}/state', data=json.dumps(body).encode(),
                                  headers={'Authorization': 'Bearer ' + info['token'], 'X-Engram-Lifecycle': 'claude'})
                    return urlopen(req, timeout=2)
                with post({**payload(), 'semantic': packet(2, 'search')}) as response:
                    self.assertNotIn('semantic', json.loads(response.read())['session'])
                with self.assertRaises(HTTPError) as stale:
                    post({**payload(state='ready'), 'semantic': packet(1, None, 'generation.completed')})
                self.assertEqual(stale.exception.code, 409)
                self.assertEqual(server._state_registry.snapshot()[0]['state'], 'working')
                with self.assertRaises(HTTPError) as invalid:
                    post({**payload(), 'semantic': {**packet(3, 'memory'), 'raw_tool': 'PRIVATE'}})
                self.assertEqual(invalid.exception.code, 400)
            finally:
                server.stop()


if __name__ == '__main__':
    unittest.main()
