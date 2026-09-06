from unittest.mock import patch

from overlay.external_renderer import InstalledRenderer, apply_renderer_selection, discover_renderers, legacy_renderer_diagnostic


def test_selection_persists_only_id_and_mode_and_preserves_unrelated_config():
    saved = {"unrelated": {"keep": True}, "overlay": {"hotkey": "alt+f12"}}
    renderer = InstalledRenderer("demo", "Demo", ("observer", "replace"))
    assert apply_renderer_selection(saved, renderer, "replace")
    assert saved["unrelated"] == {"keep": True}
    assert saved["overlay"]["hotkey"] == "alt+f12"
    assert saved["overlay"]["external_renderer"] == {
        "selected_renderer_id": "demo", "mode": "replace"
    }


def test_legacy_command_is_actionable_but_never_a_runtime_selection():
    cfg = {"overlay": {"external_renderer": {"command": ["never.exe"], "mode": "replace"}}}
    assert legacy_renderer_diagnostic(cfg)


def test_settings_discovery_keeps_each_catalog_item_as_a_logical_renderer():
    snapshot = [
        {"id": f"engram.preset-{index}", "name": f"Preset {index}",
         "supported_modes": ("observer", "replace")}
        for index in range(7)
    ]
    with patch("overlay.external_renderer.connected_renderer_snapshot", return_value=snapshot):
        renderers, diagnostics = discover_renderers()
    assert [item.id for item in renderers] == [f"engram.preset-{index}" for index in range(7)]
    assert all(item.supported_modes == ("observer", "replace") for item in renderers)
    assert diagnostics == []
