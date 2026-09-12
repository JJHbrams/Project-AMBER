import asyncio, base64, json, unittest
from claude_code_sdk.types import ClaudeCodeOptions, PermissionResultDeny
from overlay.bubble.claude_control import ClaudeControl
from overlay.bubble.claude_control import RequestKey, rich_user_payload
from overlay.bubble.rich_input import Attachment
class ControlTests(unittest.TestCase):
 def test_image_payload_is_anthropic_base64_and_key_is_immutable(self):
  key=RequestKey('s','r',2); payload=rich_user_payload('x',(Attachment('image/png',b'abc'),),key)
  block=payload['message']['content'][1]
  self.assertEqual(block['source'],{'type':'base64','media_type':'image/png','data':base64.b64encode(b'abc').decode()})
  self.assertEqual(key.attempt_generation,2)
 def test_empty_rich_request_rejected(self):
  with self.assertRaises(ValueError):rich_user_payload('',(),RequestKey('s','r',1))


class FakeTransport:
 def __init__(self):self.incoming=asyncio.Queue();self.writes=[];self.closed=False
 async def connect(self):pass
 async def write(self,line):
  message=json.loads(line);self.writes.append(message)
  if message.get('type')=='control_request':
   await self.incoming.put({'type':'control_response','response':{'subtype':'success','request_id':message['request_id'],'response':{}}})
 async def read_messages(self):
  while True:yield await self.incoming.get()
 async def end_input(self):pass
 async def close(self):self.closed=True


class SDKControlTests(unittest.IsolatedAsyncioTestCase):
 async def test_real_sdk_query_approval_interrupt_and_terminal(self):
  transport=FakeTransport();inputs=asyncio.Queue();options_seen=[];approvals=[]
  async def prompts():
   while True:yield await inputs.get()
  async def permission(name,body,context):
   approvals.append(name);return PermissionResultDeny(message='synthetic denial')
  def factory(prompt,options,passthrough):
   options_seen.append(options);return transport
  key=RequestKey('host-correlation','r',1)
  control=ClaudeControl(ClaudeCodeOptions(can_use_tool=permission),prompts(),transport_factory=factory)
  await control.connect()
  try:
   self.assertEqual(options_seen[0].permission_prompt_tool_name,'stdio')
   await inputs.put({**rich_user_payload('synthetic',(),key),'_private_request_key':key})
   for _ in range(100):
    if control._active is not None:break
    await asyncio.sleep(.005)
   self.assertEqual(control._active,key)
   self.assertTrue(await control.interrupt(key))
   self.assertEqual(control._active,key,'control acknowledgement is not terminal')
   await transport.incoming.put({'type':'control_request','request_id':'permission-1','request':{'subtype':'can_use_tool','tool_name':'Write','input':{}}})
   for _ in range(100):
    if approvals:break
    await asyncio.sleep(.005)
   self.assertEqual(approvals,['Write'])
   await transport.incoming.put({'type':'result','subtype':'success','duration_ms':1,'duration_api_ms':1,'is_error':False,'num_turns':1,'session_id':'provider-session','result':'synthetic done'})
   messages=control.messages();message,returned_key=await anext(messages)
   self.assertEqual(returned_key,key);self.assertIsNone(control._active)
   user=next(w for w in transport.writes if w.get('type')=='user')
   self.assertNotIn('_private_request_key',user)
   self.assertNotIn('session_id',user,'host correlation is not provider identity')
  finally:await control.close()
  self.assertTrue(transport.closed)

 async def test_conflicting_callback_options_rejected(self):
  async def prompts():
   yield {}
  with self.assertRaises(ValueError):
   ClaudeControl(ClaudeCodeOptions(can_use_tool=lambda *a:None,permission_prompt_tool_name='custom'),prompts())
