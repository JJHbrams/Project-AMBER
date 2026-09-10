import asyncio
import unittest
from concurrent.futures import Future, InvalidStateError
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from claude_code_sdk.types import ResultMessage, SystemMessage, ToolPermissionContext
from overlay.bubble.approval import ApprovalRequest, ToolApprovalBroker
from overlay.bubble.session import BubbleSessionManager
from overlay.bubble.state import BubbleStateController
from overlay.session_registry import SessionStateRegistry


class BubbleStateTests(unittest.TestCase):
    def test_explicit_bubble_title_survives_heartbeat_without_state_change(self):
        state=BubbleStateController(self.registry,'title-fixture')
        state.event('thought')
        before=next(row for row in self.registry.snapshot() if row['session_id']=='title-fixture')
        self.assertFalse(state.set_label('말풍선 작업 제목'))
        state.heartbeat()
        after=next(row for row in self.registry.snapshot() if row['session_id']=='title-fixture')
        self.assertEqual(after['label'],'오버레이 세션')
        self.assertEqual((after['state'],after['state_since']),(before['state'],before['state_since']))
        state.retire()
        self.assertFalse(state.set_label('retired'))

    def setUp(self):
        self.now = 0.0
        self.registry = SessionStateRegistry(clock=lambda: self.now)
        self.state = BubbleStateController(self.registry, 'owner')

    def row(self):
        rows = self.registry.snapshot()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_approval_button_tolerates_concurrent_cancellation(self):
        future = Mock()
        future.done.return_value = False
        future.set_result.side_effect = InvalidStateError()
        request = ApprovalRequest('raced', 'Write', {}, future)
        request.allow()
        request.deny()
        self.assertEqual(future.set_result.call_count, 2)

    def test_fold_and_approval_priority(self):
        self.assertEqual(self.row()['state'], 'unknown')
        for kind in ('submit', 'speech', 'thought', 'tool_use', 'tool_result'):
            self.state.event(kind)
            self.assertEqual(self.row()['state'], 'working')
        self.state.approval_requested('one')
        self.state.approval_requested('two')
        self.state.event('tool_result')
        self.state.approval_settled('one')
        self.assertEqual(self.row()['state'], 'needs_input')
        self.state.event('turn_end')
        self.state.approval_settled('two')
        self.assertEqual(self.row()['state'], 'ready')
        self.state.event('tool_result', is_error=True)
        self.assertEqual(self.row()['state'], 'blocked')
        self.state.event('unrecognized')
        self.assertEqual(self.row()['state'], 'blocked')
        self.state.event('turn_end', is_error=True)
        self.assertEqual(self.row()['state'], 'blocked')

    def test_alias_convergence_authority_and_order(self):
        self.now = 2
        self.registry.upsert({'provider': 'claude', 'session_id': 'provider-id', 'state': 'ready'})
        self.state.bind_provider_session('provider-id')
        self.state.event('submit')
        self.state.approval_requested('request')
        self.now = 3
        self.registry.upsert({'provider': 'claude', 'session_id': 'provider-id', 'state': 'working', 'is_bubble': False})
        row = self.row()
        self.assertEqual(row['key'], 'claude:owner')
        self.assertEqual(row['first_seen'], 0)
        self.assertEqual(row['last_seen'], 3)
        self.assertEqual(row['state'], 'needs_input')
        self.assertTrue(row['is_bubble'])
        self.assertNotIn('provider-id', str(row))

    def test_alias_does_not_steal_other_owner(self):
        other = BubbleStateController(self.registry, 'other')
        self.state.bind_provider_session(other.session_id)
        self.assertEqual(len(self.registry.snapshot()), 2)
        other.retire()
        self.assertEqual(self.row()['session_id'], 'owner')

    def test_heartbeat_and_expiry_then_retire_stale_callbacks(self):
        self.state.event('submit')
        self.now = 599
        self.state.heartbeat()
        self.now = 700
        self.assertEqual(self.row()['state_since'], 0)
        self.assertEqual(self.row()['last_seen'], 599)
        self.now = 1199
        self.assertEqual(self.registry.snapshot(), [])
        self.state.heartbeat()
        self.state.retire()
        self.state.event('speech')
        self.state.heartbeat()
        self.state.approval_requested('late')
        self.state.approval_settled('late')
        self.state.bind_provider_session('late-id')
        self.assertEqual(self.registry.snapshot(), [])

    def test_provider_metadata_and_failed_result(self):
        manager = BubbleSessionManager(cwd='.', state_controller=self.state)
        self.assertEqual(manager._build_options().env['ENGRAM_BUBBLE_SESSION_ID'], 'owner')
        manager._handle_message(SystemMessage(subtype='init', data={'session_id': 'actual', 'secret': 'PRIVATE'}))
        self.registry.upsert({'provider': 'claude', 'session_id': 'actual', 'state': 'working'})
        manager._emit({'kind': 'thought', 'text': 'PRIVATE'})
        manager._handle_message(ResultMessage(subtype='error', duration_ms=1, duration_api_ms=1,
                                             is_error=True, num_turns=1, session_id='actual', result='PRIVATE'))
        self.assertEqual(self.row()['state'], 'blocked')
        self.assertNotIn('PRIVATE', str(self.registry.snapshot()))
        manager.stop()
        self.assertEqual(self.registry.snapshot(), [])


class ApprovalStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = SessionStateRegistry()
        self.state = BubbleStateController(self.registry)
        self.state.event('submit')
        self.requests = []
        self.settled = []

    def request(self, request):
        self.requests.append(request)
        self.state.approval_requested(request.id)

    def settle(self, request_id):
        self.settled.append(request_id)
        self.state.approval_settled(request_id)

    def current(self):
        return self.registry.snapshot()[0]['state']

    def broker(self, **kwargs):
        return ToolApprovalBroker('confirm_always', on_request=kwargs.get('on_request', self.request),
                                  timeout=kwargs.get('timeout', 1), on_settled=self.settle)

    async def test_overlapping_allow_deny(self):
        broker = self.broker()
        tasks = [asyncio.create_task(broker.can_use_tool('Write', {'secret': 'PRIVATE'}, ToolPermissionContext())) for _ in range(2)]
        await asyncio.sleep(0)
        self.assertEqual(self.current(), 'needs_input')
        self.requests[0].allow()
        self.assertEqual((await tasks[0]).behavior, 'allow')
        self.assertEqual(self.current(), 'needs_input')
        self.requests[1].deny()
        self.assertEqual((await tasks[1]).behavior, 'deny')
        self.assertEqual(self.current(), 'working')
        self.assertEqual(len(self.settled), 2)

    async def test_timeout_callback_failure_and_cancellation(self):
        result = await self.broker(timeout=0.001).can_use_tool('Write', {}, ToolPermissionContext())
        self.assertEqual(result.behavior, 'deny')
        self.assertEqual(self.current(), 'working')
        def broken(request):
            self.request(request)
            raise RuntimeError('controlled UI failure')
        result = await self.broker(on_request=broken).can_use_tool('Write', {}, ToolPermissionContext())
        self.assertEqual(result.behavior, 'deny')
        self.assertEqual(self.current(), 'working')
        task = asyncio.create_task(self.broker().can_use_tool('Write', {}, ToolPermissionContext()))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.current(), 'working')
        self.assertEqual(len(self.settled), 3)
        self.assertTrue(all(request.future.done() for request in self.requests))

    async def test_settled_failure_preserves_permission_result(self):
        def request(req):
            req.allow()
        def broken(_):
            raise RuntimeError('controlled settlement failure')
        broker = ToolApprovalBroker('confirm_always', on_request=request, on_settled=broken)
        result = await broker.can_use_tool('Write', {}, ToolPermissionContext())
        self.assertEqual(result.behavior, 'allow')

    async def test_auto_allow_does_not_emit_approval(self):
        broker = ToolApprovalBroker('confirm_risky', on_request=self.request, on_settled=self.settle)
        result = await broker.can_use_tool('Read', {}, ToolPermissionContext())
        self.assertEqual(result.behavior, 'allow')
        self.assertEqual(self.requests, [])
        self.assertEqual(self.settled, [])

    async def test_manager_heartbeat_cancels_after_provider_exit(self):
        manager = BubbleSessionManager(cwd='.', state_controller=self.state)
        calls = []
        async def consume():
            await asyncio.sleep(0.025)
        with patch.object(manager, '_run_forever', consume), patch.object(self.state, 'heartbeat', lambda: calls.append(1)), patch('overlay.bubble.session._STATE_HEARTBEAT_SECS', 0.005):
            await manager._run_lifetime()
            count = len(calls)
            await asyncio.sleep(0.015)
            self.assertGreater(count, 1)
            self.assertEqual(len(calls), count)


class HostStateWiringTests(unittest.TestCase):
    def setUp(self):
        import overlay.main as host
        self.host = host
        self.app = object.__new__(host.OverlayApp)
        self.app.root = Mock()
        self.callbacks = []
        self.app.root.after.side_effect = lambda delay, fn: self.callbacks.append(fn)
        self.app.chat = Mock()
        self.app._bubble_manager = Mock()
        self.app._on_bubble_event = Mock()
        self.app._bubble_session = None
        self.app._session_registry = SessionStateRegistry()
        self.app._stm_server = SimpleNamespace(listening=True)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for name, value in [('load_cfg', {}), ('get_bubble_cfg', {}), ('get_workdir', '.'),
                            ('get_permission_level', 'confirm_always'), ('get_bubble_session_id', None),
                            ('bubble_bootstrap_prompt', None), ('StmBridge', None)]:
            self.patches.enter_context(patch.object(host, name, return_value=value))
        self.patches.enter_context(patch.object(BubbleSessionManager, 'start'))
        self.patches.enter_context(patch.object(host, 'set_bubble_session_id'))

    def approval(self):
        session = self.app._bubble_session
        broker = session._can_use_tool.__self__
        req = ApprovalRequest('test-request', 'Write', {}, Future())
        broker._on_request(req)
        return req

    def test_deferred_approval_failure_denies_and_stale_does_not_show(self):
        self.app._ensure_bubble_session()
        req = self.approval()
        self.app._bubble_manager.show_approval_request.side_effect = RuntimeError('controlled failure')
        self.callbacks.pop()()
        self.assertEqual(req.future.result().behavior, 'deny')
        req = self.approval()
        self.app._bubble_session = None
        self.callbacks.pop()()
        self.assertEqual(req.future.result().behavior, 'deny')
        self.assertEqual(self.app._bubble_manager.show_approval_request.call_count, 1)

    def test_completed_approval_and_stale_events_are_not_delivered(self):
        self.app._ensure_bubble_session()
        req = self.approval()
        req.future.cancel()
        self.callbacks.pop()()
        self.app._bubble_manager.show_approval_request.assert_not_called()
        session = self.app._bubble_session
        session._emit({'kind': 'thought'})
        self.app._ensure_tui_mode()
        self.callbacks.pop()()
        self.app._on_bubble_event.assert_not_called()
        self.assertEqual(self.app._session_registry.snapshot(), [])

    def test_dead_replacement_retires_and_listener_disabled_skips_state(self):
        self.app._ensure_bubble_session()
        previous = self.app._bubble_state
        self.app._ensure_bubble_session()
        self.assertNotEqual(previous.session_id, self.app._bubble_state.session_id)
        previous.event('speech')
        self.assertEqual(len(self.app._session_registry.snapshot()), 1)
        self.app._ensure_tui_mode()
        self.app._stm_server.listening = False
        self.app._ensure_bubble_session()
        self.assertIsNone(self.app._bubble_state)
        self.assertEqual(self.app._session_registry.snapshot(), [])


if __name__ == '__main__':
    unittest.main()
