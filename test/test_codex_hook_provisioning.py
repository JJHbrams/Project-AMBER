import json
import copy
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import tempfile
import tomllib
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

    def test_migrates_exact_eight_groups_to_toml_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = b'[features]\nhooks = true\n[projects.example]\ntrust_level = "trusted"\n[hooks.state]\nkeep = "exact"\n'
            (root / 'config.toml').write_bytes(config)
            before = json.dumps({'hooks': hooks.generated_hooks(), 'description': 'user-owned'}).encode()
            (root / 'hooks.json').write_bytes(before)
            dry = hooks.configure_root(root, migration=True)
            self.assertTrue(dry['changed'])
            self.assertFalse(dry['applied'])
            self.assertEqual((root / 'hooks.json').read_bytes(), before)
            result = hooks.configure_root(root, apply=True, migration=True)
            self.assertFalse(result['trust_required'])
            self.assertEqual(result['status'], 'ready')
            inline = tomllib.loads((root / 'config.toml').read_text(encoding='utf-8'))
            self.assertTrue((root / 'config.toml').read_bytes().startswith(config))
            self.assertEqual(set(inline['hooks']).intersection(hooks.EVENTS), set(hooks.EVENTS))
            self.assertEqual(inline['projects']['example']['trust_level'], 'trusted')
            self.assertEqual(inline['hooks']['state']['keep'], 'exact')
            self.assertEqual(json.loads((root / 'hooks.json').read_text())['hooks']['SessionStart'], hooks.generated_hooks()['SessionStart'])
            migrated_json = json.loads((root / 'hooks.json').read_text())
            self.assertTrue(all(migrated_json['hooks'][event][0] == {'hooks': []} for event in hooks.EVENTS))
            self.assertEqual(next(root.glob('hooks.json.engram-monitor-backup-*')).read_bytes(), before)
            self.assertFalse(hooks.configure_root(root, apply=True, migration=True)['changed'])
            self.assertEqual(len(list(root.glob('hooks.json.engram-monitor-backup-*'))), 1)

    def test_clean_root_gets_nine_hooks_without_upgrading_existing_legacy_root(self):
        with tempfile.TemporaryDirectory() as directory:
            clean = Path(directory) / 'clean'
            clean.mkdir()
            (clean / 'config.toml').write_bytes(b'')
            installed = hooks.configure_root(clean, apply=True, inspect=False)
            self.assertTrue(installed['changed'])
            self.assertEqual(set(json.loads((clean / 'hooks.json').read_text())['hooks']), set(hooks.EVENTS) | {'SessionStart'})

            legacy = Path(directory) / 'legacy'
            legacy.mkdir()
            (legacy / 'config.toml').write_bytes(b'')
            legacy_hooks = hooks.generated_hooks(subagent_activity=False)
            (legacy / 'hooks.json').write_text(json.dumps({'hooks': legacy_hooks}), encoding='utf-8')
            preserved = hooks.configure_root(legacy, apply=True, inspect=False)
            self.assertFalse(preserved['changed'])

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
                (root / 'hooks.json').write_text(json.dumps({'hooks': hooks.generated_hooks()}), encoding='utf-8')
            with patch.object(hooks, 'compatibility', return_value=(True, 'compatible')):
                result = hooks.provision(roots, apply=True)
            self.assertEqual(self.inspect.call_count, 2)
            self.assertEqual([r['root'] for r in result['roots']], [str(r) for r in roots])
            self.assertFalse(result['trust_required'])

    def test_orca_runtime_root_is_deferred_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'orca/codex-runtime-home/home'
            root.mkdir(parents=True)
            (root / 'config.toml').write_text('', encoding='utf-8')
            (root / 'hooks.json').write_text(json.dumps({'hooks': hooks.generated_hooks()}), encoding='utf-8')
            result = hooks.configure_root(root, apply=True, inspect=False, migration=True)
            self.assertEqual(result['status'], 'deferred')
            self.assertEqual(result['reason'], 'orca-runtime-root-deferred-to-system-root')
            self.assertFalse(list(root.glob('*backup*')))

    def test_inline_complete_is_idempotent_but_partial_inline_is_conflict_without_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.toml').write_text(hooks._toml_mcp_groups('engram'), encoding='utf-8')
            complete = hooks.configure_root(root, apply=True, inspect=False, migration=True)
            self.assertFalse(complete['changed'])
            self.assertFalse((root / 'hooks.json').exists())
            (root / 'config.toml').write_text('\n'.join(hooks._toml_mcp_groups('engram').splitlines()[:8]), encoding='utf-8')
            partial = hooks.configure_root(root, apply=True, inspect=False, migration=True)
            self.assertTrue(partial['conflict'])
            self.assertFalse((root / 'hooks.json').exists())

    def test_config_first_partial_is_rolled_back_before_replanning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before_config = b'[hooks.state]\nkeep = "yes"\n'
            before_hooks = json.dumps({'hooks': hooks.generated_hooks()}).encode()
            (root / 'config.toml').write_bytes(before_config)
            (root / 'hooks.json').write_bytes(before_hooks)
            token = 'a' * 32
            config_backup = root / ('config.toml.engram-monitor-backup-' + token)
            hooks_backup = root / ('hooks.json.engram-monitor-backup-' + token)
            config_backup.write_bytes(before_config); hooks_backup.write_bytes(before_hooks)
            (root / 'config.toml').write_bytes(before_config + b'\n' + hooks._toml_mcp_groups('engram').encode())
            (root / '.engram-codex-hook-migration.json').write_text(json.dumps({
                'phase': 'config-written', 'token': token, 'config_backup': config_backup.name,
                'hooks_backup': hooks_backup.name, 'config_sha256': hooks.hashlib.sha256(before_config).hexdigest(),
                'hooks_sha256': hooks.hashlib.sha256(before_hooks).hexdigest()}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'apply recovery'):
                hooks.configure_root(root, apply=False, inspect=False, migration=True)
            self.assertTrue((root / '.engram-codex-hook-migration.json').exists())
            result = hooks.configure_root(root, apply=True, inspect=False, migration=True)
            self.assertTrue(result['changed'])
            self.assertTrue((root / 'config.toml').read_bytes().startswith(before_config))
            self.assertFalse((root / '.engram-codex-hook-migration.json').exists())

    def test_repair_restores_slots_only_when_backup_and_current_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / '.codex'; root.mkdir()
            original = {'hooks': hooks.generated_hooks()}
            legacy = copy.deepcopy(original)
            for event in hooks.EVENTS:
                legacy['hooks'][event] = []
            (root / 'config.toml').write_text(hooks._toml_mcp_groups('engram'), encoding='utf-8')
            (root / 'hooks.json').write_text(json.dumps(legacy), encoding='utf-8')
            token = 'b' * 32
            (root / ('hooks.json.engram-monitor-backup-' + token)).write_text(json.dumps(original), encoding='utf-8')
            (root / ('config.toml.engram-monitor-backup-' + token)).write_text('original', encoding='utf-8')
            result = hooks.repair_migrated_system_root(home=Path(directory), apply=True)
            self.assertTrue(result['applied'])
            repaired = json.loads((root / 'hooks.json').read_text())
            self.assertTrue(all(repaired['hooks'][event] == [{'hooks': []}] for event in hooks.EVENTS))

    def test_cli_prints_per_root_action_without_opening_review(self):
        result = {'roots': [{'status': 'review-needed', 'reason': 'native-hook-review-required',
                            'review_instruction': 'CODEX_HOME=fixture; enter /hooks'}]}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(hooks, 'provision', return_value=result), redirect_stdout(stdout), redirect_stderr(stderr):
            hooks.main(['--provision'])
        self.assertEqual(json.loads(stdout.getvalue()), result)
        self.assertIn('CODEX_HOME=fixture; enter /hooks', stderr.getvalue())

    def test_repair_cli_dry_run_prints_json_without_status_lookup(self):
        result = {'root': 'fixture', 'changed': True, 'applied': False,
                  'trust_autoapproved': False, 'reason': 'repair-ready'}
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(hooks, 'repair_migrated_system_root', return_value=result), redirect_stdout(stdout), redirect_stderr(stderr):
            hooks.main(['--repair-migrated-system-root'])
        self.assertEqual(json.loads(stdout.getvalue()), result)
        self.assertEqual(stderr.getvalue(), '')


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

    def test_exact_dual_representation_warning_keeps_review_needed(self):
        root, cwd = Path.cwd() / 'fixture-root', Path.cwd()
        fixture = self.fixture(root, cwd)
        for item in fixture['data'][0]['hooks']:
            if item['handlerType'] == 'mcpTool':
                item['sourcePath'] = str(root / 'config.toml')
                item['trustStatus'] = 'untrusted'
        fixture['data'][0]['warnings'] = [status._expected_dual_representation_warning(root)]
        result = status.summarize(fixture, root, cwd)
        self.assertEqual(result['status'], 'review-needed')
        self.assertTrue(result['trust_required'])
        fixture['data'][0]['warnings'].append('other warning')
        self.assertEqual(status.summarize(fixture, root, cwd)['status'], 'unknown')

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
