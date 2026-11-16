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

    def test_old_host_exiting_during_kill_attempt_is_not_a_failure(self):
        # 기다림이 끝난 순간엔 살아 있었고, 강제 종료를 확인하는 사이에 스스로 끝났다.
        cfg = {'overlay': {'stm_server_port': 29001}, 'mcp': {'http_port': 29002}, 'dashboard': {'enabled': False}}
        with patch.object(service_lifecycle, 'listener_pid', side_effect=[777, None, 777, None, None]), \
             patch.object(service_lifecycle, 'health', return_value={'pid': 777, 'role': 'overlay-stm'}), \
             patch.object(process_identity, 'get_process_identity', return_value=self.identity()), \
             patch.object(process_identity, 'list_candidate_processes', return_value=[]), \
             patch.object(service_lifecycle, '_still_same', side_effect=[True, True, True, True, False]), \
             patch.object(service_lifecycle.time, 'monotonic', side_effect=[0.0, 0.0, 999.0]), \
             patch.object(service_lifecycle.time, 'sleep'), \
             patch.object(process_identity, 'terminate_identity_exact', return_value=False) as terminate, \
             patch.object(service_lifecycle.urllib.request, 'urlopen'):
            # 순서: preflight · 대기 2회 · 기한 후 확인(살아있음) → kill 실패 → 재확인(이미 종료)
            service_lifecycle.prepare_service_handover(cfg, ROOT, 'C:/Python/python.exe')
            terminate.assert_called_once()

    def test_old_host_still_alive_after_failed_kill_is_refused(self):
        cfg = {'overlay': {'stm_server_port': 29001}, 'mcp': {'http_port': 29002}, 'dashboard': {'enabled': False}}
        with patch.object(service_lifecycle, 'listener_pid', side_effect=[777, None, 777]), \
             patch.object(service_lifecycle, 'health', return_value={'pid': 777, 'role': 'overlay-stm'}), \
             patch.object(process_identity, 'get_process_identity', return_value=self.identity()), \
             patch.object(process_identity, 'list_candidate_processes', return_value=[]), \
             patch.object(service_lifecycle, '_still_same', return_value=True), \
             patch.object(service_lifecycle.time, 'monotonic', side_effect=[0.0, 999.0]), \
             patch.object(process_identity, 'terminate_identity_exact', return_value=False), \
             patch.object(service_lifecycle.urllib.request, 'urlopen'):
            with self.assertRaisesRegex(RuntimeError, 'did not exit'):
                service_lifecycle.prepare_service_handover(cfg, ROOT, 'C:/Python/python.exe')

    def test_handover_waits_longer_than_old_host_quit_budget(self):
        # quit(): bubble 5 s + STM promote 15 s + tunnels/children — 20 s 로는 정상 종료도 못 기다렸다.
        self.assertGreaterEqual(service_lifecycle.HANDOVER_TIMEOUT_SEC, 40)

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


class OrphanDashboardTests(unittest.TestCase):
    EXE = 'C:/App/engram-overlay.exe'
    CFG = {'mcp': {'http_port': 29002}, 'dashboard': {'enabled': True, 'port': 29003}}

    def dash(self, path='C:/App/engram-dashboard.exe'):
        return {'ProcessId': 900, 'ParentProcessId': 5, 'ExecutablePath': path,
                'CommandLine': f'"{path}"', 'CreationDate': '2026-09-09T00:00:00Z'}

    def run_clear(self, child, parent, listeners=(None, 900, 900, None)):
        # listener_pid: mcp port, dashboard (outer check), dashboard (inner), dashboard after kill
        feed = iter(listeners)
        with patch.object(service_lifecycle, 'listener_pid', side_effect=lambda port: next(feed)), \
             patch.object(process_identity, 'get_process_identity', side_effect=[child, parent]), \
             patch.object(service_lifecycle, '_still_same', return_value=True), \
             patch.object(service_lifecycle, '_exited_or_terminated', return_value=True) as stop:
            try:
                service_lifecycle._clear_proven_orphan_mcp(self.CFG, ROOT, self.EXE)
            finally:
                self.stop = stop

    def test_verified_orphan_dashboard_terminated(self):
        self.run_clear(self.dash(), None, listeners=(None, 900, 900, 900, None))
        self.stop.assert_called_once()
        self.assertEqual(self.stop.call_args.args[0]['ProcessId'], 900)

    def test_live_parent_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_clear(self.dash(), {'ProcessId': 5}, listeners=(None, 900, 900, 900))
        self.stop.assert_not_called()

    def test_foreign_exe_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_clear(self.dash('C:/Other/engram-dashboard.exe'), None, listeners=(None, 900, 900, 900))
        self.stop.assert_not_called()

    def test_both_ports_free_returns(self):
        with patch.object(service_lifecycle, 'listener_pid', return_value=None), \
             patch.object(service_lifecycle, '_exited_or_terminated') as stop:
            service_lifecycle._clear_proven_orphan_mcp(self.CFG, ROOT, self.EXE)
            stop.assert_not_called()
