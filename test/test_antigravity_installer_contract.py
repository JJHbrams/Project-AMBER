from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_installer_uses_agy_and_active_antigravity_shim_contract():
    preflight = (ROOT / "installer" / "modules" / "01_preflight.ps1").read_text(encoding="utf-8-sig")
    config = (ROOT / "installer" / "modules" / "05_config.ps1").read_text(encoding="utf-8-sig")
    shims = (ROOT / "installer" / "modules" / "07_shims.ps1").read_text(encoding="utf-8-sig")
    configure = (ROOT / "installer" / "configure.ps1").read_text(encoding="utf-8-sig")

    assert "Get-Command agy" in preflight
    assert "agy mcp add engram" in config
    assert "agy mcp add engram" in configure
    assert "engram-antigravity.cmd" in shims
    assert "(agy -i `\"!ENGRAM_BOOTSTRAP!`\") else (agy !ARGS!)" in shims
    assert 'Join-Path $ShimDir "engram-gemini.cmd"' in shims
    assert "Remove-Item $legacyGeminiShim -Force" in shims


def test_source_runtime_syncs_antigravity_mcp_outside_policy_guidance_loop():
    main = (ROOT / "overlay" / "main.py").read_text(encoding="utf-8")
    assert "sync_antigravity_mcp_config" in main
    assert '("Antigravity MCP", sync_antigravity_mcp_config())' in main
    assert main.index('("Antigravity MCP", sync_antigravity_mcp_config())') < main.index('("Antigravity PreToolUse", sync_antigravity_pretool_hook(guidance_enabled))')
