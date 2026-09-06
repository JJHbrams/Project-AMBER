import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from overlay.bubble.bubble_window import BubbleWindow
from overlay.bubble.bubble_manager import BubbleManager
from overlay.bubble.input_bar import InputBar
from overlay.main import _make_tray_icon


class InputBarTopmostTests(unittest.TestCase):
    def test_focus_handlers_are_bound_before_focus_and_acquire_then_release(self):
        bar = InputBar.__new__(InputBar)
        bar._root = Mock()
        bar._get_char_rect = lambda: (10, 20, 100, 120)
        bar._cfg = {}
        bar._terminal_cfg = {}
        bar._theme = {"input_bg": "#111", "speech_fg": "#eee"}
        bar._bubble = Mock()
        bar._bubble.ensure.return_value = Mock()
        bar._entry = None
        bar._on_submit = None
        bar._on_close = None
        bar._width_override = None
        bar._body_h = 0
        bar._input_active = False
        bar._input_idle_after_id = None
        bar._input_activity_epoch = 0
        bar._focus_epoch = 0
        bar._focus_verify_after_id = None
        bar._focus_topmost_requested = False
        bar._on_input_activity = Mock()
        bar._layout = Mock()
        entry = Mock()
        bar._root.focus_get.return_value = entry
        idle_callbacks = []
        bar._root.after_idle.side_effect = lambda callback: idle_callbacks.append(callback) or f"idle-{len(idle_callbacks)}"

        with patch("overlay.bubble.input_bar.tk.Entry", return_value=entry), patch(
            "overlay.bubble.input_bar.terminal_font_size", return_value=10
        ), patch("overlay.bubble.input_bar.geometry.default_bubble_width", return_value=180):
            bar.show(Mock())

        calls = entry.method_calls
        focus_in_index = next(i for i, call in enumerate(calls) if call.args[:1] == ("<FocusIn>",))
        focus_out_index = next(i for i, call in enumerate(calls) if call.args[:1] == ("<FocusOut>",))
        focus_set_index = next(i for i, call in enumerate(calls) if call[0] == "focus_set")
        self.assertLess(focus_in_index, focus_set_index)
        self.assertLess(focus_out_index, focus_set_index)

        focus_in = calls[focus_in_index].args[1]
        focus_out = calls[focus_out_index].args[1]
        focus_in(None)
        idle_callbacks[-1]()
        focus_out(None)
        bar._bubble.acquire_external_topmost.assert_called_once_with()
        bar._bubble.release_external_topmost.assert_called_once_with()

    def test_typing_activity_filters_keys_resets_idle_and_rejects_stale_callbacks(self):
        bar = InputBar.__new__(InputBar)
        bar._root = Mock()
        bar._root.after.side_effect = ["first", "second"]
        bar._on_input_activity = Mock()
        bar._input_active = False
        bar._input_idle_after_id = None
        bar._input_activity_epoch = 0
        bar._focus_epoch = 0
        bar._focus_verify_after_id = None
        bar._focus_topmost_requested = True

        for keysym, char in (("Shift_L", ""), ("Return", "\r"), ("Escape", "\x1b")):
            bar._on_key_press(SimpleNamespace(keysym=keysym, char=char))
        bar._on_input_activity.assert_not_called()

        bar._on_key_press(SimpleNamespace(keysym="a", char="a"))
        first_timeout = bar._root.after.call_args_list[-1].args[1]
        bar._on_input_activity.assert_called_once_with(True)
        bar._on_key_press(SimpleNamespace(keysym="Left", char=""))
        second_timeout = bar._root.after.call_args_list[-1].args[1]
        bar._root.after_cancel.assert_called_once_with("first")

        first_timeout()
        self.assertTrue(bar._input_active)
        self.assertEqual(bar._on_input_activity.call_args_list, [unittest.mock.call(True)])
        second_timeout()
        self.assertFalse(bar._input_active)
        self.assertEqual(
            bar._on_input_activity.call_args_list,
            [unittest.mock.call(True), unittest.mock.call(False)],
        )

    def test_focus_out_and_hide_end_activity_once_and_invalidate_old_timeout(self):
        bar = InputBar.__new__(InputBar)
        bar._root = Mock()
        bar._root.after.return_value = "idle"
        bar._on_input_activity = Mock()
        bar._input_active = False
        bar._input_idle_after_id = None
        bar._input_activity_epoch = 0
        bar._bubble = Mock()
        bar._entry = Mock()
        bar._on_submit = None
        bar._on_close = None
        bar._focus_epoch = 0
        bar._focus_verify_after_id = None
        bar._focus_topmost_requested = True

        bar._on_key_press(SimpleNamespace(keysym="BackSpace", char=""))
        stale_timeout = bar._root.after.call_args.args[1]
        bar._on_focus_out()
        bar.hide()
        stale_timeout()

        self.assertEqual(
            bar._on_input_activity.call_args_list,
            [unittest.mock.call(True), unittest.mock.call(False)],
        )
        bar._root.after_cancel.assert_called_once_with("idle")
        bar._bubble.release_external_topmost.assert_called_once_with()
        bar._bubble.hide.assert_called_once_with()

    def test_submit_ends_active_input_before_dispatch(self):
        order = []
        bar = InputBar.__new__(InputBar)
        bar._root = Mock()
        bar._on_input_activity = lambda active: order.append(("activity", active))
        bar._input_active = True
        bar._input_idle_after_id = "idle"
        bar._input_activity_epoch = 4
        bar._bubble = Mock(win=None)
        bar._entry = Mock()
        bar._entry.get.return_value = " hello "
        bar._on_submit = lambda text: order.append(("submit", text))
        bar._on_close = None
        bar._focus_epoch = 0
        bar._focus_verify_after_id = None
        bar._focus_topmost_requested = False

        bar._on_enter(None)

        self.assertEqual(order, [("activity", False), ("submit", "hello")])
        bar._root.after_cancel.assert_called_once_with("idle")

    def test_interactive_acquire_cancels_transient_timer_and_hide_releases(self):
        window = BubbleWindow.__new__(BubbleWindow)
        window.win = Mock()
        window._root = Mock()
        window._external_replace_active = lambda: True
        window._topmost_release_id = "stale-pulse"
        window._dismiss_after_id = None
        window._fade_after_id = None

        window.acquire_external_topmost()

        window._root.after_cancel.assert_called_once_with("stale-pulse")
        self.assertIsNone(window._topmost_release_id)
        self.assertEqual(window.win.attributes.call_args_list[-1].args, ("-topmost", True))
        window.win.lift.assert_called_once_with()

        window.hide()

        self.assertEqual(window.win.attributes.call_args_list[-1].args, ("-topmost", False))
        window.win.withdraw.assert_called_once_with()

    def test_every_bubble_takes_topmost_on_place_without_a_timed_release(self):
        """The 350ms pulse is gone and the input bar no longer waits for focus.

        Under a replace renderer the host never wins the OS foreground, so the
        input bar's focus-scoped hold never fired and it opened beneath other
        windows. Both kinds now raise on place and hold until the user clicks
        away; see docs/dev/external-overlay-bubble-flow.md.
        """
        for keep_topmost in (True, False):
            window = BubbleWindow.__new__(BubbleWindow)
            window.win = Mock()
            window._root = Mock()
            window._external_replace_active = lambda: True
            window._keep_topmost = keep_topmost
            window._topmost_release_id = None
            window._external_topmost_held = False
            window._overlay_foreground = True

            window._raise_above_external_replace()

            self.assertEqual(window.win.attributes.call_args_list[-1].args, ("-topmost", True))
            window._root.after.assert_not_called()

    def test_backgrounded_bubble_does_not_take_topmost_on_place(self):
        window = BubbleWindow.__new__(BubbleWindow)
        window.win = Mock()
        window._root = Mock()
        window._external_replace_active = lambda: True
        window._keep_topmost = False
        window._topmost_release_id = None
        window._external_topmost_held = False
        window._overlay_foreground = False

        window._raise_above_external_replace()

        for call in window.win.attributes.call_args_list:
            self.assertNotEqual(call.args, ("-topmost", True))

    def test_stale_focus_callback_and_bundled_release_never_touch_topmost(self):
        bar = InputBar.__new__(InputBar)
        current, stale = Mock(), Mock()
        bar._entry = current
        bar._focus_epoch = 4
        bar._focus_verify_after_id = None
        bar._root = Mock()
        bar._bubble = Mock()

        bar._on_focus_in(None, stale, 3)
        bar._verify_focus(stale, 3, 0)
        bar._bubble.acquire_external_topmost.assert_not_called()

        window = BubbleWindow.__new__(BubbleWindow)
        window.win = Mock()
        window._root = Mock()
        window._external_replace_active = lambda: False
        window._external_topmost_held = False
        window._topmost_release_id = None
        window.release_external_topmost()
        window.win.attributes.assert_not_called()


class BubbleInputAndStreamingTests(unittest.TestCase):
    def test_begin_input_clears_echo_only(self):
        manager = BubbleManager.__new__(BubbleManager)
        manager._echo = Mock()
        manager._echo_text = "old input"
        manager._echo_rect = (1, 2, 3, 4, "left")
        manager._echo_anchor_offset = (1, 2)
        manager._speech_text = "keep response"

        manager.begin_input()

        self.assertEqual(manager._echo_text, "")
        self.assertIsNone(manager._echo_rect)
        self.assertIsNone(manager._echo_anchor_offset)
        self.assertEqual(manager._speech_text, "keep response")
        manager._echo.cancel_dismiss.assert_called_once_with()
        manager._echo.hide.assert_called_once_with()

    def test_stream_deltas_coalesce_to_one_idle_render_and_stale_callback_is_safe(self):
        manager = BubbleManager.__new__(BubbleManager)
        callbacks = []
        manager._root = Mock()
        manager._root.after_idle.side_effect = lambda callback: callbacks.append(callback) or "render-idle"
        manager._speech_render_after_id = None
        manager._speech_render_token = None
        manager._render_speech_now = Mock()

        manager._schedule_speech_render()
        manager._schedule_speech_render()
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        manager._render_speech_now.assert_called_once_with()

        manager._schedule_speech_render()
        stale = callbacks[-1]
        manager._render_speech()
        stale()
        self.assertEqual(manager._render_speech_now.call_count, 2)

    def test_outer_geometry_commits_before_live_content_is_remapped(self):
        order = []
        window = BubbleWindow.__new__(BubbleWindow)
        window.win = Mock()
        window.canvas = Mock()
        window.win.geometry.side_effect = lambda *_: order.append("outer")
        window.canvas.pack.side_effect = lambda *_: order.append("content")

        window.suspend_content()
        window.commit_size(200, 100)

        self.assertEqual(order, ["outer", "content"])

    def test_failed_live_measurement_remaps_existing_content_and_propagates(self):
        manager = BubbleManager.__new__(BubbleManager)
        manager._speech = Mock()
        manager._speech.ensure.return_value = Mock()
        manager._speech_dismissed = False
        manager._speech_text = "visible response"
        manager._get_char_rect = lambda: (10, 20, 100, 120)
        manager._speech_width_override = None
        manager._speech_max_h_override = None
        manager._speech_color_override = None
        manager._theme = {"speech_fg": "white", "speech_bg": "black", "speech_outline": "gray"}
        manager._default_width = Mock(return_value=180)
        manager._font_family = Mock(return_value="Arial")
        manager._font_size = Mock(return_value=10)
        manager._max_body_h = Mock(return_value=200)

        with patch("overlay.bubble.bubble_manager.shapes.draw_speech_bubble", side_effect=RuntimeError("measure failed")):
            with self.assertRaisesRegex(RuntimeError, "measure failed"):
                manager._render_speech_now()

        manager._speech.suspend_content.assert_called_once_with()
        manager._speech.resume_content.assert_called_once_with()
        manager._speech.place.assert_not_called()

    def test_post_measure_layout_failure_also_remaps_existing_content(self):
        manager = BubbleManager.__new__(BubbleManager)
        manager._speech = Mock()
        manager._speech.ensure.return_value = Mock()
        manager._speech_dismissed = False
        manager._speech_text = "visible response"
        manager._get_char_rect = lambda: (10, 20, 100, 120)
        manager._speech_width_override = None
        manager._speech_max_h_override = None
        manager._speech_color_override = None
        manager._theme = {"speech_fg": "white", "speech_bg": "black", "speech_outline": "gray"}
        manager._default_width = Mock(return_value=180)
        manager._font_family = Mock(return_value="Arial")
        manager._font_size = Mock(return_value=10)
        manager._max_body_h = Mock(return_value=200)
        manager._nudge_row_height = Mock(side_effect=RuntimeError("layout failed"))

        with patch("overlay.bubble.bubble_manager.shapes.draw_speech_bubble", return_value=(200, 100)):
            with self.assertRaisesRegex(RuntimeError, "layout failed"):
                manager._render_speech_now()

        manager._speech.resume_content.assert_called_once_with()


class TrayLauncherDefaultTests(unittest.TestCase):
    def _make_app(self, hidden: bool):
        app = Mock()
        app._launcher_hidden = hidden
        app._ollama_model = ""
        app.get_cli_provider.return_value = "copilot"
        app.get_cli_model.return_value = "auto"
        return app

    def test_launcher_restore_is_default_only_while_hidden_and_dispatches_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            icon_path = Path(tmp) / "icon.png"
            Image.new("RGBA", (2, 2)).save(icon_path)
            app = self._make_app(hidden=True)
            with patch("overlay.main._resolve_icon_path", return_value=icon_path), patch(
                "overlay.main.provider_models", return_value=[]
            ):
                tray = _make_tray_icon(app)

            items = list(tray.menu)
            restore = items[0]
            self.assertEqual(restore.text, "런처 표시")
            self.assertTrue(restore.visible)
            self.assertTrue(restore.default)
            self.assertEqual(items[1].text, "채팅 열기/닫기")

            restore(None)
            app.root.after.assert_called_once_with(0, app.show_launcher_from_tray)

            app._launcher_hidden = False
            self.assertFalse(restore.visible)
            self.assertFalse(restore.default)


if __name__ == "__main__":
    unittest.main()
