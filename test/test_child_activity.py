import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.integrations.child_activity import ChildActivity, validate_summary
from core.integrations.mcp_presence import PresenceReporter
from core.integrations.tool_semantics import category_hint
from core.install import claude_monitor_hooks, codex_monitor_hooks, codex_hook_status
from overlay.state_api import validate_lifecycle_payload, validate_payload
from overlay.session_registry import SessionStateRegistry


TURN = '00000000-0000-0000-0000-000000000001'


def context(transport='parent', principal='local'):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={}, scope={
        'engram.presence.transport_id': transport, 'engram.remote_principal': principal})))


class ChildActivityTests(unittest.TestCase):
    def test_agents_not_tools_concurrency_completion_and_late_fence(self):
        reducer = ChildActivity()
        for child in ('one', 'two'):
            self.assertTrue(reducer.update('parent', 'claude', 'SubagentStart', child))
        reducer.update('parent', 'claude', 'PreToolUse', 'one', 'Read', 'r1')
        reducer.update('parent', 'claude', 'PreToolUse', 'one', 'Read', 'r2')
        reducer.update('parent', 'claude', 'PreToolUse', 'two', 'Edit', 'e1')
        self.assertEqual(reducer.summary('parent'), {'total': 2, 'counts': {'read': 1, 'write': 1}})
        reducer.update('parent', 'claude', 'PostToolUse', 'one', 'Read', 'r2')
        self.assertEqual(reducer.summary('parent')['counts']['read'], 1)
        reducer.update('parent', 'claude', 'SubagentStop', 'one')
        self.assertFalse(reducer.update('parent', 'claude', 'PreToolUse', 'one', 'Read', 'late'))
        self.assertFalse(reducer.update('parent', 'claude', 'SubagentStart', 'one'))
        self.assertEqual(reducer.summary('parent')['total'], 1)

    def test_codex_count_only_long_running_lifetime_and_bounds(self):
        now = [0]
        reducer = ChildActivity(clock=lambda: now[0])
        for n in range(64):
            self.assertTrue(reducer.update('parent', 'codex', 'SubagentStart', str(n)))
        self.assertFalse(reducer.update('parent', 'codex', 'SubagentStart', 'overflow'))
        now[0] = 600
        self.assertEqual(reducer.summary('parent'), {'total': 64, 'counts': {}})
        self.assertFalse(reducer.update('parent', 'codex', 'PreToolUse', '0', 'Read', 'r'))
        now[0] = 3601
        self.assertEqual(reducer.summary('parent'), {'total': 0, 'counts': {}})
        self.assertFalse(reducer.update('parent', 'codex', 'SubagentStart', '0'))
        reducer.forget('parent')
        self.assertEqual(reducer.summary('parent')['total'], 0)

    def test_verified_parent_routing_priority_no_child_completion_or_raw_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            reporter = PresenceReporter(Path(directory) / 'state.json')
            reporter._title_cache.access = lambda *a: None
            sent = []
            reporter._send = lambda payload, **kwargs: sent.append(copy.deepcopy(payload)) or True
            root = context()
            def root_event(event, tool=None, call=None):
                return reporter.report_claude_event(root, event, TURN, tool_name=tool, tool_use_id=call, native_session_id='native-parent')
            def child_event(event, child='child-one', tool=None, call=None, ctx=None):
                return reporter.report_claude_event(ctx or context('child'), event, TURN, agent_id=child,
                    tool_name=tool, tool_use_id=call, native_session_id='native-parent')
            root_event('UserPromptSubmit')
            root_event('PreToolUse', 'Agent', 'delegate')
            self.assertTrue(child_event('SubagentStart')['accepted'])
            self.assertTrue(child_event('PreToolUse', tool='Read', call='read1')['accepted'])
            self.assertEqual(sent[-1]['semantic']['active_category'], 'read')
            self.assertEqual(sent[-1]['child_activity'], {'total': 1, 'counts': {'read': 1}})
            self.assertEqual(sent[-1]['session_id'], reporter.context_identity(root)[0])
            root_event('PreToolUse', 'Edit', 'direct')
            root_event('PreToolUse', 'Agent', 'second-delegate')
            self.assertEqual(sent[-1]['semantic']['active_category'], 'write')
            child_event('PreToolUse', tool='Read', call='read2')
            self.assertEqual(sent[-1]['semantic']['active_category'], 'write')
            root_event('PostToolUse', 'Edit', 'direct')
            self.assertEqual(sent[-1]['semantic']['active_category'], 'read')
            root_event('PermissionRequest', 'Edit')
            child_event('PostToolUse', tool='Read', call='read2')
            self.assertEqual(sent[-1]['state'], 'needs_input')
            self.assertIsNone(sent[-1]['semantic']['active_category'])
            child_event('SubagentStop')
            self.assertEqual(sent[-1]['state'], 'needs_input')
            self.assertEqual(sent[-1]['subagent_count'], 0)
            self.assertFalse(child_event('SubagentStart', child='untrusted', ctx=context('other', 'remote-other'))['accepted'])
            reporter.report_claude_event(context('other-root'), 'UserPromptSubmit', TURN, native_session_id='other-native')
            self.assertEqual(child_event('SubagentStart', child='wrong-root', ctx=context('other-root'))['reason'], 'native_identity_conflict')
            reporter.report_claude_event(context('ambiguous'), 'UserPromptSubmit', TURN, native_session_id='native-parent')
            self.assertFalse(child_event('SubagentStart', child='ambiguous-child')['accepted'])
            for raw in ('native-parent', 'child-one', 'read1', 'delegate'):
                self.assertNotIn(raw, json.dumps(sent))

    def test_codex_child_turn_fence_and_zero_tool_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            reporter = PresenceReporter(Path(directory) / 'state.json')
            reporter._title_cache.access = lambda *a: None
            sent = []
            reporter._send = lambda payload, **kwargs: sent.append(payload) or True
            root = context()
            reporter.report_codex_event(root, 'UserPromptSubmit', 'turn', native_session_id='native')
            self.assertFalse(reporter.report_codex_event(root, 'SubagentStart', 'old', agent_id='agent', native_session_id='native')['accepted'])
            self.assertTrue(reporter.report_codex_event(root, 'SubagentStart', 'turn', agent_id='agent', native_session_id='native')['accepted'])
            self.assertEqual(sent[-1]['child_activity'], {'total': 1, 'counts': {}})
            self.assertTrue(reporter.report_codex_event(root, 'SubagentStop', 'turn', agent_id='agent', native_session_id='native')['accepted'])
            self.assertEqual(sent[-1]['state'], 'working')

    def test_last_child_stop_discards_child_derived_cached_parent_category(self):
        with tempfile.TemporaryDirectory() as directory:
            reporter = PresenceReporter(Path(directory) / 'state.json')
            reporter._title_cache.access = lambda *a: None
            sent = []
            reporter._send = lambda payload, **kw: sent.append(copy.deepcopy(payload)) or True
            root, child = context(), context('child')
            reporter.report_claude_event(root, 'UserPromptSubmit', TURN, native_session_id='native')
            reporter.report_claude_event(root, 'PreToolUse', TURN, tool_name='Agent', tool_use_id='first', native_session_id='native')
            reporter.report_claude_event(child, 'SubagentStart', TURN, agent_id='child', native_session_id='native')
            reporter.report_claude_event(child, 'PreToolUse', TURN, agent_id='child', tool_name='Read', tool_use_id='read', native_session_id='native')
            self.assertEqual(sent[-1]['semantic']['active_category'], 'read')
            reporter.report_claude_event(root, 'PreToolUse', TURN, tool_name='Agent', tool_use_id='second', native_session_id='native')
            self.assertEqual(sent[-1]['semantic']['active_category'], 'read')
            reporter.report_claude_event(child, 'SubagentStop', TURN, agent_id='child', native_session_id='native')
            self.assertEqual(sent[-1]['subagent_count'], 0)
            self.assertEqual(sent[-1]['state'], 'working')
            self.assertIsNone(sent[-1]['semantic']['active_category'])

    def test_background_child_survives_parent_stop_and_new_turn_without_state_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            reporter = PresenceReporter(Path(directory) / 'state.json')
            reporter._title_cache.access = lambda *a: None
            registry = SessionStateRegistry()
            sent = []
            def send(payload, **kwargs):
                sent.append(copy.deepcopy(payload))
                checked, semantic, error = validate_lifecycle_payload(payload, 'Claude')
                self.assertIsNone(error)
                return registry.upsert_lifecycle(checked, semantic=semantic) is not None
            reporter._send = send
            root, child = context(), context('child')
            def parent(event, turn=TURN):
                return reporter.report_claude_event(root, event, turn, native_session_id='native')
            def sub(event, turn=TURN, agent='background'):
                return reporter.report_claude_event(child, event, turn, agent_id=agent,
                    native_session_id='native', tool_name='Read' if event == 'PreToolUse' else None,
                    tool_use_id='read' if event == 'PreToolUse' else None)
            parent('UserPromptSubmit')
            sub('SubagentStart')
            parent('Stop')
            self.assertEqual((registry.snapshot()[0]['state'], registry.snapshot()[0]['subagent_count']), ('ready', 1))
            next_turn = '00000000-0000-0000-0000-000000000002'
            self.assertTrue(sub('PreToolUse', next_turn)['accepted'])
            self.assertEqual(registry.snapshot()[0]['state'], 'ready')
            self.assertIsNone(sent[-1]['semantic']['active_category'])
            parent('UserPromptSubmit', next_turn)
            self.assertTrue(sub('PreToolUse', next_turn)['accepted'])
            self.assertEqual(registry.snapshot()[0]['state'], 'working')
            self.assertIsNone(sent[-1]['semantic']['active_category'])
            self.assertFalse(sub('SubagentStart', TURN, 'unknown-stale')['accepted'])
            self.assertTrue(sub('SubagentStop', next_turn)['accepted'])
            self.assertEqual((registry.snapshot()[0]['state'], registry.snapshot()[0]['subagent_count']), ('working', 0))

    def test_child_update_cannot_renew_parent_lease_or_reactivate_ready(self):
        now = [0.0]
        registry = SessionStateRegistry(clock=lambda: now[0])
        payload = {'provider': 'mcp', 'session_id': 'parent', 'state': 'working', 'agent_name': 'Claude'}
        registry.upsert_lifecycle(payload, semantic={'seq': 1, 'events': [], 'active_category': None})
        child = {**payload, '_child_update': True, 'subagent_count': 1, 'child_activity': {'total': 1, 'counts': {'read': 1}}}
        now[0] = 100
        registry.upsert_lifecycle(child, semantic={'seq': 2, 'events': [], 'active_category': 'read'})
        now[0] = 121
        self.assertEqual(registry.snapshot()[0]['state'], 'unknown')
        registry.upsert_lifecycle(child, semantic={'seq': 3, 'events': [{'type': 'tool.started', 'category': 'read'}], 'active_category': 'read'})
        self.assertEqual(registry.snapshot()[0]['state'], 'unknown')
        registry.upsert_lifecycle({**payload, 'state': 'ready'}, semantic={'seq': 4, 'events': [], 'active_category': None})
        registry.upsert_lifecycle(child, semantic={'seq': 5, 'events': [], 'active_category': 'read'})
        self.assertEqual(registry.snapshot()[0]['state'], 'ready')
        self.assertNotIn('activity_category', registry.snapshot()[0])

    def test_snapshot_expires_child_metadata_without_any_native_events(self):
        now = [0.0]
        registry = SessionStateRegistry(clock=lambda: now[0], idle_timeout=7200)
        parent = {'provider': 'mcp', 'session_id': 'parent', 'state': 'ready', 'agent_name': 'Claude',
                  'subagent_count': 1, 'child_activity': {'total': 1, 'counts': {'read': 1}}}
        registry.upsert_lifecycle(parent, semantic={'seq': 1, 'events': [], 'active_category': None})
        before = registry.snapshot()[0]
        now[0] = 3599
        self.assertEqual(registry.snapshot()[0]['subagent_count'], 1)
        now[0] = 3600
        after = registry.snapshot()[0]
        self.assertEqual(after['subagent_count'], 0)
        self.assertNotIn('child_activity', after)
        for field in ('state', 'state_since', 'last_seen'):
            self.assertEqual(before[field], after[field])
        self.assertEqual(registry._lifecycle_deadlines, {})
        self.assertEqual(registry._child_activity_deadlines, {})

    def test_private_metadata_validation_registry_and_compatible_hints(self):
        payload = {'provider': 'mcp', 'session_id': 'parent', 'state': 'working', 'agent_name': 'Claude',
            'subagent_count': 2, 'child_activity': {'total': 2, 'counts': {'read': 1, 'write': 1}},
            'semantic': {'seq': 1, 'events': [], 'active_category': 'read'}}
        checked, semantic, error = validate_lifecycle_payload(payload, 'Claude')
        self.assertIsNone(error)
        self.assertIsNotNone(validate_payload(payload)[1])  # Not accepted by ordinary public state writers.
        registry = SessionStateRegistry()
        registry.upsert_lifecycle(checked, semantic=semantic)
        self.assertEqual(registry.snapshot()[0]['activity_category'], 'read')
        self.assertEqual(registry.snapshot()[0]['child_activity']['total'], 2)
        self.assertEqual((category_hint('read'), category_hint('write')), ('memory', 'generating'))
        for invalid in ({'total': True, 'counts': {}}, {'total': 1, 'counts': {'private': 1}}, {'total': 1, 'counts': {'read': 2}}):
            self.assertIsNone(validate_summary(invalid))

    def test_retired_child_cannot_cross_parent_turn_or_blocked_category(self):
        reducer = ChildActivity()
        reducer.update('parent', 'claude', 'SubagentStart', 'child')
        reducer.update('parent', 'claude', 'PreToolUse', 'child', 'Read', 'r')
        reducer.update('parent', 'claude', 'PermissionRequest', 'child', 'Read')
        self.assertEqual(reducer.summary('parent'), {'total': 1, 'counts': {}})
        reducer.retire('parent')
        self.assertFalse(reducer.update('parent', 'claude', 'SubagentStart', 'child'))
        self.assertEqual(reducer.summary('parent')['total'], 0)

    def test_explicit_upgrade_preserves_supported_versions_and_nine_native_hooks(self):
        for module in (claude_monitor_hooks, codex_monitor_hooks):
            for old in (module.generated_hooks(), module.generated_hooks(native_identity=True)):
                original = {'hooks': old}
                self.assertEqual(module.merge_settings(original), original)
                upgraded = module.merge_settings(original, upgrade_subagent_activity=True)
                self.assertEqual(upgraded['hooks'], module.generated_hooks(subagent_activity=True))
                self.assertEqual(module.merge_settings(upgraded), upgraded)
                self.assertEqual(module.merge_settings(upgraded, upgrade_native_identity=True), upgraded)
        root = Path.cwd() / 'fixture-root'
        metadata = []
        for event, groups in codex_monitor_hooks.generated_hooks(subagent_activity=True).items():
            handler = groups[0]['hooks'][0]
            item = {'sourcePath': str(root / 'hooks.json'), 'eventName': event[0].lower() + event[1:], 'enabled': True, 'trustStatus': 'trusted'}
            item.update({'handlerType': 'command', 'command': handler['command']} if handler['type'] == 'command'
                        else {'handlerType': 'mcpTool', 'server': 'engram', 'tool': codex_monitor_hooks.TOOL})
            metadata.append(item)
        result = {'data': [{'cwd': str(Path.cwd()), 'errors': [], 'warnings': [], 'hooks': metadata}]}
        self.assertEqual(codex_hook_status.summarize(result, root, Path.cwd())['reason'], 'native-nine-hooks-ready')
        metadata.pop(-2)
        self.assertEqual(codex_hook_status.summarize(result, root, Path.cwd())['status'], 'unknown')

    def test_foreign_subagent_event_keys_do_not_count_as_managed_upgrade(self):
        foreign = {'hooks': [{'type': 'command', 'command': 'other-provider-hook'}]}
        for module in (claude_monitor_hooks, codex_monitor_hooks):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                document = {'hooks': module.generated_hooks()}
                document['hooks'].update(SubagentStart=[copy.deepcopy(foreign)], SubagentStop=[copy.deepcopy(foreign)])
                target = root / ('hooks.json' if module is codex_monitor_hooks else 'settings.json')
                target.write_text(json.dumps(document), encoding='utf-8')
                if module is codex_monitor_hooks:
                    (root / 'config.toml').write_text('', encoding='utf-8')
                    with patch.object(module, 'inspect_root', return_value={'status': 'ready', 'trust_required': False}):
                        result = module.configure_root(root, apply=True)
                else:
                    result = module.configure(target, apply=True)
                self.assertEqual(result['hook_count'], len(module.EVENTS) + 1)
                self.assertFalse(result['changed'])
                self.assertEqual(json.loads(target.read_text(encoding='utf-8')), document)


if __name__ == '__main__':
    unittest.main()
