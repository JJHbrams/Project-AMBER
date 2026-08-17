import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from overlay.character import (
    CharacterOverlay, _CharacterProfile, SpriteStateMachine, apply_sprite_crop_y_offset,
    bottom_anchored_geometry, classify_sprite_event, select_sprite_frame,
    target_height_for_work_area,
)
from overlay.character_assets import (
    ReactionPackResolution, _inline_state_template, inline_reaction_pack,
    resolve_bundled_character_source, resolve_bundled_reaction_sheet, resolve_reaction_pack,
)


STATES = {
    "default", "idle", "hover", "click", "input", "generating", "thought",
    "search", "memory", "success", "provider_error", "error",
}


class SpriteManifestTests(unittest.TestCase):
    def test_bundled_engram_uses_declared_body_frames(self):
        pack = resolve_reaction_pack("engram")
        self.assertEqual(pack.states["default"]["frames"], (18,))
        self.assertEqual(pack.states["idle"]["frames"], (18, 19, 20, 21, 22))
        self.assertEqual(pack.states["click"]["frames"], (9, 10, 11))
        self.assertEqual(pack.states["provider_error"]["frames"], (8,))

    def test_bundled_idle_is_random_only_without_transform(self):
        idle = resolve_reaction_pack("engram").states["idle"]
        self.assertEqual(idle["frame_ms"], 7200)
        self.assertEqual(idle["transform"], "none")
        self.assertEqual(idle["vfx"], "twinkle")
        self.assertEqual(idle["selection"], "shuffle")

    def test_transient_manifest_timings_hold_their_final_state(self):
        states = resolve_reaction_pack("engram").states
        self.assertEqual(states["click"], {
            "frames": (9, 10, 11), "selection": "random", "transform": "none",
            "vfx": "sparkle_burst", "frame_ms": 1000, "dwell_ms": 1000,
        })
        self.assertEqual(states["input"]["frame_ms"], 1600)
        self.assertEqual(states["input"]["dwell_ms"], 1600)
        self.assertEqual(states["success"]["frame_ms"], 2400)
        self.assertEqual(states["success"]["dwell_ms"], 2400)

    def test_manifest_crop_offset_is_exposed_and_config_can_override_it(self):
        pack = resolve_reaction_pack("engram")
        self.assertEqual(pack.crop_y_offset_px, 32)
        profile = _CharacterProfile({
            "overlay": {
                "character": {
                    "set": "engram",
                    "source_mode": "sprite_grid",
                    "reactions": {"enabled": True, "pack": "engram", "crop_y_offset_px": 12},
                }
            }
        })
        self.assertEqual(profile.reaction_pack.crop_y_offset_px, 12)

    def test_sprite_grid_mode_ignores_stale_static_or_sequence_name(self):
        for stale_name in ("C:/missing/custom.png", "C:/missing/frames"):
            profile = _CharacterProfile({
                "overlay": {"character": {
                    "set": "engram", "name": stale_name, "source_mode": "sprite_grid",
                    "reactions": {"enabled": True, "pack": "engram"},
                }}
            })
            self.assertTrue(profile.sprite_enabled)
            self.assertEqual(profile.reaction_pack.source, "bundled")

    def test_static_and_sequence_modes_do_not_cross_resolve_source_types(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "image.png"
            Image.new("RGBA", (8, 8)).save(image)
            frames = root / "frames"
            frames.mkdir()
            Image.new("RGBA", (8, 8)).save(frames / "frames_0.png")
            static = _CharacterProfile({"overlay": {"character": {"source_mode": "static", "name": str(frames)}}})
            sequence = _CharacterProfile({"overlay": {"character": {"source_mode": "sequence", "name": str(image)}}})
        self.assertFalse(static.has_numbered_frames)
        self.assertFalse(sequence.has_numbered_frames)

    def test_legacy_logical_sequence_is_inferred_without_source_mode(self):
        profile = _CharacterProfile({"overlay": {"character": {"name": "smoke_chroma"}}})
        self.assertEqual(profile.source_mode, "sequence")
        self.assertTrue(profile.has_numbered_frames)
        self.assertTrue(profile.legacy_body_motion)

    def test_legacy_bundled_paths_map_only_to_canonical_asset_types(self):
        self.assertEqual(
            resolve_bundled_character_source("resource/character/arona.png", "static"),
            Path("resource/character/static/arona.png").resolve(),
        )
        self.assertEqual(
            resolve_bundled_character_source("resource/character/smoke_chroma", "sequence"),
            Path("resource/character/sequences/smoke_chroma").resolve(),
        )
        self.assertEqual(
            resolve_bundled_character_source(Path("resource/character/arona.png").resolve(), "static"),
            Path("resource/character/static/arona.png").resolve(),
        )
        self.assertIsNone(resolve_bundled_character_source("C:/missing/arona.png", "static"))

    def test_profiles_remap_current_checkout_legacy_absolute_paths(self):
        old_static = Path("resource/character/arona.png").resolve()
        old_sequence = Path("resource/character/smoke_chroma").resolve()
        static = _CharacterProfile({"overlay": {"character": {"source_mode": "static", "name": str(old_static)}}})
        sequence = _CharacterProfile({"overlay": {"character": {"source_mode": "sequence", "name": str(old_sequence)}}})
        self.assertEqual(static.default_frame, Path("resource/character/static/arona.png").resolve())
        self.assertTrue(sequence.has_numbered_frames)

    def test_removed_engram_grid_path_maps_only_within_bundled_layout(self):
        canonical = Path("resource/character/reactions/engram/states.png").resolve()
        self.assertEqual(
            resolve_bundled_reaction_sheet("resource/character/engram_set/engram_states.png"), canonical,
        )
        self.assertEqual(
            resolve_bundled_reaction_sheet(Path("resource/character/engram_set/engram_states.png").resolve()), canonical,
        )
        self.assertIsNone(resolve_bundled_reaction_sheet("C:/missing/engram_set/engram_states.png"))

    def test_click_vfx_is_limited_to_bundled_engram_identity(self):
        engram = _CharacterProfile({
            "overlay": {"character": {"set": "engram", "name": "engram", "source_mode": "static", "effects": {"enabled": True}}}
        })
        custom = _CharacterProfile({
            "overlay": {"character": {"set": "engram", "name": "arona", "effects": {"enabled": True}}}
        })
        self.assertTrue(engram.click_vfx_enabled)
        self.assertFalse(custom.click_vfx_enabled)

    def test_click_vfx_rejects_custom_engram_named_sequence_and_inline_grid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sequence_dir = root / "engram"
            sequence_dir.mkdir()
            Image.new("RGBA", (8, 8)).save(sequence_dir / "engram_0.png")
            sheet = root / "custom-grid.png"
            Image.new("RGBA", (8, 8)).save(sheet)

            sequence = _CharacterProfile({
                "overlay": {"character": {
                    "set": "engram", "name": str(sequence_dir), "source_mode": "sequence",
                    "effects": {"enabled": True},
                }}
            })
            inline_grid = _CharacterProfile({
                "overlay": {"character": {
                    "set": "engram", "name": "engram", "source_mode": "sprite_grid",
                    "effects": {"enabled": True},
                    "reactions": {
                        "enabled": True, "pack": "engram", "apply_to_custom": True,
                        "sprite_sheet": str(sheet), "chroma_key": "#00FF00",
                        "grid": {"columns": 1, "rows": 1, "cell_width": 8, "cell_height": 8},
                    },
                }}
            })

        self.assertFalse(sequence.click_vfx_enabled)
        self.assertFalse(inline_grid.click_vfx_enabled)

    def test_click_vfx_allows_bundled_engram_set_image_by_absolute_path(self):
        bundled = Path("resource/character/sets/engram/character.png").resolve()
        profile = _CharacterProfile({
            "overlay": {"character": {
                "set": "", "name": str(bundled), "source_mode": "static", "effects": {"enabled": True},
            }}
        })
        self.assertTrue(profile.click_vfx_enabled)

    def test_static_vfx_keeps_body_geometry_fixed_by_default(self):
        overlay = CharacterOverlay.__new__(CharacterOverlay)
        overlay._profile = SimpleNamespace(
            legacy_body_motion=False, effects_idle_interval_ms=2400,
            effects_idle_thickness_px=2, effects_click_thickness_px=3,
        )
        overlay._effect_images = {"twinkle": object(), "sparkle_burst": object()}
        overlay._render_current_image = Mock()

        overlay._render_idle_effect(1200)
        idle = overlay._render_current_image.call_args.kwargs
        self.assertEqual((idle["scale_x"], idle["scale_y"], idle["offset_y"]), (1.0, 1.0, 0))

        overlay._render_current_image.reset_mock()
        overlay._render_click_effect(0.5)
        click = overlay._render_current_image.call_args.kwargs
        self.assertEqual((click["scale_x"], click["scale_y"], click["offset_x"], click["offset_y"]), (1.0, 1.0, 0, 0))

    def test_legacy_body_motion_opt_in_restores_transform(self):
        overlay = CharacterOverlay.__new__(CharacterOverlay)
        overlay._profile = SimpleNamespace(legacy_body_motion=True, effects_click_thickness_px=3)
        overlay._effect_images = {"sparkle_burst": object()}
        overlay._render_current_image = Mock()
        overlay._render_click_effect(0.5)
        rendered = overlay._render_current_image.call_args.kwargs
        self.assertNotEqual((rendered["scale_x"], rendered["scale_y"], rendered["offset_y"]), (1.0, 1.0, 0))

    def test_inline_uses_builtin_contract_when_grid_can_hold_it(self):
        states = _inline_state_template(24)
        self.assertEqual(states["success"]["frames"], (16,))
        self.assertEqual(states["idle"]["frames"], (18, 19, 20, 21, 22))

    def test_inline_small_grid_falls_back_to_safe_cell_zero(self):
        states = _inline_state_template(12)
        self.assertTrue(all(spec["frames"] == (0,) for spec in states.values()))

    def test_inline_rejects_grid_that_does_not_match_image(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sheet.png"
            Image.new("RGBA", (16, 16)).save(path)
            pack = inline_reaction_pack(str(path), {"columns": 2, "rows": 2, "cell_width": 10, "cell_height": 10}, "#00FF00")
        self.assertEqual(pack.source, "disabled")

    def test_invalid_inline_crop_offset_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sheet.png"
            Image.new("RGBA", (20, 20)).save(path)
            pack = inline_reaction_pack(
                str(path), {"columns": 2, "rows": 2, "cell_width": 10, "cell_height": 10}, "#00FF00", 10,
            )
        self.assertEqual(pack.source, "disabled")

    def test_manifest_rejects_out_of_range_crop_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "engram"
            root.mkdir()
            (root / "states.png").write_bytes(b"png")
            (root / "manifest.yaml").write_text(
                """schema_version: 1
id: engram
sprite_sheet: states.png
chroma_key: \"#00FF00\"
crop_y_offset_px: 408
grid: {columns: 6, rows: 4, cell_width: 434, cell_height: 408}
states: {idle: {frames: [0]}}
""",
                encoding="utf-8",
            )
            with patch("overlay.character_assets.USER_REACTION_PACKS_DIR", root.parent), patch(
                "overlay.character_assets.resolve_editable_overlay_path", return_value=Path(directory) / "missing"
            ):
                pack = resolve_reaction_pack("engram")
        self.assertEqual(pack.source, "disabled")


class SpriteCropTests(unittest.TestCase):
    def test_default_crop_keeps_full_cell_aspect_ratio_at_target_height(self):
        overlay = CharacterOverlay.__new__(CharacterOverlay)
        overlay._profile = SimpleNamespace(reaction_pack=ReactionPackResolution(
            source="inline", chroma_key="#00FF00", columns=1, rows=1,
            cell_width=4, cell_height=8, crop_y_offset_px=0,
        ))
        overlay._sprite_sheet = Image.new("RGBA", (4, 8), (255, 0, 0, 255))
        overlay._sprite_cache = {}
        image = overlay._sprite_image(0, 20, False)
        self.assertEqual(image.size, (10, 20))
        self.assertEqual(image.getpixel((0, 19))[3], 255)

    def test_top_gutter_crop_scales_the_remaining_cell_without_bottom_padding(self):
        overlay = CharacterOverlay.__new__(CharacterOverlay)
        overlay._profile = SimpleNamespace(reaction_pack=ReactionPackResolution(
            source="inline", chroma_key="#00FF00", columns=1, rows=1,
            cell_width=4, cell_height=8, crop_y_offset_px=2,
        ))
        overlay._sprite_sheet = Image.new("RGBA", (4, 8), (255, 0, 0, 255))
        overlay._sprite_cache = {}
        image = overlay._sprite_image(0, 20, False)
        self.assertEqual(image.size, (13, 20))
        self.assertEqual(image.getpixel((0, 19))[3], 255)

    def test_monitor_work_area_controls_sprite_height_and_resize_geometry(self):
        self.assertEqual(target_height_for_work_area((1920, 1040), 0.125), 130)
        self.assertEqual(target_height_for_work_area((1280, 720), 0.125), 120)
        self.assertEqual(bottom_anchored_geometry(10, 700, 200, 160, 120), "160x120+10+780")

    def test_size_reload_bottom_anchors_then_clamps_to_current_monitor(self):
        overlay = CharacterOverlay.__new__(CharacterOverlay)
        overlay._img_w, overlay._img_h = 160, 260
        overlay.root = SimpleNamespace(winfo_x=lambda: 10, winfo_y=lambda: 700, geometry=Mock())
        with patch("overlay.character.clamp_overlay_position", return_value=(12, 540)) as clamp:
            overlay._resize_window_to_image(120)
        clamp.assert_called_once_with(10, 560, 160, 260)
        overlay.root.geometry.assert_called_once_with("160x260+12+540")

    def test_vertical_crop_removes_top_without_losing_the_cell_bottom(self):
        cell = Image.new("RGBA", (2, 4))
        cell.putdata([
            (255, 0, 0, 255), (255, 0, 0, 255),
            (0, 255, 0, 255), (0, 255, 0, 255),
            (0, 0, 255, 255), (0, 0, 255, 255),
            (255, 255, 0, 255), (255, 255, 0, 255),
        ])
        cropped = apply_sprite_crop_y_offset(cell, 2)
        self.assertEqual(cropped.size, (2, 2))
        self.assertEqual(cropped.getpixel((0, 0)), (0, 0, 255, 255))
        self.assertEqual(cropped.getpixel((0, 1)), (255, 255, 0, 255))


class SpriteEventTests(unittest.TestCase):
    def test_event_classification_contract(self):
        self.assertEqual(classify_sprite_event({"kind": "thought"}), "thought")
        self.assertEqual(classify_sprite_event({"kind": "tool_use", "tool_name": "mcp__engram__kg_search"}), "memory")
        self.assertEqual(classify_sprite_event({"kind": "tool_use", "tool_name": "web_search"}), "search")
        self.assertEqual(classify_sprite_event({"kind": "tool_use", "tool_name": "shell_command"}), "generating")
        self.assertEqual(classify_sprite_event({"kind": "tool_result", "is_error": True}), "error")
        self.assertEqual(classify_sprite_event({"kind": "error", "text": "anything"}), "provider_error")

    def test_hover_restores_exact_work_state(self):
        model = SpriteStateMachine(STATES)
        model.set_work("thought", 10)
        model.set_hovered(True, 20)
        model.set_hovered(False, 30)
        self.assertEqual(model.state, "thought")
        self.assertEqual(model.work_state, "thought")

    def test_click_restores_hover_then_work_state(self):
        model = SpriteStateMachine(STATES)
        model.set_work("search", 10)
        model.set_hovered(True, 20)
        model.show_transient("click", 30)
        model.expire(500, 400)
        self.assertEqual(model.state, "hover")
        model.set_hovered(False, 510)
        self.assertEqual(model.state, "search")

    def test_input_always_restores_generating(self):
        model = SpriteStateMachine(STATES)
        model.set_work("memory", 10)
        model.notify_input(20)
        model.expire(500, 450)
        self.assertEqual(model.state, "generating")

    def test_locked_error_ignores_lower_priority_events(self):
        model = SpriteStateMachine(STATES)
        model.handle_event({"kind": "error"}, 10)
        self.assertFalse(model.handle_event({"kind": "thought"}, 20))
        self.assertEqual(model.state, "provider_error")
        self.assertEqual(model.work_state, "idle")

    def test_success_completes_to_idle_or_hover(self):
        model = SpriteStateMachine(STATES)
        model.set_work("generating", 1)
        model.handle_event({"kind": "turn_end"}, 10)
        model.expire(1300, 1200)
        self.assertEqual(model.state, "idle")
        model.handle_event({"kind": "result"}, 1400)
        model.set_hovered(True, 1500)
        model.expire(2700, 1200)
        self.assertEqual(model.state, "hover")


class SpriteSelectionTests(unittest.TestCase):
    def test_random_frame_is_stable_in_a_bucket_and_can_change_in_next(self):
        spec = {"frames": (19, 20, 21, 22), "selection": "random", "frame_ms": 100}
        choices = {}
        rng = random.Random(5)
        first, bucket = select_sprite_frame(spec, "idle", 1, 20, choices, rng)
        repeat, same_bucket = select_sprite_frame(spec, "idle", 1, 99, choices, rng)
        later, next_bucket = select_sprite_frame(spec, "idle", 1, 100, choices, rng)
        self.assertEqual((bucket, same_bucket), (0, 0))
        self.assertEqual(first, repeat)
        self.assertEqual(next_bucket, 1)
        self.assertIn(later, spec["frames"])

    def test_reentering_state_uses_a_new_random_epoch(self):
        spec = {"frames": (12, 14), "selection": "random", "frame_ms": 100}
        choices = {}
        rng = random.Random(2)
        _, first_bucket = select_sprite_frame(spec, "input", 1, 0, choices, rng)
        _, next_epoch_bucket = select_sprite_frame(spec, "input", 2, 0, choices, rng)
        self.assertEqual(first_bucket, next_epoch_bucket)
        self.assertEqual(len(choices), 2)

    def test_click_random_choice_is_stable_for_its_whole_dwell(self):
        spec = {"frames": (9, 10, 11), "selection": "random", "frame_ms": 1000}
        choices = {}
        rng = random.Random(18)
        frames = [select_sprite_frame(spec, "click", 1, point, choices, rng)[0] for point in (0, 180, 360, 999)]
        self.assertEqual(len(set(frames)), 1)
        self.assertIn(frames[0], spec["frames"])

    def test_input_random_choice_is_stable_for_its_whole_dwell(self):
        spec = {"frames": (12, 14), "selection": "random", "frame_ms": 1600}
        choices = {}
        rng = random.Random(12)
        first = select_sprite_frame(spec, "input", 1, 0, choices, rng)[0]
        final = select_sprite_frame(spec, "input", 1, 1599, choices, rng)[0]
        self.assertEqual(first, final)

    def test_shuffle_cycle_is_permutation_and_stable_in_same_bucket(self):
        spec = {"frames": (18, 19, 20, 21, 22), "selection": "shuffle", "frame_ms": 100}
        choices, orders = {}, {}
        rng = random.Random(7)
        cycle = [select_sprite_frame(spec, "idle", 1, point, choices, rng, orders)[0] for point in (0, 100, 200, 300, 400)]
        repeated = select_sprite_frame(spec, "idle", 1, 299, choices, rng, orders)[0]
        self.assertEqual(set(cycle), set(spec["frames"]))
        self.assertEqual(repeated, cycle[2])

    def test_shuffle_cycle_boundary_does_not_repeat_and_seed_is_deterministic(self):
        spec = {"frames": (18, 19, 20, 21, 22), "selection": "shuffle", "frame_ms": 100}

        def render(seed):
            choices, orders = {}, {}
            rng = random.Random(seed)
            return [select_sprite_frame(spec, "idle", 1, point, choices, rng, orders)[0] for point in range(0, 1000, 100)]

        first = render(21)
        self.assertEqual(first, render(21))
        self.assertNotEqual(first[4], first[5])

    def test_shuffle_far_cycle_does_not_require_recursive_prior_cycles(self):
        spec = {"frames": (18, 19, 20, 21, 22), "selection": "shuffle", "frame_ms": 100}
        frame, bucket = select_sprite_frame(
            spec, "idle", 1, 10_000_000, {}, random.Random(3), {},
        )
        self.assertEqual(bucket, 100_000)
        self.assertIn(frame, spec["frames"])


if __name__ == "__main__":
    unittest.main()
