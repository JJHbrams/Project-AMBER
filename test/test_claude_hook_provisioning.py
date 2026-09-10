"""Installer hook mutation tests use disposable settings only, never a live CLI."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from core.install import claude_monitor_hooks as hooks

ROOT = Path(__file__).resolve().parents[1]


class HookProvisioningTests(unittest.TestCase):
    def test_source_entry_role_provisions_only_explicit_disposable_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / '.claude/settings.json'
            run = subprocess.run([sys.executable, str(ROOT / 'engram_overlay_entry.py'),
                                  '--role', 'claude-monitor-hooks', '--provision',
                                  '--apply', '--settings', str(target)],
                                 capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertEqual(set(result), {'supported', 'changed', 'applied', 'hook_count', 'reason'})
            if result['supported']:
                self.assertTrue(result['applied'])
                self.assertEqual(result['hook_count'], 9)
                self.assertEqual(json.loads(target.read_text(encoding='utf-8'))['hooks'], hooks.generated_hooks())
            else:
                self.assertFalse(target.exists())
                self.assertIn(result['reason'], {'claude-not-installed', 'claude-version-unavailable', 'claude-below-validated-minimum'})

    def test_static_sessionstart_reminder_runs_without_input_or_runtime(self):
        command = hooks.generated_hooks()['SessionStart'][0]['hooks'][0]['command']
        result = subprocess.run(command, shell=True, input='PRIVATE_TRANSCRIPT',
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), hooks.TITLE_REMINDER)
        self.assertNotIn('PRIVATE_TRANSCRIPT', result.stdout)
        self.assertNotIn(str(ROOT), command)
        self.assertNotIn('${', command)

    def test_exact_reminder_ownership_preserves_similar_user_hook(self):
        custom = {'type': 'command', 'command': 'echo engram-monitor-connection-title'}
        original = {'hooks': {'SessionStart': [{'hooks': [custom]}]}}
        merged = hooks.merge_settings(original)
        self.assertEqual(merged['hooks']['SessionStart'][0]['hooks'], [custom])
        self.assertEqual(hooks.merge_settings(merged), merged)

    def test_prior_shipped_reminder_is_upgraded_without_duplicates(self):
        for windows in (True, False):
            with self.subTest(windows=windows):
                old_command = hooks.title_reminder_command(hooks.LEGACY_TITLE_REMINDERS[0], windows=windows)
                custom = {'type': 'command', 'command': old_command + ' # user modified'}
                settings = {'hooks': {'SessionStart': [{'hooks': [
                    {'type': 'command', 'command': old_command, 'timeout': 5}, custom
                ]}]}}
                updated = hooks.merge_settings(settings)
                handlers = [handler for group in updated['hooks']['SessionStart'] for handler in group['hooks']]
                self.assertEqual(len(handlers), 2)
                self.assertIn(custom, handlers)
                self.assertEqual(sum(h['command'] == hooks.title_reminder_command() for h in handlers), 1)
                self.assertEqual(updated, hooks.merge_settings(updated))

    def test_existing_current_groups_keep_position_after_bootstrap_appends(self):
        settings = {'hooks': hooks.generated_hooks()}
        for event in ('PreToolUse', 'SessionStart'):
            settings['hooks'][event].append({'hooks': [
                {'type': 'command', 'command': 'existing bootstrap ' + event}
            ]})
        self.assertEqual(hooks.merge_settings(settings), settings)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'settings.json'
            target.write_text(json.dumps(settings), encoding='utf-8')
            before = target.read_bytes()
            self.assertFalse(hooks.configure(target)['changed'])
            self.assertFalse(hooks.configure(target, apply=True)['changed'])
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_duplicate_cleanup_keeps_first_exact_current_group_in_place(self):
        settings = {'hooks': hooks.generated_hooks()}
        current = settings['hooks']['SessionStart'][0]
        unrelated = {'hooks': [{'type': 'command', 'command': 'user existing'}]}
        legacy = {'hooks': [{'type': 'command', 'command': hooks.title_reminder_command(hooks.LEGACY_TITLE_REMINDERS[0])}]}
        settings['hooks']['SessionStart'] = [unrelated, current, legacy, current, unrelated]
        result = hooks.merge_settings(settings)
        self.assertEqual(result['hooks']['SessionStart'], [unrelated, current, unrelated])
        self.assertEqual(hooks.merge_settings(result), result)

    def test_current_connection_hint_is_explicit_and_metadata_only(self):
        from core.integrations.claude_lifecycle import TITLE_CONTEXT
        for text in (TITLE_CONTEXT, hooks.TITLE_REMINDER):
            self.assertIn('current user request', text)
            self.assertIn('engram_report_session_title', text)
            self.assertIn('Reuse the prior safe title', text)
            self.assertIn('not registered', text)
            self.assertNotIn('first substantive', text)
            self.assertNotIn('If a short safe task title', text)
            self.assertIn('Do not repeat memory', text)

    def test_compatibility_is_bounded_and_never_starts_conversation(self):
        for output, code, supported in [('2.1.266 (Claude Code)', 0, True),
                                         ('2.1.195 (Claude Code)', 0, False),
                                         ('unknown', 0, False), ('2.1.266', 1, False)]:
            with self.subTest(output=output, code=code), patch.object(hooks.shutil, 'which', return_value='claude.exe'), patch.object(hooks.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, output, '')) as run:
                self.assertEqual(hooks.compatibility()[0], supported)
                self.assertEqual(run.call_args.args[0], ['claude.exe', '--version'])
                self.assertEqual(run.call_args.kwargs['timeout'], 5)

    def test_missing_cli_does_not_create_profile(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(hooks.shutil, 'which', return_value=None):
            target = Path(directory) / '.claude/settings.json'
            result = hooks.provision(target, apply=True)
            self.assertFalse(result['changed'])
            self.assertFalse(target.parent.exists())

    def test_preserve_orca_commands_other_servers_and_user_options(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(hooks, 'compatibility', return_value=(True, 'compatible')):
            target = Path(directory) / 'settings.json'
            original = {'secret': 'PRIVATE', 'hooks': {'Stop': [{'hooks': [
                {'type': 'command', 'command': 'orca preserved'},
                {'type': 'mcp_tool', 'server': 'other', 'tool': hooks.TOOL}
            ]}]}}
            target.write_text(json.dumps(original), encoding='utf-8')
            result = hooks.provision(target, apply=True)
            current = json.loads(target.read_text(encoding='utf-8'))
            self.assertEqual(current['hooks']['Stop'][0], original['hooks']['Stop'][0])
            self.assertEqual(current['secret'], original['secret'])
            self.assertNotIn('PRIVATE', json.dumps(result))
            self.assertNotIn('orca', json.dumps(result))
            self.assertTrue(result['applied'])
            self.assertFalse(hooks.provision(target, apply=True)['changed'])

    def test_reapply_changed_owned_hook_preserves_each_revision_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'settings.json'
            target.write_text('{"first":true}', encoding='utf-8')
            hooks.configure(target, apply=True)
            edited = json.loads(target.read_text(encoding='utf-8'))
            edited['hooks']['Stop'][-1]['hooks'][0]['timeout'] = 9
            target.write_text(json.dumps(edited), encoding='utf-8')
            with self.assertRaises(ValueError):
                hooks.configure(target, apply=True)
            backups = list(target.parent.glob('settings.json.engram-monitor-backup*'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(target.read_text(encoding='utf-8')), edited)

    def test_explicit_disable_and_invalid_settings_preserved(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(hooks, 'compatibility', return_value=(True, 'compatible')):
            target = Path(directory) / 'settings.json'
            text = '{"disableAllHooks":true,"secret":"PRIVATE"}'
            target.write_text(text, encoding='utf-8')
            self.assertEqual(hooks.provision(target, apply=True)['reason'], 'user-disabled-all-hooks')
            self.assertEqual(target.read_text(encoding='utf-8'), text)
            target.write_text('{invalid', encoding='utf-8')
            with self.assertRaises(ValueError):
                hooks.provision(target, apply=True)
            self.assertEqual(target.read_text(), '{invalid')
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_missing_settings_parent_created_only_on_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / '.claude/settings.json'
            hooks.configure(target)
            self.assertFalse(target.parent.exists())
            hooks.configure(target, apply=True)
            self.assertTrue(target.is_file())

    def test_concurrent_edit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'settings.json'
            target.write_text('{}', encoding='utf-8')
            original_fsync = hooks.os.fsync
            def concurrent_edit(fd):
                original_fsync(fd)
                target.write_text('{"concurrent":true}', encoding='utf-8')
            with patch.object(hooks.os, 'fsync', side_effect=concurrent_edit), self.assertRaisesRegex(ValueError, 'changed during apply'):
                hooks.configure(target, apply=True)
            self.assertEqual(json.loads(target.read_text()), {'concurrent': True})
            self.assertFalse(list(target.parent.glob('.engram-hooks-*')))

    def test_entrypoints_wire_common_role_and_dev_nostart_precedes_mutation(self):
        dev = (ROOT / 'dev-rebuild.ps1').read_text(encoding='utf-8-sig')
        self.assertLess(dev.index('if ($NoStart) {'), dev.index('--role claude-monitor-hooks'))
        source = (ROOT / 'installer/modules/05_config.ps1').read_text(encoding='utf-8-sig')
        setup = (ROOT / 'installer/configure.ps1').read_text(encoding='utf-8-sig')
        self.assertIn('--role claude-monitor-hooks --provision --apply', source)
        self.assertIn('-Role claude-monitor-hooks', setup)
        self.assertIn('from core.install.claude_monitor_hooks import main', (ROOT / 'engram_overlay_entry.py').read_text(encoding='utf-8-sig'))


if __name__ == '__main__':
    unittest.main()
