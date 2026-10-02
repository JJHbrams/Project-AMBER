import asyncio
from core.integrations.mcp_recovery_bridge import BridgeConfig, StdioRecoveryBridge, UNKNOWN

class Writer:
    def __init__(self): self.messages=[]
    async def send(self, value): self.messages.append(value.message.model_dump(mode='json'))

def bridge():
    return StdioRecoveryBridge(BridgeConfig('http://127.0.0.1:25185/mcp','http://127.0.0.1:25184/state/mcp/events','x'))

def test_sent_mutation_is_unknown_and_never_replayed():
    async def run():
        b=bridge(); w=Writer(); b.writer=w; b.available=True
        await b.frontend_message({'jsonrpc':'2.0','id':7,'method':'tools/call','params':{'name':'mutate'}})
        assert len(b.pending)==1 and w.messages[0]['id'].startswith('engram-request-')
        await b.fail_transport()
        assert b.pending=={} and (await b.output.get())['error']['code']==UNKNOWN
        assert len(w.messages)==1
    asyncio.run(run())

def test_callback_and_cancellation_ids_are_translated():
    async def run():
        b=bridge(); w=Writer(); b.writer=w; b.available=True
        b.callbacks[11]='backend-11'
        await b.frontend_message({'jsonrpc':'2.0','id':11,'result':{'roots':[]}})
        assert w.messages[-1]['id']=='backend-11'
        b.pending['up']={'kind':'request','id':4}
        await b.frontend_message({'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':4}})
        assert 'up' not in b.pending and w.messages[-1]['params']['requestId']=='up'
    asyncio.run(run())

def test_initialize_is_only_protocol_message_replayed():
    async def run():
        b=bridge();w=Writer();b.writer=w
        await b.frontend_message({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'t','version':'1'}}})
        assert w.messages[-1]['method']=='initialize'
        await b.fail_transport(); b.writer=Writer(); await b.send_first_initialize()
        assert b.writer.messages[-1]['method']=='initialize'
    asyncio.run(run())

def test_default_stdin_reads_raw_utf8_even_on_cp949_console(monkeypatch):
    # Windows pipes give sys.stdin the locale code page; Claude writes raw UTF-8.
    import io, json, sys
    call = {'jsonrpc':'2.0','id':9,'method':'tools/call',
            'params':{'name':'engram_save_memory','arguments':{'content':'유두 apex 짝 규칙 병합'}}}
    wire = b'\xff\xfe broken\n' + (json.dumps(call, ensure_ascii=False) + '\n').encode('utf-8')
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(wire), encoding='cp949'))
    async def run():
        b=bridge(); w=Writer(); b.writer=w; b.available=True
        await b.frontend_loop()
        assert (await b.output.get())['error']['code']==-32700
        assert w.messages[-1]['params']['arguments']['content']=='유두 apex 짝 규칙 병합'
        assert b.writer is w and b.available
    asyncio.run(run())


def _fake_upstream(monkeypatch, behaviours):
    """streamable_http_client 대역. behaviours[n] = n번째 연결에서 tools/call 에 줄 응답 종류."""
    import contextlib
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage
    from core.integrations import mcp_recovery_bridge as mod
    log = {'connections': 0, 'initializes': 0, 'calls': 0}

    @contextlib.asynccontextmanager
    async def fake_client(endpoint, http_client=None):
        log['connections'] += 1
        mode = behaviours[min(log['connections'], len(behaviours)) - 1]
        queue = asyncio.Queue()

        class Reader:
            def __aiter__(self): return self
            async def __anext__(self): return await queue.get()

        class UpWriter:
            async def send(self, item):
                msg = item.message.model_dump(mode='json', by_alias=True, exclude_none=True)
                method = msg.get('method')
                if method == 'initialize' and mode == 'reinit_terminated':
                    log['initializes'] += 1
                    reply = {'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': 32600, 'message': 'Session terminated'}}
                elif method == 'initialize':
                    log['initializes'] += 1
                    reply = {'jsonrpc': '2.0', 'id': msg['id'], 'result': {
                        'protocolVersion': '2025-03-26', 'capabilities': {}, 'serverInfo': {'name': 's', 'version': '1'}}}
                elif method == 'tools/call':
                    log['calls'] += 1
                    if mode == 'terminated':
                        reply = {'jsonrpc': '2.0', 'id': msg['id'], 'error': {'code': 32600, 'message': 'Session terminated'}}
                    else:
                        reply = {'jsonrpc': '2.0', 'id': msg['id'], 'result': {'ok': True}}
                else:
                    return
                await queue.put(SessionMessage(JSONRPCMessage.model_validate(reply)))

        yield Reader(), UpWriter(), None

    monkeypatch.setattr(mod, 'streamable_http_client', fake_client)
    return log


INIT = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
    'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '1'}}}
STATE = {'instance_id': 'i', 'generation': 1, 'ready': True, 'endpoint': 'http://127.0.0.1:25185/mcp'}


async def _until(cond, what):
    for _ in range(300):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError('timeout: ' + what)


async def _start(b):
    b.snapshot = STATE
    await b.frontend_message(INIT)
    manage = asyncio.create_task(b.manage())
    b.changed.set()
    await _until(lambda: b.initialize_result is not None, 'first initialize')
    assert (await b.output.get())['id'] == 1
    await b.frontend_message({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    await _until(lambda: b.available, 'available')
    return manage


def _stop(b, manage):
    manage.cancel()
    if b.transport:
        b.transport.cancel()
    if b.init_deadline_task:
        b.init_deadline_task.cancel()
    if b.replay:
        b.replay['timer'].cancel()


CALL = {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': 'x'}}


def test_session_terminated_request_is_replayed_once_after_reinitialize(monkeypatch):
    log = _fake_upstream(monkeypatch, ['terminated', 'ok'])

    async def run():
        b = bridge()
        manage = await _start(b)
        # 만료된 세션이 핸들러 앞에서 거절한 요청: 에러 없이 보류됐다가 재연결 후 한 번 재전송된다
        await b.frontend_message(CALL)
        await _until(lambda: b.resubscribe.is_set(), 'resubscribe')
        assert b.output.empty() and b.replay is not None and b.pending == {}
        b.changed.set()
        reply = await asyncio.wait_for(b.output.get(), 2)
        assert reply == {'jsonrpc': '2.0', 'id': 7, 'result': {'ok': True}}  # 원래 frontend id
        assert log['calls'] == 2 and log['initializes'] == 2 and b.replay is None
        # 후속 요청은 한 번만 호출된다
        await b.frontend_message({**CALL, 'id': 8})
        assert (await asyncio.wait_for(b.output.get(), 2)) == {'jsonrpc': '2.0', 'id': 8, 'result': {'ok': True}}
        assert log['calls'] == 3
        _stop(b, manage)
    asyncio.run(run())


def test_replay_that_fails_again_reports_outcome_unknown_without_third_send(monkeypatch):
    log = _fake_upstream(monkeypatch, ['terminated', 'terminated'])

    async def run():
        b = bridge()
        manage = await _start(b)
        await b.frontend_message(CALL)
        await _until(lambda: b.resubscribe.is_set(), 'resubscribe')
        b.changed.set()
        reply = await asyncio.wait_for(b.output.get(), 2)
        assert reply['id'] == 7 and reply['error']['code'] == UNKNOWN and 'result' not in reply
        assert log['calls'] == 2 and b.replay is None and b.pending == {}
        assert b.output.empty()
        _stop(b, manage)
    asyncio.run(run())


def test_session_terminated_with_unrecoverable_transport_reports_outcome_unknown(monkeypatch):
    log = _fake_upstream(monkeypatch, ['terminated', 'reinit_terminated'])

    async def run():
        b = bridge()
        manage = await _start(b)
        await b.frontend_message(CALL)
        await _until(lambda: b.resubscribe.is_set(), 'resubscribe')
        b.changed.set()
        reply = await asyncio.wait_for(b.output.get(), 2)
        assert reply['id'] == 7 and reply['error']['code'] == UNKNOWN
        assert log['calls'] == 1 and b.replay is None  # 재전송 시도 자체가 없었다
        _stop(b, manage)
    asyncio.run(run())


def test_replay_window_expiry_reports_outcome_unknown(monkeypatch):
    from core.integrations import mcp_recovery_bridge as mod
    monkeypatch.setattr(mod, 'REPLAY_WINDOW', 0.05)
    _fake_upstream(monkeypatch, ['terminated', 'ok'])

    async def run():
        b = bridge()
        manage = await _start(b)
        await b.frontend_message(CALL)  # 재연결 신호가 오지 않는다
        reply = await asyncio.wait_for(b.output.get(), 2)
        assert reply['id'] == 7 and reply['error']['code'] == UNKNOWN and b.replay is None
        _stop(b, manage)
    asyncio.run(run())


def test_client_cancel_while_stashed_means_no_reply_and_no_replay(monkeypatch):
    log = _fake_upstream(monkeypatch, ['terminated', 'ok'])

    async def run():
        b = bridge()
        manage = await _start(b)
        await b.frontend_message(CALL)
        await _until(lambda: b.replay is not None and b.resubscribe.is_set(), 'stashed')
        await b.frontend_message({'jsonrpc': '2.0', 'method': 'notifications/cancelled',
                                  'params': {'requestId': 7}})
        assert b.replay is None
        b.changed.set()
        await _until(lambda: log['connections'] == 2 and b.available, 'reconnect')
        await asyncio.sleep(0.1)
        assert log['calls'] == 1, 'a cancelled request must not be re-sent'
        assert b.output.empty() and b.pending == {}
        _stop(b, manage)
    asyncio.run(run())


def test_session_terminated_detection_accepts_both_error_code_signs():
    from core.integrations.mcp_recovery_bridge import session_terminated
    for code in (32600, -32600):
        assert session_terminated({'error': {'code': code, 'message': 'Session terminated'}})
    assert not session_terminated({'error': {'code': -32600, 'message': 'Invalid Request'}})
    assert not session_terminated({'error': {'code': -32603, 'message': 'Session terminated'}})


def test_non_session_terminated_loss_is_never_replayed():
    async def run():
        b = bridge(); w = Writer(); b.writer = w; b.available = True
        await b.frontend_message(CALL)
        await b.fail_transport()
        reply = await b.output.get()
        assert reply['id'] == 7 and reply['error']['code'] == UNKNOWN
        assert b.replay is None and len(w.messages) == 1
        b.writer = Writer(); await b.replay_stashed()
        assert b.writer.messages == []
    asyncio.run(run())


def test_session_terminated_during_reinitialize_fails_fast(monkeypatch):
    # reinitialize 응답을 기다리는 connection() 이 20초 타임아웃까지 매달리면 안 된다.
    import time
    log = _fake_upstream(monkeypatch, ['reinit_terminated'])
    async def run():
        b = bridge()
        b.initialize_result = {'protocolVersion': '2025-03-26', 'capabilities': {}}
        b.initialize_params = {'protocolVersion': '2025-03-26', 'capabilities': {}}
        started = time.monotonic()
        await asyncio.wait_for(b.connection('http://127.0.0.1:25185/mcp', 0), 5)
        assert time.monotonic() - started < 3
        assert log['initializes'] == 1 and not b.available
    asyncio.run(run())
