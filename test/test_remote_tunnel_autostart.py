import subprocess
import time
import unittest
from unittest.mock import Mock, patch

from overlay.remote_tunnel import (
    STATE_AUTH_FAILED,
    STATE_CONNECTING,
    STATE_FAILED,
    STATE_UP,
    TunnelManager,
)


class _ImmediateAuthFailure:
    pid = 4321

    def __init__(self):
        self.stderr = iter(["Permission denied (publickey).\n"])

    def poll(self):
        return 255


class _ControlledProcess:
    pid = 4322

    def __init__(self):
        self.stderr = iter(())
        self.alive = True

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.alive = False

    def wait(self, timeout=None):
        return 1

    def kill(self):
        self.alive = False


class TunnelAutomaticStartTests(unittest.TestCase):
    def setUp(self):
        self.manager = TunnelManager(get_port=lambda: 17386, poll_interval=0.01)

    def tearDown(self):
        self.manager.stop_all()

    def test_register_remains_list_only_and_automatic_start_is_noninteractive(self):
        with patch.object(self.manager, "_ensure_thread") as ensure_thread:
            self.manager.register(["remote-a"])
            self.assertEqual(self.manager.status()["remote-a"].state, "down")

            self.manager.start_automatic("remote-a")

        self.assertEqual(self.manager.status()["remote-a"].state, STATE_CONNECTING)
        tunnel = self.manager._tunnels["remote-a"]
        self.assertFalse(tunnel.user_requested)
        self.assertFalse(tunnel.console_tried)
        self.assertIn("BatchMode=yes", self.manager._build_cmd("remote-a", 17386))
        self.assertEqual(ensure_thread.call_count, 2)

    def test_automatic_start_is_idempotent_for_active_and_backoff_states(self):
        self.manager.register(["remote-a"])
        with patch.object(self.manager, "_ensure_thread") as ensure_thread:
            tunnel = self.manager._tunnels["remote-a"]
            for state in (STATE_CONNECTING, STATE_UP, STATE_AUTH_FAILED):
                tunnel.status.state = state
                self.manager.start_automatic("remote-a")
                self.assertEqual(tunnel.status.state, state)

            tunnel.status.state = STATE_FAILED
            tunnel.next_attempt_at = time.monotonic() + 30
            self.manager.start_automatic("remote-a")

        self.assertEqual(tunnel.status.state, STATE_FAILED)
        ensure_thread.assert_not_called()

    def test_manual_start_still_enables_single_console_fallback(self):
        self.manager.register(["remote-a"])
        tunnel = self.manager._tunnels["remote-a"]
        tunnel.status.state = STATE_FAILED
        tunnel.status.retries = 3
        tunnel.next_attempt_at = time.monotonic() + 30
        with patch.object(self.manager, "_ensure_thread"):
            self.manager.start("remote-a")

        self.assertTrue(tunnel.user_requested)
        self.assertFalse(tunnel.console_tried)
        self.assertEqual(tunnel.status.retries, 0)
        self.assertEqual(tunnel.next_attempt_at, 0.0)
        self.assertIn("BatchMode=yes", self.manager._build_cmd("remote-a", 17386))
        self.assertNotIn(
            "BatchMode=yes", self.manager._build_cmd("remote-a", 17386, interactive=True)
        )

    def test_automatic_auth_failure_stops_without_console_or_retry_storm(self):
        launched = _ImmediateAuthFailure()
        with patch("overlay.remote_tunnel.subprocess.Popen", return_value=launched) as popen:
            self.manager.register(["remote-a"])
            self.manager.start_automatic("remote-a")
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if self.manager.status()["remote-a"].state == STATE_AUTH_FAILED:
                    break
                time.sleep(0.01)
            time.sleep(0.05)

        status = self.manager.status()["remote-a"]
        self.assertEqual(status.state, STATE_AUTH_FAILED)
        self.assertEqual(popen.call_count, 1)
        cmd = popen.call_args.args[0]
        self.assertIn("BatchMode=yes", cmd)
        self.assertNotEqual(
            popen.call_args.kwargs.get("creationflags", 0),
            getattr(subprocess, "CREATE_NEW_CONSOLE", -1),
        )

    def _exercise_up_process_exit(self, auto_reconnect):
        manager = TunnelManager(
            get_port=lambda: 17386,
            poll_interval=0.01,
            get_auto_reconnect=lambda: auto_reconnect,
        )
        process = _ControlledProcess()
        try:
            with patch("overlay.remote_tunnel.subprocess.Popen", return_value=process) as popen:
                manager.register(["remote-a"])
                manager.start_automatic("remote-a")
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    if manager.status()["remote-a"].state == STATE_UP:
                        break
                    time.sleep(0.01)
                self.assertEqual(manager.status()["remote-a"].state, STATE_UP)

                process.alive = False
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    status = manager.status()["remote-a"]
                    if status.state != STATE_UP:
                        break
                    time.sleep(0.01)

                status = manager.status()["remote-a"]
                tunnel = manager._tunnels["remote-a"]
                self.assertEqual(popen.call_count, 1)
                return status.state, status.retries, tunnel.next_attempt_at
        finally:
            manager.stop_all()

    def test_up_process_exit_schedules_reconnect_when_enabled(self):
        before = time.monotonic()
        state, retries, next_attempt_at = self._exercise_up_process_exit(auto_reconnect=True)

        self.assertEqual(state, STATE_FAILED)
        self.assertEqual(retries, 1)
        self.assertGreater(next_attempt_at, before)

    def test_up_process_exit_stays_down_when_reconnect_disabled(self):
        state, retries, next_attempt_at = self._exercise_up_process_exit(auto_reconnect=False)

        self.assertEqual(state, "down")
        self.assertEqual(retries, 0)
        self.assertEqual(next_attempt_at, 0.0)


if __name__ == "__main__":
    unittest.main()
