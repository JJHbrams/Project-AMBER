"""Headless service isolation and direct-ingress configuration checks."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import runtime_config
from core.config.remote_tokens import ensure_tokens_file, selected_tokens_path


class HeadlessServiceConfigTests(unittest.TestCase):
    def test_smoke_override_stays_above_service_data_dir(self):
        with patch.dict(os.environ, {
            "ENGRAM_SMOKE_DB_DIR": "C:/smoke-db",
            "ENGRAM_SERVICE_DATA_DIR": "C:/service-db",
        }, clear=False):
            self.assertEqual(runtime_config.get_db_root_dir(), "C:/smoke-db")

    def test_service_data_dir_precedes_installed_user_override(self):
        with patch.dict(os.environ, {
            "ENGRAM_SMOKE_DB_DIR": "",
            "ENGRAM_SERVICE_DATA_DIR": "C:/service-db",
        }, clear=False), \
             patch.object(runtime_config, "_read_yaml") as read:
            self.assertEqual(runtime_config.get_db_root_dir(), "C:/service-db")
            read.assert_not_called()

    def test_explicit_missing_token_secret_is_not_generated(self):
        missing = Path(__file__).parent / "missing-mounted-secret.yaml"
        self.assertFalse(missing.exists())
        with self.assertRaises(FileNotFoundError):
            ensure_tokens_file(17386, missing)
        self.assertFalse(missing.exists())

    def test_explicit_token_path_is_selected(self):
        path = Path(__file__).parent / "mounted-tokens.yaml"
        self.assertEqual(selected_tokens_path(path), path)


if __name__ == "__main__":
    unittest.main()
