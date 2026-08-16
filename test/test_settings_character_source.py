import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

from PIL import Image

from overlay.settings_window import (
    _SettingsWindow,
    character_height_override_value,
    character_source_mode_from_display,
    character_source_mode_to_display,
    manifest_transform_from_display,
    manifest_transform_to_display,
    manifest_vfx_from_display,
    manifest_vfx_to_display,
    rgb_to_hex,
    sample_snapshot_color,
    validate_character_source,
    validate_sprite_grid,
)


class CharacterSourceSettingsTests(unittest.TestCase):
    def test_mode_display_round_trip(self):
        self.assertEqual(character_source_mode_from_display("스프라이트 그리드"), "sprite_grid")
        self.assertEqual(character_source_mode_from_display("단일 이미지"), "static")
        self.assertEqual(character_source_mode_from_display("애니메이션 폴더"), "sequence")
        self.assertEqual(character_source_mode_to_display("sequence"), "애니메이션 폴더")

    def test_grid_validation_accepts_matching_png_and_chroma(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sprite = Path(temp_dir) / "sprite.png"
            Image.new("RGBA", (40, 30), "#00ff00").save(sprite)
            self.assertEqual(validate_sprite_grid(sprite, 2, 3, 20, 10, "#00FF00"), (True, "유효"))

    def test_grid_validation_rejects_dimension_and_chroma(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sprite = Path(temp_dir) / "sprite.png"
            Image.new("RGBA", (40, 30), "#00ff00").save(sprite)
            self.assertFalse(validate_sprite_grid(sprite, 2, 3, 21, 10, "#00FF00")[0])
            self.assertFalse(validate_sprite_grid(sprite, 2, 3, 20, 10, "green")[0])

    def test_active_mode_validation_does_not_write_unrelated_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "overlay.user.yaml"
            marker.write_text("existing: true\n", encoding="utf-8")
            before = marker.read_bytes()
            valid, _ = validate_character_source(
                "sprite_grid", "", (str(Path(temp_dir) / "missing.png"), "2", "3", "20", "10", "#00FF00"),
            )
            self.assertFalse(valid)
            self.assertEqual(marker.read_bytes(), before)

    def test_static_and_sequence_paths_are_validated_by_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            png = root / "character.png"
            Image.new("RGBA", (8, 8)).save(png)
            grid = ("", "1", "1", "8", "8", "#00FF00")
            self.assertTrue(validate_character_source("static", png, grid)[0])
            self.assertTrue(validate_character_source("sequence", root, grid)[0])
            self.assertFalse(validate_character_source("static", root, grid)[0])

    def test_eyedropper_helpers_use_snapshot_coordinates_and_canonical_hex(self):
        snapshot = Image.new("RGB", (3, 2), "#000000")
        snapshot.putpixel((1, 1), (16, 255, 3))
        self.assertEqual(rgb_to_hex((16, 255, 3, 0)), "#10FF03")
        self.assertEqual(sample_snapshot_color(snapshot, 101, 201, (100, 200)), "#10FF03")
        self.assertIsNone(sample_snapshot_color(snapshot, 99, 200, (100, 200)))

    def test_manifest_effect_labels_round_trip_to_canonical_values_and_accept_aliases(self):
        self.assertEqual(manifest_transform_to_display("hover"), "좌우 반전 + 세로 squash")
        self.assertEqual(manifest_transform_from_display("좌우 반전 + 세로 squash"), "hflip_squash")
        self.assertEqual(manifest_transform_from_display("hover_flip_squash"), "hflip_squash")
        self.assertEqual(manifest_transform_from_display("alternating_mirror_squash"), "hflip_squash")
        self.assertEqual(manifest_transform_from_display("click"), "none")
        self.assertEqual(manifest_vfx_to_display("click"), "반짝임 폭발 (sparkle burst)")
        self.assertEqual(manifest_vfx_from_display("은은한 반짝임 (twinkle)"), "twinkle")
        self.assertEqual(manifest_vfx_from_display("sparkle"), "sparkle_burst")

    def test_height_reset_removes_a_previous_user_override_against_base_config(self):
        self.assertEqual(character_height_override_value(0.2, 0.125), 0.2)
        self.assertIsNone(character_height_override_value(0.125, 0.125))

    def test_non_grid_source_disables_every_manifest_control(self):
        window = _SettingsWindow.__new__(_SettingsWindow)
        window._char_source_mode_var = SimpleNamespace(get=lambda: "단일 이미지")
        window._char_path_entry = Mock()
        window._char_file_button = Mock()
        window._char_dir_button = Mock()
        window._grid_controls = (Mock(), Mock())
        window._manifest_box = Mock()
        window._manifest_status_var = Mock()
        state, frames, selection, transform, vfx, frame_ms, dwell, save, reload, yaml_button = (Mock() for _ in range(10))
        window._manifest_state_combo = state
        window._manifest_selection_combo = selection
        window._manifest_transform_combo = transform
        window._manifest_vfx_combo = vfx
        window._manifest_controls = (state, frames, selection, transform, vfx, frame_ms, dwell, save, reload, yaml_button)
        window._update_grid_status = Mock()

        window._apply_character_source_mode()

        for control in window._grid_controls + window._manifest_controls:
            self.assertIn(call(state="disabled"), control.configure.call_args_list)
        window._manifest_status_var.set.assert_called_once()

        window._char_source_mode_var = SimpleNamespace(get=lambda: "스프라이트 그리드")
        window._apply_character_source_mode()
        for control in (state, selection, transform, vfx):
            self.assertIn(call(state="readonly"), control.configure.call_args_list)
        for control in (frames, frame_ms, dwell, save, reload, yaml_button):
            self.assertIn(call(state="normal"), control.configure.call_args_list)

    def test_actual_tk_manifest_controls_toggle_without_label_frame_state(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        root.withdraw()
        window = None
        try:
            window = _SettingsWindow(root)
            for mode in ("단일 이미지", "애니메이션 폴더"):
                window._char_source_mode_var.set(mode)
                window._apply_character_source_mode()
                root.update_idletasks()
                self.assertTrue(
                    all(str(control.cget("state")) == "disabled" for control in window._manifest_controls)
                )

            window._char_source_mode_var.set("스프라이트 그리드")
            window._apply_character_source_mode()
            root.update_idletasks()
            readonly = (
                window._manifest_state_combo,
                window._manifest_selection_combo,
                window._manifest_transform_combo,
                window._manifest_vfx_combo,
            )
            for control in window._manifest_controls:
                self.assertEqual(
                    str(control.cget("state")),
                    "readonly" if any(control is item for item in readonly) else "normal",
                )
        finally:
            if window is not None and window.window.winfo_exists():
                window.window.destroy()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
