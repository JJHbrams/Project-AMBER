import concurrent.futures
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.integrations.session_title_cache import SessionTitleCache
from core.integrations.mcp_presence import PresenceReporter
from core.install import claude_monitor_hooks, codex_monitor_hooks


def context(transport, principal='local'):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={},
        scope={'engram.presence.transport_id': transport, 'engram.remote_principal': principal})))


class TitleCacheTests(unittest.TestCase):
    def test_persistence_provider_principal_ttl_and_no_raw_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SessionTitleCache(directory, clock=lambda: 100, ttl=10)
            native = 'private-native-identity-123'
            self.assertEqual(cache.access('codex', 'one', native, 'Safe prior title'), 'Safe prior title')
            self.assertEqual(SessionTitleCache(directory, clock=lambda: 101).access('codex', 'one', native), 'Safe prior title')
            self.assertIsNone(cache.access('claude', 'one', native))
            self.assertIsNone(cache.access('codex', 'two', native))
            self.assertNotIn(native.encode(), cache.path.read_bytes())
            self.assertIsNone(SessionTitleCache(directory, clock=lambda: 111, ttl=10).access('codex', 'one', native))

    def test_corruption_invalid_metadata_and_transactional_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = SessionTitleCache(directory, capacity=3)
            self.assertIsNone(cache.access('codex', 'local', '${session_id}', 'Safe label'))
            self.assertIsNone(cache.access('codex', 'local', 'id', 'one'))
            self.assertFalse(cache.path.exists())
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda n: cache.access('codex', 'local', str(n), 'Safe label'), range(12)))
            with closing(sqlite3.connect(cache.path)) as db:
                self.assertLessEqual(db.execute('SELECT COUNT(*) FROM titles').fetchone()[0], 3)
            cache.path.write_bytes(b'corrupt retained bytes')
            self.assertIsNone(cache.access('codex', 'local', 'id'))
            self.assertEqual(cache.path.read_bytes(), b'corrupt retained bytes')

    def test_title_order_reconnect_no_hint_current_wins_no_state_merge(self):
        for title_first in (True, False):
            with self.subTest(title_first=title_first), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ENGRAM_SMOKE_DB_DIR': directory}):
                sent = []
                def send(payload, **kwargs):
                    sent.append(payload)
                    if 'title_status' in kwargs:
                        kwargs['title_status']['missing'] = True
                    return True
                first = PresenceReporter(Path(directory) / 'state.json')
                first._send = send
                original = context('original')
                if title_first:
                    first.report_title(original, 'Safe original title')
                first.report_codex_event(original, 'UserPromptSubmit', 'turn1', native_session_id='native-one')
                if not title_first:
                    first.report_title(original, 'Safe original title')
                second = PresenceReporter(Path(directory) / 'state.json')
                second._send = send
                result = second.report_codex_event(context('reconnect'), 'UserPromptSubmit', 'turn2', native_session_id='native-one')
                self.assertTrue(result['accepted'])
                self.assertNotIn('hookSpecificOutput', result)
                self.assertEqual(sent[-1]['title'], 'Safe original title')
                self.assertNotIn('native-one', json.dumps(sent))
                self.assertNotEqual(sent[0]['session_id'], sent[-1]['session_id'])
                second.report_title(context('reconnect'), 'Updated safe title')
                third = PresenceReporter(Path(directory) / 'state.json')
                third._send = send
                third.report_title(context('new'), 'Current live title')
                third.report_codex_event(context('new'), 'UserPromptSubmit', 'turn3', native_session_id='native-one')
                self.assertEqual(sent[-1]['title'], 'Current live title')

    def test_no_rebind_rejected_agent_and_stale_turn_never_bind(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'ENGRAM_SMOKE_DB_DIR': directory}):
            reporter = PresenceReporter(Path(directory) / 'state.json')
            reporter._send = lambda *a, **kw: True
            ctx = context('same')
            reporter.report_codex_event(ctx, 'UserPromptSubmit', 'one', native_session_id='native-one')
            self.assertEqual(reporter.report_codex_event(ctx, 'UserPromptSubmit', 'two', native_session_id='different')['reason'], 'native_identity_conflict')
            reporter.report_codex_event(ctx, 'UserPromptSubmit', 'two')
            self.assertEqual(reporter.report_codex_event(ctx, 'PostToolUse', 'one', native_session_id='native-one')['reason'], 'stale_turn')
            self.assertFalse(reporter.report_claude_event(context('agent'), 'Stop', '00000000-0000-0000-0000-000000000001', agent_id='child', native_session_id='native-two')['accepted'])
            self.assertEqual(len(reporter._native_titles), 1)

    def test_explicit_migration_only_and_no_default_downgrade(self):
        for module in (claude_monitor_hooks, codex_monitor_hooks):
            with self.subTest(provider=module.__name__):
                old = {'hooks': module.generated_hooks()}
                self.assertEqual(module.merge_settings(old), old)
                upgraded = module.merge_settings(old, upgrade_native_identity=True)
                self.assertEqual(upgraded['hooks'], module.generated_hooks(native_identity=True))
                self.assertEqual(module.merge_settings(upgraded), upgraded)
                self.assertEqual(module.merge_settings(upgraded, upgrade_native_identity=True), upgraded)
                self.assertIn('Do not regenerate', json.dumps(upgraded))


if __name__ == '__main__':
    unittest.main()
