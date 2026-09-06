import io
import json
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import overlay.character as character_module
import overlay.main as main_module
from overlay.character import CharacterOverlay, _clamp_menu_geometry, _is_widget_descendant, _menu_entry_is_selected, _point_outside_rect, launcher_tooltip_position, presentation_menu_action
from overlay.event_api import DISPLAY_HINTS, OverlayEventPublisher, event_for_bubble, tool_category
from overlay.main import OverlayApp
from overlay.bubble.bubble_manager import BubbleManager
from overlay.settings_window import OVERLAY_EVENT_API_MANUAL_URL, open_overlay_event_api_manual


class OverlayEventApiTests(unittest.TestCase):
    def setUp(self):
        # Motion-driven persistence is queued now. Route it back through the
        # synchronous name so every existing persistence contract below keeps
        # asserting the same thing against whatever each test patches in.
        patcher = patch(
            "overlay.character.update_overlay_state_async",
            side_effect=lambda mutator: character_module.update_overlay_state(mutator),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_regular_input_clears_echo_before_replay_without_reviving_it(self):
        app = object.__new__(OverlayApp)
        app._bubble_manager = Mock()
        app._bubble_input = Mock()
        app._bubble_input.is_showing.return_value = False
        app._ensure_bubble_session = Mock()

        app._toggle_bubble_input()

        app._bubble_manager.begin_input.assert_called_once_with()
        app._bubble_manager.replay_last.assert_called_once_with(include_echo=False)
        app._bubble_input.show.assert_called_once_with(on_submit=app._on_bubble_submit)

    def test_nudge_input_also_clears_previous_echo(self):
        app = object.__new__(OverlayApp)
        app._bubble_manager = Mock()
        app._bubble_input = Mock()
        app._bubble_input.is_showing.return_value = False
        app._initiative = Mock()
        app._initiative.active_nudge_text.return_value = "hello"
        app._initiative.has_pending_outcome.return_value = True
        app._ensure_bubble_session = Mock()

        app._engage_nudge()

        app._bubble_manager.begin_input.assert_called_once_with()
        app._bubble_input.show.assert_called_once()

    def test_hello_capability_is_recorded_without_changing_old_renderers(self):
        publisher = OverlayEventPublisher({"overlay": {"external_renderer": {"command": ["renderer.exe"], "mode": "replace"}}})
        self.assertFalse(publisher.supports("overlay.set_size"))
        publisher.capabilities = frozenset({"overlay.set_size"})
        self.assertTrue(publisher.supports("overlay.set_size"))

    def test_replace_size_uses_bundled_dimensions_not_stale_external_rect(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app._overlay_events.supports.return_value = True
        app.character = Mock()
        app.character.get_phys_rect.return_value = (9, 9, 270, 302)
        app.character.get_bundled_phys_rect.return_value = (1, 2, 400, 500)
        app._publish_external_size("config_reload")
        app._overlay_events.publish.assert_called_once_with(
            "overlay.set_size", "idle", {"width": 400, "height": 500}
        )

    def test_launcher_and_full_presentation_records_use_separate_state_keys(self):
        overlay = object.__new__(CharacterOverlay)
        overlay._launcher_canvas = Mock()
        overlay._full_rect = (100, 200, 270, 302)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = 400
        overlay.root.winfo_y.return_value = 500
        overlay._label = Mock()
        state = {"launcher_window": {"x": 400, "y": 500}}
        with patch("overlay.character.get_overlay_state", return_value=state), \
             patch("overlay.character.update_overlay_state", side_effect=lambda fn: fn(state)), \
             patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(0, 0, 800, 800)), \
             patch("overlay.character.bubble_geometry.clamp_rect", side_effect=lambda x, y, w, h, _work: (x, y)):
            overlay.show_full()
        self.assertEqual(state["launcher_window"], {"x": 400, "y": 500})
        self.assertEqual(state["overlay_window"]["x"], 291)

    def test_launcher_drag_persists_only_launcher_without_reloading_full_art(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = 140
        overlay.root.winfo_y.return_value = 240
        overlay._launcher_moved = True
        overlay._launcher_press = (100, 200)
        overlay._full_rect = (10, 20, 300, 400)
        state = {"overlay_window": {"x": 10, "y": 20, "width": 300, "height": 400}}
        with (
            patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(0, 0, 800, 800)),
            patch("overlay.character.bubble_geometry.clamp_rect", side_effect=lambda x, y, *_args: (x, y)),
            patch("overlay.character.update_overlay_state", side_effect=lambda update: update(state)),
        ):
            overlay._on_launcher_release(Mock())
        self.assertEqual(state["overlay_window"], {"x": 10, "y": 20, "width": 300, "height": 400})
        self.assertEqual(state["launcher_window"]["x"], 140)
        self.assertIn("52x52+140+240", overlay.root.geometry.call_args.args)

    def test_launcher_click_and_keyboard_activate_expand_without_drag(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.on_expand = Mock()
        overlay.on_activate = Mock()
        overlay._launcher_moved = False
        overlay._on_launcher_release(Mock())
        overlay._activate_launcher()
        self.assertEqual(overlay.on_expand.call_count, 2)
        overlay.on_activate.assert_not_called()

    def test_launcher_canvas_uses_native_purple_chat_button_visual_tokens(self):
        source = Path(__file__).resolve().parents[1].joinpath("overlay", "character.py").read_text(encoding="utf-8")
        launcher = source[source.index("    def show_launcher("):source.index("    def show_full(")]
        for token in ("#5b3db7", "launcher-shadow", "launcher-glyph", "create_polygon", "_set_launcher_hover"):
            self.assertIn(token, launcher)
        self.assertIn("#7659cf", source)

    def test_external_presentation_show_keeps_launcher_until_visible_ack(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app._overlay_events.supports.return_value = True
        app.character = Mock()
        app.character.launcher_full_target.return_value = (101, 202, 303, 404)
        app.character.get_presentation_rect.return_value = (999, 888, 303, 404)
        app._set_presentation("full")
        self.assertEqual(app._overlay_events.publish.call_args_list[0].args, ("overlay.set_position", "idle", {"x": 101, "y": 202}))
        self.assertEqual(app._overlay_events.publish.call_args_list[1].args, ("overlay.show", "idle", {}))

    def test_external_full_target_keeps_acknowledged_size_not_bundled_size(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app._overlay_events.supports.return_value = True
        app.character = Mock()
        app.character.get_presentation_rect.return_value = (1, 2, 270, 302)
        app.character.launcher_full_target.return_value = (400, 198, 270, 302)
        app._set_presentation("full")
        app.character.launcher_full_target.assert_called_once_with((270, 302))
        self.assertEqual(app._overlay_events.publish.call_args_list[0].args, ("overlay.set_position", "idle", {"x": 400, "y": 198}))

    def test_launcher_hide_latches_and_publishes_before_host_ui_work_and_is_idempotent(self):
        order = []
        app = object.__new__(OverlayApp)
        app._presentation_mode = "full"
        app._external_renderer_hiding = False
        app.root = Mock()
        app.root.after.return_value = "hide-timeout"
        app._overlay_events = Mock(mode="replace")
        app._overlay_events.supports.return_value = True
        app._overlay_events.publish.side_effect = lambda *args: order.append(("publish",) + args)
        app.character = Mock()
        app.character.snapshot_launcher_anchor.side_effect = lambda: order.append(("snapshot",))
        app.character.restore_bundled_renderer.side_effect = lambda: order.append(("restore",))
        app.character.show_launcher.side_effect = lambda: order.append(("show",))
        app._bubble_manager = Mock()
        app._bubble_manager.defer_nudge.side_effect = lambda: order.append(("defer",)) or True
        app._initiative = Mock()
        app._initiative.defer_active.side_effect = lambda: order.append(("defer_active",))

        app._set_presentation("launcher")

        self.assertTrue(app._external_renderer_hiding)
        self.assertEqual(order[0], ("publish", "overlay.hide", "idle", {}))
        self.assertEqual(order[1:], [("snapshot",), ("defer",), ("defer_active",)])
        app.character.restore_bundled_renderer.assert_not_called()
        app.character.show_launcher.assert_not_called()
        app._set_presentation("launcher")
        self.assertEqual(len(order), 4)

        app._handle_external_renderer_message({
            "schema_version": 2,
            "type": "overlay.visibility_changed",
            "payload": {"visible": False},
            "_renderer": {"id": "engram.rabbit-2d", "mode": "replace"},
        })
        app.root.after_cancel.assert_called_once_with("hide-timeout")
        self.assertEqual(order[-2:], [("restore",), ("show",)])

    def test_missing_hidden_ack_reveals_launcher_after_timeout_but_keeps_pointer_latched(self):
        callbacks = []
        app = object.__new__(OverlayApp)
        app._presentation_mode = "full"
        app._external_renderer_hiding = False
        app._launcher_hidden = False
        app._overlay_events = Mock(mode="replace")
        app._overlay_events.supports.return_value = True
        app.character = Mock()
        app.root = Mock()
        app.root.after.side_effect = lambda _ms, callback: callbacks.append(callback) or "timeout"
        app._bubble_manager = Mock()
        app._bubble_manager.defer_nudge.return_value = False

        app._set_presentation("launcher")
        app.character.show_launcher.assert_not_called()
        callbacks[0]()

        app.character.restore_bundled_renderer.assert_called_once_with()
        app.character.show_launcher.assert_called_once_with()
        self.assertTrue(app._external_renderer_hiding)

    def test_hiding_latch_suppresses_pointer_until_authoritative_hidden_ack(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app._overlay_events.supports.return_value = True
        app.character = Mock()
        app._presentation_mode = "launcher"
        app._launcher_hidden = False
        app._external_renderer_visible = True
        app._external_renderer_hiding = True

        for action in ("pointer_enter", "pointer_leave", "drag_move", "drag_end", "left_click", "right_click"):
            app._handle_external_renderer_message({
                "schema_version": 1,
                "type": "pointer.action",
                "payload": {"action": action, "screen_x": 10, "screen_y": 20},
            })
        self.assertEqual(app.character.method_calls, [])
        app._overlay_events.publish.assert_not_called()

        app._handle_external_renderer_message({
            "schema_version": 1,
            "type": "overlay.visibility_changed",
            "payload": {"visible": False},
        })
        self.assertFalse(app._external_renderer_hiding)
        app._handle_external_renderer_message({
            "schema_version": 1,
            "type": "pointer.action",
            "payload": {"action": "pointer_enter"},
        })
        app._overlay_events.publish.assert_called_once_with("pointer.entered", "hover")

    def test_input_activity_publishes_metadata_and_updates_bundled_override(self):
        app = object.__new__(OverlayApp)
        app.character = Mock()
        app._overlay_events = Mock()

        app._on_bubble_input_activity(True)
        app._on_bubble_input_activity(False)

        self.assertEqual(app.character.set_input_active.call_args_list, [unittest.mock.call(True), unittest.mock.call(False)])
        self.assertEqual(
            app._overlay_events.publish.call_args_list,
            [
                unittest.mock.call("conversation.input_active", "input"),
                unittest.mock.call("conversation.input_idle", "idle"),
            ],
        )

    def test_submit_clears_input_override_then_preserves_submission_generation_order(self):
        app = object.__new__(OverlayApp)
        app.character = Mock()
        app._overlay_events = Mock()
        app._bubble_turn_active = False
        app._nudge_awaiting_reply = False
        app._bubble_manager = Mock()
        app._bubble_input = Mock()
        app._bubble_input.get_last_rect.return_value = None
        app._bubble_session = None

        app._on_bubble_submit("hello")

        self.assertEqual(
            app.character.method_calls,
            [unittest.mock.call.set_input_active(False), unittest.mock.call.set_sprite_state("generating")],
        )
        self.assertEqual(
            app._overlay_events.publish.call_args_list,
            [
                unittest.mock.call("conversation.input_submitted", "input"),
                unittest.mock.call("generation.started", "generating"),
            ],
        )
        self.assertTrue(app._bubble_turn_active)

    def test_launcher_right_click_uses_nonmodal_external_menu_and_stops_native_propagation(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.external_context_menu = Mock()
        event = Mock(x_root=30, y_root=40)
        self.assertEqual(overlay._on_launcher_context_menu(event), "break")
        overlay.external_context_menu.assert_called_once_with(30, 40)

    def test_presentation_context_action_is_state_aware_and_never_quits(self):
        self.assertEqual(
            presentation_menu_action(False, can_collapse=True, can_hide_to_tray=True),
            ("런처로 접기", "collapse"),
        )
        self.assertEqual(
            presentation_menu_action(True, can_collapse=True, can_hide_to_tray=True),
            ("트레이로 숨기기", "hide_to_tray"),
        )
        self.assertIsNone(presentation_menu_action(True, can_collapse=True, can_hide_to_tray=False))

    def test_bundled_and_renderer_requests_share_custom_menu_presentation(self):
        source = Path(__file__).resolve().parents[1].joinpath("overlay", "character.py").read_text(encoding="utf-8")
        body = source[source.index("    def _show_context_menu("):source.index("    def _pointer_screen_position(")]
        self.assertIn("self.external_context_menu", body)
        self.assertNotIn(".post(", body)
        self.assertNotIn("tk_popup", body)
        self.assertEqual(
            presentation_menu_action(True, can_collapse=True, can_hide_to_tray=True),
            ("트레이로 숨기기", "hide_to_tray"),
        )
        self.assertEqual(
            presentation_menu_action(False, can_collapse=True, can_hide_to_tray=True),
            ("런처로 접기", "collapse"),
        )

    def test_tray_hiding_and_restoring_launcher_do_not_stop_backend(self):
        app = object.__new__(OverlayApp)
        app._presentation_mode = "launcher"
        app._launcher_hidden = False
        app.character = Mock()
        app.root = Mock()
        app.tray = Mock()

        app.hide_launcher_to_tray()
        self.assertTrue(app._launcher_hidden)
        app.character._hide_launcher_tooltip.assert_called_once_with()
        app.root.withdraw.assert_called_once_with()
        app.tray.update_menu.assert_called_once_with()

        app.show_launcher_from_tray()
        self.assertFalse(app._launcher_hidden)
        app.character.restore_bundled_renderer.assert_called_once_with()
        app.character.show_launcher.assert_called_once_with()
        self.assertEqual(app.tray.update_menu.call_count, 2)

    def test_launcher_full_target_centers_on_launcher_and_clamps_monitor(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = -180
        overlay.root.winfo_y.return_value = 10
        overlay.get_bundled_phys_rect = Mock(return_value=(999, 999, 300, 400))
        with patch("overlay.character.get_overlay_state", return_value={}), \
             patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(-200, 0, 800, 600)), \
             patch("overlay.character.bubble_geometry.clamp_rect", side_effect=lambda x, y, w, h, _work: (max(-200, x), max(0, y))):
            self.assertEqual(overlay.launcher_full_target(), (-200, 0, 300, 400))

    def test_launcher_move_shifts_full_by_same_delta_while_preserving_offset(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.get_bundled_phys_rect = Mock(return_value=(900, 800, 270, 302))
        state = {"overlay_window": {"launcher_offset_x": 40, "launcher_offset_y": -20}}
        with patch("overlay.character.get_overlay_state", return_value=state), \
             patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(0, 0, 2000, 1200)), \
             patch("overlay.character.bubble_geometry.clamp_rect", side_effect=lambda x, y, *_args: (x, y)):
            overlay.root.winfo_x.return_value = 100
            overlay.root.winfo_y.return_value = 500
            first = overlay.launcher_full_target()
            overlay.root.winfo_x.return_value = 130
            overlay.root.winfo_y.return_value = 550
            second = overlay.launcher_full_target()
        self.assertEqual(first, (31, 204, 270, 302))
        self.assertEqual(second, (61, 254, 270, 302))

    def test_cross_monitor_offset_clamps_against_preferred_full_center(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = -69
        overlay.root.winfo_y.return_value = 2288
        overlay.get_bundled_phys_rect = Mock(return_value=(999, 999, 270, 302))
        state = {"overlay_window": {"launcher_offset_x": 178, "launcher_offset_y": -202}}
        with patch("overlay.character.get_overlay_state", return_value=state), \
             patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(0, 1080, 1920, 1080)) as work_rect, \
             patch("overlay.character.bubble_geometry.clamp_rect", side_effect=lambda x, y, *_args: (x, y)):
            self.assertEqual(overlay.launcher_full_target(), (0, 1810, 270, 302))
        work_rect.assert_called_once_with(135, 1961)

    def test_legacy_or_malformed_launcher_offset_uses_nominal_zero(self):
        overlay = object.__new__(CharacterOverlay)
        for record in (
            {},
            {"launcher_offset_x": 1},
            {"launcher_offset_x": "2", "launcher_offset_y": 3},
            {"launcher_offset_x": True, "launcher_offset_y": 3},
            {"launcher_offset_x": 1_000_001, "launcher_offset_y": 0},
        ):
            with self.subTest(record=record), patch(
                "overlay.character.get_overlay_state", return_value={"overlay_window": record}
            ):
                self.assertEqual(overlay._saved_launcher_offset(), (0, 0))

    def test_clamped_reopen_does_not_overwrite_preferred_offset(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = 900
        overlay.root.winfo_y.return_value = 700
        overlay._launcher_canvas = Mock()
        overlay._launcher_tooltip = None
        overlay._label = Mock()
        overlay._full_rect = (100, 200, 300, 400)
        overlay.get_bundled_phys_rect = Mock(return_value=(0, 0, 300, 400))
        state = {"overlay_window": {"launcher_offset_x": 250, "launcher_offset_y": -80, "keep": "yes"}}
        with patch("overlay.character.get_overlay_state", return_value=state), \
             patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(0, 0, 1000, 800)), \
             patch("overlay.character.bubble_geometry.clamp_rect", return_value=(700, 400)), \
             patch("overlay.character.update_overlay_state", side_effect=lambda update: update(state)):
            overlay.show_full()
        self.assertEqual(state["overlay_window"]["x"], 700)
        self.assertEqual(state["overlay_window"]["y"], 400)
        self.assertEqual(state["overlay_window"]["launcher_offset_x"], 250)
        self.assertEqual(state["overlay_window"]["launcher_offset_y"], -80)
        self.assertEqual(state["overlay_window"]["keep"], "yes")

    def test_drag_offset_is_bounded_and_preserves_overlay_state(self):
        overlay = object.__new__(CharacterOverlay)
        overlay._launcher_expand_anchor = (100, 500)
        state = {"overlay_window": {"x": 31, "y": 204, "keep": "yes"}}
        with patch("overlay.character.update_overlay_state", side_effect=lambda update: update(state)):
            self.assertEqual(overlay.remember_launcher_relative_offset(31, 204, 270, 302), (40, -20))
            self.assertEqual(
                overlay.remember_launcher_relative_offset(9_000_000, -9_000_000, 270, 302),
                (1_000_000, -1_000_000),
            )
        self.assertEqual(state["overlay_window"]["keep"], "yes")
        self.assertEqual(state["overlay_window"]["x"], 31)

    def test_full_drag_offset_round_trips_after_collapse_and_reopen(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = 100
        overlay.root.winfo_y.return_value = 500
        overlay._launcher_expand_anchor = (100, 500)
        overlay.get_bundled_phys_rect = Mock(return_value=(0, 0, 270, 302))
        state = {"launcher_window": {"x": 100, "y": 500}}
        with patch("overlay.character.get_overlay_state", return_value=state), \
             patch("overlay.character.update_overlay_state", side_effect=lambda update: update(state)), \
             patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(0, 0, 1200, 900)), \
             patch("overlay.character.bubble_geometry.clamp_rect", side_effect=lambda x, y, *_args: (x, y)):
            self.assertEqual(overlay.remember_launcher_relative_offset(31, 204, 270, 302), (40, -20))
            self.assertEqual(overlay.snapshot_launcher_anchor(), (100, 500))
            overlay.capture_launcher_expand_anchor()
            self.assertEqual(overlay.launcher_full_target(), (31, 204, 270, 302))

    def test_first_run_without_launcher_state_seeds_inverse_anchor(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.get_phys_rect = Mock(return_value=(-100, 200, 270, 302))
        state = {}
        with patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(-200, 0, 400, 500)), \
             patch("overlay.character.bubble_geometry.clamp_rect", side_effect=lambda x, y, *_args: (x, y)), \
             patch("overlay.character.get_overlay_state", return_value=state), \
             patch("overlay.character.update_overlay_state", side_effect=lambda update: update(state)):
            self.assertEqual(overlay.snapshot_launcher_anchor(), (9, 476))
        self.assertEqual(state["launcher_window"]["x"], 9)

    def test_clamp_on_open_does_not_change_exact_launcher_collapse_anchor(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = -190
        overlay.root.winfo_y.return_value = 40
        overlay.capture_launcher_expand_anchor()
        # Full target is deliberately clamped away from its launcher anchor.
        overlay.get_phys_rect = Mock(return_value=(-200, 0, 300, 400))
        state = {}
        with patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(-200, 0, 400, 600)), \
             patch("overlay.character.update_overlay_state", side_effect=lambda update: update(state)):
            self.assertEqual(overlay.snapshot_launcher_anchor(), (-190, 40))
        self.assertEqual((state["launcher_window"]["x"], state["launcher_window"]["y"]), (-190, 40))

    def test_full_drag_preserves_captured_launcher_on_negative_monitor(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = -190
        overlay.root.winfo_y.return_value = 40
        overlay.capture_launcher_expand_anchor()
        # Moving the full presentation is persisted independently and must not
        # influence the launcher captured before expansion.
        overlay.get_phys_rect = Mock(return_value=(-180, 120, 270, 302))
        state = {}
        with patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(-200, 0, 400, 600)), \
             patch("overlay.character.bubble_geometry.clamp_rect", side_effect=lambda x, y, *_args: (x, y)), \
             patch("overlay.character.update_overlay_state", side_effect=lambda update: update(state)):
            self.assertEqual(overlay.snapshot_launcher_anchor(), (-190, 40))
        self.assertEqual((state["launcher_window"]["x"], state["launcher_window"]["y"]), (-190, 40))

    def test_captured_launcher_is_clamped_and_persisted_after_monitor_topology_change(self):
        overlay = object.__new__(CharacterOverlay)
        overlay._launcher_expand_anchor = (-900, 700)
        state = {}
        with patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(-200, 0, 400, 600)), \
             patch("overlay.character.bubble_geometry.clamp_rect", return_value=(-200, 548)) as clamp, \
             patch("overlay.character.update_overlay_state", side_effect=lambda update: update(state)):
            self.assertEqual(overlay.snapshot_launcher_anchor(), (-200, 548))
        clamp.assert_called_once_with(-900, 700, 52, 52, (-200, 0, 400, 600))
        self.assertEqual(
            state["launcher_window"],
            {"x": -200, "y": 548, "width": 52, "height": 52, "work_area": [-200, 0, 400, 600]},
        )

    def test_external_full_drag_persists_only_full_geometry(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app._presentation_mode = "full"
        app.character = Mock()
        app.character.get_phys_rect.return_value = (10, 20, 270, 302)
        app.character.apply_external_geometry.return_value = (30, 40, 270, 302)
        app._cancel_pending_external_menu_activation = Mock()
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action",
            "payload": {"action": "drag_end", "screen_x": 30, "screen_y": 40},
        })
        app.character.remember_launcher_relative_offset.assert_called_once_with(30, 40, 270, 302)
        self.assertEqual(app._overlay_events.publish.call_args.args, ("overlay.set_position", "idle", {"x": 30, "y": 40}))

    def test_bundled_full_drag_persists_overlay_without_changing_launcher(self):
        overlay = object.__new__(CharacterOverlay)
        overlay._moved = True
        overlay._img_w = 270
        overlay._img_h = 302
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = 600
        overlay.root.winfo_y.return_value = 100
        def reload_for_monitor():
            overlay._img_w = 300
            overlay._img_h = 400
        overlay._reload_image_for_current_monitor = Mock(side_effect=reload_for_monitor)
        overlay._emit_pointer_event = Mock()
        state = {"launcher_window": {"x": 125, "y": 350, "width": 52, "height": 52}}
        with patch("overlay.character.get_overlay_state", return_value=state), \
             patch("overlay.character.update_overlay_state", side_effect=lambda update: update(state)), \
             patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(0, 0, 1000, 800)):
            overlay._on_release(Mock())
        self.assertEqual((state["launcher_window"]["x"], state["launcher_window"]["y"]), (125, 350))
        self.assertEqual((state["overlay_window"]["x"], state["overlay_window"]["y"]), (600, 100))
        self.assertEqual(
            (state["overlay_window"]["launcher_offset_x"], state["overlay_window"]["launcher_offset_y"]),
            (599, 124),
        )
        overlay._emit_pointer_event.assert_called_once_with(
            "drag_end", {"screen_x": 600, "screen_y": 100}
        )

    def test_repeated_collapse_uses_persisted_launcher_without_drift(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = 125
        overlay.root.winfo_y.return_value = 350
        overlay.capture_launcher_expand_anchor()
        state = {}
        with patch("overlay.character.get_overlay_state", return_value=state), \
             patch("overlay.character.update_overlay_state", side_effect=lambda update: update(state)), \
             patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(0, 0, 800, 600)), \
             patch("overlay.character.bubble_geometry.clamp_rect", side_effect=lambda x, y, *_args: (x, y)):
            self.assertEqual(overlay.snapshot_launcher_anchor(), (125, 350))
            overlay.get_phys_rect = Mock(return_value=(500, 100, 270, 302))
            self.assertEqual(overlay.snapshot_launcher_anchor(), (125, 350))
        self.assertEqual((state["launcher_window"]["x"], state["launcher_window"]["y"]), (125, 350))

    def test_persisted_launcher_is_safely_clamped_on_negative_monitor(self):
        overlay = object.__new__(CharacterOverlay)
        overlay._launcher_expand_anchor = None
        state = {"launcher_window": {"x": -500, "y": 700}}
        with patch("overlay.character.get_overlay_state", return_value=state), \
             patch("overlay.character.update_overlay_state", side_effect=lambda update: update(state)), \
             patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(-200, 0, 400, 600)), \
             patch("overlay.character.bubble_geometry.clamp_rect", return_value=(-200, 548)):
            self.assertEqual(overlay.snapshot_launcher_anchor(), (-200, 548))
        self.assertEqual((state["launcher_window"]["x"], state["launcher_window"]["y"]), (-200, 548))

    def test_next_expansion_ignores_moved_full_xy_and_uses_current_launcher(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = 300
        overlay.root.winfo_y.return_value = 500
        overlay.get_bundled_phys_rect = Mock(return_value=(-900, -800, 270, 302))
        with patch("overlay.character.get_overlay_state", return_value={"overlay_window": {"x": -900, "y": -800}}), \
             patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(0, 0, 1000, 800)), \
             patch("overlay.character.bubble_geometry.clamp_rect", side_effect=lambda x, y, *_args: (x, y)):
            self.assertEqual(overlay.launcher_full_target(), (191, 224, 270, 302))

    def test_collapse_terminates_popup_child_and_clears_ui_without_host_shutdown(self):
        app = object.__new__(OverlayApp)
        app._presentation_mode = "full"
        app._overlay_events = Mock(mode="observer")
        app.character = Mock()
        app._bubble_manager = Mock()
        app._bubble_manager.defer_nudge.return_value = True
        app._initiative = Mock()
        app._bubble_input = Mock()
        app._bubble_history = Mock()
        app._bubble_turn_active = True
        app._nudge_awaiting_reply = True
        app._nudge_engage_live = True
        app._pending_nudge_text = "nudge"
        stopped = threading.Event()
        session = app._bubble_session = Mock()
        session.stop.side_effect = stopped.set

        app._set_presentation("launcher")

        app._initiative.defer_active.assert_called_once_with()
        app._bubble_input.hide.assert_called_once_with()
        app._bubble_manager.clear_all.assert_called_once_with()
        app._bubble_history.hide.assert_called_once_with()
        self.assertTrue(stopped.wait(1.0))
        session.stop.assert_called_once_with()
        self.assertIsNone(app._bubble_session)
        self.assertFalse(app._bubble_turn_active)

        app._set_presentation("full")
        app._bubble_input.show.assert_not_called()
        app._bubble_manager.replay_last.assert_not_called()

    def test_blocking_session_cleanup_does_not_delay_hidden_ack_or_clear_new_session(self):
        stop_entered = threading.Event()
        stop_release = threading.Event()
        stop_finished = threading.Event()

        class BlockingSession:
            def __init__(self):
                self.stop_calls = 0

            def stop(self):
                self.stop_calls += 1
                stop_entered.set()
                stop_release.wait(2.0)
                stop_finished.set()

        app = object.__new__(OverlayApp)
        app._presentation_mode = "full"
        app._external_renderer_hiding = False
        app._external_renderer_visible = True
        app._launcher_hidden = False
        app._overlay_events = Mock(mode="replace")
        app._overlay_events.supports.return_value = True
        app.character = Mock()
        app.root = Mock()
        app.root.after.return_value = "hide-timeout"
        app._bubble_manager = Mock()
        app._bubble_manager.defer_nudge.return_value = False
        old_session = BlockingSession()
        app._bubble_session = old_session

        started = time.perf_counter()
        app._set_presentation("launcher")
        elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.1)
        self.assertTrue(stop_entered.wait(1.0))
        self.assertIsNone(app._bubble_session)
        app._cleanup_bubble_session_async(old_session)
        self.assertEqual(old_session.stop_calls, 1)

        new_session = Mock()
        app._bubble_session = new_session
        ack_started = time.perf_counter()
        app._handle_external_renderer_message({
            "schema_version": 2,
            "type": "overlay.visibility_changed",
            "payload": {"visible": False},
            "_renderer": {"id": "engram.bolttagu-2d", "mode": "replace"},
        })
        self.assertLess(time.perf_counter() - ack_started, 0.1)
        app.character.restore_bundled_renderer.assert_called_once_with()
        app.character.show_launcher.assert_called_once_with()

        stop_release.set()
        self.assertTrue(stop_finished.wait(1.0))
        self.assertIs(app._bubble_session, new_session)

    def test_background_session_cleanup_logs_failure_and_releases_dedup_guard(self):
        app = object.__new__(OverlayApp)
        session = Mock()
        session.stop.side_effect = RuntimeError("cleanup failed")
        logged = threading.Event()

        with patch("overlay.main.log.exception", side_effect=lambda *_args, **_kwargs: logged.set()):
            app._cleanup_bubble_session_async(session)
            self.assertTrue(logged.wait(1.0))

        self.assertNotIn(id(session), app._bubble_sessions_cleaning)
        session.stop.assert_called_once_with()

    def test_background_session_reaper_is_non_daemon_and_completes(self):
        app = object.__new__(OverlayApp)
        stopped = threading.Event()
        session = Mock()
        session.stop.side_effect = stopped.set
        created = []
        real_thread = threading.Thread

        def capture_thread(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            created.append(thread)
            return thread

        with patch("overlay.main.threading.Thread", side_effect=capture_thread):
            app._cleanup_bubble_session_async(session)

        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].daemon)
        self.assertTrue(stopped.wait(1.0))
        created[0].join(timeout=1.0)
        self.assertFalse(created[0].is_alive())

    def test_existing_hidden_launcher_canvas_repositions_before_reveal(self):
        overlay = object.__new__(CharacterOverlay)
        overlay._launcher_canvas = Mock()
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = 1
        overlay.root.winfo_y.return_value = 2
        with patch("overlay.character.get_overlay_state", return_value={"launcher_window": {"x": 300, "y": 400}}), \
             patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(0, 0, 800, 800)), \
             patch("overlay.character.bubble_geometry.clamp_rect", side_effect=lambda x, y, *_args: (x, y)):
            overlay.show_launcher()
        overlay.root.geometry.assert_called_once_with("52x52+300+400")
        overlay.root.deiconify.assert_not_called()

    def test_launcher_mode_blocks_initiative_screen_clear(self):
        app = object.__new__(OverlayApp)
        app._presentation_mode = "launcher"
        app._chat_mode = "bubble"
        app._bubble_turn_active = False
        app._bubble_input = Mock()
        app._bubble_manager = Mock()
        self.assertFalse(app._bubble_screen_clear())
        app._bubble_input.is_showing.assert_not_called()

    def test_tooltip_position_matrix_handles_edges_and_negative_work_area(self):
        cases = [
            ((100, 100, 80, 24, (0, 0, 500, 500)), (158, 108)),
            ((450, 100, 80, 24, (0, 0, 500, 500)), (364, 108)),
            ((30, 2, 80, 24, (0, 0, 100, 500)), (16, 60)),
            ((30, 100, 80, 24, (0, 0, 100, 500)), (16, 70)),
            ((-190, 10, 80, 24, (-200, 0, 0, 300)), (-132, 18)),
            ((470, 470, 80, 24, (0, 0, 500, 480)), (420, 456)),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(launcher_tooltip_position(*args), expected)

    def test_visibility_ack_switches_only_the_capable_replace_surface(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app.character = Mock()
        app._presentation_mode = "full"
        app._overlay_events.supports.return_value = True
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "overlay.visibility_changed",
            "payload": {"visible": True},
        })
        app.character.hide_for_external_renderer.assert_called_once()
        app.character.reset_mock()
        app._presentation_mode = "launcher"
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "overlay.visibility_changed",
            "payload": {"visible": False},
        })
        app.character.restore_bundled_renderer.assert_called_once()
        app.character.show_launcher.assert_called_once()

    def test_late_hidden_ack_does_not_restore_launcher_hidden_to_tray(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app._overlay_events.supports.return_value = True
        app.character = Mock()
        app._presentation_mode = "launcher"
        app._launcher_hidden = True
        app._external_renderer_visible = True

        app._handle_external_renderer_message({
            "schema_version": 1, "type": "overlay.visibility_changed",
            "payload": {"visible": False},
        })

        app.character.restore_bundled_renderer.assert_not_called()
        app.character.show_launcher.assert_not_called()
        self.assertFalse(app._external_renderer_visible)

    def test_renderer_message_pump_recovers_and_reschedules_after_handler_exception(self):
        app = object.__new__(OverlayApp)
        app._renderer_inbound = queue.Queue()
        app._renderer_inbound.put({"type": "pointer.action", "payload": {}})
        app._quitting = False
        app.root = Mock()
        app._handle_external_renderer_message = Mock(side_effect=RuntimeError("Tk failure"))
        app._restore_bundled_renderer = Mock()

        app._drain_external_renderer_messages()

        app._restore_bundled_renderer.assert_called_once()
        app.root.after.assert_called_once_with(
            main_module._RENDERER_DRAIN_MS, app._drain_external_renderer_messages
        )

    def test_external_menu_selection_uses_tk_value_semantics_not_string_truthiness(self):
        # Provider/model siblings share one non-empty StringVar: only its
        # matching entry receives the selected marker.
        self.assertTrue(_menu_entry_is_selected("radiobutton", "codex", value="codex"))
        self.assertFalse(_menu_entry_is_selected("radiobutton", "codex", value="claude"))
        self.assertFalse(_menu_entry_is_selected("radiobutton", "", value="codex"))
        self.assertTrue(_menu_entry_is_selected("checkbutton", "enabled", onvalue="enabled"))
        self.assertFalse(_menu_entry_is_selected("checkbutton", "enabled", onvalue="disabled"))
        self.assertFalse(_menu_entry_is_selected("checkbutton", "", onvalue="enabled"))

    def test_context_menu_path_contains_no_low_level_mouse_hook(self):
        source = Path(__file__).resolve().parents[1].joinpath("overlay", "character.py").read_text(encoding="utf-8")
        for forbidden in ("WH_MOUSE_LL", "SetWindowsHookExW", "UnhookWindowsHookEx"):
            self.assertNotIn(forbidden, source)
        self.assertIn("GetAsyncKeyState", source)
        self.assertIn("GetCursorPos", source)

    def test_renderer_menu_dismiss_precedes_and_does_not_consume_next_left_click(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app.character = Mock()
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "menu_dismiss"},
        })
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "left_click"},
        })
        app.character._dismiss_context_menu.assert_called_once()
        app.character.external_activate.assert_called_once()
        self.assertEqual(app._overlay_events.publish.call_args_list[-1].args[:2], ("pointer.left_clicked", "click"))

    def _replace_menu_app(self, *, menu_open=True):
        app = object.__new__(OverlayApp)
        app.root = Mock()
        app._quitting = False
        app._pending_external_menu_activation = None
        app._settings_active = False
        app._overlay_events = Mock(mode="replace")
        app.character = Mock()
        app.character._context_menu_open = menu_open
        return app

    def test_replace_open_menu_dismiss_without_release_activates_once(self):
        app = self._replace_menu_app()
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "menu_dismiss"},
        })
        callback = app.root.after.call_args.args[1]
        callback()
        app.character.external_activate.assert_called_once_with()

    def test_replace_menu_dismiss_then_left_click_activates_exactly_once(self):
        app = self._replace_menu_app()
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "menu_dismiss"},
        })
        callback = app.root.after.call_args.args[1]
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "left_click"},
        })
        callback()
        app.character.external_activate.assert_called_once_with()

    def test_replace_delayed_release_after_fallback_does_not_toggle_twice(self):
        app = self._replace_menu_app()
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "menu_dismiss"},
        })
        fallback = app.root.after.call_args.args[1]
        fallback()
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "left_click"},
        })
        app.character.external_activate.assert_called_once_with()
        self.assertEqual(app._overlay_events.publish.call_args.args[:2], ("pointer.left_clicked", "click"))

    def test_replace_recovered_click_guard_expires_for_next_independent_click(self):
        app = self._replace_menu_app()
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "menu_dismiss"},
        })
        app.root.after.call_args_list[0].args[1]()  # fallback activation
        app.root.after.call_args_list[1].args[1]()  # grace expiry
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "left_click"},
        })
        self.assertEqual(app.character.external_activate.call_count, 2)

    def test_replace_menu_dismiss_then_right_click_or_drag_cancels_fallback(self):
        for action, payload in (
            ("right_click", {"screen_x": 10, "screen_y": 20}),
            ("drag_move", {"screen_x": 10, "screen_y": 20}),
        ):
            app = self._replace_menu_app()
            app.character.get_phys_rect.return_value = (0, 0, 30, 40)
            app.character.apply_external_geometry.return_value = (10, 20, 30, 40)
            app._handle_external_renderer_message({
                "schema_version": 1, "type": "pointer.action", "payload": {"action": "menu_dismiss"},
            })
            callback = app.root.after.call_args.args[1]
            app._handle_external_renderer_message({
                "schema_version": 1, "type": "pointer.action", "payload": {"action": action, **payload},
            })
            callback()
            app.character.external_activate.assert_not_called()

    def test_replace_menu_dismiss_when_not_open_never_arms_fallback(self):
        app = self._replace_menu_app(menu_open=False)
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "menu_dismiss"},
        })
        app.root.after.assert_not_called()
        app.character.external_activate.assert_not_called()

    def test_open_settings_cancels_replace_menu_recovery_before_showing_window(self):
        app = self._replace_menu_app()
        app._tunnels = Mock()
        app._reload_config = Mock()
        app._arm_external_menu_activation_fallback()
        pending = app._pending_external_menu_activation
        with patch("overlay.main.open_settings") as opened:
            app.open_settings()
        self.assertIsNone(app._pending_external_menu_activation)
        app.root.after_cancel.assert_called_once_with(pending["after_id"])
        opened.assert_called_once()
        # A callback already queued by Tk becomes an inert stale callback;
        # closing Settings therefore cannot cause a delayed activation.
        pending["callback"]()
        app.character.external_activate.assert_not_called()

    def test_open_settings_cleanup_is_idempotent_when_no_menu_recovery_exists(self):
        app = self._replace_menu_app()
        app._tunnels = Mock()
        app._reload_config = Mock()
        with patch("overlay.main.open_settings"):
            app.open_settings()
            app.open_settings()
        self.assertIsNone(app._pending_external_menu_activation)
        app.root.after_cancel.assert_not_called()

    def test_settings_active_blocks_late_replace_menu_dismiss_then_allows_next_click(self):
        app = self._replace_menu_app()
        app._tunnels = Mock()
        app._reload_config = Mock()
        with patch("overlay.main.open_settings") as opened:
            app.open_settings()
        on_closed = opened.call_args.kwargs["on_closed"]
        self.assertTrue(app._settings_active)
        # A menu_dismiss arriving after Settings opens must not arm recovery.
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "menu_dismiss"},
        })
        self.assertIsNone(app._pending_external_menu_activation)
        app.character.external_activate.assert_not_called()

        on_closed()
        on_closed()  # callback cleanup is intentionally idempotent
        self.assertFalse(app._settings_active)
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "left_click"},
        })
        app.character.external_activate.assert_called_once_with()

    def test_external_menu_uses_nonmodal_dark_surface_with_tk_polling(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay._context_menu_open = False
        overlay._external_context_surface = None
        overlay._flip_h = False
        overlay._flip_var = Mock()
        overlay._rebuild_provider_menu = Mock()
        overlay._context_menu = Mock()
        overlay._build_context_menu = Mock()
        overlay._context_menu.index.return_value = None
        overlay._dismiss_context_menu = Mock()
        overlay._invoke_activate = Mock()
        overlay._invoke_history = Mock()
        overlay._invoke_settings = Mock()
        overlay._invoke_restart = Mock()
        overlay._invoke_quit = Mock()
        overlay.set_flip = Mock()
        surface, frame = Mock(), Mock()
        frame.winfo_children.return_value = []
        surface.winfo_reqwidth.return_value = 100
        surface.winfo_reqheight.return_value = 50
        with patch("overlay.character.tk.Toplevel", return_value=surface), \
             patch("overlay.character.tk.Frame", return_value=frame), \
             patch("overlay.character.tk.Button"), \
             patch.object(overlay, "_start_context_menu_outside_poll") as poll, \
             patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(0, 0, 500, 500)):
            overlay.external_context_menu(10, 20)
        self.assertIs(overlay._external_context_surface, surface)
        surface.geometry.assert_called_once_with("100x50+10+20")
        surface.focus_force.assert_called_once_with()
        poll.assert_called_once_with(surface, (10, 20, 110, 70))
        bound_events = [call.args[0] for call in surface.bind.call_args_list]
        self.assertIn("<Escape>", bound_events)
        self.assertIn("<FocusOut>", bound_events)

    def test_custom_menu_action_dismisses_and_invokes_exactly_once(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay._context_menu_open = False
        overlay._external_context_surface = None
        overlay._flip_h = False
        overlay._flip_var = Mock()
        overlay._rebuild_provider_menu = Mock()
        overlay._build_context_menu = Mock()
        model = Mock()
        model.index.return_value = 0
        model.type.return_value = "command"
        model.entrycget.side_effect = lambda _index, key: {"label": "설정", "state": "normal"}[key]
        overlay._context_menu = model
        overlay._dismiss_context_menu = Mock()
        surface, frame = Mock(), Mock()
        frame.winfo_children.return_value = []
        surface.winfo_reqwidth.return_value = 100
        surface.winfo_reqheight.return_value = 50
        commands = []

        def button(_parent, **kwargs):
            commands.append(kwargs["command"])
            return Mock()

        with patch("overlay.character.tk.Toplevel", return_value=surface), \
             patch("overlay.character.tk.Frame", return_value=frame), \
             patch("overlay.character.tk.Button", side_effect=button), \
             patch.object(overlay, "_start_context_menu_outside_poll"), \
             patch("overlay.character.bubble_geometry.get_monitor_work_rect", return_value=(0, 0, 500, 500)):
            overlay.external_context_menu(10, 20)
        overlay._dismiss_context_menu.reset_mock()
        commands[0]()
        overlay._dismiss_context_menu.assert_called_once_with()
        model.invoke.assert_called_once_with(0)

    def test_renderer_disconnect_tears_down_menu(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="observer")
        app.character = Mock()
        app._observer_rects = {}
        app._bubble_anchor = "bundled"
        app._handle_external_renderer_message({
            "type": "_renderer.disconnected",
            "payload": {"renderer_id": "observer", "mode": "observer"},
        })
        app.character._dismiss_context_menu.assert_called_once_with()

    def test_in_surface_context_menu_collapses_to_launcher_and_never_quits_process(self):
        source = Path(__file__).resolve().parents[1].joinpath("overlay", "character.py").read_text(encoding="utf-8")
        menu_body = source[source.index("    def _build_context_menu("):source.index("    def _get_ollama_model_value(")]
        self.assertIn("presentation_menu_action(", menu_body)
        self.assertNotIn('label="종료"', menu_body)
        self.assertNotIn("command=self._invoke_quit", menu_body)

    def test_outside_poll_debounces_existing_press_and_dismisses_first_new_outside_edge(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        surface = Mock()
        overlay._external_context_surface = surface
        overlay._context_menu_open = True
        overlay._left_button_pressed = Mock(side_effect=[True, True, False, True])
        overlay._pointer_screen_position = Mock(return_value=(50, 50))
        overlay._dismiss_context_menu = Mock()
        callbacks = []
        overlay.root.after.side_effect = lambda _delay, callback: callbacks.append(callback) or f"after-{len(callbacks)}"

        overlay._start_context_menu_outside_poll(surface, (0, 0, 10, 10))
        callbacks.pop(0)()  # same press that opened the menu: no edge
        overlay._dismiss_context_menu.assert_not_called()
        callbacks.pop(0)()  # release arms the next edge
        overlay._dismiss_context_menu.assert_not_called()
        callbacks.pop(0)()  # first new press outside
        overlay._dismiss_context_menu.assert_called_once_with()

    def test_repeated_menu_dismiss_cancels_poll_and_destroys_surface_once(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        surface = Mock()
        surface.winfo_exists.return_value = True
        overlay._external_context_surface = surface
        overlay._context_menu_poll_after = "poll-1"
        overlay._context_menu_poll_generation = 1
        overlay._context_menu_open = True
        overlay._provider_menu = None
        overlay._context_menu = None

        overlay._dismiss_context_menu()
        overlay._dismiss_context_menu()

        overlay.root.after_cancel.assert_called_once_with("poll-1")
        surface.destroy.assert_called_once_with()
        self.assertFalse(overlay._context_menu_open)

    def test_nonmodal_menu_focus_descendant_and_geometry_helpers(self):
        surface = Mock()
        child = Mock(master=surface)
        self.assertTrue(_is_widget_descendant(child, surface))
        self.assertFalse(_is_widget_descendant(None, surface))
        self.assertFalse(_is_widget_descendant(Mock(master=None), surface))
        self.assertEqual(_clamp_menu_geometry(490, 490, 100, 50, (0, 0, 500, 500)), (100, 50, 400, 450))
        self.assertTrue(_point_outside_rect((-700, 1800), (-198, 2181, 145, 2329)))
        self.assertFalse(_point_outside_rect((-100, 2200), (-198, 2181, 145, 2329)))
    def test_manifest_compatible_hints_and_metadata_only_mapping(self):
        expected = {
            "idle", "hover", "click", "input", "generating", "search",
            "thought", "memory", "success", "provider_error", "error",
        }
        self.assertLessEqual(expected, DISPLAY_HINTS)
        self.assertEqual(
            event_for_bubble(
                {"kind": "tool_use", "tool_name": "web_search", "tool_input": {"secret": "no"}}
            ),
            ("tool.started", "search", {"category": "search"}),
        )
        self.assertEqual(
            event_for_bubble({"kind": "thought", "text": "private"}),
            ("generation.thinking", "thought", {}),
        )
        self.assertEqual(tool_category("kg_search"), "memory")
        self.assertEqual(tool_category("read_file"), "read")
        self.assertEqual(tool_category("Task"), "execute")
        self.assertEqual(tool_category("Bash"), "execute")
        self.assertEqual(tool_category("PowerShell"), "execute")
        self.assertEqual(tool_category("spawn_agent"), "execute")

    def test_legacy_command_is_diagnostic_only_and_never_spawned(self):
        cfg = {"overlay": {"external_renderer": {"mode": "replace", "command": ["never.exe"]}}}
        with patch("overlay.event_api.subprocess.Popen") as spawn:
            publisher = OverlayEventPublisher(cfg)
        spawn.assert_not_called()
        self.assertTrue(publisher.legacy_diagnostic)
        self.assertEqual(publisher.mode, "observer")
        self.assertFalse(publisher.connected)

    def test_stop_owns_connections_only_and_never_kills_a_renderer(self):
        source = (Path(__file__).resolve().parents[1] / "overlay/event_api.py").read_text(encoding="utf-8")
        for forbidden in ("subprocess.Popen", "taskkill", ".terminate()", ".kill()"):
            self.assertNotIn(forbidden, source)

    def test_v1_child_protocol_is_explicitly_retired(self):
        legacy = (Path(__file__).resolve().parents[1] / "docs/overlay-event-api-v1.md").read_text(encoding="utf-8")
        self.assertIn("retired", legacy)
        self.assertIn("never executes", legacy)
        self.assertIn("overlay-event-api-v2.md", legacy)

    def test_external_geometry_becomes_anchor_and_restores_bundled_rect(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = 10
        overlay.root.winfo_y.return_value = 20
        overlay._img_w = 30
        overlay._img_h = 40
        overlay._external_rect = None
        saved = {}

        with (
            patch("overlay.character.clamp_overlay_position", side_effect=lambda x, y, width, height: (x, y)),
            patch(
                "overlay.character.bubble_geometry.get_monitor_work_rect",
                return_value=(0, 0, 1920, 1080),
            ),
            patch("overlay.character.update_overlay_state", side_effect=lambda update: update(saved)),
        ):
            overlay.hide_for_external_renderer()
            self.assertEqual(
                overlay.apply_external_geometry(100, 200, 300, 400),
                (100, 200, 300, 400),
            )

        self.assertEqual(overlay.get_phys_rect(), (100, 200, 300, 400))
        self.assertEqual(saved["overlay_window"]["x"], 100)
        overlay.restore_bundled_renderer()
        self.assertEqual(overlay.get_phys_rect(), (10, 20, 30, 40))

    def test_initial_replace_geometry_keeps_saved_position_without_persisting(self):
        overlay = object.__new__(CharacterOverlay)
        overlay.root = Mock()
        overlay.root.winfo_x.return_value = 10
        overlay.root.winfo_y.return_value = 20
        overlay._img_w = 30
        overlay._img_h = 40
        overlay._external_rect = (500, 600, 30, 40)
        saved = {}
        with (
            patch("overlay.character.clamp_overlay_position", side_effect=lambda x, y, width, height: (x, y)),
            patch("overlay.character.update_overlay_state") as update_state,
        ):
            self.assertEqual(
                overlay.apply_external_geometry(100, 200, 300, 400, preserve_position=True),
                (500, 600, 300, 400),
            )
        update_state.assert_not_called()
        self.assertEqual(saved, {})

    def test_external_drag_move_updates_anchor_without_writing_state(self):
        overlay = object.__new__(CharacterOverlay)
        overlay._external_rect = (10, 20, 30, 40)
        with (
            patch("overlay.character.clamp_overlay_position", side_effect=lambda x, y, width, height: (x, y)),
            patch("overlay.character.update_overlay_state") as update_state,
        ):
            self.assertEqual(
                overlay.apply_external_geometry(100, 200, 300, 400, persist_position=False),
                (100, 200, 300, 400),
            )
        update_state.assert_not_called()

    def test_replace_drag_move_updates_rect_without_persisting_or_acknowledging(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app.character = Mock()
        app.character.get_phys_rect.return_value = (10, 20, 70, 80)
        app.character.apply_external_geometry.return_value = (50, 60, 70, 80)

        app._handle_external_renderer_message({
            "schema_version": 1,
            "type": "pointer.action",
            "payload": {"action": "drag_move", "screen_x": 50, "screen_y": 60},
        })

        app.character.apply_external_geometry.assert_called_once_with(
            50, 60, 70, 80, persist_position=False, presentation_mode="full"
        )
        app._overlay_events.publish.assert_not_called()

    def test_replace_drag_end_persists_and_acknowledges_final_position(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app.character = Mock()
        app.character.get_phys_rect.return_value = (10, 20, 70, 80)
        app.character.apply_external_geometry.return_value = (50, 60, 70, 80)

        app._handle_external_renderer_message({
            "schema_version": 1,
            "type": "pointer.action",
            "payload": {"action": "drag_end", "screen_x": 50, "screen_y": 60},
        })

        app.character.apply_external_geometry.assert_called_once_with(
            50, 60, 70, 80, persist_position=True, presentation_mode="full"
        )
        app.character.remember_launcher_relative_offset.assert_called_once_with(50, 60, 70, 80)
        app._overlay_events.publish.assert_called_once_with(
            "overlay.set_position", "idle", {"x": 50, "y": 60}
        )

    def test_replace_launcher_drag_end_persists_launcher_state(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app.character = Mock()
        app.character.get_phys_rect.return_value = (10, 20, 52, 52)
        app.character.apply_external_geometry.return_value = (50, 60, 52, 52)
        app._presentation_mode = "launcher"
        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action",
            "payload": {"action": "drag_end", "screen_x": 50, "screen_y": 60},
        })
        app.character.apply_external_geometry.assert_called_once_with(
            50, 60, 52, 52, persist_position=True, presentation_mode="launcher"
        )
        app.character.remember_launcher_relative_offset.assert_not_called()

    def test_observer_geometry_is_transient_and_click_selects_shared_anchor(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="observer")
        app.character = Mock()
        app.character.get_bundled_phys_rect.return_value = (1, 2, 30, 40)
        app._bubble_manager = Mock()
        app._observer_rect = None
        app._bubble_anchor = "bundled"

        app._handle_external_renderer_message({
            "schema_version": 1, "type": "overlay.geometry_changed",
            "payload": {"x": 100, "y": 200, "width": 300, "height": 400},
        })
        self.assertEqual(app._observer_rect, (100, 200, 300, 400))
        app.character.apply_external_geometry.assert_not_called()
        self.assertEqual(app._get_bubble_anchor_rect(), (1, 2, 30, 40))

        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "left_click"},
        })
        self.assertEqual(app._bubble_anchor, "observer")
        self.assertEqual(app._get_bubble_anchor_rect(), (100, 200, 300, 400))
        app.character.external_activate.assert_called_once()

    def test_active_observer_geometry_reflows_all_shared_surfaces(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="observer")
        app.character = Mock()
        app._bubble_manager = Mock()
        app._bubble_input = Mock()
        app._bubble_history = Mock()
        app._observer_rect = (100, 200, 30, 40)
        app._bubble_anchor = "observer"

        app._handle_external_renderer_message({
            "schema_version": 1, "type": "overlay.geometry_changed",
            "payload": {"x": 500, "y": 600, "width": 50, "height": 60},
        })
        self.assertEqual(app._observer_rect, (500, 600, 50, 60))
        app._bubble_manager.refresh_positions.assert_called_once()
        app._bubble_input.refresh_position.assert_called_once()
        app._bubble_history.refresh_position.assert_called_once()

    def test_renderer_failure_drops_observer_anchor_even_when_restore_raises(self):
        app = object.__new__(OverlayApp)
        app.character = Mock()
        app.character.restore_bundled_renderer.side_effect = RuntimeError("gone")
        app._bubble_manager = Mock()
        app._bubble_input = Mock()
        app._bubble_history = Mock()
        app._observer_rect = (100, 200, 30, 40)
        app._bubble_anchor = "observer"
        app._external_renderer_visible = True

        app._restore_bundled_renderer()
        self.assertIsNone(app._observer_rect)
        self.assertEqual(app._bubble_anchor, "bundled")
        self.assertFalse(app._external_renderer_visible)
        app._bubble_manager.refresh_positions.assert_called_once()
        app._bubble_input.refresh_position.assert_called_once()
        app._bubble_history.refresh_position.assert_called_once()

    def test_replace_geometry_still_persists_and_acknowledges_position(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app.character = Mock()
        app.character.apply_external_geometry.return_value = (50, 60, 70, 80)
        app._observer_rect = None
        app._bubble_anchor = "bundled"
        app._replace_startup_geometry_pending = False

        app._handle_external_renderer_message({
            "schema_version": 1, "type": "overlay.geometry_changed",
            "payload": {"x": 50, "y": 60, "width": 70, "height": 80},
        })
        app.character.apply_external_geometry.assert_called_once_with(50, 60, 70, 80, preserve_position=False)
        app._overlay_events.publish.assert_called_once_with(
            "overlay.set_position", "idle", {"x": 50, "y": 60}
        )

    def test_first_replace_geometry_adopts_size_but_acknowledges_saved_position(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="replace")
        app.character = Mock()
        app.character.apply_external_geometry.return_value = (500, 600, 70, 80)
        app._observer_rect = None
        app._bubble_anchor = "bundled"
        app._replace_startup_geometry_pending = True

        app._handle_external_renderer_message({
            "schema_version": 1, "type": "overlay.geometry_changed",
            "payload": {"x": 50, "y": 60, "width": 70, "height": 80},
        })
        app.character.apply_external_geometry.assert_called_once_with(50, 60, 70, 80, preserve_position=True)
        self.assertFalse(app._replace_startup_geometry_pending)
        app._overlay_events.publish.assert_called_once_with(
            "overlay.set_position", "idle", {"x": 500, "y": 600}
        )

    def test_echo_refresh_uses_anchor_relative_input_position(self):
        manager = object.__new__(BubbleManager)
        manager._echo_text = "hello"
        manager._echo_rect = (110, 220, 120, 50, "left")
        manager._echo_anchor_offset = (100, 200)
        manager._get_char_rect = lambda: (1000, 2000, 30, 40)
        manager._echo = Mock()
        manager._echo.ensure.return_value = Mock()
        manager._font_family = lambda: "Arial"
        manager._font_size = lambda: 12
        manager._theme = {"speech_fg": "#fff", "input_bg": "#000", "input_outline": "#111"}
        with (
            patch("overlay.bubble.bubble_manager.shapes.draw_speech_bubble", return_value=(120, 40)),
            patch("overlay.bubble.bubble_manager.geometry.clamp_rect", side_effect=lambda x, y, w, h, _m: (x, y)),
            patch("overlay.bubble.bubble_manager.geometry.get_monitor_work_rect", return_value=(0, 0, 3000, 3000)),
            patch("overlay.bubble.bubble_manager.geometry.get_monitor_rect", return_value=(0, 0, 3000, 3000)),
            patch("overlay.bubble.bubble_manager.geometry.monitor_bottom_center_pixel", return_value=(1500, 3000)),
            patch("overlay.bubble.bubble_manager.geometry.angle_to_point", return_value=0),
        ):
            manager._render_echo()
        # Original input (110,220) was relative to (10,20), so reflow uses
        # (1100,2200) rather than stale absolute coordinates.
        manager._echo.place.assert_called_once_with(1100, 2210, 120, 40)

    def test_refresh_positions_reanchors_thought_speech_and_echo(self):
        manager = object.__new__(BubbleManager)
        calls = []
        manager._render_thought = lambda: calls.append("thought")
        manager._render_speech = lambda: calls.append("speech")
        manager._render_echo = lambda: calls.append("echo")
        manager.refresh_positions()
        self.assertEqual(calls, ["thought", "speech", "echo"])

    def test_observer_click_without_geometry_does_not_activate(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="observer")
        app.character = Mock()
        app._bubble_manager = Mock()
        app._observer_rect = None
        app._bubble_anchor = "bundled"

        app._handle_external_renderer_message({
            "schema_version": 1, "type": "pointer.action", "payload": {"action": "left_click"},
        })
        app.character.external_activate.assert_not_called()
        self.assertEqual(app._bubble_anchor, "bundled")

    def test_bundled_click_reselects_bundled_anchor(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock()
        app._bubble_manager = Mock()
        app._observer_rect = (100, 200, 30, 40)
        app._bubble_anchor = "observer"
        app._on_bundled_pointer_event("left_click", {})
        self.assertEqual(app._bubble_anchor, "bundled")

    def test_observer_disconnect_clears_owned_anchor_before_same_id_reconnect(self):
        app = object.__new__(OverlayApp)
        app._overlay_events = Mock(mode="observer")
        app.character = Mock()
        app.character.get_bundled_phys_rect.return_value = (1, 2, 30, 40)
        app._bubble_manager = Mock()
        app._observer_rects = {"observer-a": (100, 200, 300, 400)}
        app._observer_rect = (100, 200, 300, 400)
        app._active_observer_id = "observer-a"
        app._bubble_anchor = "observer"

        app._handle_external_renderer_message({
            "type": "_renderer.disconnected",
            "payload": {"renderer_id": "observer-a", "mode": "observer"},
        })

        self.assertNotIn("observer-a", app._observer_rects)
        self.assertIsNone(app._observer_rect)
        self.assertIsNone(app._active_observer_id)
        self.assertEqual(app._bubble_anchor, "bundled")
        self.assertEqual(app._get_bubble_anchor_rect(), (1, 2, 30, 40))

        app._handle_external_renderer_message({
            "schema_version": 2,
            "type": "pointer.action",
            "_renderer": {"id": "observer-a", "mode": "observer"},
            "payload": {"action": "left_click"},
        })
        app.character.external_activate.assert_not_called()

        app._handle_external_renderer_message({
            "schema_version": 2,
            "type": "overlay.geometry_changed",
            "_renderer": {"id": "observer-a", "mode": "observer"},
            "payload": {"x": 500, "y": 600, "width": 50, "height": 60},
        })
        app._handle_external_renderer_message({
            "schema_version": 2,
            "type": "pointer.action",
            "_renderer": {"id": "observer-a", "mode": "observer"},
            "payload": {"action": "left_click"},
        })
        self.assertEqual(app._observer_rect, (500, 600, 50, 60))
        self.assertEqual(app._active_observer_id, "observer-a")
        app.character.external_activate.assert_called_once_with()

    def test_overlay_api_help_uses_manual_deep_link(self):
        opened = []
        open_overlay_event_api_manual(opened.append)
        self.assertEqual(opened, [OVERLAY_EVENT_API_MANUAL_URL])

    def test_custom_overlay_copy_and_quick_reference_contract(self):
        root = Path(__file__).resolve().parents[1]
        manual = (root / "installer/templates/manual/overlay-event-api.md").read_text(encoding="utf-8")
        docs = (root / "docs/overlay-event-api-v2.md").read_text(encoding="utf-8")
        settings = (root / "overlay/settings_window.py").read_text(encoding="utf-8")
        for text in (manual, docs, settings):
            self.assertNotIn("외주", text)
            self.assertNotIn("Engram의 stdin은 커스텀 오버레이로 가는", text)
        for text in (manual, docs):
            for name in ("overlay.register", "engram.welcome", "state.snapshot", "renderer.assignment", "overlay.geometry_changed", "pointer.action", "overlay.heartbeat", "overlay.set_position"):
                self.assertIn(name, text)
            self.assertIn("metadata_only", text)
            self.assertIn("selected_renderer_id", text)
        self.assertIn('text="?", width=3', settings)
        self.assertIn("커스텀 오버레이 적용 방법", settings)
        readme = (root / "README.md").read_text(encoding="utf-8")
        for expected in (
            "https://github.com/JJHbrams/engram-overlay",
            "loopback Event API",
            "docs/overlay-event-api-v2.md",
        ):
            self.assertIn(expected, readme)

    def test_persona_shortcut_is_tab_scoped_not_a_cli_control(self):
        source = (Path(__file__).resolve().parents[1] / "overlay/settings_window.py").read_text(encoding="utf-8")
        cli_section = source[source.index("def _build_cli_tab"):source.index("def _refresh_ollama_models")]
        self.assertNotIn("페르소나", cli_section)
        self.assertIn("grid(row=5, column=0", cli_section)
        self.assertIn("grid(row=6, column=0", cli_section)
        self.assertIn("self._persona_tip_frame", source)
        self.assertIn("<<NotebookTabChanged>>", source)
        self.assertIn("selected is self._tab_persona", source)
        self.assertIn("pack_forget", source)


if __name__ == "__main__":
    unittest.main()
