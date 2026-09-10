import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/dev'))
from codex_child_diagnostic_proxy import request_summary, response_summary
spec = importlib.util.spec_from_file_location('native_codex_child_probe', ROOT / 'scripts/dev/native_codex_child_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class NativeCodexChildProbeTests(unittest.TestCase):
    def test_command_preserves_trust_and_has_only_title_approval(self):
        command = probe.command('codex')
        self.assertIn('read-only', command)
        self.assertIn('approval_policy="never"', command)
        approvals = [arg for arg in command if 'approval_mode=' in arg]
        self.assertEqual(approvals, ['mcp_servers.engram.tools.engram_report_session_title.approval_mode="approve"'])
        self.assertNotIn('bypass', ' '.join(command))
        self.assertNotIn('--ignore-user-config', command)

    def test_output_discards_private_payloads_and_arbitrary_fields(self):
        event = {'type': 'item.completed', 'thread_id': 'PRIVATE_ID', 'item': {
            'type': 'collab_agent_tool_call', 'tool': 'spawn_agent', 'status': 'completed',
            'prompt': 'PRIVATE_PROMPT', 'arguments': 'PRIVATE_INPUT', 'result': 'PRIVATE_RESULT'}}
        output = probe.finite_event(event)
        self.assertEqual(output['collab_tool'], 'spawn_agent')
        self.assertNotIn('PRIVATE', json.dumps(output))

    def test_unique_title_correlation_finite_count_sequence(self):
        rows = [{'key': 'PRIVATE_NATIVE_KEY', 'label': 'owned', 'subagent_count': 2,
                 'state': 'working', 'child_activity': {'counts': {}}, 'raw': 'PRIVATE'}]
        samples = probe.sample_rows(rows, 'owned')
        self.assertEqual(samples[0]['count'], 2)
        self.assertNotIn('PRIVATE', json.dumps(samples))
        self.assertEqual(probe.sample_rows(rows, 'other'), [])
        self.assertEqual(probe.count_path([{'count': n} for n in (0, 0, 1, 2, 2, 1, 0)]), [0, 1, 2, 1, 0])

    def test_proxy_retains_only_finite_reasons_and_hashed_native_fields(self):
        body = {'method': 'tools/call', 'params': {'name': 'engram_report_codex_event', 'arguments': {
            'event': 'SubagentStart', 'native_session_id': 'PRIVATE_NATIVE', 'turn_id': 'PRIVATE_TURN',
            'agent_id': 'PRIVATE_AGENT', 'tool_input': 'PRIVATE_INPUT'}}}
        summary = request_summary(body, b'test-salt')
        self.assertNotIn('PRIVATE', json.dumps(summary))
        self.assertEqual(summary['event'], 'SubagentStart')
        self.assertEqual(summary, request_summary(body, b'test-salt'))
        self.assertNotEqual(summary['native_hash'], request_summary(body, b'other-salt')['native_hash'])
        value = {'result': {'structuredContent': {'accepted': False, 'reason': 'stale_child_turn', 'raw': 'PRIVATE'}}}
        self.assertEqual(response_summary(value), {'accepted': False, 'reason': 'stale_child_turn'})


if __name__ == '__main__':
    unittest.main()
