"""Behavioral coverage for Settings-driven first remote provisioning."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from overlay import settings_window as sw


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Combo(dict):
    pass


class _Tree:
    def exists(self, _host):
        return True

    def set(self, *_args):
        return None


class _Window:
    def winfo_exists(self):
        return True

    def after(self, *_args):
        return "after-id"


class _Tunnels:
    def __init__(self):
        self.state = "down"
        self.starts = []
        self.stops = []

    def start(self, host):
        self.starts.append(host)
        self.state = "connecting"

    def stop(self, host):
        self.stops.append(host)
        self.state = "down"

    def status(self):
        return {"remote-a": _Status(self.state)}


class _Status:
    def __init__(self, state):
        self.state = state
        self.retries = 0
        self.last_error = ""

    def uptime_secs(self):
        return 0.0


class _ImmediateThread:
    def __init__(self, *, target, args, **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


def _settings_fixture():
    window = sw._SettingsWindow.__new__(sw._SettingsWindow)
    window.window = _Window()
    window._tunnels = _Tunnels()
    window._selected_tunnel_host = lambda: "remote-a"
    window._provision_tokens = {"remote-default  (scope=overlay)": "remote-default"}
    window._provision_token_var = _Var("remote-default  (scope=overlay)")
    window._pending_manual_provision = {}
    window._provision_status = {}
    window._tunnel_rows = ["remote-a"]
    window._tunnel_tree = _Tree()
    window._remote_port_var = _Var("0")
    window._remote_listener_var = _Var()
    window._audit_var = _Var()
    window._remote_after_id = None
    window._redraw_tunnel_tree = Mock()
    window._run_manual_provision = Mock()
    return window


class RemoteSettingsProvisionTests(unittest.TestCase):
    @patch("core.integrations.remote_provision.load_records", return_value={})
    def test_manual_start_then_up_runs_exactly_one_first_provision(self, _records):
        window = _settings_fixture()
        window._tunnel_action("start")
        self.assertEqual(window._pending_manual_provision, {"remote-a": "remote-default"})
        self.assertEqual(window._tunnels.starts, ["remote-a"])

        window._tunnels.state = "up"
        with patch.object(sw.threading, "Thread", _ImmediateThread), \
             patch.object(sw, "_is_port_listening", return_value=False), \
             patch.object(sw, "_audit_tail", return_value=""):
            window._refresh_remote_status()
            window._refresh_remote_status()

        window._run_manual_provision.assert_called_once_with("remote-a", "remote-default")
        self.assertNotIn("remote-a", window._pending_manual_provision)

    @patch("core.integrations.remote_provision.load_records", return_value={})
    def test_cancelled_or_failed_manual_attempt_cannot_run_on_later_automatic_up(self, _records):
        for terminal_state in ("down", "auth_failed", "failed"):
            with self.subTest(terminal_state=terminal_state):
                window = _settings_fixture()
                window._tunnel_action("start")
                if terminal_state == "down":
                    window._tunnel_action("stop")
                else:
                    window._tunnels.state = terminal_state
                    with patch.object(sw, "_is_port_listening", return_value=False), \
                         patch.object(sw, "_audit_tail", return_value=""):
                        window._refresh_remote_status()

                self.assertNotIn("remote-a", window._pending_manual_provision)
                window._tunnels.state = "up"
                with patch.object(sw.threading, "Thread", _ImmediateThread), \
                     patch.object(sw, "_is_port_listening", return_value=False), \
                     patch.object(sw, "_audit_tail", return_value=""):
                    window._refresh_remote_status()
                window._run_manual_provision.assert_not_called()

    def test_token_loader_keeps_values_out_of_tk_and_auto_selects_only_one(self):
        with TemporaryDirectory() as tmp:
            token_path = Path(tmp) / ".engram" / "mcp-tokens.yaml"
            token_path.parent.mkdir()
            token_path.write_text(
                "tokens:\n  - name: remote-default\n    token: secret-value\n    scope: overlay\n",
                encoding="utf-8",
            )
            window = sw._SettingsWindow.__new__(sw._SettingsWindow)
            window._provision_token_var = _Var()
            window._provision_token_combo = _Combo()
            with patch.object(sw.Path, "home", return_value=Path(tmp)):
                window._load_provision_tokens()

            self.assertEqual(window._provision_token_var.get(), "remote-default  (scope=overlay)")
            self.assertEqual(list(window._provision_tokens.values()), ["remote-default"])
            self.assertNotIn("secret-value", repr(window._provision_tokens))

    def test_no_key_auth_holds_first_provision_without_spawning_setup(self):
        window = sw._SettingsWindow.__new__(sw._SettingsWindow)
        window._provision_status = {}
        with patch.object(sw, "_manual_ssh_key_available", return_value=False), \
             patch.object(sw.subprocess, "Popen") as popen:
            window._run_manual_provision("remote-a", "remote-default")

        popen.assert_not_called()
        self.assertEqual(
            window._provision_status["remote-a"],
            "첫 배치 보류 — SSH 키 등록 필요",
        )

    def test_key_authenticated_setup_receives_log_path_and_nonce_but_no_batchmode(self):
        window = sw._SettingsWindow.__new__(sw._SettingsWindow)
        window._provision_status = {}
        process = Mock()
        process.wait.return_value = 0
        with TemporaryDirectory() as tmp, \
             patch.object(sw, "_manual_ssh_key_available", return_value=True), \
             patch.object(sw.Path, "home", return_value=Path(tmp)), \
             patch.object(sw.secrets, "token_urlsafe", return_value="proof_nonce_123456"), \
             patch.object(sw.subprocess, "Popen", return_value=process) as popen:
            window._run_manual_provision("remote-a", "remote-default")

        args = popen.call_args.args[0]
        self.assertIn("-ResultLog", args)
        self.assertIn("-ProofNonce", args)
        self.assertIn("proof_nonce_123456", args)
        self.assertNotIn("-BatchMode", args)
        self.assertEqual(window._provision_status["remote-a"], "첫 배치 완료")


if __name__ == "__main__":
    unittest.main()
