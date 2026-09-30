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
