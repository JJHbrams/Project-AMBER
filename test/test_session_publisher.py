import unittest
from unittest.mock import Mock

from overlay.event_api import OverlayEventPublisher


class SelectedSnapshotTests(unittest.TestCase):
    def test_neutral_selection_clears_work_without_false_completion(self):
        publisher = OverlayEventPublisher({})
        client = Mock()
        client.handshake_complete = True
        publisher._clients['fixture'] = client
        for state in ('needs_input', 'unknown'):
            publisher.publish('generation.thinking', 'thought')
            client.reset_mock()
            publisher.select_session_state(state)
            message = client.enqueue.call_args.args[0]
            self.assertEqual(message['type'], 'state.snapshot')
            self.assertEqual(message['display_hint'], 'idle')
            self.assertEqual(message['payload'], {'generation_active': False, 'tool_category': None})
            self.assertEqual(client.enqueue.call_count, 1)

    def test_known_states_use_unchanged_snapshot_shape(self):
        publisher = OverlayEventPublisher({})
        client = Mock()
        client.handshake_complete = True
        publisher._clients['fixture'] = client
        for state, hint in [('working', 'generating'), ('ready', 'success'), ('blocked', 'error')]:
            publisher.select_session_state(state)
            message = client.enqueue.call_args.args[0]
            self.assertEqual(message['schema_version'], 2)
            self.assertEqual(message['display_hint'], hint)
            self.assertEqual(set(message['payload']), {'generation_active','tool_category'})
            self.assertEqual(message['payload']['generation_active'], state == 'working')


if __name__ == '__main__':
    unittest.main()
