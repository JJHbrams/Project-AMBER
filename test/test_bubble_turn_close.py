"""턴 종료가 ResultMessage 한 종류에 종속되지 않는지 확인하는 회귀 테스트.

버그: ResultMessage가 끝내 안 오면(연결 종료, 재시도 등) 생각풍선/상태/게이트가
영영 안 풀렸다 — session.py._close_turn이 그 네 가지(state turn_end, terminal
emit, terminal_gate.set(), _current_request_key=None)를 어떤 종료 경로로도
정확히 한 번만 수행하는지를 검증한다.
"""
import asyncio
import unittest
from unittest.mock import patch

from claude_code_sdk.types import ResultMessage
from overlay.bubble.session import BubbleSessionManager
from overlay.bubble.turn_queue import TurnKey


class _EmptyStreamControl:
    """No messages at all — the stream simply ends (no ResultMessage)."""

    def __init__(self, *_a, **_k):
        pass

    async def connect(self):
        pass

    async def messages(self):
        return
        yield  # pragma: no cover - makes this an async generator

    async def close(self):
        pass


class _SingleMessageControl:
    """Yields exactly one (message, key) pair, then the stream ends."""

    def __init__(self, item):
        self._item = item

    async def connect(self):
        pass

    async def messages(self):
        yield self._item

    async def close(self):
        pass


class TurnCloseTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self):
        events = []
        manager = BubbleSessionManager(cwd='.', on_event=events.append)
        manager._prompt_queue = asyncio.Queue()
        return manager, events

    async def test_stream_ends_without_result_message_still_closes_the_turn(self):
        manager, events = self.make_manager()
        manager._terminal_gate = asyncio.Event()
        key = TurnKey('sess', 'req-1', manager._attempt_generation)
        manager._current_request_key = key

        with patch('overlay.bubble.claude_control.ClaudeControl', _EmptyStreamControl):
            await manager._consume()

        self.assertIsNone(manager._current_request_key)
        self.assertTrue(manager._terminal_gate.is_set())
        kinds = [e['kind'] for e in events]
        self.assertIn('provider_unknown', kinds)
        closing = next(e for e in events if e['kind'] == 'provider_unknown')
        self.assertTrue(closing['terminal'])
        self.assertTrue(closing['is_error'])

    async def test_stream_ends_with_no_pending_turn_is_a_silent_noop(self):
        # Gate 1: _current_request_key is already None (no active turn) —
        # closing must not fabricate an event or raise.
        manager, events = self.make_manager()
        manager._current_request_key = None

        with patch('overlay.bubble.claude_control.ClaudeControl', _EmptyStreamControl):
            await manager._consume()  # must not raise

        self.assertEqual(events, [])
        self.assertIsNone(manager._current_request_key)

    async def test_result_message_then_stream_end_closes_exactly_once(self):
        manager, events = self.make_manager()
        manager._terminal_gate = asyncio.Event()
        key = TurnKey('sess', 'req-1', manager._attempt_generation)
        manager._current_request_key = key
        manager._current_turn_seq = 1
        result = ResultMessage(
            subtype='success', duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id=None, result='done',
        )

        factory = lambda *_a, **_k: _SingleMessageControl(item=(result, key))
        with patch('overlay.bubble.claude_control.ClaudeControl', factory):
            await manager._consume()

        terminal_events = [e for e in events if e.get('terminal')]
        self.assertEqual(len(terminal_events), 1)
        self.assertEqual(terminal_events[0]['kind'], 'turn_end')
        self.assertIsNone(manager._current_request_key)
        self.assertTrue(manager._terminal_gate.is_set())

    async def test_generation_rotation_does_not_orphan_a_late_result_message(self):
        # Gate 4: a ResultMessage from a since-superseded attempt generation
        # must still close the turn it names instead of being silently
        # dropped by the top-of-function stale-generation guard.
        manager, events = self.make_manager()
        manager._terminal_gate = asyncio.Event()
        key = TurnKey('sess', 'req-1', 1)
        manager._current_request_key = key
        manager._attempt_generation = 2  # a retry already rotated past generation 1
        result = ResultMessage(
            subtype='success', duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id=None, result='stale answer',
        )

        manager._handle_message(result, generation=1, request_key=key)

        self.assertIsNone(manager._current_request_key)
        self.assertTrue(manager._terminal_gate.is_set())
        self.assertEqual(events[0]['kind'], 'provider_unknown')
        self.assertTrue(events[0]['terminal'])

    async def test_close_turn_is_idempotent_for_a_repeated_key(self):
        manager, events = self.make_manager()
        manager._terminal_gate = asyncio.Event()
        key = TurnKey('sess', 'req-1', manager._attempt_generation)
        manager._current_request_key = key

        first = manager._close_turn({'kind': 'provider_unknown', 'text': 'x'}, key, is_error=True)
        second = manager._close_turn({'kind': 'provider_unknown', 'text': 'x'}, key, is_error=True)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(events), 1)


if __name__ == '__main__':
    unittest.main()
