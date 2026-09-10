import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.install import service_config, service_lifecycle, process_identity
from core.config import runtime_config

ROOT = Path(__file__).resolve().parents[1]


class ServiceConfigTests(unittest.TestCase):
    def test_explicit_canonical_wins_and_overlay_port_is_valid_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            user, legacy = folder / 'user.config.yaml', folder / 'runtime.user.yaml'
            with patch.object(runtime_config, '_USER_CONFIG_PATH', user), patch.object(runtime_config, '_LEGACY_USER_CONFIG_PATH', legacy):
                base = {'overlay': {'stm_server_port': 29001}, 'mcp': {'http_port': 29002}}
                self.assertEqual(service_config.merge_service_config(base)['mcp']['http_port'], 29002)
                user.write_text('overlay:\n  stm_server_port: 29003\nmcp:\n  http_port: 29004\n', encoding='utf-8')
                result = service_config.merge_service_config(base)
                self.assertEqual(result['overlay']['stm_server_port'], 29003)
                self.assertEqual(result['mcp']['http_port'], 29004)
                self.assertEqual(base['mcp']['http_port'], 29002)
                user.write_text('mcp:\n  http_port: false\n', encoding='utf-8')
                with self.assertRaises(ValueError):
                    service_config.merge_service_config(base)

    def test_effective_preflight_does_not_create_profile_config(self):
        from overlay import config
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with patch.object(config, '_USER_CONFIG_PATH', folder / 'overlay.user.yaml'), \
                 patch.object(config, '_STATE_PATH', folder / 'overlay.state.yaml'), \
                 patch.object(runtime_config, '_USER_CONFIG_PATH', folder / 'user.config.yaml'), \
                 patch.object(runtime_config, '_LEGACY_USER_CONFIG_PATH', folder / 'runtime.user.yaml'):
                cfg = service_config.effective_service_config()
                self.assertGreater(cfg['overlay']['stm_server_port'], 0)
                self.assertEqual(list(folder.iterdir()), [])


class ServiceHandoverTests(unittest.TestCase):
    def identity(self):
        return {'ProcessId': 777, 'ParentProcessId': 1, 'ExecutablePath': 'C:/Python/python.exe',
                'CommandLine': f'"C:/Python/python.exe" "{ROOT / "engram_overlay_entry.py"}"',
                'CreationDate': '2026-09-09T00:00:00Z'}

    @unittest.skipUnless(os.name == 'nt', 'Windows TCP owner table')
    def test_real_listener_owner_table(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            sock.listen()
            self.assertEqual(service_lifecycle.listener_pid(sock.getsockname()[1]), os.getpid())

    def test_pid_reuse_rejected_even_with_same_command(self):
        first = self.identity()
        self.assertFalse(process_identity._same_process(first, dict(first, CreationDate='new-instance')))
        self.assertFalse(process_identity._same_process(dict(first, CreationDate=''), dict(first, CreationDate='')))

    def test_arbitrary_executable_with_entry_argument_not_authorized(self):
        process = dict(self.identity(), ExecutablePath='C:/Tools/other.exe',
                       CommandLine=f'"C:/Tools/other.exe" "{ROOT / "engram_overlay_entry.py"}"')
        self.assertFalse(service_lifecycle.is_project_host(process, ROOT, 'C:/Python/python.exe'))

    def test_process_query_failure_is_not_proof_of_exit(self):
        with patch.object(process_identity, '_run_powershell', side_effect=RuntimeError('query failed')):
            with self.assertRaises(RuntimeError):
                service_lifecycle._still_same(self.identity())

    def test_foreign_mcp_preflight_does_not_stop_old_host(self):
        cfg = {'overlay': {'stm_server_port': 29001}, 'mcp': {'http_port': 29002}, 'dashboard': {'enabled': False}}
        with patch.object(service_lifecycle, 'listener_pid', side_effect=[777, 888]), \
             patch.object(service_lifecycle, 'health', return_value={'pid': 777, 'role': 'overlay-stm'}), \
             patch.object(process_identity, 'get_process_identity', return_value=self.identity()), \
             patch.object(process_identity, 'list_candidate_processes', return_value=[]), \
             patch.object(service_lifecycle.urllib.request, 'urlopen') as request:
            with self.assertRaises(RuntimeError):
                service_lifecycle.prepare_service_handover(cfg, ROOT, 'C:/Python/python.exe')
            request.assert_not_called()

    def test_wrong_health_identity_never_requests_shutdown(self):
        with patch.object(service_lifecycle, 'listener_pid', return_value=777), \
             patch.object(service_lifecycle, 'health', return_value={'pid': 778, 'role': 'overlay-stm'}), \
             patch.object(process_identity, 'get_process_identity', return_value=self.identity()), \
             patch.object(service_lifecycle.urllib.request, 'urlopen') as request:
            with self.assertRaises(RuntimeError):
                service_lifecycle.prepare_service_handover({'overlay': {'stm_server_port': 29001}, 'mcp': {'http_port': 29002}}, ROOT, 'C:/Python/python.exe')
            request.assert_not_called()

    def test_cross_worktree_repository_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            main, work = folder / 'main', folder / 'work'
            (main / '.git/worktrees/work').mkdir(parents=True)
            work.mkdir()
            (work / '.git').write_text(f'gitdir: {main / ".git/worktrees/work"}', encoding='utf-8')
            (main / '.git/worktrees/work/commondir').write_text('../..', encoding='utf-8')
            self.assertEqual(service_lifecycle.repository_identity(main), service_lifecycle.repository_identity(work))

    def test_proven_host_graceful_handover(self):
        cfg = {'overlay': {'stm_server_port': 29001}, 'mcp': {'http_port': 29002}, 'dashboard': {'enabled': False}}
        with patch.object(service_lifecycle, 'listener_pid', side_effect=[777, None, 777, None, None]), \
             patch.object(service_lifecycle, 'health', return_value={'pid': 777, 'role': 'overlay-stm'}), \
             patch.object(process_identity, 'get_process_identity', return_value=self.identity()), \
             patch.object(process_identity, 'list_candidate_processes', return_value=[]), \
             patch.object(service_lifecycle, '_still_same', side_effect=[True, False, False]), \
             patch.object(service_lifecycle.urllib.request, 'urlopen') as request:
            service_lifecycle.prepare_service_handover(cfg, ROOT, 'C:/Python/python.exe')
            self.assertEqual(request.call_args.args[0].method, 'POST')

    def test_live_parent_mcp_not_adopted_or_killed(self):
        child = dict(self.identity(), CommandLine=f'"C:/Python/python.exe" "{ROOT / "mcp_server.py"}"')
        with patch.object(service_lifecycle, 'listener_pid', return_value=777), \
             patch.object(process_identity, 'get_process_identity', side_effect=[child, self.identity()]), \
             patch.object(process_identity, 'terminate_identity_exact') as terminate:
            with self.assertRaises(RuntimeError):
                service_lifecycle._clear_proven_orphan_mcp({'mcp': {'http_port': 29002}}, ROOT)
            terminate.assert_not_called()

    def test_proven_orphan_mcp_and_watcher_siblings_cleaned_together(self):
        child = dict(self.identity(), CommandLine=f'"C:/Python/python.exe" "{ROOT / "mcp_server.py"}"')
        watcher = dict(child, ProcessId=778, CommandLine=f'"C:/Python/python.exe" "{ROOT / "scripts/kg/kg_watcher.py"}"')
        cfg = {'mcp': {'http_port': 29002}, 'dashboard': {'enabled': False}}
        with patch.object(service_lifecycle, 'listener_pid', side_effect=[777, 777, None]), \
             patch.object(process_identity, 'get_process_identity', side_effect=[child, None]), \
             patch.object(service_lifecycle, '_still_same', return_value=True), \
             patch.object(process_identity, 'list_candidate_processes', return_value=[child, watcher]), \
             patch.object(process_identity, 'terminate_identity_exact', return_value=True) as terminate:
            service_lifecycle._clear_proven_orphan_mcp(cfg, ROOT)
            self.assertEqual([call.args[0]['ProcessId'] for call in terminate.call_args_list], [777, 778])
