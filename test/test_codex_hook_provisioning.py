import json
import copy
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core.install import codex_monitor_hooks as hooks
from core.install import codex_hook_status as status


class CodexHookProvisioningTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(hooks, 'inspect_root', return_value={
            'status': 'ready', 'trust_required': False, 'reason': 'native-seven-hooks-ready'})
        self.inspect = patcher.start()
        self.addCleanup(patcher.stop)

    def test_provider_owned_documented_fields(self):
        generated = hooks.generated_hooks()
        self.assertEqual(set(generated), set(hooks.EVENTS) | {'SessionStart'})
        for event in hooks.EVENTS:
            handler = generated[event][0]['hooks'][0]
            self.assertEqual(handler['type'], 'mcp_tool')
            self.assertEqual(handler['tool'], 'engram_report_codex_event')
            self.assertEqual(handler['input']['turn_id'], '${turn_id}')
            self.assertLessEqual(
                set(handler['input']),
                {'event', 'turn_id', 'tool_name', 'tool_use_id', 'native_session_id', 'agent_id'},
            )
            self.assertEqual(handler['input']['native_session_id'], '${session_id}')
            if event in hooks.SUBAGENT_EVENTS:
                self.assertEqual(handler['input']['agent_id'], '${agent_id}')
        self.assertNotIn('tool_use_id', generated['PermissionRequest'][0]['hooks'][0]['input'])
        text = json.dumps(generated)
        for forbidden in ('${prompt}', '${transcript_path}', '${tool_input}', '${tool_response}'):
            self.assertNotIn(forbidden, text)

    def test_merge_preserves_foreign_and_is_idempotent(self):
        foreign = {'hooks': [{'type': 'command', 'command': 'orca existing hook'}]}
        original = {'description': 'user', 'hooks': {'PreToolUse': [foreign]}, 'trust': {'keep': 'exact'}}
        first = hooks.merge_settings(original)
        self.assertEqual(first, hooks.merge_settings(first))
        self.assertEqual(first['hooks']['PreToolUse'][0], foreign)
        self.assertEqual(first['trust'], original['trust'])
        self.assertEqual(len(original['hooks']['PreToolUse']), 1)

    def test_discovery_existing_separate_roots_and_no_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / 'home'
            envroot = base / 'env'
            orcaroot = base / 'roaming/orca/codex-runtime-home/home'
            for root in (home / '.codex', envroot, orcaroot):
                root.mkdir(parents=True)
                (root / 'config.toml').write_text('', encoding='utf-8')
            roots = hooks.existing_roots(home=home, environ={'CODEX_HOME': str(envroot), 'APPDATA': str(base / 'roaming')})
            self.assertEqual(roots, [home / '.codex', envroot, orcaroot])
            self.assertEqual(hooks.existing_roots(home=base / 'absent', environ={}), [])
            self.assertFalse((base / 'absent').exists())

    def test_apply_preserves_config_and_backup_then_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = b'[features]\nhooks = true\n[projects.example]\ntrust_level = "trusted"\n'
            (root / 'config.toml').write_bytes(config)
            before = b'{"hooks":{}, "description":"user-owned"}'
            (root / 'hooks.json').write_bytes(before)
            dry = hooks.configure_root(root)
            self.assertTrue(dry['changed'])
            self.assertFalse(dry['applied'])
            self.assertEqual((root / 'hooks.json').read_bytes(), before)
            result = hooks.configure_root(root, apply=True)
            self.assertFalse(result['trust_required'])
            self.assertEqual(result['status'], 'ready')
            self.assertEqual((root / 'config.toml').read_bytes(), config)
            self.assertEqual(next(root.glob('*.engram-monitor-backup-*')).read_bytes(), before)
            self.assertFalse(hooks.configure_root(root, apply=True)['changed'])
            self.assertEqual(len(list(root.glob('*.engram-monitor-backup-*'))), 1)

    def test_clean_root_gets_nine_hooks_without_upgrading_existing_legacy_root(self):
        with tempfile.TemporaryDirectory() as directory:
            clean = Path(directory) / 'clean'
            clean.mkdir()
            (clean / 'config.toml').write_bytes(b'')
            installed = hooks.configure_root(clean, apply=True, inspect=False)
            document = json.loads((clean / 'hooks.json').read_text(encoding='utf-8'))
            self.assertEqual(set(document['hooks']), set(hooks.EVENTS) | {'SessionStart'})
            self.assertEqual(installed['hook_count'], 9)

            legacy = Path(directory) / 'legacy'
            legacy.mkdir()
            (legacy / 'config.toml').write_bytes(b'')
            legacy_hooks = hooks.generated_hooks(subagent_activity=False)
            (legacy / 'hooks.json').write_text(json.dumps({'hooks': legacy_hooks}), encoding='utf-8')
            preserved = hooks.configure_root(legacy, apply=True, inspect=False)
            self.assertFalse(preserved['changed'])
            self.assertEqual(preserved['hook_count'], 7)

    def test_explicit_disable_and_absent_root_never_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for feature in ('hooks', 'codex_hooks'):
                (root / 'config.toml').write_text(f'[features]\n{feature}=false\n', encoding='utf-8')
                result = hooks.configure_root(root, apply=True)
                self.assertEqual(result['reason'], 'user-disabled-hooks')
                self.assertFalse((root / 'hooks.json').exists())
            self.assertFalse(hooks.configure_root(root / 'absent', apply=True)['applied'])
            self.assertFalse((root / 'absent').exists())

    def test_invalid_json_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.toml').write_text('', encoding='utf-8')
            (root / 'hooks.json').write_bytes(b'broken')
            with self.assertRaises(ValueError):
                hooks.configure_root(root, apply=True)
            self.assertEqual((root / 'hooks.json').read_bytes(), b'broken')
            self.assertFalse(list(root.glob('*.engram-monitor-backup-*')))

    def test_no_provider_no_apply(self):
        with patch.object(hooks, 'compatibility', return_value=(False, 'codex-not-installed')):
            self.assertEqual(hooks.provision(apply=True)['roots'], [])

    def test_preflight_all_roots_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory) / 'normal', Path(directory) / 'broken']
            for root in roots:
                root.mkdir()
                (root / 'config.toml').write_text('', encoding='utf-8')
            (roots[1] / 'hooks.json').write_bytes(b'broken')
            with patch.object(hooks, 'compatibility', return_value=(True, 'compatible')):
                with self.assertRaises(ValueError):
                    hooks.provision(roots, apply=True)
            self.assertFalse((roots[0] / 'hooks.json').exists())

    def test_canonical_disable_precedence_and_inline_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = '[features]\nhooks=false\ncodex_hooks=true\n'
            (root / 'config.toml').write_text(original, encoding='utf-8')
            self.assertEqual(hooks.configure_root(root, apply=True)['reason'], 'user-disabled-hooks')
            inline = '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype="mcp_tool"\nserver="engram"\ntool="engram_report_codex_event"\n'
            (root / 'config.toml').write_text(inline, encoding='utf-8')
            self.assertEqual(hooks.configure_root(root, apply=True)['reason'], 'inline-monitor-hooks-preserved')
            self.assertFalse((root / 'hooks.json').exists())

    def test_modified_handlers_and_group_metadata_are_conflicts_without_writes(self):
        for edit in ('input', 'timeout', 'matcher', 'mixed'):
            with self.subTest(edit=edit), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'config.toml').write_bytes(b'')
                document = {'hooks': hooks.generated_hooks()}
                group = document['hooks']['PreToolUse'][0]
                if edit == 'input':
                    group['hooks'][0]['input']['tool_name'] = 'user-change'
                elif edit == 'timeout':
                    group['hooks'][0]['timeout'] = 10
                elif edit == 'matcher':
                    group['matcher'] = 'user-filter'
                else:
                    group['hooks'].append({'type': 'command', 'command': 'user-command'})
                original = json.dumps(document).encode()
                (root / 'hooks.json').write_bytes(original)
                result = hooks.configure_root(root, apply=True)
                self.assertTrue(result['conflict'])
                self.assertEqual(result['status'], 'conflict')
                self.assertIn('preserved', result['review_instruction'])
                self.assertFalse(result['changed'])
                self.assertEqual((root / 'hooks.json').read_bytes(), original)
                self.assertEqual((root / 'config.toml').read_bytes(), b'')
                self.assertFalse(list(root.glob('*backup*')))
        self.inspect.assert_not_called()

    def test_owned_groups_keep_order_and_only_exact_duplicates_removed(self):
        document = {'hooks': hooks.generated_hooks()}
        foreign = {'hooks': [{'type': 'command', 'command': 'user'}]}
        document['hooks']['PreToolUse'].append(foreign)
        self.assertEqual(hooks.merge_settings(document), document)
        duplicate = copy.deepcopy(document)
        duplicate['hooks']['PreToolUse'].append(copy.deepcopy(duplicate['hooks']['PreToolUse'][0]))
        self.assertEqual(hooks.merge_settings(duplicate), document)

    def test_preflight_does_not_query_native_status_and_final_queries_once_per_root(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory) / name for name in ('cli', 'orca')]
            for root in roots:
                root.mkdir()
                (root / 'config.toml').write_bytes(b'')
            with patch.object(hooks, 'compatibility', return_value=(True, 'compatible')):
                result = hooks.provision(roots, apply=True)
            self.assertEqual(self.inspect.call_count, 2)
            self.assertEqual([r['root'] for r in result['roots']], [str(r) for r in roots])
            self.assertFalse(result['trust_required'])

    def test_cli_prints_per_root_action_without_opening_review(self):
        result = {'roots': [{'status': 'review-needed', 'reason': 'native-hook-review-required',
                            'review_instruction': 'CODEX_HOME=fixture; enter /hooks'}]}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(hooks, 'provision', return_value=result), redirect_stdout(stdout), redirect_stderr(stderr):
            hooks.main(['--provision'])
        self.assertEqual(json.loads(stdout.getvalue()), result)
        self.assertIn('CODEX_HOME=fixture; enter /hooks', stderr.getvalue())


class CodexNativeHookStatusTests(unittest.TestCase):
    def fixture(self, root, cwd):
        metadata = []
        for event, groups in hooks.generated_hooks().items():
            handler = groups[0]['hooks'][0]
            item = {'sourcePath': str(root / 'hooks.json'), 'eventName': event[0].lower() + event[1:],
                    'enabled': True, 'trustStatus': 'trusted'}
            if handler['type'] == 'command':
                item.update(handlerType='command', command=handler['command'])
            else:
                item.update(handlerType='mcpTool', server='engram', tool=hooks.TOOL)
            metadata.append(item)
        return {'data': [{'cwd': str(cwd), 'errors': [], 'warnings': [], 'hooks': metadata}]}

    def test_seven_hooks_including_title_are_required(self):
        root, cwd = Path.cwd() / 'fixture-root', Path.cwd()
        fixture = self.fixture(root, cwd)
        self.assertEqual(status.summarize(fixture, root, cwd)['status'], 'ready')
        fixture['data'][0]['hooks'].pop()
        self.assertEqual(status.summarize(fixture, root, cwd)['status'], 'unknown')

    def test_review_disabled_and_unavailable_are_distinct(self):
        root, cwd = Path.cwd() / 'fixture-root', Path.cwd()
        for trust in ('untrusted', 'modified'):
            fixture = self.fixture(root, cwd)
            fixture['data'][0]['hooks'][-1]['trustStatus'] = trust
            result = status.summarize(fixture, root, cwd)
            self.assertEqual(result['status'], 'review-needed')
            self.assertTrue(result['trust_required'])
        fixture['data'][0]['hooks'][0]['enabled'] = False
        self.assertEqual(status.summarize(fixture, root, cwd)['status'], 'disabled')
        with patch.object(status, 'native_list', side_effect=RuntimeError('private diagnostic')):
            result = status.inspect_root(root, cwd=cwd)
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('private diagnostic', json.dumps(result))
        self.assertIn(str(root), result['review_instruction'])
        self.assertIn('/hooks', result['review_instruction'])

    def test_exact_root_and_cwd_cannot_be_satisfied_by_other_root(self):
        root, cwd = Path.cwd() / 'fixture-root', Path.cwd()
        self.assertEqual(status.summarize(self.fixture(root / 'other', cwd), root, cwd)['status'], 'unknown')
        self.assertEqual(status.summarize(self.fixture(root, cwd / 'other'), root, cwd)['status'], 'unknown')

    def test_malformed_native_results_fail_closed(self):
        root, cwd = Path.cwd() / 'fixture-root', Path.cwd()
        fixtures = [None, {}, {'data': []}]
        for field, value in [('enabled', 'true'), ('trustStatus', None), ('eventName', []), ('command', {})]:
            fixture = self.fixture(root, cwd)
            fixture['data'][0]['hooks'][-1][field] = value
            fixtures.append(fixture)
        fixture = self.fixture(root, cwd)
        fixture['data'][0]['errors'] = ['private-error']
        fixtures.append(fixture)
        for fixture in fixtures:
            self.assertEqual(status.summarize(fixture, root, cwd)['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
