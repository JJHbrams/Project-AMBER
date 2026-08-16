import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from overlay.character import (
    _CharacterProfile,
    CharacterOverlay,
    _animation_state,
    _key_chroma_background,
    _render_effect_frame,
    _thicken_effect_pixels,
)


class CharacterEffectsTests(unittest.TestCase):
    def test_chroma_key_removes_near_black_background(self):
        image = Image.new("RGBA", (2, 1), (1, 1, 1, 255))
        image.putpixel((1, 0), (240, 120, 10, 255))

        keyed = _key_chroma_background(image, (1, 1, 1))

        self.assertEqual(keyed.getpixel((0, 0))[3], 0)
        self.assertEqual(keyed.getpixel((1, 0)), (240, 120, 10, 255))

    def test_effect_composition_preserves_base_canvas_and_alpha(self):
        base = Image.new("RGBA", (8, 8), (20, 30, 40, 255))
        effect = Image.new("RGBA", (8, 8), (255, 0, 0, 255))

        rendered = _render_effect_frame(base, effect, opacity=0.5, scale_y=1.05)

        self.assertEqual(rendered.size, base.size)
        self.assertGreater(rendered.getpixel((4, 4))[0], 20)

    def test_thickened_downscaled_effect_keeps_a_visible_two_pixel_mark(self):
        effect = Image.new("RGBA", (100, 100))
        for y in range(50, 60):
            for x in range(50, 60):
                effect.putpixel((x, y), (255, 180, 20, 255))
        downscaled = effect.resize((10, 10), Image.NEAREST)

        expanded = _thicken_effect_pixels(downscaled, 2)
        colored = [
            (x, y)
            for y in range(expanded.height)
            for x in range(expanded.width)
            if expanded.getpixel((x, y))[3] > 0
        ]

        self.assertGreaterEqual(len({x for x, _ in colored}), 2)
        self.assertGreaterEqual(len({y for _, y in colored}), 2)

    def test_click_state_has_priority_and_restarts_from_new_start_time(self):
        self.assertEqual(_animation_state(1000, 1100, 420), "click")
        self.assertEqual(_animation_state(1000, 1420, 420), "idle")
        self.assertEqual(_animation_state(1300, 1310, 420), "click")

    def test_idle_and_click_render_paths_forward_their_effect_thickness(self):
        overlay = CharacterOverlay.__new__(CharacterOverlay)
        overlay._profile = SimpleNamespace(
            effects_idle_interval_ms=2400,
            effects_idle_thickness_px=2,
            effects_click_thickness_px=3,
        )
        overlay._effect_images = {
            "twinkle": Image.new("RGBA", (1, 1)),
            "sparkle_burst": Image.new("RGBA", (1, 1)),
        }
        calls = []
        overlay._render_current_image = lambda **kwargs: calls.append(kwargs)

        overlay._render_idle_effect(1200)
        overlay._render_click_effect(0.5)

        self.assertEqual(calls[0]["effect_thickness_px"], 2)
        self.assertEqual(calls[1]["effect_thickness_px"], 3)

    def test_missing_effect_asset_keeps_effects_enabled_but_has_no_asset(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = {
                "overlay": {
                    "character": {
                        "name": str(Path(temporary) / "missing-character.png"),
                        "effects": {"enabled": True, "idle_asset": "missing.png"},
                    }
                }
            }

            profile = _CharacterProfile(cfg)

        self.assertTrue(profile.effects_enabled)
        self.assertIsNone(profile.effects_idle_asset)
        self.assertIsNone(profile.effects_click_asset)
        self.assertEqual(profile.effects_idle_thickness_px, 2)
        self.assertEqual(profile.effects_click_thickness_px, 3)


if __name__ == "__main__":
    unittest.main()
