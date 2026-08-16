import tempfile
import unittest
from pathlib import Path

import yaml

from core.install.user_config import update_installer_paths


ROOT = Path(__file__).resolve().parents[1]


class InstallUserConfigTests(unittest.TestCase):
    def test_updates_only_installer_owned_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "user.config.yaml"
            config_path.write_text(
                """db:
  root_dir: D:/old-engram
memory:
  auto_checkpoint:
    external_daily_dir: C:/Notes/Daily
workdir: C:/old-workspace
session:
  auto_inject: true
""",
                encoding="utf-8",
            )

            update_installer_paths(
                config_path,
                db_dir=r"E:\new-engram",
                workdir=r"C:\new-workspace",
            )

            updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(updated["db"]["root_dir"], "E:/new-engram")
            self.assertEqual(updated["workdir"], "C:/new-workspace")
            self.assertEqual(
                updated["memory"]["auto_checkpoint"]["external_daily_dir"],
                "C:/Notes/Daily",
            )
            self.assertTrue(updated["session"]["auto_inject"])

    def test_frozen_installer_loads_existing_paths_and_merges_selections(self):
        configure = (ROOT / "installer" / "configure.ps1").read_text(encoding="utf-8-sig")
        iss = (ROOT / "installer" / "engram-overlay.iss").read_text(encoding="utf-8-sig")

        self.assertIn("LoadExistingUserPaths(InitialDbDir, InitialWorkDir)", iss)
        self.assertIn("DirPage.Values[0] := InitialDbDir", iss)
        self.assertIn("DirPage.Values[1] := InitialWorkDir", iss)
        self.assertIn("--role install-user-config", configure)
        self.assertNotIn('Write-Ok "Exists (보존): $UserConfig"', configure)

    def test_rejects_invalid_db_config_without_overwriting_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "user.config.yaml"
            original = "db: invalid\nworkdir: C:/old-workspace\n"
            config_path.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "'db' value must be a mapping"):
                update_installer_paths(
                    config_path,
                    db_dir=r"E:\new-engram",
                    workdir=r"C:\new-workspace",
                )

            self.assertEqual(config_path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
