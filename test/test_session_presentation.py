import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from copy import deepcopy
from overlay.session_presentation import SessionPresentation
from overlay.session_registry import SessionStateRegistry
from overlay.bubble.state import BubbleStateController


class PresentationTests(unittest.TestCase):
    def test_observed_bubble_terminal_event_once_new_turn_and_no_late_replay(self):
        from overlay.main import OverlayApp
        from overlay.session_selection import SessionSelection
        app = object.__new__(OverlayApp)
        app._bubble_state = SimpleNamespace(session_id='owned')
        app._stm_server = SimpleNamespace(listening=True)
        app._initiative = app._bubble_manager = Mock()
        app.character = Mock()
        app._overlay_events = Mock()
        app._session_selection = SessionSelection()
        row = dict(key='claude:owned', state='ready', state_since=1, first_seen=0)
        app._session_selection.update([row])
        presentation = Mock()
        presentation.state.return_value = 'ready'
        app._session_stack = SimpleNamespace(presentation=presentation)
        app._refresh_session_stack = Mock()
        event = {'kind': 'turn_end', 'is_error': False}
        app._on_bubble_event(event)
        app._on_bubble_event(event)
        self.assertEqual(app._overlay_events.publish_bubble.call_count, 1)
        row['state_since'] = 2
        app._session_selection.update([row])
        app._on_bubble_event(event)
        self.assertEqual(app._overlay_events.publish_bubble.call_count, 2)
        presentation.state.return_value = 'idle'
        app._on_bubble_event(event)
        self.assertEqual(app._overlay_events.publish_bubble.call_count, 2)

    def setUp(self):
        self.now = 0
        self.view = SessionPresentation(clock=lambda: self.now)
        self.rows = [dict(key='a', state='working', state_since=1),
                     dict(key='b', state='unknown', state_since=1)]
        self.view.update(self.rows, 'a')

    def test_dwell_idle_no_raw_mutation_then_new_completion(self):
        self.rows[0].update(state='ready', state_since=2)
        self.view.update(self.rows, 'a')
        before = deepcopy(self.rows)
        self.assertEqual(self.view.state(self.rows[0]), 'ready')
        self.now = 1.99
        self.view.update(self.rows, 'a')
        self.assertEqual(self.view.state(self.rows[0]), 'ready')
        self.now = 2
        self.view.update(self.rows, 'a')
        self.assertEqual(self.view.state(self.rows[0]), 'idle')
        self.assertEqual(self.rows, before)
        self.rows[0]['state_since'] = 3  # A turn completed between two polls.
        self.view.update(self.rows, 'a')
        self.assertEqual(self.view.state(self.rows[0]), 'ready')

    def test_initial_ready_idle_and_away_back_consumes(self):
        initial = SessionPresentation()
        self.rows[0].update(state='ready', state_since=2)
        initial.update(self.rows, 'a')
        self.assertEqual(initial.state(self.rows[0]), 'idle')
        self.view.update(self.rows, 'a')
        self.view.update(self.rows, 'b')
        self.view.update(self.rows, 'a')
        self.assertEqual(self.view.state(self.rows[0]), 'idle')

    def test_simultaneous_completion_handoff_does_not_rearm_old_card(self):
        self.rows[0].update(state='ready', state_since=2)
        self.view.update(self.rows, 'b')
        self.view.update(self.rows, 'a')
        self.assertEqual(self.view.state(self.rows[0]), 'idle')
        for state in ('working', 'blocked', 'needs_input', 'unknown'):
            self.rows[0]['state'] = state
            self.view.update(self.rows, 'a')
            self.assertEqual(self.view.state(self.rows[0]), state)

    def test_owned_bubble_projection_not_spoof_and_private_metadata_survives(self):
        registry = SessionStateRegistry()
        bubble = BubbleStateController(registry)
        key = f'claude:{bubble.session_id}'
        old = {'producer': 'Existing saved title', 'manual': 'Saved preference'}
        registry.restore_title_metadata(key, old)
        for row in (registry.snapshot()[0], registry.presence(
                {'provider': 'claude', 'session_id': bubble.session_id, 'bubble_owner': True}),
                registry.upsert({'provider': 'claude', 'session_id': bubble.session_id, 'state': 'ready'})):
            self.assertEqual(row['label'], '오버레이 세션')
            self.assertTrue(row['is_bubble'])
        self.assertFalse(bubble.set_label('New name'))
        self.assertEqual(registry.title_metadata(key), old)
        spoof = registry.upsert({'provider': 'mcp', 'session_id': 'unowned',
                                 'state': 'ready', 'is_bubble': True, 'label': 'External title'})
        self.assertIsNone(spoof['is_bubble'])
        self.assertEqual(spoof['label'], 'External title')
        self.assertTrue(registry.set_label(spoof['key'], 'External renamed'))
