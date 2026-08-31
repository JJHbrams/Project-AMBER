import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from overlay.chat_window import _resolve_provider_launch
from overlay.main import _make_tray_icon
from overlay.config import get_cli_model, get_cli_provider, normalize_cli_provider, set_cli_model, set_cli_provider


class CodexCliProviderTests(unittest.TestCase):
    def test_legacy_gemini_provider_loads_and_saves_as_antigravity(self):
        self.assertEqual(normalize_cli_provider("gemini"), "antigravity")
        self.assertEqual(get_cli_provider({"cli": {"provider": "gemini"}}), "antigravity")
        state: dict = {}
        with patch("overlay.config.update_overlay_state", side_effect=lambda updater: updater(state)):
            self.assertEqual(set_cli_provider("gemini"), "antigravity")
        self.assertEqual(state["cli"]["provider"], "antigravity")

    def test_legacy_gemini_model_is_available_then_saved_as_antigravity_model(self):
        self.assertEqual(get_cli_model("antigravity", {"cli": {"gemini_model": "legacy-pro"}}), "legacy-pro")
        state: dict = {"cli": {"gemini_model": "legacy-pro"}}
        with patch("overlay.config.update_overlay_state", side_effect=lambda updater: updater(state)), patch(
            "overlay.config._set_user_cli_value"
        ) as save:
            self.assertEqual(set_cli_model("antigravity", "new-pro", sync_user=True), "new-pro")
        self.assertEqual(state["cli"], {"antigravity_model": "new-pro"})
        save.assert_called_once_with("antigravity_model", "new-pro")

    def test_saving_antigravity_model_retires_only_legacy_model_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_path = Path(tmp) / "overlay.user.yaml"
            user_path.write_text("cli:\n  gemini_model: legacy-pro\n  custom: keep\n", encoding="utf-8")
            with patch("overlay.config._USER_CONFIG_PATH", user_path), patch("overlay.config.update_overlay_state"):
                set_cli_model("antigravity", "new-pro", sync_user=True)
            saved = user_path.read_text(encoding="utf-8")
        self.assertIn("antigravity_model: new-pro", saved)
        self.assertNotIn("gemini_model", saved)
        self.assertIn("custom: keep", saved)

    def test_antigravity_launch_uses_agy_when_shim_is_missing(self):
        missing_shim = Path(tempfile.gettempdir()) / "missing-engram-antigravity.cmd"
        with patch("overlay.chat_window.ENGRAM_ANTIGRAVITY_CMD", missing_shim):
            provider, args, label, env, warnings = _resolve_provider_launch(
                {"cli": {"antigravity_command": "agy", "antigravity_model": "gemini-3.1-pro-preview"}},
                "antigravity",
            )
        self.assertEqual(provider, "antigravity")
        self.assertEqual(args, ["cmd", "/k", "agy", "--model", "gemini-3.1-pro-preview"])
        self.assertEqual(label, "agy")
        self.assertEqual(env, {})
        self.assertEqual(warnings, [])

    def test_codex_is_a_supported_provider(self):
        self.assertEqual(normalize_cli_provider("codex"), "codex")
        self.assertEqual(get_cli_provider({"cli": {"provider": "codex"}}), "codex")

    def test_launch_falls_back_to_configured_codex_command(self):
        missing_shim = Path(tempfile.gettempdir()) / "missing-engram-codex.cmd"
        with patch("overlay.chat_window.ENGRAM_CODEX_CMD", missing_shim):
            provider, args, label, env, warnings = _resolve_provider_launch(
                {"cli": {"codex_command": "codex-preview"}}, "codex"
            )
        self.assertEqual(provider, "codex")
        self.assertEqual(args, ["cmd", "/k", "codex-preview"])
        self.assertEqual(label, "codex-preview")
        self.assertEqual(env, {})
        self.assertEqual(warnings, [])

    def test_launch_prefers_engram_codex_shim(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "engram-codex.cmd"
            shim.touch()
            with patch("overlay.chat_window.ENGRAM_CODEX_CMD", shim):
                provider, args, label, _env, _warnings = _resolve_provider_launch({}, "codex")
        self.assertEqual(provider, "codex")
        self.assertEqual(args, ["cmd", "/k", str(shim)])
        self.assertEqual(label, "engram-codex.cmd")

    def test_direct_claude_has_no_legacy_ollama_model_or_base_url(self):
        missing_shim = Path(tempfile.gettempdir()) / "missing-engram-claude.cmd"
        with patch("overlay.chat_window.ENGRAM_CLAUDE_CMD", missing_shim):
            provider, args, label, env, _warnings = _resolve_provider_launch(
                {"cli": {"claude_model": "", "ollama_model": ""}}, "claude-code"
            )
        self.assertEqual(provider, "claude-code")
        self.assertEqual(args, ["cmd", "/k", "claude"])
        self.assertEqual(label, "claude")
        self.assertNotIn("ANTHROPIC_BASE_URL", env)

    def test_legacy_claude_ollama_value_still_routes_when_untouched(self):
        missing_shim = Path(tempfile.gettempdir()) / "missing-engram-claude.cmd"
        with patch("overlay.chat_window.ENGRAM_CLAUDE_CMD", missing_shim), patch(
            "overlay.chat_window._query_ollama_capabilities", return_value={"tools"}
        ):
            _provider, args, label, env, _warnings = _resolve_provider_launch(
                {"cli": {"ollama_model": "qwen-local"}}, "claude-code"
            )
        self.assertEqual(args, ["cmd", "/k", "claude", "--model", "qwen-local"])
        self.assertEqual(label, "claude --model qwen-local")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://localhost:11434")

    def test_claude_alias_launch_uses_anthropic_without_ollama_base_url(self):
        missing_shim = Path(tempfile.gettempdir()) / "missing-engram-claude.cmd"
        with patch("overlay.chat_window.ENGRAM_CLAUDE_CMD", missing_shim):
            provider, args, label, env, _warnings = _resolve_provider_launch(
                {"cli": {"claude_model": "opus", "ollama_model": "qwen-local"}}, "claude-code"
            )
        self.assertEqual(provider, "claude-code")
        self.assertEqual(args, ["cmd", "/k", "claude", "--model", "opus"])
        self.assertEqual(label, "claude --model opus")
        self.assertNotIn("ANTHROPIC_BASE_URL", env)

    def test_tray_model_item_binds_model_not_pystray_menu_item(self):
        class App:
            _ollama_model = ""
            selected = None
            def get_cli_provider(self): return "copilot"
            def get_cli_model(self, _provider): return "auto"
            def _set_provider_model(self, provider, model): self.selected = (provider, model)
            def toggle_chat(self): pass
            def show_bubble_history(self): pass
            def open_settings(self): pass
            def request_quit(self): pass
        with tempfile.TemporaryDirectory() as tmp:
            from PIL import Image
            icon_path = Path(tmp) / "icon.png"
            Image.new("RGBA", (2, 2)).save(icon_path)
            app = App()
            with patch("overlay.main._resolve_icon_path", return_value=icon_path), patch(
                "overlay.main.provider_models", return_value=["gpt-5.4"]
            ):
                tray = _make_tray_icon(app)
                provider_menu = list(tray.menu)[2].submenu
                copilot_model_item = list(list(provider_menu)[0].submenu)[0]
                copilot_model_item(None)
        self.assertEqual(app.selected, ("copilot", "gpt-5.4"))

    def test_tray_claude_alias_item_binds_alias_string(self):
        class App:
            _ollama_model = ""
            selected = None
            def get_cli_provider(self): return "claude-code"
            def get_cli_model(self, _provider): return "opus"
            def _set_provider_model(self, provider, model): self.selected = (provider, model)
            def toggle_chat(self): pass
            def show_bubble_history(self): pass
            def open_settings(self): pass
            def request_quit(self): pass
        with tempfile.TemporaryDirectory() as tmp:
            from PIL import Image
            icon_path = Path(tmp) / "icon.png"
            Image.new("RGBA", (2, 2)).save(icon_path)
            app = App()
            with patch("overlay.main._resolve_icon_path", return_value=icon_path), patch(
                "overlay.main.load_cfg", return_value={"cli": {"claude_model": "opus"}}
            ):
                tray = _make_tray_icon(app)
                provider_menu = list(tray.menu)[2].submenu
                claude_menu = list(provider_menu)[3].submenu
                opus_item = next(item for item in claude_menu if item.text == "claude: opus")
                opus_item(None)
        self.assertEqual(app.selected, ("claude-code", "opus"))

    def test_installer_connects_codex_detection_shim_and_dispatch(self):
        root = Path(__file__).resolve().parents[1]
        common = (root / "installer" / "common.ps1").read_text(encoding="utf-8")
        preflight = (root / "installer" / "modules" / "01_preflight.ps1").read_text(encoding="utf-8")
        shims = (root / "installer" / "modules" / "07_shims.ps1").read_text(encoding="utf-8")
        config = (root / "installer" / "modules" / "05_config.ps1").read_text(encoding="utf-8")

        self.assertIn('"codex" { return "codex" }', common)
        self.assertIn("$CodexShimPath", common)
        self.assertIn("$CodexCmdDetected = Get-Command codex", preflight)
        self.assertIn("[System.IO.File]::WriteAllLines($CodexShimPath", shims)
        self.assertIn('==`"codex`"', shims)
        self.assertIn("codex mcp add engram --url", config)


if __name__ == "__main__":
    unittest.main()
