import ast
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from core.integrations.codex_lifecycle import LifecycleTracker, TurnState, fold, validate_event
from core.integrations.mcp_presence import PresenceReporter
from overlay.stm_server import STMServer
from overlay.session_registry import NATIVE_WORKING_LEASE_SECONDS


def context(transport='connection', *, bubble=False):
    request = SimpleNamespace(headers={'x-engram-bubble-owner': 'owned'} if bubble else {},
                              scope={'engram.presence.transport_id': transport,
                                     'engram.presence.local': bubble})
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


class CodexLifecycleTests(unittest.TestCase):
    def test_native_opaque_turn_and_private_tokens(self):
        for turn in ('1', 'turn_abc-27', '019c6e27-e55b-73d1-87d8-4e01f1f75043'):
            checked, error = validate_event('PreToolUse', turn, 'Bash', 'call_123')
            self.assertIsNone(error)
            self.assertEqual(len(checked['turn']), 64)
            self.assertNotIn('Bash', repr(checked))
            self.assertNotIn('call_123', repr(checked))
        for bad in (None, True, '${turn_id}', 'private prompt', 'C:/private', 'x' * 129):
            self.assertEqual(validate_event('Stop', bad)[1], 'missing_or_invalid_turn')
        for event in ('SessionEnd', 'SubagentStop', 'PostToolUseFailure', 'SubagentStart'):
            self.assertEqual(validate_event(event, '1')[1], 'invalid_event')
        for tool in ('engram_report_codex_event', 'mcp__engram__engram_report_codex_event',
                     'mcp__other__engram_report_claude_event'):
            self.assertEqual(validate_event('PreToolUse', '1', tool)[1], 'self_report_ignored')

    def test_terminal_and_interrupt_fence_late_tools_then_accept_next_prompt(self):
        for terminal in ('Stop', 'Interrupt'):
            state = TurnState()
            self.assertEqual(fold(state, 'UserPromptSubmit', 'one'), ('working', None))
            self.assertEqual(fold(state, terminal, 'one'), ('ready', None))
            self.assertEqual(fold(state, 'PostToolUse', 'one'),
                             (None, 'stale_turn' if terminal == 'Interrupt' else 'completed_turn'))
            self.assertEqual(fold(state, 'UserPromptSubmit', 'two'), ('working', None))
            self.assertEqual(fold(state, 'Stop', 'one'), (None, 'stale_turn'))
            self.assertEqual(fold(state, 'Stop', 'unestablished'), (None, 'unestablished_turn'))

    def test_parallel_approvals_and_ambiguous_idless_permissions(self):
        state = TurnState()
        fold(state, 'PreToolUse', '1', 'a', 'read')
        fold(state, 'PreToolUse', '1', 'b', 'write')
        fold(state, 'PermissionRequest', '1', None, 'read')
        fold(state, 'PermissionRequest', '1', None, 'write')
        self.assertEqual(fold(state, 'PostToolUse', '1', 'a'), ('needs_input', None))
        self.assertEqual(fold(state, 'PostToolUse', '1', 'b'), ('working', None))
        fold(state, 'PermissionRequest', '1', None, 'unknown')
        self.assertEqual(fold(state, 'PostToolUse', '1', 'c'), ('needs_input', None))
        self.assertEqual(fold(state, 'Interrupt', '1'), ('ready', None))

    def test_stop_attempt_allows_real_same_turn_pretool_continuation(self):
        state = TurnState()
        fold(state, 'UserPromptSubmit', '1')
        fold(state, 'Stop', '1')
        self.assertEqual(fold(state, 'PostToolUse', '1', 'late'), (None, 'completed_turn'))
        self.assertEqual(fold(state, 'PreToolUse', '1', 'new', 'read'), ('working', None))

    def test_idless_permission_recovers_matching_post_after_reconnect(self):
        state = TurnState()
        fold(state, 'PermissionRequest', '1', None, 'read')
        self.assertEqual(fold(state, 'PostToolUse', '1', 'call', 'read'), ('working', None))
        fold(state, 'PreToolUse', '1', 'a', 'read')
        fold(state, 'PreToolUse', '1', 'b', 'read')
        fold(state, 'PermissionRequest', '1', None, 'read')
        self.assertEqual(fold(state, 'PostToolUse', '1', 'a', 'read'), ('needs_input', None))
        self.assertEqual(fold(state, 'PostToolUse', '1', 'b', 'read'), ('working', None))

    def test_fresh_reconnect_accepts_actual_terminal_or_tool_evidence(self):
        for event, expected in [('Stop', 'ready'), ('Interrupt', 'ready'), ('PostToolUse', 'working')]:
            self.assertEqual(fold(TurnState(), event, 'current'), (expected, None))

    def test_bounded_connection_turn_and_tool_maps(self):
        tracker = LifecycleTracker(capacity=1)
        with tracker.transaction('first') as state:
            fold(state, 'UserPromptSubmit', 'one')
            state.inflight = {str(i): None for i in range(64)}
            self.assertEqual(fold(state, 'PreToolUse', 'one', 'overflow'), (None, 'tool_capacity'))
            state.retired = {str(i) for i in range(256)}
            self.assertEqual(fold(state, 'UserPromptSubmit', 'two'), (None, 'turn_capacity'))
        with tracker.transaction('second') as missing:
            self.assertIsNone(missing)
        tracker.forget('first')
        with tracker.transaction('second') as present:
            self.assertIsNotNone(present)

    def test_reporter_reuses_transport_key_no_raw_metadata_and_hints_once(self):
        reporter = PresenceReporter()
        calls = []
        def send(payload, **kwargs):
            calls.append((payload, kwargs))
            kwargs['title_status']['missing'] = True
            return True
        reporter._send = send
        ctx = context()
        first = reporter.report_codex_event(ctx, 'UserPromptSubmit', 'opaque-turn')
        second = reporter.report_codex_event(ctx, 'PreToolUse', 'opaque-turn', 'Bash', 'call_abc')
        self.assertTrue(first['accepted'])
        self.assertIn('hookSpecificOutput', first)
        self.assertNotIn('hookSpecificOutput', second)
        self.assertEqual(calls[0][0]['agent_name'], 'Codex')
        self.assertEqual(calls[0][0]['session_id'], reporter.context_identity(ctx)[0])
        self.assertEqual(calls[0][1]['lifecycle'], 'codex')
        for private in ('opaque-turn', 'Bash', 'call_abc'):
            self.assertNotIn(private, repr(calls))
        reporter._send = Mock(return_value=True)
        self.assertEqual(reporter.report_codex_event(context(bubble=True), 'Stop', '1')['reason'], 'bubble_controller_owned')
        reporter._send.assert_not_called()

    def test_actual_authenticated_stm_codex_header_lease_and_single_card(self):
        with tempfile.TemporaryDirectory() as directory:
            discovery = Path(directory) / 'state.json'
            server = STMServer(port=0, state_discovery_path=discovery)
            server.start()
            try:
                reporter = PresenceReporter(discovery, timeout=2)
                ctx = context()
                self.assertTrue(reporter.report_codex_event(ctx, 'UserPromptSubmit', '1')['accepted'])
                self.assertTrue(reporter.report_title(ctx, 'Safe test title')['accepted'])
                self.assertTrue(reporter.report_codex_event(ctx, 'PreToolUse', '1', 'Bash', 'call_1')['accepted'])
                rows = server._state_registry.snapshot()
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]['agent_name'], rows[0]['label']), ('Codex', 'Safe test title'))
                self.assertIn(rows[0]['key'], server._state_registry._lifecycle_deadlines)
                self.assertTrue(reporter.report_codex_event(ctx, 'Stop', '1')['accepted'])
                self.assertEqual(server._state_registry.snapshot()[0]['state'], 'ready')
                self.assertNotIn(rows[0]['key'], server._state_registry._lifecycle_deadlines)
                self.assertFalse(reporter.report_codex_event(ctx, 'PostToolUse', '1', 'Bash', 'call_1')['accepted'])
            finally:
                server.stop()

    def test_registered_signature_excludes_raw_session_and_hook_output_is_nonblocking(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'mcp_server.py').read_text(encoding='utf-8-sig'))
        fn = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'engram_report_codex_event')
        self.assertEqual([arg.arg for arg in fn.args.args], ['event', 'turn_id', 'tool_name', 'tool_use_id', 'native_session_id', 'agent_id'])
        text = ast.unparse(fn)
        self.assertNotIn("'suppressOutput'", text)
        self.assertNotIn("'decision'", text)


if __name__ == '__main__':
    unittest.main()
