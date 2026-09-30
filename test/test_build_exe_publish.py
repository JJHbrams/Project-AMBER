"""Source-text checks for the fast exe build wrapper and detached publish cleanup."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = (ROOT / "installer" / "build-exe.ps1").read_text(encoding="utf-8-sig")
BUILD = (ROOT / "installer" / "build-overlay.ps1").read_text(encoding="utf-8-sig")
INSTALL = (ROOT / "INSTALL.ps1").read_text(encoding="utf-8-sig")


def _function_body(name: str) -> str:
    start = BUILD.index(f"function {name}")
    nxt = BUILD.find("\nfunction ", start + 1)
    return BUILD[start:nxt]


def test_wrapper_forwards_switches():
    assert "build-overlay.ps1" in WRAPPER
    assert "$PSScriptRoot" in WRAPPER
    for token in ("NoStart", "FullBuild", "FullSmoke", "FullSmoke = $true", '"rebuild"', '"auto"'):
        assert token in WRAPPER
    assert "dist\\engram-overlay" in WRAPPER
    assert "exit $LASTEXITCODE" in WRAPPER


def test_install_help_points_to_wrapper():
    assert "build-exe.ps1" in INSTALL.split("#>")[0]


def test_build_overlay_params():
    params = BUILD.split("param(", 1)[1].split("\n)", 1)[0]
    assert "[switch]$FullBuild" in params and "[switch]$FullSmoke" in params
    assert "$script:ForceFullSmoke" in BUILD


def test_publish_no_sync_backup_delete():
    body = _function_body("Publish-OverlayArtifact")
    assert "Move-Item -LiteralPath $SourceDir -Destination $stage" in body
    assert not re.search(r"Remove-Item[^\n]*\$backup", body)
    assert "Remove-DirectoryDetached $backup" in body
    helper = _function_body("Remove-DirectoryDetached")
    assert "$env:ComSpec" in helper and "'rd', '/s', '/q'" in helper
    assert "-WindowStyle Hidden" in helper and "catch" in helper


def test_clear_stale_trash_called_before_publish():
    assert ".engram-overlay-backup-*" in _function_body("Clear-StaleOverlayTrash")
    assert ".engram-overlay-stage-*" in _function_body("Clear-StaleOverlayTrash")
    full = BUILD.split("# ---- full build ----", 1)[1]
    call = full.index("Clear-StaleOverlayTrash -TargetDir")
    publish = full.index("Publish-OverlayArtifact $tempArtifact")
    assert call < full.index("PyInstaller build") < publish
    # The live overlay keeps serving during PyInstaller/smoke: stop only right before the swap.
    stop = full.index("Stop-EngramArtifactProcesses -ArtifactDir")
    assert full.index("-FullSmokeSelection") < stop < publish
    # fast path clears trash before its own stage exists so the stage is never swept
    fast = BUILD.split('if ($buildKind -eq "fast")', 1)[1].split("# ---- full build ----", 1)[0]
    assert fast.index("Clear-StaleOverlayTrash") < fast.index("$fastStage = Join-Path")


INSTALLER = (ROOT / "installer" / "build-installer.ps1").read_text(encoding="utf-8-sig")


def test_plan_drives_build_kind():
    assert '"--plan"' in BUILD and "Get-OverlayPlan" in BUILD and "Write-OverlayPlan" in BUILD
    assert "$FullBuild -or $Mode -in" in BUILD
    assert "Get-FullSmokeSelection" in BUILD and "$script:ForceFullSmoke" in BUILD


def test_fast_path_apply_and_kind():
    fast = BUILD.split('if ($buildKind -eq "fast")', 1)[1].split("# ---- full build ----", 1)[0]
    assert '"--apply"' in fast
    assert '"--kind", "fast-patch"' in fast
    assert "core.install.stage_clone" in fast
    assert "engram-mcp-bridge.spec" in fast and "$plan.bridge_rebuild" in fast
    assert "Invoke-BridgeSmoke" in fast
    # runtime-contract reads the manifest: it must be written before smoke.
    assert fast.index('"--kind", "fast-patch"') < fast.index("Invoke-SmokeSelection")
    assert fast.index("Invoke-SmokeSelection") < fast.index("Publish-OverlayArtifact")
    assert fast.index("Stop-EngramArtifactProcesses") < fast.index("Publish-OverlayArtifact")
    assert "build-native-bubble" not in fast


def test_fast_path_has_no_silent_fallback():
    fast = BUILD.split('if ($buildKind -eq "fast")', 1)[1].split("# ---- full build ----", 1)[0]
    assert "-FullBuild" in fast and "no automatic fallback" in fast
    assert "throw" in fast and "Remove-DirectoryDetached $fastStage" in fast
    assert 'buildKind = "full"' not in fast


def test_smoke_loop_skips_embedding_when_covered_and_records():
    body = _function_body("Invoke-SmokeSelection")
    assert "skipped:covered-by-smoke" in body and "embedding_in_smoke" in body
    assert "--record-smoke" in _function_body("Get-SmokeRecordArgs")
    assert '"--kind", "full"' in BUILD


def test_persistent_model_cache():
    body = _function_body("Resolve-SmokeModelCache")
    assert "build\smoke-model-cache" in body and "--ensure-cache" in body
    assert "engram-smoke-model-" not in BUILD


def test_phase_timing():
    for phase in ('"plan"', '"clone"', '"apply"', '"bridge"', '"publish"', '"smoke:$key"'):
        assert f"Start-OverlayPhase {phase}" in BUILD
    assert "Write-OverlayTimings" in BUILD


def test_installer_release_forces_full_build_and_smoke():
    # A release never ships a fast-patched artifact.
    assert "-Deploy $DistDir -NoStart -FullBuild -FullSmoke" in INSTALLER
    assert "if ($Release) {" in INSTALLER.split("-NoStart -FullBuild -FullSmoke", 1)[0].splitlines()[-4]
    assert "$frozenBuiltNow -and $fullSmokePassed" in INSTALLER
    assert "-ValidateOnly" in INSTALLER
