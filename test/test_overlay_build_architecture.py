import json
import tempfile
import unittest
from pathlib import Path

from core.install.model_manifest import create_manifest, validate_manifest
from core.install.overlay_manifest import (
    make_manifest,
    validate_build,
)


ROOT = Path(__file__).resolve().parents[1]


class OverlayBuildArchitectureTests(unittest.TestCase):
    def test_public_builders_delegate_to_shared_engine(self):
        dev = (ROOT / "dev-rebuild.ps1").read_text(encoding="utf-8-sig")
        module = (ROOT / "installer" / "modules" / "09_overlay.ps1").read_text(
            encoding="utf-8-sig"
        )
        release = (ROOT / "installer" / "build-installer.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("installer\\build-overlay.ps1", dev)
        self.assertIn("build-overlay.ps1", module)
        self.assertIn("build-overlay.ps1", release)
        self.assertNotIn("PyInstaller --noconfirm", release)

    def test_build_manifest_reuse_and_input_invalidation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "overlay").mkdir()
            (root / "overlay" / "main.py").write_text("value = 1", encoding="utf-8")
            model_dir = root / "resource" / "embedding-model"
            model_dir.mkdir(parents=True)
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "model.safetensors").write_bytes(b"weights")
            model_manifest = model_dir / "manifest.json"
            model_manifest.write_text(
                json.dumps(create_manifest(
                    model_dir,
                    model_id="test/model",
                    sentence_transformers_version="1.0",
                    resolved_revision="local-test",
                )),
                encoding="utf-8",
            )
            artifact = root / "artifact"
            artifact.mkdir()
            (artifact / "engram-overlay.exe").write_bytes(b"exe")
            (artifact / "engram-dashboard.exe").write_bytes(b"exe")
            build_manifest = make_manifest(root, model_manifest, "rebuild")
            (artifact / "build-manifest.json").write_text(
                json.dumps(build_manifest),
                encoding="utf-8",
            )

            valid, reason = validate_build(root, artifact, model_manifest)
            self.assertTrue(valid, reason)

            (root / "overlay" / "main.py").write_text("value = 2", encoding="utf-8")
            valid, reason = validate_build(root, artifact, model_manifest)
            self.assertFalse(valid)
            self.assertIn("inputs", reason)

    def test_model_manifest_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary)
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            weights = model_dir / "model.safetensors"
            weights.write_bytes(b"weights")
            manifest = create_manifest(
                model_dir,
                model_id="test/model",
                sentence_transformers_version="1.0",
                resolved_revision="local-test",
            )
            (model_dir / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            valid, reason = validate_manifest(model_dir, "test/model")
            self.assertTrue(valid, reason)
            weights.write_bytes(b"tampered")
            valid, reason = validate_manifest(model_dir, "test/model")
            self.assertFalse(valid)
            self.assertIn("hash mismatch", reason)

    def test_mcp_check_requires_fastmcp_import(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        dependencies = (
            ROOT / "installer" / "modules" / "04_dependencies.ps1"
        ).read_text(encoding="utf-8-sig")
        manifest_helper = (
            ROOT / "core" / "install" / "overlay_manifest.py"
        ).read_text(encoding="utf-8")

        self.assertIn("mcp>=1.0.0,<2.0.0", requirements)
        self.assertIn("from mcp.server.fastmcp import FastMCP", dependencies)
        self.assertIn("from mcp.server.fastmcp import FastMCP", manifest_helper)

    def test_frozen_dashboard_uses_external_python_runtime(self):
        overlay_main = (ROOT / "overlay" / "main.py").read_text(encoding="utf-8")
        entry = (ROOT / "engram_overlay_entry.py").read_text(encoding="utf-8")
        spec = (ROOT / "engram-overlay.spec").read_text(encoding="utf-8")

        self.assertNotIn('cmd = [sys.executable, "--role", "dashboard"]', overlay_main)
        self.assertIn('Path(sys.executable).parent / "engram-dashboard.exe"', overlay_main)
        self.assertIn('stem.lower() == "engram-dashboard"', entry)
        self.assertIn("dashboard title missing", entry)
        self.assertIn("streamlit_bootstrap.load_config_options(options)", entry)
        self.assertIn("dashboard_exe = EXE(", spec)

    def test_dashboard_setting_is_exposed_in_global_settings(self):
        defaults = (ROOT / "config" / "overlay.yaml").read_text(encoding="utf-8")
        settings = (ROOT / "overlay" / "settings_window.py").read_text(encoding="utf-8")

        self.assertIn("dashboard:\n", defaults)
        self.assertIn("대시보드 자동 실행", settings)
        self.assertIn("대시보드 보기", settings)
        self.assertIn('["dashboard", "enabled"]', settings)


if __name__ == "__main__":
    unittest.main()
