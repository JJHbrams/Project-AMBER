"""Real loopback HTTP tests; discovery, SQLite and clocks are fully isolated."""
import os
from pathlib import Path
import tempfile
import time
import threading
import json
from urllib.request import Request, urlopen
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.integrations.mcp_presence import PresenceReporter
from overlay.session_registry import SessionStateRegistry
from overlay.stm_server import STMServer


def context(transport, principal='local'):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
        headers={}, scope={'engram.presence.transport_id': transport,
                           'engram.remote_principal': principal})))


class TitleRetentionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'ENGRAM_SMOKE_DB_DIR': self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = 0.
        self.registry = SessionStateRegistry(clock=lambda: self.now)
        self.discovery = Path(self.temp.name) / 'state.json'
        self.server = STMServer(port=0, state_registry=self.registry,
                                state_discovery_path=self.discovery)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.reporter = self.new_reporter()

    def new_reporter(self):
        reporter = PresenceReporter(self.discovery, timeout=2)
        self.addCleanup(reporter.stop)
        return reporter

    def wait_title(self, reporter, ctx, title):
        key = 'mcp:' + reporter.context_identity(ctx)[0]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            rows = self.registry.snapshot()
            row = next((row for row in rows if row['key'] == key), None)
            if row and row['label'] == title:
                return row
            time.sleep(.01)
        self.fail('worker did not restore the expected safe title')

    def flush_presence(self, reporter, ctx):
        sent = threading.Event()
        original = reporter._send
        def send(*args, **kwargs):
            result = original(*args, **kwargs)
            sent.set()
            return result
        with patch.object(reporter, '_send', side_effect=send):
            reporter.report_context(ctx)
            self.assertTrue(sent.wait(3))
            # _run retains this lock until title restoration is also finished.
            with reporter._title_lock:
                pass

    def http_rows(self):
        info = json.loads(self.discovery.read_text(encoding='utf-8'))
        request = Request(f'http://127.0.0.1:{self.server.port}/state',
                          headers={'Authorization': 'Bearer ' + info['token']})
        with urlopen(request, timeout=2) as response:
            return json.loads(response.read())['sessions']

    def test_http_timeout_boundary_heartbeat_and_manual_title(self):
        ctx = context('heartbeat-transport')
        self.assertTrue(self.reporter.report_title(ctx, 'Producer safe title')['accepted'])
        row = self.http_rows()[0]
        self.registry.set_label(row['key'], 'Manual safe title')
        for stamp in (601, 3599):
            self.now = stamp
            self.assertEqual(self.http_rows()[0]['label'], 'Manual safe title')
        self.flush_presence(self.reporter, ctx)
        self.assertEqual(self.http_rows()[0]['last_seen'], 3599)
        self.assertEqual(self.http_rows()[0]['label'], 'Manual safe title')
        self.now = 7198.999
        self.assertEqual(len(self.http_rows()), 1)
        self.now = 7199
        self.assertEqual(self.http_rows(), [])

    def test_ordinary_presence_restores_same_transport_without_native_binding(self):
        ctx = context('transport-a')
        self.assertTrue(self.reporter.report_title(ctx, 'Safe original title')['accepted'])
        self.now = 3600
        self.assertEqual(self.registry.snapshot(), [])
        self.reporter.report_context(ctx)
        row = self.wait_title(self.reporter, ctx, 'Safe original title')
        self.assertEqual(row['state'], 'unknown')
        self.assertFalse(self.reporter._title_cache.path.exists())
        # Ordinary renewal must not replace a newer producer title.
        self.registry.set_producer_label('mcp', row['session_id'], 'Current live title')
        with patch.object(self.reporter._title_cache, 'access', wraps=self.reporter._title_cache.access) as access:
            self.flush_presence(self.reporter, ctx)
            access.assert_not_called()
        self.wait_title(self.reporter, ctx, 'Current live title')

    def test_unknown_and_bubble_presence_do_not_allocate_title_identity(self):
        self.flush_presence(self.reporter, context('untitled-transport'))
        bubble = context('bubble-transport')
        bubble.request_context.request.headers['x-engram-bubble-owner'] = 'b' * 32
        bubble.request_context.request.scope['engram.presence.local'] = True
        self.flush_presence(self.reporter, bubble)
        self.assertEqual(self.reporter._native_titles, {})
        self.assertFalse(self.reporter._title_cache.path.exists())
        self.assertEqual(len(self.registry.snapshot()), 1)

    def test_ended_native_session_is_not_resurrected_by_presence(self):
        ctx = context('ended-transport')
        turn = '00000000-0000-0000-0000-000000000001'
        self.reporter.report_claude_event(ctx, 'UserPromptSubmit', turn)
        self.reporter.report_title(ctx, 'Safe prior title')
        self.assertTrue(self.reporter.report_claude_event(ctx, 'SessionEnd', turn)['accepted'])
        self.reporter.report_context(ctx)
        self.assertEqual(self.registry.snapshot(), [])

    def test_default_retention_boundary_and_separate_working_lease(self):
        self.registry.upsert_lifecycle({'provider': 'mcp', 'session_id': 'native', 'state': 'working'})
        self.now = 119.999
        self.assertEqual(self.registry.snapshot()[0]['state'], 'working')
        self.now = 120
        self.assertEqual(self.registry.snapshot()[0]['state'], 'unknown')
        for stamp in (600, 3599.999):
            self.now = stamp
            self.assertEqual(len(self.registry.snapshot()), 1)
        self.now = 3600
        self.assertEqual(self.registry.snapshot(), [])

    def test_native_reconnect_provider_principal_and_current_title(self):
        for provider in ('claude', 'codex'):
            with self.subTest(provider=provider):
                original = context('original-' + provider)
                hook = getattr(self.reporter, 'report_' + provider + '_event')
                turn = '00000000-0000-0000-0000-000000000001'
                self.assertTrue(hook(original, 'UserPromptSubmit', turn,
                    native_session_id='native-' + provider)['accepted'])
                self.assertTrue(self.reporter.report_title(original, 'Saved safe title')['accepted'])
                reconnect = self.new_reporter()
                restored = context('reconnected-' + provider)
                result = getattr(reconnect, 'report_' + provider + '_event')(
                    restored, 'UserPromptSubmit', turn, native_session_id='native-' + provider)
                self.assertNotIn('hookSpecificOutput', result)
                self.wait_title(reconnect, restored, 'Saved safe title')
                self.now += 3600
                self.assertEqual(self.registry.snapshot(), [])
                reconnect.report_context(restored)
                self.wait_title(reconnect, restored, 'Saved safe title')
                other = self.new_reporter()
                foreign = context('foreign-' + provider, principal='other-principal')
                result = getattr(other, 'report_' + provider + '_event')(
                    foreign, 'UserPromptSubmit', turn, native_session_id='native-' + provider)
                self.assertIn('hookSpecificOutput', result)
                self.assertTrue(other.report_title(foreign, 'Current safe title')['accepted'])
                getattr(other, 'report_' + provider + '_event')(
                    foreign, 'PreToolUse', turn, tool_name='Read', tool_use_id='tool-one',
                    native_session_id='native-' + provider)
                self.wait_title(other, foreign, 'Current safe title')


if __name__ == '__main__':
    unittest.main()
