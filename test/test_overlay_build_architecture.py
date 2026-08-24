import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from core.install.model_manifest import create_manifest, validate_manifest
from core.install.overlay_manifest import (
    input_hashes,
    make_manifest,
    validate_build,
)


ROOT = Path(__file__).resolve().parents[1]


class OverlayBuildArchitectureTests(unittest.TestCase):
    def test_spec_collects_tk_runtime_without_hook_probe(self):
        spec = (ROOT / "engram-overlay.spec").read_text(encoding="utf-8")
        self.assertIn("_collect_tk_python_runtime", spec)
        self.assertIn("Path(sys.prefix) / 'Lib' / 'tkinter'", spec)
        self.assertIn("Path(sys.prefix) / 'DLLs' / '_tkinter.pyd'", spec)
        self.assertIn("installer\\\\pyi_rth_engram_tk.py", spec)

        hook = (ROOT / "installer" / "pyi_rth_engram_tk.py").read_text(encoding="utf-8")
        self.assertIn('os.environ["TCL_LIBRARY"]', hook)
        self.assertIn('os.environ["TK_LIBRARY"]', hook)

    def test_dev_runtime_is_source_only_and_installer_owns_frozen_engine(self):
        dev = (ROOT / "dev-rebuild.ps1").read_text(encoding="utf-8-sig")
        module = (ROOT / "installer" / "modules" / "09_overlay.ps1").read_text(
            encoding="utf-8-sig"
        )
        release = (ROOT / "installer" / "build-installer.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertNotIn("installer\\build-overlay.ps1", dev)
        self.assertNotIn('"-m", "PyInstaller"', dev)
        self.assertNotIn("dist\\engram-overlay", dev)
        self.assertIn('Join-Path $Root "engram_overlay_entry.py"', dev)
        self.assertIn("--role runtime-contract", dev)
        self.assertIn("Start-Process -FilePath $python", dev)
        self.assertIn('[int]$stm.pid -ne $overlay.Id', dev)
        self.assertIn('$mcp.runtime -ne "source"', dev)
        self.assertIn('$PSBoundParameters.ContainsKey("FreshBuild")', dev)
        self.assertIn("build-overlay.ps1", module)
        self.assertIn("build-overlay.ps1", release)
        self.assertIn('Deploy = (Join-Path $ProjectRoot "dist\\\\engram-overlay")', module)
        self.assertIn("-Deploy $DistDir -ValidateOnly", release)
        self.assertIn("-Deploy $DistDir -NoStart", release)
        self.assertNotIn("PyInstaller --noconfirm", release)
        self.assertIn('$overlayMode = if ($FreshBuild) { "rebuild" } else { "auto" }', release)

        build = (ROOT / "installer" / "build-overlay.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("Invoke-SourceRuntimeContract", build)
        self.assertIn('"runtime-contract"', build)
        self.assertLess(
            build.index("Invoke-SourceRuntimeContract $python"),
            build.index("Validating offline embedding model"),
        )

    def test_release_builder_caches_setup_and_has_explicit_release_profile(self):
        release = (ROOT / "installer" / "build-installer.ps1").read_text(
            encoding="utf-8-sig"
        )
        iss = (ROOT / "installer" / "engram-overlay.iss").read_text(
            encoding="utf-8-sig"
        )
        cache = (ROOT / "installer" / "build-cache.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("Test-EngramInstallerCache", release)
        self.assertIn("Write-EngramInstallerCache", release)
        self.assertLess(
            release.index("Test-EngramInstallerCache"),
            release.index('Invoke-FrozenRole "embedding-check"'),
        )
        self.assertIn("Release smoke already passed during fresh frozen build", release)
        self.assertIn("ISCC short path", release)
        self.assertIn('if ($Release) { "release-lzma2-solid" }', release)
        self.assertIn('#define BuildCompression "zip"', iss)
        self.assertIn('#define BuildOutputSuffix "-dev"', iss)
        self.assertIn('if ($Release) { "" } else { "-dev" }', release)
        self.assertIn("build-manifest.json", cache)
        self.assertIn("output_sha256", cache)

    def test_auto_reuse_allows_explicit_default_dist_target(self):
        build = (ROOT / "installer" / "build-overlay.ps1").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn("$deploysToDefault", build)
        self.assertIn('$Mode -eq "auto" -and $reuseValid -and $deploysToDefault', build)

    def test_build_manifest_reuse_and_input_invalidation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "build" / "overlay-worktree"
            (root / "VERSION").parent.mkdir(parents=True, exist_ok=True)
            (root / "VERSION").write_text("1.5.5\n", encoding="utf-8")
            (root / "overlay").mkdir(parents=True)
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
            self.assertRegex(build_manifest["version"]["version"], r"^1\.5\.5\.\d+$")

            build_manifest["version"]["version"] = "0.0.0.0"
            (artifact / "build-manifest.json").write_text(
                json.dumps(build_manifest), encoding="utf-8"
            )
            valid, reason = validate_build(root, artifact, model_manifest)
            self.assertFalse(valid)
            self.assertIn("version", reason)

            build_manifest = make_manifest(root, model_manifest, "rebuild")
            build_manifest["inputs"] = {}
            (artifact / "build-manifest.json").write_text(
                json.dumps(build_manifest), encoding="utf-8"
            )
            valid, reason = validate_build(root, artifact, model_manifest)
            self.assertFalse(valid)
            self.assertIn("no inputs", reason)

            build_manifest = make_manifest(root, model_manifest, "rebuild")
            (artifact / "build-manifest.json").write_text(
                json.dumps(build_manifest), encoding="utf-8"
            )
            (root / "overlay" / "main.py").write_text("value = 2", encoding="utf-8")
            valid, reason = validate_build(root, artifact, model_manifest)
            self.assertFalse(valid)
            self.assertIn("inputs", reason)

    def test_build_manifest_rejects_empty_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model_dir = root / "resource" / "embedding-model"
            model_dir.mkdir(parents=True)
            (model_dir / "manifest.json").write_text("{}", encoding="utf-8")

            self.assertIn("resource/embedding-model/manifest.json", input_hashes(root))
            with patch("core.install.overlay_manifest.input_hashes", return_value={}):
                with self.assertRaisesRegex(ValueError, "without inputs"):
                    make_manifest(root, model_dir / "manifest.json", "rebuild")

    def test_user_config_does_not_invalidate_frozen_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "VERSION").write_text("1.5.5\n", encoding="utf-8")
            (root / "overlay").mkdir()
            (root / "overlay" / "main.py").write_text("value = 1", encoding="utf-8")
            config = root / "config"
            config.mkdir()
            (config / "overlay.yaml").write_text("enabled: true", encoding="utf-8")
            (config / "overlay.user.yaml").write_text("x: 1", encoding="utf-8")
            model_dir = root / "resource" / "embedding-model"
            model_dir.mkdir(parents=True)
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            (model_dir / "model.safetensors").write_bytes(b"weights")
            model_manifest = model_dir / "manifest.json"
            model_manifest.write_text(
                json.dumps(create_manifest(
                    model_dir,
                    model_id="test/model",
                    resolved_revision="local-test",
                )),
                encoding="utf-8",
            )
            artifact = root / "artifact"
            artifact.mkdir()
            (artifact / "engram-overlay.exe").write_bytes(b"exe")
            (artifact / "engram-dashboard.exe").write_bytes(b"exe")
            (artifact / "build-manifest.json").write_text(
                json.dumps(make_manifest(root, model_manifest, "rebuild")),
                encoding="utf-8",
            )

            (config / "overlay.user.yaml").write_text("x: 2", encoding="utf-8")
            valid, reason = validate_build(root, artifact, model_manifest)
            self.assertTrue(valid, reason)

            (config / "overlay.yaml").write_text("enabled: false", encoding="utf-8")
            valid, reason = validate_build(root, artifact, model_manifest)
            self.assertFalse(valid)
            self.assertIn("inputs", reason)

    def test_installer_uses_userprofile_environment_expansion(self):
        installer = (ROOT / "installer" / "engram-overlay.iss").read_text(encoding="utf-8-sig")

        self.assertIn("{%USERPROFILE}\\.engram\\user.config.yaml", installer)
        self.assertNotIn("{userprofile}", installer)

    def test_model_manifest_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary)
            (model_dir / "config.json").write_text("{}", encoding="utf-8")
            weights = model_dir / "model.safetensors"
            weights.write_bytes(b"weights")
            manifest = create_manifest(
                model_dir,
                model_id="test/model",
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

    def test_build_uses_canonical_manifest_without_refreshing_it(self):
        build = (ROOT / "installer" / "build-overlay.ps1").read_text(encoding="utf-8-sig")

        self.assertIn('"--ensure"', build)
        self.assertIn('"--allow-download"', build)
        self.assertNotIn("--refresh-manifest", build)

    def test_frozen_and_installer_versions_come_from_canonical_snapshot(self):
        spec = (ROOT / "engram-overlay.spec").read_text(encoding="utf-8")
        build = (ROOT / "installer" / "build-overlay.ps1").read_text(
            encoding="utf-8-sig"
        )
        release = (ROOT / "installer" / "build-installer.ps1").read_text(
            encoding="utf-8-sig"
        )
        iss = (ROOT / "installer" / "engram-overlay.iss").read_text(
            encoding="utf-8-sig"
        )

        self.assertIn('"--write-snapshot", $VersionSnapshot', build)
        self.assertEqual(spec.count("version=_windows_version"), 2)
        self.assertIn("StringStruct('FileVersion', _version_text)", spec)
        self.assertIn("StringStruct('ProductVersion', _version_text)", spec)
        self.assertIn("$frozenManifest.version.version", release)
        self.assertIn('$versionDefine = "/DAppVersion=', release)
        self.assertIn("#ifndef AppVersion", iss)
        self.assertIn("OutputBaseFilename=EngramOverlay_{#AppVersion}", iss)

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

    def test_source_restart_uses_canonical_entrypoint_and_mcp_provenance(self):
        overlay_main = (ROOT / "overlay" / "main.py").read_text(encoding="utf-8")
        entry = (ROOT / "engram_overlay_entry.py").read_text(encoding="utf-8")
        mcp = (ROOT / "mcp_server.py").read_text(encoding="utf-8")

        self.assertIn('PROJECT_ROOT / "engram_overlay_entry.py"', overlay_main)
        self.assertNotIn('cmd = [sys.executable, "-m", "overlay.main"]', overlay_main)
        self.assertIn('"ENGRAM_RUNTIME_PARENT_PID"', overlay_main)
        self.assertIn('"ENGRAM_RUNTIME_SOURCE_ROOT"', overlay_main)
        self.assertIn('"parent_pid": int(os.environ.get("ENGRAM_RUNTIME_PARENT_PID"', mcp)
        self.assertIn('if role == "runtime-contract":', entry)
        dev = (ROOT / "dev-rebuild.ps1").read_text(encoding="utf-8-sig")
        process_identity = (ROOT / "core" / "install" / "process_identity.py").read_text(encoding="utf-8")
        self.assertIn("ENGRAM_DEV_SOURCE_RESTART", dev)
        self.assertIn("WaitForExit(25000)", dev)
        self.assertIn("snapshot --parent-pid", dev)
        self.assertIn("cleanup-snapshot", dev)
        self.assertIn("_cleanup_dev_restart_orphans", entry)
        self.assertIn("cleanup_dev_restart_orphans", entry)
        self.assertIn('"mcp-server", "kg-watcher"', process_identity)
        self.assertIn('"engram-dashboard.exe"', process_identity)
        self.assertNotIn("patterns = [\"mcp_server.py\"", entry)

    def test_publish_uses_explicit_target_and_restores_all_prior_overlays_on_failure(self):
        build = (ROOT / "installer" / "build-overlay.ps1").read_text(encoding="utf-8-sig")
        stopper = (ROOT / "installer" / "stop-engram-processes.ps1").read_text(encoding="utf-8-sig")

        self.assertIn('Join-Path $PSScriptRoot "stop-engram-processes.ps1"', build)
        self.assertIn("Stop-EngramArtifactProcesses -ArtifactDir $deployTarget", build)
        self.assertIn('Get-Process -Name $processName', stopper)
        self.assertIn('"engram-overlay.exe", "engram-dashboard.exe"', stopper)
        self.assertIn("$managedExecutables.ContainsKey($processPath)", stopper)
        self.assertIn("Start-Process -FilePath (Join-Path $deployTarget \"engram-overlay.exe\")", build)
        self.assertIn("foreach ($previousOverlayPath in $previousOverlayPaths)", build)
        self.assertIn("Move-Item -LiteralPath $SourceDir -Destination $stage", build)
        self.assertNotIn('Copy-Item -Path (Join-Path $SourceDir "*")', build)
        self.assertIn("Start-Process -FilePath $previousOverlayPath", build)
        self.assertNotIn("Get-CimInstance Win32_Process", build)

    def test_dashboard_setting_is_exposed_in_global_settings(self):
        defaults = (ROOT / "config" / "overlay.yaml").read_text(encoding="utf-8")
        settings = (ROOT / "overlay" / "settings_window.py").read_text(encoding="utf-8")

        self.assertIn("dashboard:\n", defaults)
        self.assertIn("대시보드 자동 실행", settings)
        self.assertIn("대시보드 보기", settings)
        self.assertIn('["dashboard", "enabled"]', settings)


if __name__ == "__main__":
    unittest.main()
