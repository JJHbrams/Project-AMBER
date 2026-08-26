import tempfile
import unittest
from pathlib import Path

import yaml

from core.install.user_config import update_installer_paths, update_overlay_installer_config


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
        self.assertIn('-Role "install-user-config"', configure)
        self.assertNotIn('Write-Ok "Exists (보존): $UserConfig"', configure)

    def test_daily_note_setting_uses_clear_obsidian_labels_without_changing_key(self):
        settings = (ROOT / "overlay" / "settings_window.py").read_text(encoding="utf-8")

        self.assertIn('text="Obsidian Daily Note 디렉터리"', settings)
        self.assertIn('title="Obsidian Daily Note 디렉터리 선택"', settings)
        self.assertIn("Engram Wiki daily note와 함께 Obsidian Daily Note에도 기록합니다.", settings)
        self.assertIn('["memory", "auto_checkpoint", "external_daily_dir"]', settings)
        self.assertNotIn('text="자동 체크포인트"', settings)

    def test_installer_version_is_1_5_6(self):
        iss = (ROOT / "installer" / "engram-overlay.iss").read_text(encoding="utf-8-sig")

        self.assertIn('#define AppVersion "1.5.6"', iss)
        self.assertIn(
            'OutputBaseFilename=EngramOverlay_{#AppVersion}{#BuildOutputSuffix}_x64-setup',
            iss,
        )

    def test_configure_waits_for_every_frozen_installer_role_exit_code(self):
        configure = (ROOT / "installer" / "configure.ps1").read_text(encoding="utf-8-sig")
        helper_start = configure.index("function Invoke-EngramFrozenRole")
        helper_end = configure.index("function Remove-EngramManagedClaudeHooks", helper_start)
        helper = configure[helper_start:helper_end]

        self.assertIn('"--role", $Role', helper)
        self.assertIn("Start-Process", helper)
        self.assertIn("-Wait", helper)
        self.assertIn("-PassThru", helper)
        self.assertIn("-WindowStyle Hidden", helper)
        self.assertIn("return $process.ExitCode", helper)

        user_start = configure.index("$installUserConfigArgs = @(")
        user_end = configure.index('Write-Ok "Updated: $UserConfig', user_start)
        user_block = configure[user_start:user_end]
        self.assertIn("('\"{0}\"' -f $UserConfig)", user_block)
        self.assertIn("('\"{0}\"' -f $DbDir)", user_block)
        self.assertIn("('\"{0}\"' -f $WorkDir)", user_block)
        self.assertIn('-Role "install-user-config"', user_block)
        self.assertIn("$installUserConfigExitCode -ne 0", user_block)

        bootstrap_start = configure.index("$installBootstrapArgs = @(")
        bootstrap_end = configure.index('Write-Ok "DB schema, wiki starter files, directives"', bootstrap_start)
        bootstrap_block = configure[bootstrap_start:bootstrap_end]
        self.assertIn("('\"{0}\"' -f $DbDir)", bootstrap_block)
        self.assertIn("('\"{0}\"' -f $InstallerTemplates)", bootstrap_block)
        self.assertIn('-Role "install-bootstrap"', bootstrap_block)
        self.assertIn("$installBootstrapExitCode -ne 0", bootstrap_block)
        self.assertNotIn("$LASTEXITCODE", user_block + bootstrap_block)

    def test_installer_stops_only_the_target_artifact_before_copying_files(self):
        iss = (ROOT / "installer" / "engram-overlay.iss").read_text(encoding="utf-8-sig")

        self.assertIn('Source: "stop-engram-processes.ps1"; Flags: dontcopy', iss)
        self.assertIn("function PrepareToInstall", iss)
        self.assertIn("ExtractTemporaryFile('stop-engram-processes.ps1')", iss)
        self.assertIn("-ArtifactDir", iss)
        self.assertIn("ewWaitUntilTerminated", iss)

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

    def test_rejects_non_mapping_overlay_sections_without_overwriting_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "overlay.user.yaml"
            original = "cli: invalid\nmcp:\n  remote_port: 20000\n"
            config_path.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "'cli' value must be a mapping"):
                update_overlay_installer_config(config_path, provider="codex", mcp_port=17385)
            self.assertEqual(config_path.read_text(encoding="utf-8"), original)

    def test_overlay_update_migrates_legacy_custom_character_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            static_image = root / "custom.png"
            static_image.touch()
            frames = root / "frames"
            frames.mkdir()

            cases = ((static_image, "static"), (frames, "sequence"))
            for index, (source, expected_mode) in enumerate(cases):
                config_path = root / f"overlay-{index}.user.yaml"
                config_path.write_text(
                    yaml.safe_dump({"overlay": {"character": {"name": str(source)}}}),
                    encoding="utf-8",
                )

                update_overlay_installer_config(config_path, provider="codex", mcp_port=17385)

                updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                self.assertEqual(updated["overlay"]["character"]["name"], str(source))
                self.assertEqual(updated["overlay"]["character"]["source_mode"], expected_mode)

    def test_overlay_update_keeps_explicit_mode_and_leaves_untouched_default_legacy_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            explicit_path = root / "explicit.yaml"
            explicit_path.write_text(
                "overlay:\n  character:\n    name: C:/custom/image.png\n    source_mode: sequence\n",
                encoding="utf-8",
            )
            default_path = root / "default.yaml"
            default_path.write_text(
                "overlay:\n  character:\n    name: engram\n",
                encoding="utf-8",
            )

            update_overlay_installer_config(explicit_path, provider="codex", mcp_port=17385)
            update_overlay_installer_config(default_path, provider="codex", mcp_port=17385)

            explicit = yaml.safe_load(explicit_path.read_text(encoding="utf-8"))
            default = yaml.safe_load(default_path.read_text(encoding="utf-8"))
            self.assertEqual(explicit["overlay"]["character"]["source_mode"], "sequence")
            self.assertNotIn("source_mode", default["overlay"]["character"])


if __name__ == "__main__":
    unittest.main()
