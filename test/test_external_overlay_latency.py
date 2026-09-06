"""Regressions for the external-overlay bubble latency and z-order design.

Each test maps to one acceptance criterion in
docs/dev/external-overlay-bubble-flow.md §6.
"""

import json
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

import overlay.config as config_module
from overlay.bubble.bubble_window import BubbleWindow
from overlay.event_api import MAX_OUTBOUND_MESSAGES, OverlayEventPublisher, _Client


class AsyncOverlayStateTests(unittest.TestCase):
    """D1 — geometry persistence must leave the Tk main thread."""

    def setUp(self):
        self.calls = []
        self._original_write = config_module._write_state_locked
        self._original_load = config_module._safe_load_yaml
        self._disk = {}

        def fake_write(state):
            self.calls.append(dict(state))
            self._disk = dict(state)

        def fake_load(path, *, strict=False):
            if path is config_module._STATE_PATH or path == config_module._STATE_PATH:
                return dict(self._disk)
            return self._original_load(path, strict=strict)

        config_module._write_state_locked = fake_write
        config_module._safe_load_yaml = fake_load
        self.addCleanup(self._restore)
        config_module.flush_overlay_state(5.0)
        with config_module._STATE_LOCK:
            config_module._STATE_PENDING.clear()
            config_module._STATE_INFLIGHT.clear()

    def _restore(self):
        config_module.flush_overlay_state(5.0)
        config_module._write_state_locked = self._original_write
        config_module._safe_load_yaml = self._original_load

    def test_queued_write_returns_immediately_and_coalesces(self):
        started = time.perf_counter()
        for index in range(100):
            config_module.update_overlay_state_async(
                lambda state, index=index: state.update({"overlay_window": {"x": index}})
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        # 100 synchronous writes cost seconds; queueing must stay negligible.
        self.assertLess(elapsed_ms, 50, f"queueing 100 updates took {elapsed_ms:.1f}ms")

        self.assertTrue(config_module.flush_overlay_state(5.0))
        self.assertLessEqual(len(self.calls), 3, f"expected coalescing, saw {len(self.calls)} writes")
        self.assertEqual(self.calls[-1]["overlay_window"]["x"], 99)

    def test_read_sees_queued_write_before_it_reaches_disk(self):
        config_module.update_overlay_state_async(
            lambda state: state.update({"overlay_window": {"x": 4321}})
        )
        # Deliberately read before the trailing coalesce window elapses.
        self.assertEqual(config_module.get_overlay_state()["overlay_window"]["x"], 4321)
        self.assertEqual(self.calls, [])
        self.assertTrue(config_module.flush_overlay_state(5.0))
        self.assertEqual(self.calls[-1]["overlay_window"]["x"], 4321)

    def test_synchronous_update_absorbs_queued_writes(self):
        config_module.update_overlay_state_async(
            lambda state: state.update({"launcher_window": {"x": 7}})
        )
        merged = config_module.update_overlay_state(
            lambda state: state.update({"overlay_window": {"x": 9}})
        )
        self.assertEqual(merged["launcher_window"]["x"], 7)
        self.assertEqual(merged["overlay_window"]["x"], 9)


class NonBlockingPublishTests(unittest.TestCase):
    """D2 — a renderer that stops reading must not stall or lose control state."""

    def _stalled_client(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        peer = socket.create_connection(("127.0.0.1", port))
        conn, _ = server.accept()
        conn.settimeout(1.0)
        self.addCleanup(server.close)
        self.addCleanup(peer.close)
        self.addCleanup(conn.close)
        # peer never calls recv, so the kernel buffer fills and stays full.
        return _Client(conn, "stalled", "Stalled", "replace", ("replace",), frozenset()), peer

    def test_publish_never_blocks_on_a_stalled_renderer(self):
        client, _peer = self._stalled_client()
        host = OverlayEventPublisher.__new__(OverlayEventPublisher)
        host._lock = threading.RLock()
        host._sequence = 0
        host._clients = {id(client): client}
        host._replace_owner = id(client)
        host._work_hint, host._tool_category = "idle", None
        host._generation_active = host._hovered = host._input_active = False

        worst_ms = 0.0
        for _ in range(1000):
            started = time.perf_counter()
            host.publish("generation.thinking", "thought", {})
            worst_ms = max(worst_ms, (time.perf_counter() - started) * 1000)
        self.assertLess(worst_ms, 25, f"publish blocked for {worst_ms:.1f}ms")

        client.close_outbound()

    def test_control_messages_are_kept_when_events_are_shed(self):
        # A connection that blocks unconditionally makes the backlog
        # deterministic instead of depending on the kernel's buffer size.
        blocked = threading.Event()
        self.addCleanup(blocked.set)
        connection = Mock()
        connection.sendall.side_effect = lambda _data: blocked.wait()
        client = _Client(connection, "wedged", "Wedged", "replace", ("replace",), frozenset())

        client.enqueue({"type": "overlay.set_position", "payload": {"x": 1}}, droppable=False)
        for index in range(MAX_OUTBOUND_MESSAGES * 2):
            client.enqueue({"type": "generation.thinking", "payload": {"n": index}}, droppable=True)

        with client._outbound_lock:
            queued = [json.loads(payload)["type"] for _droppable, payload in client._outbound]
        self.assertIn("overlay.set_position", queued)
        self.assertGreater(client.dropped_events, 0)
        self.assertLessEqual(len(queued), MAX_OUTBOUND_MESSAGES + 1)
        client.close_outbound()


class BubbleForegroundTopmostTests(unittest.TestCase):
    """D4 — the topmost hold follows real focus, not a timer."""

    def _bubble(self):
        root = Mock()
        bubble = BubbleWindow(root, lambda: True)
        bubble.win = Mock()
        bubble.win.winfo_exists.return_value = True
        bubble.win.state.return_value = "normal"
        return bubble

    def test_render_does_not_schedule_a_timed_release(self):
        bubble = self._bubble()
        bubble._raise_above_external_replace()
        bubble._root.after.assert_not_called()
        bubble.win.attributes.assert_called_with("-topmost", True)

    def test_foreground_loss_releases_and_return_reacquires(self):
        bubble = self._bubble()
        bubble._raise_above_external_replace()

        bubble.set_overlay_foreground(False)
        self.assertEqual(bubble.win.attributes.call_args.args, ("-topmost", False))

        bubble.set_overlay_foreground(True)
        self.assertEqual(bubble.win.attributes.call_args.args, ("-topmost", True))

    def test_render_while_backgrounded_stays_behind(self):
        bubble = self._bubble()
        bubble.set_overlay_foreground(False)
        bubble.win.attributes.reset_mock()
        bubble._raise_above_external_replace()
        for call in bubble.win.attributes.call_args_list:
            self.assertNotEqual(call.args, ("-topmost", True))

    def test_input_bar_hold_is_untouched_by_foreground_polling(self):
        root = Mock()
        bubble = BubbleWindow(root, lambda: True, keep_topmost=True)
        bubble.win = Mock()
        bubble.set_overlay_foreground(False)
        bubble.win.attributes.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class OverlayClickToStepBackTests(unittest.TestCase):
    """The answer bubble stays on top until the user clicks another window.

    Which window merely holds the foreground is not the signal: submitting from
    an editor destroys the input Entry and Windows hands the foreground back to
    that editor unasked. Only an actual button press means "bring that window
    forward".
    """

    def _app(self, foreground, clicked):
        from overlay.main import OverlayApp

        app = object.__new__(OverlayApp)
        app._quitting = True
        app.root = Mock()
        app._bubble_manager = Mock()
        app._bubble_input = Mock()
        app._overlay_foreground = True
        app._renderer_interaction_at = 0.0
        app._renderer_pid = 999
        app._overlay_events = Mock()
        app._overlay_events.replace_owner_pid.return_value = 999
        app._foreground_window = lambda: foreground[0]
        app._pointer_pressed_since_last_poll = lambda: clicked[0]
        return app

    def test_holds_while_the_user_works_elsewhere_without_clicking(self):
        editor, clicked = [(1111, 4242)], [False]
        app = self._app(editor, clicked)
        for _ in range(8):
            app._poll_overlay_foreground()
        self.assertTrue(app._overlay_foreground)
        app._bubble_manager.set_overlay_foreground.assert_not_called()

    def test_clicking_another_window_steps_back(self):
        editor, clicked = [(1111, 4242)], [False]
        app = self._app(editor, clicked)
        app._poll_overlay_foreground()
        self.assertTrue(app._overlay_foreground)

        clicked[0] = True
        app._poll_overlay_foreground()

        self.assertFalse(app._overlay_foreground)
        app._bubble_manager.set_overlay_foreground.assert_called_with(False, 1111)

    def test_clicking_the_window_already_in_front_still_steps_back(self):
        # The editor the user submitted from never leaves the foreground, so a
        # foreground-change test would miss this entirely.
        editor, clicked = [(1111, 4242)], [True]
        app = self._app(editor, clicked)
        app._poll_overlay_foreground()
        self.assertFalse(app._overlay_foreground)
        app._bubble_manager.set_overlay_foreground.assert_called_with(False, 1111)

    def test_repeated_clicks_keep_restacking(self):
        editor, clicked = [(1111, 4242)], [True]
        app = self._app(editor, clicked)
        app._poll_overlay_foreground()
        editor[0] = (2222, 4242)
        app._poll_overlay_foreground()
        self.assertEqual(
            [c.args for c in app._bubble_manager.set_overlay_foreground.call_args_list],
            [(False, 1111), (False, 2222)],
        )

    def test_clicking_the_overlay_brings_it_back(self):
        editor, clicked = [(1111, 4242)], [True]
        app = self._app(editor, clicked)
        app._poll_overlay_foreground()
        self.assertFalse(app._overlay_foreground)

        editor[0] = (3333, 999)  # the renderer's own window
        app._poll_overlay_foreground()
        self.assertTrue(app._overlay_foreground)
        app._bubble_manager.set_overlay_foreground.assert_called_with(True, 0)

    def test_unreadable_foreground_never_demotes(self):
        app = self._app([None], [True])
        app._poll_overlay_foreground()
        self.assertTrue(app._overlay_foreground)

    def test_renderer_interaction_grace_wins_over_a_click(self):
        import time as _time

        editor, clicked = [(1111, 4242)], [True]
        app = self._app(editor, clicked)
        app._renderer_pid = None
        app._overlay_events.replace_owner_pid.return_value = None
        app._renderer_interaction_at = _time.monotonic()
        app._poll_overlay_foreground()
        self.assertTrue(app._overlay_foreground)


class RestartPresentationTests(unittest.TestCase):
    """#2 — a restart returns to the presentation the user was in."""

    def _app(self):
        from overlay.main import OverlayApp

        app = object.__new__(OverlayApp)
        return app

    def test_note_is_one_shot(self):
        import overlay.main as main_module

        app = self._app()
        store = {"restore_presentation": "full"}
        with patch.object(main_module, "get_overlay_state", return_value=dict(store)), \
             patch.object(main_module, "update_overlay_state",
                          side_effect=lambda mutator: mutator(store)):
            self.assertEqual(app._consume_presentation_restore(), "full")
        self.assertNotIn("restore_presentation", store)

        with patch.object(main_module, "get_overlay_state", return_value=dict(store)), \
             patch.object(main_module, "update_overlay_state",
                          side_effect=lambda mutator: mutator(store)):
            self.assertIsNone(app._consume_presentation_restore())

    def test_unknown_note_is_ignored_but_still_cleared(self):
        import overlay.main as main_module

        app = self._app()
        store = {"restore_presentation": "nonsense"}
        with patch.object(main_module, "get_overlay_state", return_value=dict(store)), \
             patch.object(main_module, "update_overlay_state",
                          side_effect=lambda mutator: mutator(store)):
            self.assertIsNone(app._consume_presentation_restore())
        self.assertNotIn("restore_presentation", store)


class QuitWithdrawsFirstTests(unittest.TestCase):
    """#3 — the click is answered before the slow teardown runs."""

    def test_surfaces_are_hidden_before_teardown(self):
        from overlay.main import OverlayApp

        app = object.__new__(OverlayApp)
        app._bubble_input = Mock()
        app._bubble_manager = Mock()
        app._bubble_history = Mock()
        app.character = Mock()
        app.root = Mock()

        app._withdraw_visible_surfaces()

        app._bubble_input.hide.assert_called_once()
        app._bubble_manager.clear_all.assert_called_once()
        app._bubble_history.hide.assert_called_once()
        app.character.root.withdraw.assert_called_once()
        app.root.update_idletasks.assert_called_once()

    def test_one_failing_surface_does_not_stop_the_rest(self):
        from overlay.main import OverlayApp

        app = object.__new__(OverlayApp)
        app._bubble_input = Mock()
        app._bubble_input.hide.side_effect = RuntimeError("gone")
        app._bubble_manager = Mock()
        app._bubble_history = Mock()
        app.character = Mock()
        app.root = Mock()

        app._withdraw_visible_surfaces()

        app.character.root.withdraw.assert_called_once()


class StepBackRestacksTests(unittest.TestCase):
    """Releasing WS_EX_TOPMOST leaves the window above the activated app."""

    def test_release_also_restacks_below_the_activated_window(self):
        from overlay.bubble import bubble_window as bw

        root = Mock()
        bubble = bw.BubbleWindow(root, lambda: True)
        bubble.win = Mock()
        bubble.win.winfo_exists.return_value = True
        bubble.win.state.return_value = "normal"
        bubble._raise_above_external_replace()

        with patch.object(bw, "place_behind", return_value=True) as placed:
            bubble.set_overlay_foreground(False, below_hwnd=4242)

        self.assertEqual(bubble.win.attributes.call_args.args, ("-topmost", False))
        placed.assert_called_once_with(bubble.win, 4242)

    def test_repeated_activation_keeps_restacking(self):
        from overlay.bubble import bubble_window as bw

        root = Mock()
        bubble = bw.BubbleWindow(root, lambda: True)
        bubble.win = Mock()
        bubble.win.winfo_exists.return_value = True
        bubble.win.state.return_value = "normal"

        with patch.object(bw, "place_behind", return_value=True) as placed:
            bubble.set_overlay_foreground(False, below_hwnd=1)
            bubble.set_overlay_foreground(False, below_hwnd=2)
        # The second activation is not a state change, but it still has to
        # restack: that is exactly the "does not go behind again" report.
        self.assertEqual([c.args[1] for c in placed.call_args_list], [1, 2])

    def test_input_bar_follows_the_same_rule(self):
        from overlay.bubble.input_bar import InputBar

        bar = object.__new__(InputBar)
        bar._bubble = Mock()
        bar.set_overlay_foreground(False, 77)
        bar._bubble.set_overlay_foreground.assert_called_once_with(False, 77)


class RendererIdentityFromSocketTests(unittest.TestCase):
    """The renderer is identified by its connection, not by a lucky click."""

    def test_poll_resolves_the_owner_pid_when_unknown(self):
        from overlay.main import OverlayApp

        app = object.__new__(OverlayApp)
        app._quitting = True
        app.root = Mock()
        app._bubble_manager = Mock()
        app._bubble_input = Mock()
        app._overlay_foreground = True
        app._renderer_interaction_at = 0.0
        app._renderer_pid = None
        app._overlay_events = Mock()
        app._overlay_events.replace_owner_pid.return_value = 909
        app._foreground_window = lambda: (55, 909)   # the renderer owns the front
        app._pointer_pressed_since_last_poll = lambda: False

        app._poll_overlay_foreground()

        self.assertEqual(app._renderer_pid, 909)
        self.assertTrue(app._overlay_foreground)

    def test_port_owner_lookup_is_defensive(self):
        from overlay.event_api import _loopback_port_owner

        self.assertIsNone(_loopback_port_owner(0))

    def test_owner_pid_is_none_without_a_replace_owner(self):
        from overlay.event_api import OverlayEventPublisher

        host = OverlayEventPublisher.__new__(OverlayEventPublisher)
        host._lock = threading.RLock()
        host._clients = {}
        host._replace_owner = None
        self.assertIsNone(host.replace_owner_pid())


class PointerPressLatchTests(unittest.TestCase):
    """The click has to be latched: a 250ms poll would miss most presses."""

    def _app(self, states):
        from overlay.main import OverlayApp

        app = object.__new__(OverlayApp)
        app._quitting = True
        app.root = Mock()
        app._pointer_was_down = False
        app._pointer_press_pending = False
        self._states = list(states)
        return app

    def _run(self, app, states):
        import overlay.main as main_module

        user32 = Mock()
        seq = list(states)

        def get_state(_vk):
            return 0x8000 if seq[0] else 0

        user32.GetAsyncKeyState.side_effect = get_state
        with patch.object(main_module.ctypes, "windll") as windll:
            windll.user32 = user32
            for value in states:
                seq[0] = value
                app._watch_pointer_presses()

    def test_press_edge_is_latched_and_consumed_once(self):
        app = self._app([])
        self._run(app, [False, True, True, False])
        self.assertTrue(app._pointer_pressed_since_last_poll())
        self.assertFalse(app._pointer_pressed_since_last_poll())

    def test_held_button_latches_only_the_leading_edge(self):
        app = self._app([])
        self._run(app, [True, True, True])
        self.assertTrue(app._pointer_pressed_since_last_poll())
        self._run(app, [True, True])
        self.assertFalse(app._pointer_pressed_since_last_poll())

    def test_no_press_no_latch(self):
        app = self._app([])
        self._run(app, [False, False, False])
        self.assertFalse(app._pointer_pressed_since_last_poll())
