"""Session extension contract; legacy event envelopes remain unchanged."""
import json
import threading
from types import SimpleNamespace

import pytest

from overlay.event_api import (
    MAX_MESSAGE_BYTES, MAX_SESSION_ROWS, OverlayEventPublisher, _CatalogItem, _Client,
)


def client(identifier, capable=False, mode='observer', catalog=()):
    consumer = _Client(SimpleNamespace(), identifier, identifier, mode, ('observer', 'replace'),
                       frozenset({'session_stack'} if capable else ()), catalog)
    # Inspect the real serialized queue without starting socket IO in unit tests.
    consumer._writer = object()
    consumer.handshake_complete = True
    return consumer


def drain(consumer):
    result = [json.loads(data) for _, data in consumer._outbound]
    consumer._outbound.clear()
    return result


def rows(count=1):
    return [dict(provider='claude', session_id=f'id-{i}', key=f'claude:id-{i}',
                 state='working', first_seen=i, last_seen=i, label='Public alias',
                 subagent_count=2, thinking='PRIVATE_THOUGHT', tool_output='PRIVATE_OUTPUT')
            for i in range(count)]


def test_capability_gate_exact_fields_and_clock_dedup():
    pub = OverlayEventPublisher()
    legacy, capable = client('legacy'), client('capable', True)
    pub._clients = {1: legacy, 2: capable}
    source = rows()
    pub.publish_session_stack(source, 'claude:id-0')
    assert drain(legacy) == []
    events = drain(capable)
    assert [e['type'] for e in events] == ['session.stack_changed', 'session.state_changed']
    public = events[0]['payload']['sessions'][0]
    assert set(public) == {'provider', 'session_id', 'label', 'state', 'subagent_count', 'selected'}
    assert public == events[1]['payload']['session']
    assert 'PRIVATE' not in json.dumps(events)
    source[0]['last_seen'] = 99
    pub.publish_session_stack(source, 'claude:id-0')
    assert drain(capable) == []
    pub.publish_session_stack(source, None)
    assert drain(capable)[-1]['payload'] == {'session': None}
    with pytest.raises(ValueError):
        pub.publish('session.stack_changed', 'idle', {'thinking': 'PRIVATE'})


def test_optional_project_display_name_is_sanitized_and_capability_gated():
    pub = OverlayEventPublisher()
    legacy, capable = client('legacy'), client('capable', True)
    pub._clients = {1: legacy, 2: capable}
    source = rows()
    source[0]['project_name'] = 'Caller Project'
    pub.publish_session_stack(source, 'claude:id-0')
    assert drain(legacy) == []
    events = drain(capable)
    assert events[0]['payload']['sessions'][0]['project_name'] == 'Caller Project'
    assert events[1]['payload']['session']['project_name'] == 'Caller Project'
    source[0]['project_name'] = 'C:/PRIVATE_PROJECT'
    pub.publish_session_stack(source, 'claude:id-0')
    events = drain(capable)
    assert 'PRIVATE_PROJECT' not in json.dumps(events)
    assert events[0]['payload']['truncated']


def test_agent_display_claim_does_not_change_provider_identity():
    pub = OverlayEventPublisher()
    capable = client('capable', True)
    pub._clients = {1: capable}
    source = rows()
    source[0].update(provider='mcp', key='mcp:id-0', agent_name='Claude')
    pub.publish_session_stack(source, 'mcp:id-0')
    public = drain(capable)[0]['payload']['sessions'][0]
    assert (public['provider'], public['session_id'], public['agent_name']) == ('mcp', 'id-0', 'Claude')
    source[0]['agent_name'] = 'PRIVATE/not-allowed'
    pub.publish_session_stack(source, 'mcp:id-0')
    assert 'PRIVATE' not in json.dumps(drain(capable))


def test_initial_replay_preserves_legacy_handshake_and_empty_selection():
    pub = OverlayEventPublisher()
    pub.publish_session_stack(rows(), 'claude:id-0')
    legacy, capable = client('legacy'), client('capable', True)
    pub._send_welcome(legacy)
    pub._send_welcome(capable)
    old, new = drain(legacy), drain(capable)
    assert [e['type'] for e in old] == ['engram.welcome', 'state.snapshot', 'renderer.assignment']
    assert set(old[1]['payload']) == {'generation_active', 'tool_category'}
    assert [e['type'] for e in new][-2:] == ['session.stack_changed', 'session.state_changed']


def test_ownership_requires_active_ready_item_not_envelope_or_observer():
    pub = OverlayEventPublisher()
    capable = _CatalogItem('item', 'Item', ('observer', 'replace'), frozenset({'session_stack'}))
    legacy = _CatalogItem('legacy-item', 'Legacy', ('observer', 'replace'), frozenset())
    consumer = client('provider', True, 'replace', (capable, legacy))
    pub._clients = {1: consumer}
    pub._replace_owner = 1
    pub.selected_renderer_id = 'item'
    pub.publish_session_stack(rows(), 'claude:id-0')
    assert not pub.owns_session_stack
    assert drain(consumer) == []
    consumer.active_renderer_id = 'item'
    assert pub.owns_session_stack
    pub._send_session_snapshot(consumer)
    assert len(drain(consumer)) == 2
    consumer.mode = 'observer'
    assert not pub.owns_session_stack
    consumer.mode = 'replace'
    consumer.active_renderer_id = 'legacy-item'
    pub.selected_renderer_id = 'legacy-item'
    assert not pub.owns_session_stack
    pub._send_session_snapshot(consumer)
    assert drain(consumer) == []
    pub._clients.clear()
    assert not pub.owns_session_stack


def test_caps_utf8_bytes_validation_and_fail_visible():
    pub = OverlayEventPublisher()
    consumer = client('capable', True, 'replace')
    pub._clients = {1: consumer}
    pub._replace_owner = 1
    pub.selected_renderer_id = 'capable'
    source = rows(100)
    for row in source:
        row['label'] = '한' * 128
        row['session_id'] += '한' * 120
    pub.publish_session_stack(source, None)
    assert pub._session_snapshot['truncated']
    assert pub._session_snapshot['total_count'] == 100
    assert len(pub._session_snapshot['sessions']) <= MAX_SESSION_ROWS
    assert not pub.owns_session_stack
    for _, data in consumer._outbound:
        assert len(data) <= MAX_MESSAGE_BYTES
    pub.publish_session_stack([dict(rows()[0], label='C:/PRIVATE/PATH')], None)
    assert pub._session_snapshot['sessions'] == []
    assert pub._session_snapshot['truncated']
    pub.publish_session_stack(rows(), 'claude:id-0')
    assert pub.owns_session_stack


def test_registration_publication_cannot_preempt_welcome():
    pub = OverlayEventPublisher()
    consumer = client('capable', True)
    consumer.handshake_complete = False
    pub._clients = {1: consumer}
    pub._replace_owner = 1
    pub.publish('generation.thinking', 'thought')
    pub.publish_bubble({'kind': 'thought'})
    pub.publish('overlay.hide', 'idle')
    pub.select_session_state('working')
    pub.publish_session_stack(rows(), 'claude:id-0')
    assert drain(consumer) == []
    pub._send_welcome(consumer)
    assert [event['type'] for event in drain(consumer)] == [
        'engram.welcome', 'state.snapshot', 'renderer.assignment',
        'session.stack_changed', 'session.state_changed',
    ]


def test_assignment_replay_is_atomic_against_concurrent_snapshot():
    pub = OverlayEventPublisher()
    consumer = client('capable', True)
    pub._clients = {1: consumer}
    pub.publish_session_stack(rows(), None)
    drain(consumer)
    original = consumer.enqueue
    finished = threading.Event()
    workers = []

    def concurrent_publish():
        pub.publish_session_stack(rows(), 'claude:id-0')
        finished.set()

    def enqueue(message, *, droppable):
        if message['type'] == 'renderer.assignment':
            worker = threading.Thread(target=concurrent_publish)
            workers.append(worker)
            worker.start()
            assert not finished.wait(.05), 'publication overtook assignment'
        original(message, droppable=droppable)

    consumer.enqueue = enqueue
    pub.set_selection('capable', 'replace')
    for worker in workers:
        worker.join(2)
    assert finished.is_set()
    events = drain(consumer)
    assert events[0]['type'] == 'renderer.assignment'
    assert events[-1]['payload']['session']['selected']


def test_stalled_snapshot_queue_coalesces_and_control_overflow_fails_visible():
    from overlay.event_api import MAX_OUTBOUND_MESSAGES

    pub = OverlayEventPublisher()
    consumer = client('capable', True, 'replace')
    pub._clients = {1: consumer}
    pub._replace_owner = 1
    pub.selected_renderer_id = 'capable'
    source = rows()
    for n in range(1000):
        source[0]['state'] = 'working' if n % 2 else 'ready'
        pub.publish_session_stack(source, 'claude:id-0')
    assert len(consumer._outbound) == 2
    assert pub.owns_session_stack
    for n in range(MAX_OUTBOUND_MESSAGES + 1):
        consumer.enqueue({'type': 'renderer.assignment', 'payload': {'n': n}}, droppable=False)
    assert len(consumer._outbound) <= MAX_OUTBOUND_MESSAGES
    assert consumer._outbound_closed
    assert not pub.owns_session_stack
