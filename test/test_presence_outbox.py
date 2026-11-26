"""Presence outbox: the MCP event loop never waits for presence locks or IO.

Every reporter here is isolated: the title cache lives in a temporary
ENGRAM_SMOKE_DB_DIR (ENGRAM_DB_DIR is NOT isolation) and the discovery file in
the same temporary folder.
"""
import ast
import asyncio
import contextlib
import gc
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.integrations import mcp_presence
from core.integrations.mcp_presence import PresenceMiddleware, PresenceReporter
from core.integrations.presence_outbox import Op, Outbox, Ticket
from overlay.stm_server import STMServer

TURN = '00000000-0000-0000-0000-000000000001'
TURN2 = '00000000-0000-0000-0000-000000000002'


def context(transport, principal='local'):
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
        headers={}, scope={'engram.presence.transport_id': transport,
                           'engram.remote_principal': principal})))


class TrackingLock:
    """RLock that knows whether the *current thread* holds it."""

    def __init__(self):
        self._lock = threading.RLock()
        self._local = threading.local()

    def acquire(self, blocking=True, timeout=-1):
        ok = self._lock.acquire(blocking, timeout)
        if ok:
            self._local.depth = getattr(self._local, 'depth', 0) + 1
        return ok

    def release(self):
        self._local.depth -= 1
        self._lock.release()

    __enter__ = acquire

    def __exit__(self, *exc):
        self.release()

    def held(self):
        return getattr(self._local, 'depth', 0) > 0


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        guard = patch.dict(os.environ, {'ENGRAM_SMOKE_DB_DIR': self.temp.name})
        guard.start()
        self.addCleanup(guard.stop)
        self.discovery = Path(self.temp.name) / 'state.json'

    def reporter(self, **kwargs):
        reporter = PresenceReporter(self.discovery, **kwargs)
        self.addCleanup(reporter.stop)
        # Never touch the real user cache.
        self.assertEqual(Path(reporter._title_cache.path).parent, Path(self.temp.name))
        return reporter

    def write_discovery(self, instance_id='epoch-a', port=17999, token='a' * 43):
        self.discovery.write_text(json.dumps({
            'schema_version': 1, 'host': '127.0.0.1', 'port': port,
            'instance_id': instance_id, 'token': token}), encoding='utf-8')


class LoopNeverWaitsTests(IsolatedCase):
    """AC1: loop-facing entry points return immediately while presence is wedged."""

    def test_loop_paths_return_within_20ms_while_lock_and_io_are_blocked(self):
        reporter = self.reporter(ack_wait=0.01)
        gate = threading.Event()
        self.addCleanup(gate.set)
        reporter._send = lambda *a, **k: (gate.wait(10), True)[1]
        reporter._title_cache.lookup = lambda *a, **k: (gate.wait(10), None)[1]
        reporter._title_cache.store = lambda *a, **k: (gate.wait(10), None)[1]
        reporter._ensure_worker()  # starting the threads is not what is under test
        holding = threading.Event()

        def hold():
            with reporter._state_lock:
                holding.set()
                gate.wait(10)
        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        self.assertTrue(holding.wait(2))

        class Session:
            pass
        session = Session()
        legacy = SimpleNamespace(session=session, request_context=SimpleNamespace(request=None))
        http = context('transport-blocked')

        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'mcp-session-id', b'issued-session-id')]})
            await send({'type': 'http.response.body', 'body': b'{}'})

        middleware = PresenceMiddleware(app, reporter)

        async def request(method='POST', sid=None):
            scope = {'type': 'http', 'path': '/mcp', 'method': method,
                     'server': ('127.0.0.1', 17385), 'headers': []}
            if sid:
                scope['headers'].append((b'mcp-session-id', sid.encode()))
            async def receive():
                return {'type': 'http.request', 'body': b''}
            async def send(message):
                return None
            await middleware(scope, receive, send)

        timings = {}

        async def timed(name, call):
            started = time.perf_counter()
            result = call()
            if asyncio.iscoroutine(result):
                await result
            timings[name] = time.perf_counter() - started

        async def exercise():
            await timed('submit', lambda: reporter.submit('s' * 32))
            await timed('submit_ended', lambda: reporter.submit('s' * 32, ended=True))
            await timed('report_context', lambda: reporter.report_context(http))
            await timed('context_identity_http', lambda: reporter.context_identity(http))
            await timed('context_identity_legacy', lambda: reporter.context_identity(legacy))
            await timed('identity', lambda: reporter.identity('transport'))
            await timed('middleware_initialize', lambda: request())
            await timed('middleware_delete', lambda: request('DELETE', 'issued-session-id'))

        asyncio.run(exercise())
        self.assertEqual(set(timings), {'submit', 'submit_ended', 'report_context',
                                        'context_identity_http', 'context_identity_legacy',
                                        'identity', 'middleware_initialize', 'middleware_delete'})
        for name, elapsed in timings.items():
            self.assertLess(elapsed, 0.02, f'{name} took {elapsed * 1000:.1f} ms with presence wedged')
        # The wedge is real: a worker thread asking for the lock really would block.
        self.assertFalse(reporter._state_lock.acquire(timeout=0.05))
        gate.set()
        holder.join(3)

    def test_explicit_reports_answer_queued_without_the_overlay(self):
        reporter = self.reporter(ack_wait=0.03)
        gate = threading.Event()
        self.addCleanup(gate.set)
        reporter._send = lambda *a, **k: (gate.wait(10), True)[1]
        started = time.perf_counter()
        result = reporter.report_state(context('transport-q'), 'working')
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(result, {'accepted': True, 'reason': 'queued'})


class LockDisciplineTests(IsolatedCase):
    """AC2: no network, sqlite, DPAPI or discovery read under _state_lock / entry locks."""

    def instrument(self, reporter):
        lock = TrackingLock()
        reporter._state_lock = lock
        local = threading.local()
        violations, calls = [], {'send': 0, 'lookup': 0, 'store': 0, 'discovery': 0}

        def entry_held():
            return getattr(local, 'depth', 0) > 0

        for tracker in (reporter._claude_lifecycle, reporter._codex_lifecycle):
            original = tracker.transaction

            @contextlib.contextmanager
            def wrapped(identifier, original=original):
                with original(identifier) as entry:
                    local.depth = getattr(local, 'depth', 0) + 1
                    try:
                        yield entry
                    finally:
                        local.depth -= 1
            tracker.transaction = wrapped

        def guard(label):
            calls[label] += 1
            if lock.held() or entry_held():
                violations.append(label)

        counter = {'n': 0}

        def send(payload, **kwargs):
            guard('send')
            counter['n'] += 1
            if 'title_status' in kwargs:
                kwargs['title_status']['missing'] = counter['n'] % 2 == 0
            return True
        reporter._send = send
        real_lookup, real_store = reporter._title_cache.lookup, reporter._title_cache.store

        def lookup(*args, **kwargs):
            guard('lookup')
            return real_lookup(*args, **kwargs)

        def store(*args, **kwargs):
            guard('store')
            return real_store(*args, **kwargs)
        reporter._title_cache.lookup, reporter._title_cache.store = lookup, store
        real_discovery = reporter._discovery_info

        def discovery():
            guard('discovery')
            return real_discovery()
        reporter._discovery_info = discovery
        return violations, calls, guard

    def test_harness_detects_a_violation(self):
        reporter = self.reporter()
        violations, _, guard = self.instrument(reporter)
        with reporter._state_lock:
            guard('send')
        self.assertEqual(violations, ['send'])

    def test_scenarios_never_do_io_while_holding_state_or_entry_lock(self):
        reporter = self.reporter(ack_wait=5)
        violations, calls, _ = self.instrument(reporter)
        self.write_discovery('epoch-a')
        # A remembered title for a codex conversation forces a restore path.
        codex_binding = reporter._native_binding(context('probe'), 'codex', 'native-codex-restore')
        reporter._title_cache.store(codex_binding[0], 'scoped', codex_binding[1], 'Remembered codex title')
        calls['store'] = 0

        a, b = context('claude-a'), context('claude-b')
        emit = lambda ctx, event, **kw: reporter.report_claude_event(ctx, event, TURN, native_session_id='native-claude', **kw)
        self.assertTrue(emit(a, 'UserPromptSubmit')['accepted'])
        self.assertTrue(reporter.report_title(a, 'Safe claude title')['accepted'])
        self.assertTrue(reporter.report_project(a, project_name='Safe project', source=3)['accepted'])
        self.assertTrue(emit(b, 'PreToolUse', tool_name='Read', tool_use_id='read')['accepted'])  # alias binds
        self.assertTrue(reporter.report_title(b, 'Alias title')['accepted'])
        self.assertTrue(emit(a, 'PostToolUse', tool_name='Read', tool_use_id='read')['accepted'])
        self.assertTrue(reporter.report_state(a, 'working')['accepted'])

        c = context('codex-a')
        cemit = lambda ctx, event, **kw: reporter.report_codex_event(
            ctx, event, TURN, native_session_id='native-codex-restore', **kw)
        self.assertTrue(cemit(c, 'UserPromptSubmit')['accepted'])
        self.assertTrue(cemit(c, 'PreToolUse', tool_name='spawn_agent', tool_use_id='spawn')['accepted'])
        child = context('codex-child')
        self.assertTrue(cemit(child, 'SubagentStart', agent_id='child-a')['accepted'])
        self.assertTrue(cemit(child, 'SubagentStop', agent_id='child-a')['accepted'])

        class Session:
            pass
        session = Session()
        legacy = SimpleNamespace(session=session, request_context=SimpleNamespace(request=None))
        reporter.report_context(legacy)
        self.assertTrue(reporter.flush(5))
        del legacy, session
        gc.collect()
        self.write_discovery('epoch-b')  # overlay restart: metadata is re-published
        reporter._wake.set()
        self.assertTrue(reporter.flush(5))
        self.assertTrue(emit(a, 'SessionEnd')['accepted'])
        reporter.submit(reporter.context_identity(c)[0], ended=True)
        self.assertTrue(reporter.flush(5))

        self.assertEqual(violations, [])
        for label in ('send', 'lookup', 'store', 'discovery'):
            self.assertGreater(calls[label], 0, f'scenario never exercised {label}')

    def test_ast_no_forbidden_call_inside_state_lock_or_entry_lock_blocks(self):
        forbidden = {'_send', '_transmit', 'lookup', 'store', 'access', '_discovery_info', '_cached_title',
                     '_refresh_epoch', '_deliver', '_tick', '_process_inbox', '_outcome', 'wait', 'join',
                     'flush', 'sleep', 'open', 'urlopen', 'read_text', 'stat', 'connect', 'acquire'}
        source = Path(mcp_presence.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)

        def is_guarded(item):
            expr = item.context_expr
            if isinstance(expr, ast.Attribute) and expr.attr == '_state_lock':
                return True
            return (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
                    and expr.func.attr == 'transaction')
        guarded_blocks = offenders = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.With) and any(is_guarded(item) for item in node.items):
                guarded_blocks += 1
                for statement in node.body:
                    for call in ast.walk(statement):
                        if isinstance(call, ast.Call):
                            func = call.func
                            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
                            if name in forbidden:
                                offenders += 1
                                self.fail(f'{name}() inside a guarded block at line {call.lineno}')
        self.assertGreater(guarded_blocks, 10)
        self.assertEqual(offenders, 0)
        attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertNotIn('_title_lock', attributes)
        self.assertNotIn('_serialize_titles', names | attributes)


class OrderingTests(IsolatedCase):
    def sent_log(self, reporter):
        log = []

        def send(payload, **kwargs):
            log.append((dict(payload), kwargs.get('endpoint', '/state/presence')))
            if 'title_status' in kwargs:
                kwargs['title_status']['missing'] = False
            return True
        reporter._send = send
        return log

    def remember(self, title, native='native-shared'):
        first = self.reporter(ack_wait=5)
        self.sent_log(first)
        first.report_codex_event(context('first'), 'UserPromptSubmit', TURN, native_session_id=native)
        self.assertTrue(first.report_title(context('first'), title)['accepted'])
        self.assertTrue(first.flush(5))

    def test_stale_remembered_title_never_overrides_a_new_explicit_title(self):
        for order in ('event_then_title', 'title_then_event'):
            with self.subTest(order=order):
                self.remember('Stale remembered title')
                second = self.reporter(ack_wait=0.01)
                log = self.sent_log(second)
                ctx = context('second-' + order)
                second.pause_sender()
                if order == 'event_then_title':
                    second.report_codex_event(ctx, 'UserPromptSubmit', TURN, native_session_id='native-shared')
                    second.report_title(ctx, 'Fresh explicit title')
                else:
                    second.report_title(ctx, 'Fresh explicit title')
                    second.report_codex_event(ctx, 'UserPromptSubmit', TURN, native_session_id='native-shared')
                second.resume_sender()
                self.assertTrue(second.flush(5))
                titles = [p['title'] for p, endpoint in log if endpoint == '/state/title']
                self.assertEqual(titles[-1], 'Fresh explicit title')
                self.assertNotIn('Stale remembered title', titles)
                owner = second.context_identity(ctx)[0]
                self.assertEqual(second._native_titles[owner]['title'], 'Fresh explicit title')
                # The cache agrees with the in-memory record (invariant 4).
                binding = second._native_binding(ctx, 'codex', 'native-shared')
                self.assertEqual(second._title_cache.lookup(binding[0], 'scoped', binding[1]), 'Fresh explicit title')

    def test_only_a_delivered_latest_title_is_recorded(self):
        reporter = self.reporter(ack_wait=5)
        outcomes = iter([False, True])
        titles = []

        def send(payload, **kwargs):
            if 'title' in payload:
                titles.append(payload['title'])
                return next(outcomes)
            return True
        reporter._send = send
        ctx = context('title-failure')
        self.assertFalse(reporter.report_title(ctx, 'Undelivered title')['accepted'])
        owner = reporter.context_identity(ctx)[0]
        self.assertIsNone(reporter._native_titles[owner]['title'])
        self.assertTrue(reporter.report_title(ctx, 'Delivered title')['accepted'])
        self.assertEqual(reporter._native_titles[owner]['title'], 'Delivered title')

    def test_ended_or_detached_connection_is_not_resurrected_by_delayed_presence(self):
        reporter = self.reporter(ack_wait=0.01)
        log = self.sent_log(reporter)
        ctx = context('ending-transport')
        identifier = reporter.context_identity(ctx)[0]
        reporter.pause_sender()
        reporter.report_context(ctx)            # presence queued ...
        reporter.submit(identifier, ended=True)  # ... then the connection ends
        reporter.resume_sender()
        self.assertTrue(reporter.flush(5))
        mine = [p for p, _ in log if p['session_id'] == identifier]
        self.assertEqual([p.get('ended') for p in mine], [True])
        reporter.report_context(ctx)            # late presence for a detached transport
        reporter.submit(identifier)
        self.assertTrue(reporter.flush(5))
        self.assertEqual(len([p for p, _ in log if p['session_id'] == identifier]), 1)
        self.assertEqual(reporter.report_state(ctx, 'ready')['reason'], 'session_ended')
        self.assertEqual(reporter.report_title(ctx, 'Safe title here')['reason'], 'session_ended')

    def test_session_end_replaces_queued_state_and_blocks_later_presence(self):
        reporter = self.reporter(ack_wait=0.01)
        log = self.sent_log(reporter)
        ctx = context('claude-ending')
        identifier = reporter.context_identity(ctx)[0]
        reporter.pause_sender()
        reporter.report_claude_event(ctx, 'UserPromptSubmit', TURN)
        reporter.report_context(ctx)
        reporter.report_claude_event(ctx, 'SessionEnd')
        reporter.report_context(ctx)
        reporter.resume_sender()
        self.assertTrue(reporter.flush(5))
        mine = [p for p, _ in log if p['session_id'] == identifier]
        self.assertEqual(mine, [{'provider': 'mcp', 'session_id': identifier, 'ended': True}])


class SemanticOrderingTests(IsolatedCase):
    def test_semantic_seq_is_monotonic_and_events_survive_merging_on_a_real_server(self):
        server = STMServer(port=0, state_discovery_path=self.discovery)
        server.start()
        self.addCleanup(server.stop)
        reporter = self.reporter(timeout=2, ack_wait=0.0)
        recorded, sent = [], []
        put = reporter._outbox.put_lifecycle_state
        real_send = reporter._send

        def spy_put(key, payload, lifecycle, **kwargs):
            recorded.extend(payload['semantic']['events'])
            return put(key, payload, lifecycle, **kwargs)

        def spy_send(payload, **kwargs):
            if 'semantic' in payload:
                sent.append(payload['semantic'])
            return real_send(payload, **kwargs)
        reporter._outbox.put_lifecycle_state, reporter._send = spy_put, spy_send
        ctx = context('claude-semantic')
        reporter.pause_sender()
        calls = [('UserPromptSubmit', None), ('PreToolUse', 'a'), ('PostToolUse', 'a'), ('PreToolUse', 'b'),
                 ('PostToolUse', 'b'), ('PreToolUse', 'c'), ('PostToolUseFailure', 'c'),
                 ('PreToolUse', 'd'), ('PostToolUse', 'd'), ('Stop', None)]
        for event, tool in calls:
            result = reporter.report_claude_event(ctx, event, TURN, tool_name='Read' if tool else None,
                                                  tool_use_id=tool, native_session_id='native-semantic')
            self.assertEqual(result['reason'], 'queued')
        reporter.resume_sender()
        self.assertTrue(reporter.flush(8))
        self.assertEqual(reporter._stats['superseded'], 0, 'the overlay answered 409 (stale semantic seq)')
        self.assertEqual(reporter._stats['failed'], 0)
        seqs = [packet['seq'] for packet in sent]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertLess(len(sent), len(calls), 'packets were coalesced')
        delivered_events = [event for packet in sent for event in packet['events']]
        self.assertEqual(delivered_events, recorded)
        self.assertEqual(len(recorded), len(calls))
        row = server._state_registry.snapshot()[0]
        self.assertEqual(row['state'], 'ready')

    def test_child_update_merges_never_own_parent_state(self):
        outbox = Outbox(8)

        def packet(state, seq, events, child=False, total=0):
            payload = {'provider': 'mcp', 'session_id': 'owner', 'state': state, 'agent_name': 'Codex',
                       'subagent_count': total, 'child_activity': {'total': total, 'counts': {}},
                       'semantic': {'seq': seq, 'events': events, 'active_category': None}}
            if child:
                payload['child_update'] = True
            return payload
        key = ('mcp', 'owner')
        outbox.put_lifecycle_state(key, packet('working', 1, [{'type': 'generation.started', 'category': None}]), 'codex')
        outbox.put_lifecycle_state(key, packet('unknown', 2, [], child=True, total=2), 'codex')
        (merged,) = outbox.slots[key].packets
        self.assertEqual(merged.payload['state'], 'working')   # the parent keeps its own state
        self.assertNotIn('child_update', merged.payload)
        self.assertEqual(merged.payload['subagent_count'], 2)
        self.assertEqual(merged.payload['semantic']['seq'], 2)
        self.assertEqual(len(merged.payload['semantic']['events']), 1)
        # Past the overlay limit of two events per request, packets stay separate (no loss).
        events = [{'type': 'tool.started', 'category': 'other'}] * 2
        outbox.put_lifecycle_state(key, packet('working', 3, events), 'codex')
        outbox.put_lifecycle_state(key, packet('working', 4, events), 'codex')
        self.assertGreaterEqual(len(outbox.slots[key].packets), 3)
        self.assertEqual([p.payload['semantic']['seq'] for p in outbox.slots[key].packets],
                         sorted(p.payload['semantic']['seq'] for p in outbox.slots[key].packets))
        # Different providers never merge.
        outbox.put_lifecycle_state(key, dict(packet('working', 5, []), agent_name='Claude'), True)
        self.assertEqual(outbox.slots[key].packets[-1].lifecycle, True)


class StressTests(IsolatedCase):
    def test_eight_threads_keep_transport_owner_binding_maps_consistent(self):
        reporter = self.reporter(ack_wait=0.01)
        reporter._send = lambda *a, **k: True
        errors = []
        transports = [f'stress-{n}' for n in range(16)]

        def worker(index):
            try:
                for round_number in range(25):
                    name = transports[(index * 2 + round_number) % len(transports)]
                    ctx = context(name)
                    native = f'native-{(index + round_number) % 5}'
                    turn = f'00000000-0000-0000-0000-{index * 100 + round_number:012d}'
                    reporter.report_codex_event(ctx, 'UserPromptSubmit', turn, native_session_id=native)
                    reporter.report_title(ctx, f'Stress title {index}')
                    reporter.report_project(ctx, project_name=f'Project {index}')
                    reporter.report_context(ctx)
                    if round_number % 7 == 6:
                        reporter.submit(reporter.context_identity(ctx)[0], ended=True)
            except Exception as error:  # pragma: no cover - failure path
                errors.append(repr(error))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertTrue(reporter.flush(15))
        with reporter._state_lock:
            for transport, owner in reporter._transport_owners.items():
                self.assertIn(transport, reporter._owner_members.get(owner, set()))
            for owner, members in reporter._owner_members.items():
                self.assertTrue(members)
                for member in members:
                    self.assertEqual(reporter._transport_owners.get(member), owner)
            for transport in reporter._transport_owners:
                self.assertNotIn(transport, reporter._detached_transports)
            self.assertLessEqual(len(reporter._native_titles), reporter.capacity)
        # Retire everything: no map may keep a ghost.
        for transport in list(reporter._transport_owners):
            reporter.submit(transport, ended=True)
        self.assertTrue(reporter.flush(15))
        with reporter._state_lock:
            self.assertFalse(reporter._owner_members)
            self.assertFalse(reporter._transport_owners)
            self.assertFalse(reporter._binding_owners)


class SenderResilienceTests(IsolatedCase):
    def test_sender_survives_exceptions_and_keeps_delivering(self):
        reporter = self.reporter(ack_wait=5)
        delivered = []
        reporter._send = lambda payload, **kwargs: delivered.append(payload) or True
        real_inbox, boom = reporter._process_inbox, {'left': 1}

        def flaky_inbox():
            if boom['left']:
                boom['left'] -= 1
                raise RuntimeError('simulated sender failure')
            return real_inbox()
        reporter._process_inbox = flaky_inbox
        epoch = {'left': 1}
        real_epoch = reporter._refresh_epoch

        def flaky_epoch():
            if epoch['left']:
                epoch['left'] -= 1
                raise OSError('simulated discovery failure')
            return real_epoch()
        reporter._refresh_epoch = flaky_epoch
        reporter.report_context(context('resilient'))
        deadline = time.monotonic() + 5
        while not delivered and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(delivered, 'presence never delivered after sender exceptions')
        self.assertGreaterEqual(reporter._stats['errors'], 1)
        self.assertTrue(reporter._thread.is_alive())
        # A raising _send is a failed delivery, not a dead sender.
        reporter._send = lambda *a, **k: (_ for _ in ()).throw(ConnectionError('down'))
        self.assertEqual(reporter.report_state(context('resilient'), 'ready'),
                         {'accepted': False, 'reason': 'overlay_unavailable'})
        reporter._send = lambda *a, **k: True
        self.assertEqual(reporter.report_state(context('resilient'), 'ready')['reason'], 'delivered')

    def test_logs_carry_counts_only(self):
        reporter = self.reporter(ack_wait=0.5)
        token = 'SECRET_TOKEN_VALUE_' + 'x' * 20
        reporter._send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(token))
        with self.assertLogs('engram.mcp.presence', level='WARNING') as captured:
            reporter._note_error(RuntimeError(token))
        self.assertNotIn(token, ' '.join(captured.output))
        self.assertIn('RuntimeError', ' '.join(captured.output))


class OverlayEpochTests(IsolatedCase):
    def test_overlay_restart_republishes_title_and_project_without_a_request(self):
        reporter = self.reporter(ack_wait=5)
        self.write_discovery('epoch-a')
        log = []
        reporter._send = lambda payload, **kw: log.append((dict(payload), kw.get('endpoint', '/state/presence'))) or True
        ctx = context('epoch-transport')
        self.assertTrue(reporter.report_title(ctx, 'Safe epoch title')['accepted'])
        self.assertTrue(reporter.report_project(ctx, project_name='Epoch project', source=3)['accepted'])
        self.assertTrue(reporter.flush(5))
        log.clear()
        reporter._wake.set()
        self.assertTrue(reporter.flush(5))
        self.assertEqual(log, [], 'same overlay instance must not re-publish')
        self.write_discovery('epoch-b')  # a new overlay process (new instance_id)
        reporter._wake.set()
        self.assertTrue(reporter.flush(5))
        endpoints = [endpoint for _, endpoint in log]
        self.assertIn('/state/title', endpoints)
        self.assertIn('/state/project', endpoints)
        title = next(p for p, e in log if e == '/state/title')
        self.assertEqual(title['title'], 'Safe epoch title')
        self.assertEqual(next(p for p, e in log if e == '/state/project')['project_name'], 'Epoch project')
        self.assertEqual(endpoints.index('/state/presence'), 0)  # the row is recreated first

    def test_discovery_is_reread_only_when_the_file_changes(self):
        reporter = self.reporter()
        self.write_discovery('epoch-a')
        old = time.time() - 60
        os.utime(self.discovery, (old, old))  # past the racy-timestamp window
        reads = []
        real_read = Path.read_text

        def counting_read(path, *args, **kwargs):
            if path == self.discovery:
                reads.append(1)
            return real_read(path, *args, **kwargs)
        with patch.object(Path, 'read_text', counting_read):
            for _ in range(5):
                self.assertEqual(reporter._discovery_info()['instance_id'], 'epoch-a')
            self.assertEqual(len(reads), 1)
            self.write_discovery('epoch-bb-changed-size')
            os.utime(self.discovery, (old - 5, old - 5))
            self.assertEqual(reporter._discovery_info()['instance_id'], 'epoch-bb-changed-size')
            self.assertEqual(len(reads), 2)

    def test_missing_discovery_fails_fast_without_busy_waiting(self):
        reporter = self.reporter(ack_wait=2)
        started = time.perf_counter()
        result = reporter.report_state(context('docker-server'), 'working')
        self.assertEqual(result, {'accepted': False, 'reason': 'overlay_unavailable'})
        self.assertLess(time.perf_counter() - started, 1.0)
        cpu_before = time.process_time()
        time.sleep(0.4)
        self.assertLess(time.process_time() - cpu_before, 0.15)


class TitleHintTests(IsolatedCase):
    def missing_overlay(self, reporter):
        def send(payload, **kwargs):
            if 'title_status' in kwargs:
                kwargs['title_status']['missing'] = True
            return True
        reporter._send = send

    def test_title_is_requested_once_per_turn_when_none_is_known(self):
        reporter = self.reporter(ack_wait=5)
        self.missing_overlay(reporter)
        ctx = context('untitled')
        first = reporter.report_claude_event(ctx, 'UserPromptSubmit', TURN)
        self.assertEqual(first['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
        second = reporter.report_claude_event(ctx, 'PreToolUse', TURN, tool_name='Read')
        self.assertNotIn('hookSpecificOutput', second)
        third = reporter.report_claude_event(ctx, 'UserPromptSubmit', TURN2)
        self.assertIn('hookSpecificOutput', third)
        reporter.report_title(ctx, 'Now it has a title')
        fourth = reporter.report_claude_event(ctx, 'PostToolUse', TURN2, tool_name='Read')
        self.assertNotIn('hookSpecificOutput', fourth)
        self.assertNotIn('hookSpecificOutput', reporter.report_claude_event(
            ctx, 'UserPromptSubmit', '00000000-0000-0000-0000-000000000003'))

    def test_ack_timeout_answers_queued_and_defers_the_title_request_to_the_next_event(self):
        reporter = self.reporter(ack_wait=0.02)
        self.missing_overlay(reporter)
        ctx = context('queued-untitled')
        reporter.pause_sender()
        queued = reporter.report_claude_event(ctx, 'UserPromptSubmit', TURN)
        self.assertEqual(queued, {'accepted': True, 'reason': 'queued'})
        reporter.resume_sender()
        self.assertTrue(reporter.flush(5))  # the overlay now said "no title"
        nxt = reporter.report_claude_event(ctx, 'PreToolUse', TURN, tool_name='Read')
        self.assertEqual(nxt['accepted'], True)
        self.assertEqual(nxt['hookSpecificOutput']['hookEventName'], 'PreToolUse')

    def test_a_title_waiting_to_be_sent_counts_as_known(self):
        reporter = self.reporter(ack_wait=0.02)
        self.missing_overlay(reporter)
        ctx = context('pending-title')
        reporter.pause_sender()
        reporter.report_title(ctx, 'Pending safe title')
        result = reporter.report_claude_event(ctx, 'UserPromptSubmit', TURN)
        reporter.resume_sender()
        self.assertTrue(reporter.flush(5))
        self.assertNotIn('hookSpecificOutput', result)
        self.assertNotIn('hookSpecificOutput',
                         reporter.report_claude_event(ctx, 'PreToolUse', TURN, tool_name='Read'))


class CapacityTests(IsolatedCase):
    def test_end_marks_survive_capacity_pressure(self):
        reporter = self.reporter(capacity=3)
        reporter._thread = SimpleNamespace(is_alive=lambda: True)  # hold the sender off
        reporter.submit('e' * 32)
        reporter.submit('e' * 32, ended=True)
        for n in range(40):
            reporter.submit(f'{n:032d}')
        reporter._process_inbox()
        slots = reporter._outbox.slots
        self.assertIn(('mcp', 'e' * 32), slots)
        self.assertTrue(slots[('mcp', 'e' * 32)].ended.payload['ended'])
        self.assertLessEqual(len(slots), 4)

    def test_presence_only_slots_are_evicted_before_state_title_or_end_marks(self):
        outbox = Outbox(3)
        outbox.put_ended(('mcp', 'ended'), {'ended': True})
        outbox.put_title(('mcp', 'titled'), {'title': 'x'}, 1)
        outbox.put_presence(('mcp', 'presence'), {'p': 1})
        outbox.put_presence(('mcp', 'newcomer'), {'p': 2})
        self.assertEqual(set(outbox.slots), {('mcp', 'ended'), ('mcp', 'titled'), ('mcp', 'newcomer')})
        outbox.put_presence(('mcp', 'another'), {'p': 3})
        self.assertIn(('mcp', 'ended'), outbox.slots)  # end marks are never evicted
        self.assertIn(('mcp', 'titled'), outbox.slots)
        self.assertEqual(len(outbox.slots), 3)
        # An end mark is accepted even when full of end marks.
        for n in range(6):
            outbox.put_ended(('mcp', f'e{n}'), {'ended': True})
        self.assertTrue(all(('mcp', f'e{n}') in outbox.slots for n in range(6)))

    def test_alias_end_marks_are_retried_until_delivered(self):
        reporter = self.reporter(ack_wait=0.01)
        results = iter([False, False, True])
        alias_calls = []

        def send(payload, **kwargs):
            if payload.get('ended'):
                alias_calls.append(payload['session_id'])
                return next(results)
            return True
        reporter._send = send
        with patch('core.integrations.presence_outbox.ENDED_RETRY_SECONDS', 0.05):
            a, b = context('alias-a'), context('alias-b')
            reporter.report_codex_event(a, 'UserPromptSubmit', TURN, native_session_id='native-alias')
            reporter.report_codex_event(b, 'PreToolUse', TURN, tool_name='Read', native_session_id='native-alias')
            alias = reporter.context_identity(b)[0]
            deadline = time.monotonic() + 5
            while reporter._pending_alias_retirements and time.monotonic() < deadline:
                time.sleep(0.02)
        self.assertFalse(reporter._pending_alias_retirements)
        self.assertEqual(alias_calls, [alias] * 3)


class OutboxUnitTests(unittest.TestCase):
    def test_ticket_resolves_once_and_wait_zero_does_not_block(self):
        ticket = Ticket()
        started = time.perf_counter()
        self.assertFalse(ticket.wait(0))
        self.assertLess(time.perf_counter() - started, 0.05)
        ticket.resolve('delivered')
        ticket.resolve('failed')
        self.assertEqual(ticket.status, 'delivered')
        self.assertTrue(ticket.wait(0))

    def test_newer_title_replaces_pending_one_and_keeps_every_waiter(self):
        outbox = Outbox(4)
        first, second = Ticket(), Ticket()
        outbox.put_title(('mcp', 'o'), {'title': 'one'}, 1, ticket=first)
        outbox.put_title(('mcp', 'o'), {'title': 'two'}, 2, ticket=second)
        key, op = outbox.take(0)
        self.assertEqual((op.payload['title'], op.seq, len(op.tickets)), ('two', 2, 2))
        outbox.finish(key, op, 'delivered')
        self.assertEqual((first.status, second.status), ('delivered', 'delivered'))
        self.assertEqual(len(outbox), 0)

    def test_presence_lane_runs_on_its_own_pool_not_the_serial_or_default_executor(self):
        from core.integrations import mcp_tool_runtime as runtime

        async def go():
            return await runtime.run_helper('probe', lambda: threading.current_thread().name, lane='presence')
        self.assertTrue(asyncio.run(go()).startswith('engram-tool-presence'))

    def test_ended_drops_everything_else_and_blocks_new_work(self):
        outbox = Outbox(4)
        waiting = Ticket()
        outbox.put_presence(('mcp', 'o'), {'p': 1}, ticket=waiting)
        outbox.put_title(('mcp', 'o'), {'title': 't'}, 1)
        outbox.put_ended(('mcp', 'o'), {'ended': True})
        self.assertEqual(waiting.status, 'dropped')
        self.assertFalse(outbox.put_presence(('mcp', 'o'), {'p': 2}))
        key, op = outbox.take(0)
        self.assertEqual(op.kind, 'ended')
        self.assertIsNone(outbox.take(0))


if __name__ == '__main__':
    unittest.main()
