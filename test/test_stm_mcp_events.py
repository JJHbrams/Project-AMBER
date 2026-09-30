import json, threading, time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from overlay.mcp_recovery_events import McpRecoveryEvents
from overlay.stm_server import STMServer

def test_snapshot_is_metadata_only():
 h=McpRecoveryEvents('instance'); s=h.snapshot()
 assert set(s)=={'instance_id','generation','ready','endpoint','revision'}
 assert s['instance_id']=='instance' and s['ready'] is False

def test_replaced_false_ready_increments_and_wakes():
 h=McpRecoveryEvents('x'); out=[]
 t=threading.Thread(target=lambda:out.append(h.wait_after(0,1)),daemon=True);t.start();time.sleep(.03)
 s=h.publish(ready=False,replaced=True);t.join(1)
 assert s['generation']==1 and s['revision']==1 and out[0]['revision']==1

def test_old_revision_returns_immediately_without_lost_edge():
 h=McpRecoveryEvents('x'); h.publish(ready=True,endpoint='http://127.0.0.1:1/mcp')
 start=time.monotonic(); s=h.wait_after(0,1)
 assert time.monotonic()-start<.1 and s['ready'] and s['revision']==1

def test_stm_sse_authenticated_lf_and_invalid_after(tmp_path):
 server=STMServer(port=0,state_discovery_path=tmp_path/'discovery.json');server.start()
 try:
  server.publish_mcp_ready(True,'http://127.0.0.1:9/mcp')
  token=json.loads((tmp_path/'discovery.json').read_text())['token']; opener=build_opener(ProxyHandler({}))
  req=Request(f'http://127.0.0.1:{server.port}/state/mcp/events',headers={'Authorization':'Bearer '+token})
  with opener.open(req,timeout=2) as r:
   lines=[r.readline().decode() for _ in range(4)]
  assert lines[0].startswith('id: ') and lines[1]=='event: mcp.ready\n' and lines[2].startswith('data: ') and lines[3]=='\n'
  data=json.loads(lines[2][6:]); assert set(data)<= {'instance_id','generation','ready','endpoint','revision'}
  bad=Request(f'http://127.0.0.1:{server.port}/state/mcp/events?after=no',headers={'Authorization':'Bearer '+token})
  try: opener.open(bad,timeout=2); assert False
  except HTTPError as e: assert e.code==400
 finally: server.stop()

def test_stm_sse_rejects_missing_and_wrong_bearer(tmp_path):
 server=STMServer(port=0,state_discovery_path=tmp_path/'discovery.json');server.start()
 try:
  url=f'http://127.0.0.1:{server.port}/state/mcp/events'; opener=build_opener(ProxyHandler({}))
  for headers in ({},{'Authorization':'Bearer wrong'}):
   try: opener.open(Request(url,headers=headers),timeout=2); assert False
   except HTTPError as e: assert e.code==401
 finally: server.stop()

def test_stm_sse_current_after_gets_immediate_snapshot(tmp_path):
 server=STMServer(port=0,state_discovery_path=tmp_path/'discovery.json');server.start()
 try:
  server.publish_mcp_ready(True,'http://127.0.0.1:9/mcp'); d=json.loads((tmp_path/'discovery.json').read_text()); opener=build_opener(ProxyHandler({}))
  req=Request(f'http://127.0.0.1:{server.port}/state/mcp/events?after=1',headers={'Authorization':'Bearer '+d['token']})
  started=time.monotonic()
  with opener.open(req,timeout=2) as r:
   lines=[r.readline().decode() for _ in range(4)]
  assert lines[0].startswith('id: ') and lines[1]=='event: mcp.ready\n' and lines[2].startswith('data: ') and lines[3]=='\n'
  assert json.loads(lines[2][6:])['ready'] is True
  assert time.monotonic()-started<2
 finally: server.stop()

def test_stm_stop_closes_active_sse_promptly(tmp_path):
 server=STMServer(port=0,state_discovery_path=tmp_path/'discovery.json');server.start(); opened=threading.Event(); result=[]
 try:
  token=json.loads((tmp_path/'discovery.json').read_text())['token']; opener=build_opener(ProxyHandler({}))
  def reader():
   try:
    with opener.open(Request(f'http://127.0.0.1:{server.port}/state/mcp/events',headers={'Authorization':'Bearer '+token}),timeout=2) as r:
     for _ in range(4): r.readline()
     opened.set(); result.append(r.read())
   except Exception as exc: result.append(exc)
  t=threading.Thread(target=reader,daemon=True);t.start();assert opened.wait(2)
  started=time.monotonic();server.stop();t.join(3)
  assert not t.is_alive() and time.monotonic()-started<3
  assert len(result)==1 and isinstance(result[0],bytes), result
 finally:
  if server.listening: server.stop()
