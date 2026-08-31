from pathlib import Path

from core.install.user_config import update_overlay_installer_config
from overlay.cli_capabilities import codex_catalog, control_state, efforts, models


def test_provider_catalogs_preserve_custom_selected_values():
    assert "auto" in models("antigravity", {"antigravity_model": "custom"})
    assert "custom" in models("antigravity", {"antigravity_model": "custom"})
    assert "max" in efforts("copilot", {"copilot_effort": "max"})


def test_codex_catalog_uses_cached_supported_efforts(tmp_path: Path):
    cache = tmp_path / "models_cache.json"
    cache.write_text('{"models":[{"slug":"o3","supported_reasoning_levels":[{"effort":"low","description":"fast"},{"effort":"high","description":"deep"}],"default_reasoning_level":"medium"},{"slug":"legacy","supported_reasoning_levels":["minimal"]}]}', encoding="utf-8")
    assert codex_catalog(cache) == {"o3": ["low", "high", "medium"], "legacy": ["minimal"]}


def test_ui_independent_control_state_disables_unsupported_provider_controls():
    assert control_state("antigravity") == ("readonly", "disabled")
    assert control_state("claude-code") == ("readonly", "readonly")


def test_claude_direct_catalog_uses_stable_official_aliases():
    choices = models("claude-code", {})
    assert choices == ["default", "best", "sonnet", "opus", "haiku", "opusplan", "sonnet[1m]", "opus[1m]"]


def test_installer_overlay_merge_preserves_unrelated_values(tmp_path: Path):
    path = tmp_path / "overlay.user.yaml"
    path.write_text("overlay:\n  flip_horizontal: true\ncli:\n  antigravity_model: pro\nmcp:\n  remote_port: 20000\n", encoding="utf-8")
    update_overlay_installer_config(path, provider="antigravity", mcp_port=17385)
    text = path.read_text(encoding="utf-8")
    assert "flip_horizontal: true" in text and "antigravity_model: pro" in text
    assert "provider: antigravity" in text and "http_port: 17385" in text and "remote_port: 20000" in text
