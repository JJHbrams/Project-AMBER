import os
import unittest
from unittest.mock import patch

from core.config.runtime_config import get_db_root_dir


class RuntimeConfigTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

