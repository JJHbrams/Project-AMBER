"""Project display metadata is connection-scoped and never a filesystem lookup."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from core.integrations.mcp_presence import PresenceReporter
from core.integrations.project_metadata import ClientRootProjectResolver
from overlay.session_registry import SessionStateRegistry
from overlay.state_api import project_display_name, validate_project_payload


class ProjectMetadataTests(unittest.TestCase):
    def test_isolated_mcp_schema_privacy_and_cached_bootstrap_refresh(self):
        code = '''
import asyncio, time
from types import SimpleNamespace
from unittest.mock import Mock, patch
import mcp_server as server
tool = server.engramMCP._tool_manager.get_tool('engram_report_session_project')
for payload in ({'cwd':{'raw':'PRIVATE_CANARY'}}, {'session_id':'PRIVATE_CANARY'}):
    try:
        tool.fn_metadata.arg_model.model_validate(payload)
        raise AssertionError('invalid metadata accepted')
    except ValueError as error:
        assert 'PRIVATE_CANARY' not in str(error)
context = SimpleNamespace()
server._CONTEXT_ONCE_KEYS['fixture'] = (123, time.time())
fn = server.engram_get_context_once
while hasattr(fn, '__wrapped__'):
    fn = fn.__wrapped__
with patch.object(server, 'ensure_repo_policy', return_value={'ok':True}), patch.object(server, '_context_session_fingerprint', return_value='unchanged-stm'), patch.object(server, '_build_context_once_key', return_value='fixture'), patch.object(server, '_session_is_open', return_value=True), patch.object(server, '_mark_trusted_root_bootstrap'), patch.object(server._mcp_presence, 'report_project', return_value={'accepted':True}) as report:
    result = asyncio.run(fn(caller='test', project_key='CallerKey', cwd='C:/private/Folder', ctx=context))
    assert 'already initialized' in result
    report.assert_called_once_with(context, 'CallerKey', 'C:/private/Folder', source=1)
    for machine_key in ('provider:machine', 'folder/key', 'x'*65):
        report.reset_mock()
        asyncio.run(fn(caller='test', project_key=machine_key, cwd='C:/private/Folder', ctx=context))
        report.assert_called_once_with(context, None, 'C:/private/Folder', source=1)
    report.reset_mock()
    asyncio.run(fn(caller='test', project_name='invalid/name', cwd='C:/private/Folder', ctx=context))
    report.assert_not_called()
    report.reset_mock()
    asyncio.run(fn(caller='test', project_key='machine:key', ctx=context))
    report.assert_called_once_with(context, None, None, source=1)
print('isolated schema and cache PASS')
'''
        with tempfile.TemporaryDirectory(prefix='engram-project-test-') as directory:
            env = dict(os.environ, HOME=directory, USERPROFILE=directory,
                       APPDATA=str(Path(directory)/'AppData'),
                       ENGRAM_SMOKE_DB_DIR=str(Path(directory)/'db'), PYTHONUTF8='1')
            result = subprocess.run([sys.executable, '-c', code], env=env,
                                    capture_output=True, text=True, encoding='utf-8', timeout=35)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])

    def test_textual_basename_and_explicit_precedence(self):
        for cwd, expected in [(r'C:\private\Client One', 'Client One'),
                              ('/private/client-two/', 'client-two'),
                              (r'\\server\share\Project', 'Project')]:
            self.assertEqual(project_display_name(cwd=cwd), expected)
            self.assertEqual(project_display_name('Explicit', cwd), 'Explicit')
        for cwd in ('C:/', '/', r'\\server\share', '//server/', '.', '..', None,
                    '/private/bad\u202ename', '/private/bad\x00name', 'x'*4097):
            self.assertIsNone(project_display_name(cwd=cwd))

    def test_display_validation_rejects_paths_controls_and_empty(self):
        for name in (None, '', ' ', '/private', r'C:\private', 'x'*65,
                     'secret\nvalue', 'bad\u200bname', {}, 4, '..'):
            checked, error = validate_project_payload(
                {'provider':'mcp', 'session_id':'safe', 'project_name':name})
            self.assertIsNone(checked)
            self.assertEqual(error, 'invalid project payload')
        self.assertEqual(project_display_name('Project Name'), 'Project Name')
        self.assertIsNone(project_display_name(' ', '/private/valid'))

    def test_registry_metadata_preserves_semantics_and_expiration(self):
        now = [10.0]
        registry = SessionStateRegistry(clock=lambda:now[0])
        row = registry.upsert({'provider':'mcp','session_id':'one',
                               'state':'needs_input','label':'Producer title'})
        registry.set_label(row['key'], 'Manual title')
        before = registry.snapshot()[0]
        now[0] += 1
        self.assertTrue(registry.set_project_name('mcp','one','Caller Project'))
        after = registry.snapshot()[0]
        self.assertEqual({k:v for k,v in before.items() if k!='project_name'},
                         {k:v for k,v in after.items() if k!='project_name'})
        registry.presence({'provider':'mcp','session_id':'one'})
        self.assertEqual(registry.snapshot()[0]['project_name'], 'Caller Project')
        self.assertFalse(registry.set_project_name('mcp','missing','New'))
        registry.remove('mcp','one')
        self.assertFalse(registry.set_project_name('mcp','one','Old'))
        registry.upsert({'provider':'mcp','session_id':'new','state':'unknown'})
        self.assertIsNone(registry.snapshot()[0]['project_name'])

    def test_reporter_sends_only_display_name_on_own_identity(self):
        reporter = PresenceReporter()
        reporter._send = Mock(return_value=True)
        request = SimpleNamespace(headers={}, scope={'engram.presence.transport_id':'transport'})
        context = SimpleNamespace(request_context=SimpleNamespace(request=request))
        result = reporter.report_project(context, cwd='C:/PRIVATE_CANARY/CallerProject')
        self.assertEqual(result, {'accepted':True,'reason':'delivered'})
        calls = reporter._send.call_args_list
        self.assertEqual(calls[0].args[0]['session_id'], calls[1].args[0]['session_id'])
        self.assertEqual(calls[1].args[0]['project_name'], 'CallerProject')
        self.assertEqual(calls[1].kwargs['endpoint'], '/state/project')
        self.assertNotIn('PRIVATE_CANARY', repr(calls))
        self.assertNotIn('session_id', json.dumps(result))
        reporter._send.reset_mock()
        self.assertFalse(reporter.report_project(context, cwd='/')['accepted'])
        reporter._send.assert_not_called()
        reporter._send.return_value = False
        self.assertFalse(reporter.report_project(context, project_name='Safe')['accepted'])

    def test_project_source_rank_and_alias_merge_preserve_canonical_record(self):
        reporter = PresenceReporter()
        reporter._send = Mock(return_value=True)
        a = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
            headers={}, scope={'engram.presence.transport_id':'transport-a'})))
        b = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
            headers={}, scope={'engram.presence.transport_id':'transport-b'})))
        owner, alias = reporter.context_identity(a)[0], reporter.context_identity(b)[0]
        self.assertTrue(reporter.report_project(a, 'Canonical', source=1)['accepted'])
        self.assertTrue(reporter.report_project(b, 'Alias explicit', source=3)['accepted'])
        binding = ('codex', 'opaque-native')
        with reporter._title_lock:
            reporter._bind_owner(owner, binding, owner)
            reporter._bind_owner(alias, binding, owner)
        record = reporter._native_titles[owner]
        self.assertEqual(record['project'], 'Alias explicit')
        self.assertEqual(record['project_source'], 3)
        self.assertEqual(reporter.report_project(b, 'Alias root', source=2)['reason'], 'alias_transport_owned')
        self.assertEqual(reporter.report_project(a, 'Root cannot replace', source=2)['reason'], 'retained_higher_quality')
        self.assertEqual(reporter._native_titles[owner]['project'], 'Alias explicit')
        self.assertTrue(reporter.report_project(a, 'Explicit later', source=3)['accepted'])
        self.assertEqual(reporter.report_project(a, 'Cwd cannot downgrade', source=1)['reason'], 'retained_higher_quality')
        self.assertEqual(reporter._native_titles[owner]['project'], 'Explicit later')
        self.assertEqual(reporter.report_project(a, cwd='C:/private/PublicCwd')['reason'], 'retained_higher_quality')
        self.assertEqual(reporter._native_titles[owner]['project'], 'Explicit later')

    def test_client_root_resolver_requires_one_advertised_safe_file_root(self):
        async def run():
            resolver = ClientRootProjectResolver(ttl=20, timeout=.1)
            session = SimpleNamespace(client_params=SimpleNamespace(capabilities=SimpleNamespace(roots=object())))
            session.list_roots = Mock(return_value=asyncio.sleep(0, result=SimpleNamespace(
                roots=[SimpleNamespace(uri='file:///private/Client%20Root', name=None)])))
            context = SimpleNamespace(session=session)
            self.assertEqual(await resolver.resolve(context, 'opaque'), 'Client Root')
            self.assertEqual(await resolver.resolve(context, 'opaque'), 'Client Root')
            self.assertEqual(session.list_roots.call_count, 1)
            session.list_roots.return_value = asyncio.sleep(0, result=SimpleNamespace(
                roots=[SimpleNamespace(uri='file:///one', name=None), SimpleNamespace(uri='file:///two', name=None)]))
            self.assertIsNone(await resolver.resolve(context, 'other'))
            session.client_params = SimpleNamespace(capabilities=SimpleNamespace(roots=None))
            self.assertIsNone(await resolver.resolve(context, 'no-advertisement'))
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
