import math
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from overlay.bubble import bubble_image, shapes
from overlay.bubble.bubble_manager import BubbleManager
from overlay.bubble.bubble_window import BubbleWindow, normalize_tail_vector, tail_handle_point


class _Canvas:
    def __init__(self):
        self.calls = []

    def config(self, **kwargs):
        self.calls.append(("config", kwargs))

    def create_polygon(self, *args, **kwargs):
        self.calls.append(("polygon", args, kwargs))

    def create_oval(self, *args, **kwargs):
        self.calls.append(("oval", args, kwargs))

    def create_image(self, *args, **kwargs):
        self.calls.append(("image", args, kwargs))

    def create_line(self, *args, **kwargs):
        self.calls.append(("line", args, kwargs))


class SpeechTailInteractionTests(unittest.TestCase):
    def _window(self):
        window = BubbleWindow.__new__(BubbleWindow)
        window._current_w, window._current_h = 236, 136
        window._tail_angle = 0.0
        window._tail_dragging = False
        window._tail_shell_size = None
        window._on_tail_drag_end = None
        window._resize_start = None
        window._move_start = None
        window._dragged = False
        window._on_resize = lambda _w: None
        window._on_resize_h = None
        window._grip_corner = "top-right"
        window.win = None
        return window

    def test_tail_hit_is_at_rendered_tip_and_is_separate_from_body_drag(self):
        window = self._window()
        changed = []
        window._on_tail_drag = lambda dx, dy: changed.append((dx, dy))
        tip = tail_handle_point(window._current_w, window._current_h, 0.0)
        self.assertTrue(window._in_tail_zone(*map(round, tip)))
        self.assertFalse(window._in_tail_zone(118, 68))

        window._handle_press(SimpleNamespace(x=round(tip[0]), y=round(tip[1]), x_root=1, y_root=1))
        self.assertTrue(window._tail_dragging)
        self.assertIsNone(window._move_start)
        self.assertIsNone(window._resize_start)
        window._handle_drag(SimpleNamespace(x=118, y=4, x_root=1, y_root=1))
        self.assertAlmostEqual(changed[-1][0], 0.0, places=4)
        self.assertLess(changed[-1][1], -0.99)
        window._handle_release()
        self.assertFalse(window._tail_dragging)

    def test_tail_state_is_persisted_once_on_release(self):
        window = self._window()
        saved = []
        window._on_tail_drag = lambda _dx, _dy: None
        window._on_tail_drag_end = lambda: saved.append(True)
        window._tail_dragging = True
        window._handle_release()
        self.assertEqual(saved, [True])

    def test_nudge_height_does_not_shift_tail_drag_direction(self):
        window = self._window()
        window._current_h = 176
        window._tail_shell_size = (236, 136)
        changed = []
        window._on_tail_drag = lambda dx, dy: changed.append((dx, dy))
        window._tail_dragging = True
        window._handle_drag(SimpleNamespace(x=118, y=4, x_root=1, y_root=1))
        self.assertAlmostEqual(changed[-1][0], 0.0, places=4)
        self.assertLess(changed[-1][1], -0.99)

    def test_tail_normalization_rejects_invalid_values_and_stays_unit_length(self):
        self.assertIsNone(normalize_tail_vector(0, 0))
        self.assertIsNone(normalize_tail_vector(float("nan"), 1))
        vector = normalize_tail_vector(30, -40)
        self.assertEqual(vector, (0.6, -0.8))
        for angle in (0, math.pi / 2, math.pi, -math.pi / 2):
            x, y = tail_handle_point(236, 136, angle)
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x, 236)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(y, 136)


class ExternalReplaceZOrderTests(unittest.TestCase):
    def test_speech_window_holds_topmost_above_external_renderer_until_released(self):
        """No timed pulse: streaming chunks arrive faster than 350ms, so the
        release was cancelled forever and the bubble covered other apps."""
        window = BubbleWindow.__new__(BubbleWindow)
        window.win = Mock()
        window._root = Mock()
        window._external_replace_active = lambda: True
        window._keep_topmost = False
        window._topmost_release_id = None
        window._external_topmost_held = False
        window._overlay_foreground = True

        window._raise_above_external_replace()

        window.win.attributes.assert_called_once_with("-topmost", True)
        window.win.lift.assert_called_once()
        window._root.after.assert_not_called()

        window.release_external_topmost()
        self.assertEqual(window.win.attributes.call_args_list[-1].args, ("-topmost", False))

    def test_non_external_bubble_never_changes_topmost(self):
        window = BubbleWindow.__new__(BubbleWindow)
        window.win = Mock()
        window._external_replace_active = lambda: False

        window._raise_above_external_replace()

        window.win.attributes.assert_not_called()


class SpeechTailPersistenceTests(unittest.TestCase):
    def _manager(self):
        manager = BubbleManager.__new__(BubbleManager)
        manager._speech_manual_pos = None
        manager._speech_tail_side = "left"
        manager._speech_tail_vector = None
        manager._thought_manual_pos = None
        return manager

    def test_restore_accepts_legacy_position_and_normalizes_tail(self):
        manager = self._manager()
        with patch("overlay.bubble.bubble_manager.get_overlay_state", return_value={
            "bubble_positions": {"speech": {"x": "0.25", "y": "-0.5", "tail_side": "right", "tail_dx": 30, "tail_dy": -40}}
        }):
            manager._restore_manual_positions()
        self.assertEqual(manager._speech_manual_pos, (0.25, -0.5))
        self.assertEqual(manager._speech_tail_side, "right")
        self.assertEqual(manager._speech_tail_vector, (0.6, -0.8))

    def test_tail_only_roundtrip_does_not_require_a_body_position(self):
        manager = self._manager()
        manager._speech_tail_vector = (0.6, -0.8)
        state = {}
        with patch("overlay.bubble.bubble_manager.update_overlay_state", side_effect=lambda fn: fn(state)):
            manager._save_manual_positions()
        speech = state["bubble_positions"]["speech"]
        self.assertEqual((speech["tail_dx"], speech["tail_dy"]), (0.6, -0.8))
        self.assertNotIn("x", speech)
        restored = self._manager()
        with patch("overlay.bubble.bubble_manager.get_overlay_state", return_value=state):
            restored._restore_manual_positions()
        self.assertEqual(restored._speech_tail_vector, (0.6, -0.8))

    def test_tail_callback_does_not_change_body_position(self):
        manager = self._manager()
        manager._speech_manual_pos = (0.1, 0.2)
        saves = []
        manager._save_manual_positions = lambda: saves.append(True)
        manager._render_speech = lambda: None
        manager._on_speech_tail_drag(10, 0)
        self.assertEqual(manager._speech_manual_pos, (0.1, 0.2))
        self.assertEqual(manager._speech_tail_vector, (1.0, 0.0))
        self.assertEqual(saves, [])


class SpeechTailRenderingTests(unittest.TestCase):
    @staticmethod
    def _luminance(hex_color):
        value = hex_color.lstrip("#")
        red, green, blue = (int(value[idx:idx + 2], 16) for idx in (0, 2, 4))
        return red * 0.2126 + green * 0.7152 + blue * 0.0722

    def test_signal_glass_default_tokens_have_dark_readable_and_warm_roles(self):
        theme = shapes.DEFAULT_THEME
        self.assertLess(self._luminance(theme["speech_bg"]), 55)
        self.assertGreater(self._luminance(theme["speech_fg"]), 220)
        self.assertGreater(self._luminance(theme["echo_outline"]), self._luminance(theme["echo_bg"]))
        self.assertNotEqual(theme["echo_bg"], theme["input_bg"])

    def test_code_tokens_follow_dark_and_bright_theme_contrast(self):
        dark_bg, dark_fg = shapes.code_tokens("#1b2029", "#eef1f6", "#8878d7")
        light_bg, light_fg = shapes.code_tokens("#fffdf8", "#2d2a3d", "#6c5ce7")
        self.assertNotEqual(dark_bg, "#eef0f4")
        self.assertLess(self._luminance(dark_bg), 90)
        self.assertGreater(self._luminance(dark_fg) - self._luminance(dark_bg), 100)
        self.assertGreater(self._luminance(light_bg), 190)
        self.assertGreater(self._luminance(light_bg) - self._luminance(light_fg), 100)

    def test_signal_glass_flat_preserves_center_text_surface_and_bounded_shell(self):
        flat = bubble_image.build_bubble_flat(160, 80, 0.0, "#1b2029", "#8878d7", glow=True)
        self.assertEqual(flat.size, (196, 116))
        self.assertEqual(flat.getpixel((80, 58)), (27, 32, 41))
        # The chroma-key canvas remains transparent outside the bounded shell.
        self.assertEqual(flat.getpixel((0, 0)), (1, 1, 1))

    def test_pillow_tail_tip_is_rendered_with_theme_derived_pixels(self):
        flat = bubble_image.build_bubble_flat(160, 80, 0.0, "#20242b", "#536072", glow=False)
        tip = tail_handle_point(flat.width, flat.height, 0.0)
        self.assertNotEqual(flat.getpixel((round(tip[0]), round(tip[1]))), (1, 1, 1))

    def test_canvas_fallback_retains_the_tail_handle_ring(self):
        canvas = _Canvas()
        with patch("overlay.bubble.shapes.bubble_image.build_bubble_photo", side_effect=RuntimeError("no PIL")):
            shapes._place_bubble_image(canvas, 160, 80, 0.0, "#20242b", "#536072", radius=16)
        ovals = [call for call in canvas.calls if call[0] == "oval"]
        self.assertGreaterEqual(len(ovals), 2)
        self.assertEqual(ovals[-2][2]["outline"], shapes._lighten("#536072", 0.35))
        self.assertTrue(any(call[0] == "line" for call in canvas.calls))


if __name__ == "__main__":
    unittest.main()
