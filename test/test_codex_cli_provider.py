import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from overlay.chat_window import _resolve_provider_launch
from overlay.config import get_cli_provider, normalize_cli_provider


class CodexCliProviderTests(unittest.TestCase):
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
