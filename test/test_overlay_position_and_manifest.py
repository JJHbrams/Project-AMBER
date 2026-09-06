import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from overlay import config
from overlay.character import clamp_overlay_position
from overlay.character_assets import ReactionPackResolution
from overlay.bubble.bubble_manager import BubbleManager
from overlay.settings_window import ensure_user_reaction_pack, save_reaction_manifest, validate_manifest_state
import overlay.settings_window as settings_window


class OverlayStateTests(unittest.TestCase):
    def test_malformed_runtime_state_falls_back_to_empty_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "overlay.state.yaml"
            state_path.write_text("[not a mapping]", encoding="utf-8")
            with patch.object(config, "_STATE_PATH", state_path):
                self.assertEqual(config.get_overlay_state(), {})

    def test_atomic_state_update_merges_existing_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "overlay.state.yaml"
            state_path.write_text("cli:\n  provider: copilot\n", encoding="utf-8")
            with patch.object(config, "_STATE_PATH", state_path):
                config.update_overlay_state(lambda state: state.update({"overlay_window": {"x": 12, "y": 24}}))
                current = config.get_overlay_state()
            self.assertEqual(current["cli"]["provider"], "copilot")
            self.assertEqual(current["overlay_window"], {"x": 12, "y": 24})

    def test_existing_state_setter_does_not_clobber_window_position(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "overlay.state.yaml"
            state_path.write_text("overlay_window: {x: 12, y: 24}\n", encoding="utf-8")
            with patch.object(config, "_STATE_PATH", state_path):
                config.set_cli_provider("copilot")
                current = config.get_overlay_state()
            self.assertEqual(current["overlay_window"], {"x": 12, "y": 24})


class ManifestEditorValidationTests(unittest.TestCase):
    def test_manifest_state_rejects_bad_cells_and_preserves_optional_dwell(self):
        valid = {"frames": "1, 2", "selection": "random", "transform": "none", "vfx": "twinkle", "frame_ms": "300", "dwell_ms": ""}
        self.assertEqual(validate_manifest_state(valid, 4)[0], True)
        invalid = {**valid, "frames": "4"}
        self.assertEqual(validate_manifest_state(invalid, 4)[0], False)

    def test_manifest_state_rejects_enum_and_nonpositive_timing(self):
        state = {"frames": "1", "selection": "bad", "transform": "none", "vfx": "none", "frame_ms": "0", "dwell_ms": "-1"}
        self.assertFalse(validate_manifest_state(state, 2)[0])

    def test_user_pack_clone_and_save_preserves_unknown_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundled = root / "bundled"
            bundled.mkdir()
            (bundled / "states.png").write_bytes(b"sprite")
            (bundled / "manifest.yaml").write_text("schema_version: 1\nid: demo\nunknown: retain\nstates:\n  idle: {frames: [0]}\n", encoding="utf-8")
            resolved = ReactionPackResolution(source="bundled", sprite_sheet=bundled / "states.png")
            with patch.object(settings_window, "USER_REACTION_PACKS_DIR", root / "user"), patch.object(settings_window, "resolve_reaction_pack", return_value=resolved):
                target = ensure_user_reaction_pack("demo")
                raw = {"schema_version": 1, "id": "demo", "unknown": "retain", "states": {"idle": {"frames": [1]}}}
                saved = save_reaction_manifest("demo", raw)
            self.assertEqual(saved, target / "manifest.yaml")
            self.assertEqual(__import__("yaml").safe_load(saved.read_text(encoding="utf-8"))["unknown"], "retain")

    def test_incomplete_user_pack_is_not_merged_with_bundled_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            user_root = Path(temporary) / "user"
            (user_root / "demo").mkdir(parents=True)
            with patch.object(settings_window, "USER_REACTION_PACKS_DIR", user_root):
                with self.assertRaisesRegex(ValueError, "manifest.yaml"):
                    ensure_user_reaction_pack("demo")


class PositionPersistenceTests(unittest.TestCase):
    def test_removed_monitor_falls_back_to_visible_work_area(self):
        with patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(0, 0, 1000, 700)):
            self.assertEqual(clamp_overlay_position(5000, 4000, 200, 100), (800, 600))

    def test_clear_all_does_not_reset_persistent_manual_positions(self):
        manager = BubbleManager.__new__(BubbleManager)
        manager._speech_manual_pos = (1.25, -0.5)
        manager._thought_manual_pos = (0.75, -1.0)
        manager._settle_nudge_outcome = lambda: None
        manager._clear_nudge_state = lambda: None
        manager._speech = SimpleNamespace(hide=lambda: None)
        manager._thought = SimpleNamespace(hide=lambda: None)
        manager._echo = SimpleNamespace(hide=lambda: None)
        manager._approval_windows = []
        manager._approval_requests = {}
        manager._tool_order = []
        manager._tool_info = {}
        for name, value in {
            "_nudge_reply_cb": None, "_speech_text": "x", "_speech_dismissed": True,
            "_speech_rect": (0, 0, 1, 1), "_last_speech_text": "x", "_last_was_nudge": True,
            "_speech_block_id": "x", "_thought_text": "x", "_thought_dismissed": True,
            "_thought_rect": (0, 0, 1, 1), "_thought_block_id": "x", "_echo_text": "x",
            "_echo_rect": (0, 0, 1, 1),
        }.items():
            setattr(manager, name, value)
        manager.clear_all()
        self.assertEqual(manager._speech_manual_pos, (1.25, -0.5))
        self.assertEqual(manager._thought_manual_pos, (0.75, -1.0))


if __name__ == "__main__":
    unittest.main()
