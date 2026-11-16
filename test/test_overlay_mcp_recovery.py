import subprocess
import threading
import unittest
from unittest.mock import Mock

from overlay.main import OverlayApp


SETTINGS = {
    "port": 17385,
    "restart_cooldown_secs": 0.0,
    "ready_timeout_secs": 2.0,
    "interval_secs": 1.0,
    "fail_threshold": 1,
    "start_delay_secs": 0.0,
}


class _Proc:
    def __init__(self, wait_results=(), dead=False):
        self.wait_results = list(wait_results)
        self.dead = dead
        self.terminate = Mock()
        self.kill = Mock()
        self.wait = Mock(side_effect=self._wait)

    def _wait(self, timeout):
        if self.wait_results:
            result = self.wait_results.pop(0)
            if isinstance(result, BaseException):
                raise result
        self.dead = True

    def poll(self):
        return 0 if self.dead else None


class OverlayMcpRecoveryTests(unittest.TestCase):
    def _app(self, proc=None):
        app = OverlayApp.__new__(OverlayApp)
        app._quitting = False
        app._mcp_recovery_lock = threading.Lock()
        app._last_mcp_recovery_at = 0.0
        app._mcp_http_proc = proc
        app._kg_watcher_proc = None
        app._dashboard_proc = None
        app._stm_server = Mock()
        app._get_mcp_health_settings = Mock(return_value=dict(SETTINGS))
        app._start_mcp_http_server = Mock()
        app._wait_mcp_ready = Mock(return_value=True)
        app._wait_mcp_listener_released = Mock(return_value=True)
        app._restart_managed_dependents = Mock()
        return app

    def test_terminate_waits_after_kill_before_clearing_handle(self):
        proc = _Proc([subprocess.TimeoutExpired("mcp", 5), None])
        app = self._app(proc)

        self.assertTrue(app._terminate_managed_process("_mcp_http_proc", "mcp", "MCP", timeout=5.0))

        proc.terminate.assert_called_once_with()
        proc.kill.assert_called_once_with()
        self.assertEqual([call.kwargs["timeout"] for call in proc.wait.call_args_list], [5.0, 1.0])
        self.assertIsNone(app._mcp_http_proc)

    def test_persistent_exit_timeout_retains_handle_and_never_spawns_replacement(self):
        proc = _Proc([subprocess.TimeoutExpired("mcp", 5), subprocess.TimeoutExpired("mcp", 1)])
        app = self._app(proc)

        app._recover_mcp_listener("proc_dead")

        self.assertIs(app._mcp_http_proc, proc)
        app._start_mcp_http_server.assert_not_called()
        app._wait_mcp_listener_released.assert_not_called()
        self.assertTrue(app._stm_server.publish_mcp_ready.call_args.args[0] is False)

    def test_transient_port_occupation_retries_after_release_and_starts_owned_child(self):
        old = _Proc([None])
        new = _Proc()
        app = self._app(old)
        app._start_mcp_http_server.return_value = new
        app._wait_mcp_listener_released.side_effect = [False, True]

        app._recover_mcp_listener("listener_unavailable")
        app._recover_mcp_listener("listener_unavailable")

        self.assertEqual(app._start_mcp_http_server.call_count, 1)
        self.assertIs(app._mcp_http_proc, new)
        app._restart_managed_dependents.assert_called_once_with()
        self.assertEqual(app._wait_mcp_ready.call_args.kwargs["require_managed_process"], True)

    def test_persistent_occupied_port_never_starts_or_kills_foreign_listener(self):
        old = _Proc([None])
        app = self._app(old)
        app._wait_mcp_listener_released.return_value = False

        app._recover_mcp_listener("listener_unavailable")

        old.terminate.assert_called_once_with()
        old.kill.assert_not_called()
        app._start_mcp_http_server.assert_not_called()
        self.assertIsNone(app._mcp_http_proc)

    def test_successful_recovery_restarts_dependents_only_after_strict_ready(self):
        old = _Proc([None])
        new = _Proc()
        app = self._app(old)
        app._start_mcp_http_server.return_value = new

        app._recover_mcp_listener("http_unhealthy")

        app._wait_mcp_ready.assert_called_once_with(
            timeout=SETTINGS["ready_timeout_secs"], port=SETTINGS["port"], require_managed_process=True
        )
        app._restart_managed_dependents.assert_called_once_with()

    def test_monitor_continues_with_next_cycle_after_health_exception(self):
        app = self._app()
        calls = {"listener": 0, "sleep": 0}

        def listener(_port):
            calls["listener"] += 1
            if calls["listener"] == 1:
                raise RuntimeError("temporary probe failure")
            return True

        def sleep(_seconds):
            calls["sleep"] += 1
            if calls["sleep"] >= 3:
                app._quitting = True

        app._is_mcp_listener_ready = Mock(side_effect=listener)
        app._is_mcp_http_healthy = Mock(return_value=True)
        with unittest.mock.patch("overlay.main.time.sleep", side_effect=sleep):
            app._mcp_health_monitor_loop()

        self.assertGreaterEqual(calls["listener"], 2)
        self.assertTrue(any(call.args[0] is False for call in app._stm_server.publish_mcp_ready.call_args_list))

    def test_monitor_recovers_after_start_exception_in_real_recovery_path(self):
        new = _Proc()
        app = self._app()
        app._is_mcp_listener_ready = Mock(return_value=False)
        app._is_mcp_http_healthy = Mock(return_value=False)
        app._start_mcp_http_server.side_effect = [RuntimeError("start failed"), new]

        def restarted_dependents():
            app._quitting = True

        app._restart_managed_dependents.side_effect = restarted_dependents
        with unittest.mock.patch("overlay.main.time.sleep"):
            app._mcp_health_monitor_loop()

        self.assertEqual(app._start_mcp_http_server.call_count, 2)
        self.assertIs(app._mcp_http_proc, new)
        self.assertTrue(any(call.args[0] is False for call in app._stm_server.publish_mcp_ready.call_args_list))

    def test_dependent_stop_failure_preserves_handle_without_replacement(self):
        watcher = _Proc([subprocess.TimeoutExpired("watcher", 5), subprocess.TimeoutExpired("watcher", 1)])
        app = self._app()
        app._kg_watcher_proc = watcher
        app._dashboard_proc = None
        app._start_kg_watcher = Mock()
        app._start_dashboard = Mock()
        app._is_dashboard_enabled = Mock(return_value=False)

        app._restart_managed_dependents()

        self.assertIs(app._kg_watcher_proc, watcher)
        app._start_kg_watcher.assert_not_called()
        app._start_dashboard.assert_not_called()

