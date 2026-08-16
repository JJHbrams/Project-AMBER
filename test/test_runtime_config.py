import os
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config.runtime_config import (
    _normalize_legacy_policy_config,
    get_db_root_dir,
    load_runtime_cfg,
    normalize_policy_guidance_level,
)


class RuntimeConfigTests(unittest.TestCase):
    @patch("core.config.runtime_config._read_yaml")
    def test_smoke_db_override_never_reads_user_config(self, mock_read_yaml):
        with patch.dict(os.environ, {"ENGRAM_SMOKE_DB_DIR": "C:/smoke-db"}, clear=False):
            self.assertEqual(get_db_root_dir(), "C:/smoke-db")
        mock_read_yaml.assert_not_called()

    @patch("core.config.runtime_config.resolve_runtime_path", return_value="config/config.yaml")
    @patch("core.config.runtime_config._read_yaml")
    def test_get_db_root_dir_prefers_user_config_over_env(self, mock_read_yaml, mock_resolve_runtime_path):
        mock_read_yaml.side_effect = [
            {"db": {"root_dir": "E:/engram-root"}},
            {},
            {"db": {"root_dir": "D:/intel_engram"}},
        ]

        with patch.dict(os.environ, {"ENGRAM_DB_DIR": "C:/legacy-root"}, clear=False):
            self.assertEqual(get_db_root_dir(), "E:/engram-root")

        self.assertEqual(mock_read_yaml.call_count, 1)
        mock_resolve_runtime_path.assert_not_called()

    @patch("core.config.runtime_config._read_yaml", return_value={})
    def test_default_directive_policy_limits_exist(self, _mock_read_yaml):
        cfg = load_runtime_cfg(force_reload=True)

        self.assertEqual(cfg["directives"]["policy"]["audit_default_limit"], 20)
        self.assertEqual(cfg["directives"]["policy"]["audit_max_limit"], 100)
        self.assertEqual(cfg["directives"]["policy"]["guidance_level"], "warn")

    def test_legacy_policy_off_is_projected_before_layer_merge(self):
        normalized = _normalize_legacy_policy_config(
            {"directives": {"policy": {"claude_pretool_enforcement": False}}}
        )

        self.assertEqual(normalized["directives"]["policy"]["guidance_level"], "off")

    def test_canonical_policy_level_wins_over_legacy_value(self):
        normalized = _normalize_legacy_policy_config(
            {
                "directives": {
                    "policy": {
                        "guidance_level": "enforce_agents",
                        "guidance_enabled": True,
                        "claude_pretool_enforcement": False,
                    }
                }
            }
        )

        self.assertEqual(normalized["directives"]["policy"]["guidance_level"], "enforce_agents")

    def test_policy_level_aliases_normalize(self):
        self.assertEqual(normalize_policy_guidance_level("off"), "off")
        self.assertEqual(normalize_policy_guidance_level("advisory"), "warn")
        self.assertEqual(normalize_policy_guidance_level("enforce-agent"), "enforce_agents")

    @patch("core.config.runtime_config._ensure_user_config_file")
    @patch("core.config.runtime_config.resolve_runtime_path", return_value=Path("config/config.yaml"))
    @patch("core.config.runtime_config._read_yaml")
    def test_legacy_user_off_overrides_new_project_default(
        self, mock_read_yaml, _mock_resolve_runtime_path, _mock_ensure_user_config
    ):
        mock_read_yaml.side_effect = [
            {"directives": {"policy": {"guidance_level": "warn"}}},
            {"directives": {"policy": {"claude_pretool_enforcement": False}}},
            {},
        ]

        cfg = load_runtime_cfg(force_reload=True)

        self.assertEqual(cfg["directives"]["policy"]["guidance_level"], "off")


if __name__ == "__main__":
    unittest.main()
