import unittest
from unittest.mock import Mock, patch

from overlay.main import OverlayApp


class OverlayTunnelOrchestrationTests(unittest.TestCase):
    def _app(self, listener_ready=True):
        app = OverlayApp.__new__(OverlayApp)
        app._tunnels = Mock()
        app._is_mcp_listener_ready = Mock(return_value=listener_ready)
        return app

    def test_auto_on_starts_saved_hosts_only_after_remote_listener_is_ready(self):
        app = self._app(listener_ready=True)
        cfg = {
            "mcp": {
                "remote_enabled": True,
                "remote_port": 17386,
                "tunnel_auto_reconnect": True,
                "tunnels": [{"host": "remote-a"}, {"host": "remote-b"}],
            }
        }
        with patch("overlay.main.load_cfg", return_value=cfg):
            app._apply_tunnels(listener_ready=True)

        app._tunnels.register.assert_called_once_with(["remote-a", "remote-b"])
        self.assertEqual(
            app._tunnels.start_automatic.call_args_list,
            [unittest.mock.call("remote-a"), unittest.mock.call("remote-b")],
        )
        app._is_mcp_listener_ready.assert_called_once_with(17386, timeout=1.0)

    def test_auto_off_restores_list_without_listener_probe_or_start(self):
        app = self._app(listener_ready=True)
        cfg = {
            "mcp": {
                "remote_enabled": True,
                "tunnel_auto_reconnect": False,
                "tunnels": [{"host": "remote-a"}],
            }
        }
        with patch("overlay.main.load_cfg", return_value=cfg):
            app._apply_tunnels()

        app._tunnels.register.assert_called_once_with(["remote-a"])
        app._tunnels.start_automatic.assert_not_called()
        app._is_mcp_listener_ready.assert_not_called()

    def test_unready_remote_listener_restores_list_but_does_not_start(self):
        app = self._app(listener_ready=False)
        cfg = {
            "mcp": {
                "remote_enabled": True,
                "remote_port": 17386,
                "tunnel_auto_reconnect": True,
                "tunnels": [{"host": "remote-a"}],
            }
        }
        with patch("overlay.main.load_cfg", return_value=cfg):
            app._apply_tunnels(listener_ready=True)

        app._tunnels.register.assert_called_once_with(["remote-a"])
        app._tunnels.start_automatic.assert_not_called()

    def test_deferred_startup_passes_mcp_readiness_to_tunnel_apply(self):
        app = self._app()
        app._get_mcp_health_settings = Mock(
            return_value={"ready_timeout_secs": 20.0, "port": 17385}
        )
        app._wait_mcp_ready = Mock(return_value=False)
        app._start_kg_watcher = Mock(return_value=None)
        app._is_dashboard_enabled = Mock(return_value=False)
        app._apply_tunnels = Mock()
        with patch("overlay.main.sync_sessionstart_hook"), patch(
            "overlay.main.is_auto_inject_enabled", return_value=False
        ), patch("overlay.main.is_policy_guidance_enabled", return_value=False), patch(
            "overlay.main.sync_policy_guidance_disabled_marker", return_value={"ok": True}
        ), patch("overlay.main.sync_claude_pretool_hook", return_value={"ok": True}), patch(
            "overlay.main.sync_codex_pretool_hook", return_value={"ok": True}
        ):
            app._deferred_startup()

        app._apply_tunnels.assert_called_once_with(listener_ready=False)

    def test_config_reload_reapplies_tunnels(self):
        app = self._app()
        app.character = Mock()
        app.character.reload_config.return_value = True
        app.chat = Mock()
        app._bubble_session = None
        app._set_provider_model = Mock()
        app._bubble_manager = Mock()
        app._bubble_input = Mock()
        app._is_dashboard_enabled = Mock(return_value=False)
        app._terminate_managed_process = Mock()
        app._initiative = Mock()
        app._apply_tunnels = Mock()
        cfg = {"overlay": {}, "terminal": {}}
        with patch("overlay.main.load_cfg", return_value=cfg), patch(
            "overlay.main.get_cli_provider", return_value="claude-code"
        ), patch("overlay.main.get_chat_mode", return_value="tui"), patch(
            "overlay.main.get_flip_horizontal", return_value=False
        ), patch("overlay.main.get_bubble_cfg", return_value={}):
            app._reload_config()

        app._apply_tunnels.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
