import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from overlay.session_selection import SessionSelection


class SelectionTests(unittest.TestCase):
    def idle_candidates(self):
        s = SessionSelection(clock=lambda: self.now)
        rows = [dict(key=key, first_seen=i, last_seen=i, state='ready', state_since=0)
                for i, key in enumerate(('old', 'current'))]
        s.update(rows)
        self.assertEqual(s.selected_key, 'current')
        return s, rows

    def test_auto_ready_follows_existing_candidate_starting_work(self):
        s, rows = self.idle_candidates()
        rows[0]['state'] = 'working'
        s.update(rows)
        self.assertEqual(s.selected_key, 'old')
        rows[0]['state'] = 'ready'
        s.update(rows)
        self.assertEqual(s.selected_key, 'old')

    def test_working_activation_beats_newer_unknown_arrival(self):
        s, rows = self.idle_candidates()
        rows[0]['state'] = 'working'
        unknown = dict(key='new unknown', first_seen=10, last_seen=10, state='unknown')
        s.update(rows + [unknown])
        self.assertEqual(s.selected_key, 'old')
        rows[0]['state'] = 'ready'
        s.update(rows + [unknown])
        self.assertEqual(s.selected_key, 'old')

    def test_new_working_arrival_beats_later_unknown_registration(self):
        s, rows = self.idle_candidates()
        working = dict(key='new work', first_seen=10, last_seen=10, state='working')
        unknown = dict(key='new unknown', first_seen=11, last_seen=11, state='unknown')
        s.update(rows + [working, unknown])
        self.assertEqual(s.selected_key, 'new work')

    def test_working_current_queues_latest_activation_not_latest_arrival(self):
        s, rows = self.idle_candidates()
        middle = dict(key='middle', first_seen=.5, last_seen=0, state='ready')
        rows.insert(1, middle)
        rows[-1]['state'] = 'working'
        s.update(rows)
        rows[1]['state'] = 'working'
        s.update(rows)
        rows[0]['state'] = 'working'
        s.update(rows)
        self.assertEqual(s.selected_key, 'current')
        rows[-1]['state'] = 'ready'
        s.update(rows)
        self.assertEqual(s.selected_key, 'old')  # Latest work, not newest registration.
        rows[0]['state'] = 'ready'
        s.update(rows)
        self.assertEqual(s.selected_key, 'middle')  # Older pending work is not consumed.
        rows[1]['state'] = 'ready'
        for i in range(3):
            rows[0]['last_seen'] = 100 + i
            s.update(rows)
            self.assertEqual(s.selected_key, 'middle')
        self.assertFalse(s._working_activations)

    def test_completed_or_removed_pending_work_does_not_return_after_latest_finishes(self):
        for remove in (True, False):
            with self.subTest(remove=remove):
                s, rows = self.idle_candidates()
                rows[1]['state'] = 'working'
                s.update(rows)
                rows[0]['state'] = 'working'
                s.update(rows)
                newest = dict(key='newest', first_seen=10, last_seen=10, state='working')
                s.update(rows + [newest])
                rows[1]['state'] = 'ready'
                s.update(rows + [newest])
                self.assertEqual(s.selected_key, 'newest')
                if remove:
                    rows = rows[1:]
                else:
                    rows[0]['state'] = 'ready'
                newest['state'] = 'ready'
                s.update(rows + [newest])
                self.assertEqual(s.selected_key, 'newest')
                self.assertNotIn('old', s._working_activations)

    def test_completed_pending_activation_does_not_steal_or_revive_on_heartbeat(self):
        s, rows = self.idle_candidates()
        rows[1]['state'] = 'working'
        s.update(rows)
        rows[0]['state'] = 'working'
        s.update(rows)
        rows[0]['state'] = 'ready'
        rows[1]['state'] = 'ready'
        s.update(rows)
        self.assertEqual(s.selected_key, 'current')
        rows[0]['last_seen'] = 999
        s.update(rows)
        self.assertEqual(s.selected_key, 'current')
        rows[0]['state'] = 'working'
        s.update(rows)
        self.assertEqual(s.selected_key, 'old')

    def test_off_keeps_pending_activation_until_auto_resumes(self):
        s, rows = self.idle_candidates()
        s.set_auto_enabled(False)
        rows[0]['state'] = 'working'
        s.update(rows)
        self.assertEqual(s.selected_key, 'current')
        s.resume_auto()
        self.assertEqual(s.selected_key, 'old')

    def test_manual_pin_consumes_old_work_and_working_heartbeat_is_not_activation(self):
        s, rows = self.idle_candidates()
        rows[0]['state'] = 'working'
        s.update(rows)
        s.pin('current')
        s.resume_auto()
        for i in range(3):
            rows[0]['last_seen'] = 100 + i
            rows[0]['state_since'] = 10 + i
            s.update(rows)
            self.assertEqual(s.selected_key, 'current')
        rows[0]['state'] = 'ready'
        s.update(rows)
        rows[0]['state'] = 'working'
        s.update(rows)
        self.assertEqual(s.selected_key, 'old')

    def test_preview_freezes_activation_handoff_until_manual_commit(self):
        s, rows = self.idle_candidates()
        s.browse(1)
        rows[0]['state'] = 'working'
        self.now = .2
        s.update(rows)
        self.assertEqual((s.selected_key, s.candidate_key), ('current', 'old'))
        self.now = .5
        s.update(rows)
        self.assertEqual(s.selected_key, 'old')
        self.assertFalse(s.auto_enabled)

    def test_removed_activation_tracking_is_pruned(self):
        s, rows = self.idle_candidates()
        s.set_auto_enabled(False)
        rows[0]['state'] = 'working'
        s.update(rows)
        s.update(rows[1:])
        self.assertNotIn('old', s._working_activations)
        self.assertNotIn('old', s._previous_states)

    def test_unknown_recovery_active_priority_and_watermark(self):
        for state in ('working', 'needs_input', 'blocked'):
            for active_first in (True, False):
                with self.subTest(state=state, active_first=active_first):
                    s = SessionSelection()
                    unknown = dict(key='unknown', first_seen=2 if active_first else 1,
                                   last_seen=8, state='unknown', state_since=7, label='Safe label')
                    active = dict(key='active', first_seen=1 if active_first else 2,
                                  last_seen=3, state=state, state_since=3)
                    before = dict(unknown)
                    s.update([unknown])
                    s.set_auto_enabled(False)
                    s.update([unknown, active])
                    self.assertEqual(s.selected_key, 'unknown')
                    s.resume_auto()
                    self.assertEqual(s.selected_key, 'active')
                    self.assertEqual(unknown, before)
                    active['state'] = 'ready'
                    s.update([unknown, active])
                    self.assertEqual(s.selected_key, 'active')
                    s.update([unknown, active, dict(key='new', first_seen=9,
                                                  last_seen=9, state='unknown')])
                    self.assertEqual(s.selected_key, 'new')
                    initial = SessionSelection()
                    active['state'] = state
                    initial.update([unknown, active])
                    self.assertEqual(initial.selected_key, 'active')

    def test_unknown_preview_and_unknown_only_stability(self):
        s = SessionSelection(clock=lambda: 0)
        rows = [dict(key=str(i), first_seen=i, last_seen=i, state='unknown') for i in range(3)]
        s.update(rows[:1])
        s.update(rows)
        self.assertEqual(s.selected_key, '0')
        s.browse(1)
        rows[2]['state'] = 'working'
        s.update(rows)
        self.assertEqual(s.selected_key, '0')
        s.resume_auto()
        self.assertEqual(s.selected_key, '2')

    def test_old_unknown_becomes_active_and_latest_active_wins(self):
        s = SessionSelection()
        rows = [dict(key=str(i), first_seen=i, last_seen=i, state='unknown') for i in range(3)]
        s.update(rows)
        self.assertEqual(s.selected_key, '2')
        rows[0]['state'] = 'working'
        rows[1]['state'] = 'blocked'
        s.update(rows)
        self.assertEqual(s.selected_key, '1')
        rows[1]['state'] = 'ready'
        for _ in range(10):
            s.update(rows)
        self.assertEqual(s.selected_key, '1')

    def test_explicit_off_retains_ready_and_arrivals_until_on(self):
        s = self.selection
        s.set_auto_enabled(False)
        selected = s.selected_key
        self.rows[2]["state"] = "ready"
        new = dict(key="new", first_seen=4, last_seen=4, state="working")
        s.update(self.rows + [new])
        self.assertEqual(s.selected_key, selected)
        self.assertFalse(s.auto_enabled)
        s.set_auto_enabled(True)
        self.assertTrue(s.auto_enabled)
        self.assertEqual(s.selected_key, "new")
        s.set_auto_enabled(False)
        s.update(self.rows)
        self.assertIn(s.selected_key, {r["key"] for r in self.rows})
        self.assertFalse(s.auto_enabled)

    def test_visible_character_anchor_not_launcher_or_stale_external(self):
        from overlay.main import OverlayApp

        app = object.__new__(OverlayApp)
        app.character = Mock()
        app.character._launcher_canvas = None
        app.character._external_rect = None
        app.character.root.winfo_viewable.return_value = True
        app.character.get_bundled_phys_rect.return_value = (50, 500, 160, 160)
        app._overlay_events = Mock(mode="observer")
        app._presentation_mode = "full"
        self.assertEqual(app._session_stack_anchor(), (50, 500, 160, 160))
        app._presentation_mode = "launcher"
        self.assertIsNone(app._session_stack_anchor())
        app._presentation_mode = "full"
        app.character._external_rect = (300, 400, 240, 280)
        app.character.get_phys_rect.return_value = app.character._external_rect
        app._overlay_events.mode = "replace"
        app._overlay_events.supports.return_value = True
        app._external_renderer_visible = False
        self.assertIsNone(app._session_stack_anchor())
        app._external_renderer_visible = True
        self.assertEqual(app._session_stack_anchor(), (300, 400, 240, 280))
        app.character.get_phys_rect.return_value = (700, 800, 240, 280)
        self.assertEqual(app._session_stack_anchor(), (700, 800, 240, 280))
        app._overlay_events.supports.return_value = False
        app._presentation_mode = (
            "launcher"  # Legacy renderers do not implement collapse.
        )
        self.assertEqual(app._session_stack_anchor(), (700, 800, 240, 280))

    def test_bubble_foreground_fanout_does_not_demote_monitor(self):
        from overlay.main import OverlayApp

        app = object.__new__(OverlayApp)
        app._bubble_manager = Mock()
        app._bubble_input = Mock()
        app._session_stack = Mock()
        app._apply_overlay_foreground(False, 456)
        app._bubble_manager.set_overlay_foreground.assert_called_once_with(False, 456)
        app._session_stack.set_overlay_foreground.assert_not_called()
        app._session_stack.restack_monitor.assert_called_once_with()

    def setUp(self):
        self.now = 0
        self.selection = SessionSelection(clock=lambda: self.now)
        self.rows = [
            dict(key=str(i), first_seen=i, last_seen=i, state="working", state_since=0)
            for i in range(3)
        ]
        self.selection.update(self.rows)

    def test_auto_order_and_pin(self):
        s = self.selection
        self.assertEqual(s.selected_key, "2")
        s.pin("0")
        self.rows[1]["last_seen"] = 99
        s.update(self.rows)
        self.assertEqual(s.selected_key, "0")
        s.resume_auto()
        self.assertEqual(s.selected_key, "0")
        self.assertEqual([r["key"] for r in s.rows], ["0", "1", "2"])

    def test_trailing_debounce(self):
        s = self.selection
        s.browse(1)
        self.now = 0.3
        s.browse(1)
        self.now = 0.5
        s.settle()
        self.assertIsNone(s.pinned_key)
        self.now = 0.71
        s.settle()
        self.assertEqual(s.pinned_key, "1")

    def test_removal_and_ack_reset(self):
        s = self.selection
        self.rows[0]["state"] = "needs_input"
        s.update(self.rows)
        s.jump_attention()
        self.assertTrue(s.rows[0]["acknowledged"])
        self.rows[0]["state"] = "blocked"
        s.update(self.rows)
        self.assertFalse(s.rows[0]["acknowledged"])
        s.browse(1)
        s.update([self.rows[0], self.rows[2]])
        self.assertIsNone(s.pinned_key)
        self.assertEqual(s.selected_key, "0")
        s.update([])
        self.assertIsNone(s.selected_key)

    def test_completion_handoff_accumulates_arrivals_not_heartbeats(self):
        s = SessionSelection()
        first = dict(key="a", first_seen=0, last_seen=0, state="working")
        second = dict(key="b", first_seen=1, last_seen=1, state="unknown")
        s.update([first])
        s.update([first, second])
        self.assertEqual(s.selected_key, "a")
        second["last_seen"] = 999
        for state in ("working", "needs_input", "blocked", "unknown"):
            first["state"] = state
            s.update([first, second])
            self.assertEqual(s.selected_key, "a")
        first["state"] = "ready"
        s.update([first, second])
        self.assertEqual(s.selected_key, "b")
        second["state"] = "ready"
        first["last_seen"] = 1000
        s.update([first, second])
        self.assertEqual(s.selected_key, "b")  # No backwards completed bounce.

    def test_manual_ready_choice_consumes_observed_not_future_arrivals(self):
        s = self.selection
        self.rows[0]["state"] = "ready"
        s.update(self.rows)
        s.pin("0")
        s.update(self.rows)
        self.assertEqual(s.selected_key, "0")
        new = dict(key="new", first_seen=3, last_seen=3, state="unknown")
        s.update(self.rows + [new])
        self.assertEqual(s.selected_key, "0")
        self.assertFalse(s.auto_enabled)
        s.resume_auto()
        self.assertEqual(s.selected_key, "new")
        self.assertIsNone(s.pinned_key)

    def test_manual_work_and_auto_preserve_pending_arrival(self):
        s = self.selection
        s.pin("0")
        new = dict(key="new", first_seen=3, last_seen=3, state="working")
        s.update(self.rows + [new])
        s.resume_auto()
        self.assertEqual(s.selected_key, "0")
        self.rows[0]["state"] = "ready"
        s.update(self.rows + [new])
        self.assertEqual(s.selected_key, "new")

    def test_ready_arrival_during_wheel_preview_waits_for_manual_commit(self):
        s = self.selection
        self.rows[2]["state"] = "ready"
        s.update(self.rows)
        s.browse(1)
        new = dict(key="new", first_seen=3, last_seen=3, state="working")
        self.now = 0.2
        s.update(self.rows + [new])
        self.assertEqual((s.selected_key, s.candidate_key), ("2", "0"))
        self.now = 0.41
        s.update(self.rows + [new])
        self.assertEqual(s.selected_key, "0")
        self.rows[0]["state"] = "ready"
        s.update(self.rows + [new])
        self.assertEqual(
            s.selected_key, "0"
        )  # New arrival was consumed at manual commit.


if __name__ == "__main__":
    unittest.main()


class HostSelectionRoutingTests(unittest.TestCase):
    def test_unselected_submit_and_typing_preserve_monitored_semantics(self):
        app = self.make_host()
        app._session_selection.update(
            app._session_selection.rows
            + [dict(key="mcp:other", first_seen=1, last_seen=1, state="working")]
        )
        app._session_selection.pin("mcp:other")
        app._mark_overlay_engaged = Mock()
        app._bubble_input = Mock()
        app._bubble_session = Mock()
        app._nudge_awaiting_reply = False
        app._on_bubble_input_activity(True)
        app._on_bubble_input_activity(False)
        app._on_bubble_submit("safe fixture message")
        app.character.set_input_active.assert_not_called()
        app.character.set_sprite_state.assert_not_called()
        app._overlay_events.publish.assert_not_called()
        app._bubble_session.send.assert_called_once_with("safe fixture message")
        app._bubble_manager.show_user_message.assert_called_once_with(
            "safe fixture message"
        )
        app._bubble_manager.show_echo.assert_called_once()
        self.assertTrue(app._bubble_turn_active)

    def make_host(self, listening=True):
        from overlay.main import OverlayApp

        app = object.__new__(OverlayApp)
        app._quitting = False
        app._stm_server = SimpleNamespace(listening=listening)
        app._bubble_state = SimpleNamespace(session_id="owned") if listening else None
        app._session_selection = SessionSelection()
        app._session_selection.update(
            [
                dict(
                    key="claude:owned",
                    first_seen=0,
                    last_seen=0,
                    state="needs_input",
                    state_since=0,
                )
            ]
        )
        app.character = Mock()
        app._overlay_events = Mock()
        app._bubble_manager = Mock()
        app._initiative = Mock()
        return app

    def test_waiting_keeps_raw_ui_but_not_character_work(self):
        app = self.make_host()
        event = dict(
            kind="thought", text="private thought must not enter semantic cache"
        )
        app._on_bubble_event(event)
        app._bubble_manager.handle_event.assert_called_once_with(event)
        app._initiative.feed_event.assert_called_once_with(event)
        app.character.handle_bubble_event.assert_not_called()
        app._overlay_events.publish_bubble.assert_not_called()
        self.assertEqual(
            app._bubble_semantic,
            ("claude:owned", ("generation.thinking", "thought", {})),
        )

    def test_unowned_listener_preserves_original_bubble_routing(self):
        app = self.make_host(False)
        app._session_selection.update([])
        event = dict(kind="thought")
        app._on_bubble_event(event)
        app.character.handle_bubble_event.assert_called_once_with(event)
        app._overlay_events.publish_bubble.assert_called_once_with(event)
