import os, tempfile, time, unittest
from pathlib import Path
from overlay.bubble.native_shell import ALLOWED_ACTIONS, NativeBubbleShell
from overlay.bubble.rich_input import MAX_DECODED_PIXELS, validate_attachments

WORKER='''import json,os,sys
print(json.dumps({"type":"hello","protocol":1,"nonce":os.environ["ENGRAM_NATIVE_BUBBLE_NONCE"]}),flush=True)
for label in ('input','speech','thought'): print(json.dumps({"type":"ready","window":label}),flush=True)
for action in ('nudge_reply','nudge_defer'): print(json.dumps({"type":"action","action":action,"action_id":action,"payload":{"presentation_revision":1}}),flush=True)
for line in sys.stdin:
 m=json.loads(line)
 if m["type"]=="shutdown": break
'''
class NativeShellTests(unittest.TestCase):
 def test_source_selects_newest_built_profile(self):
  from pathlib import Path
  from types import SimpleNamespace
  from unittest.mock import patch
  shell=NativeBubbleShell(lambda _:None,lambda _:None)
  for latest in ('debug','release'):
   with patch.object(Path,'is_file',return_value=True),patch.object(Path,'stat',lambda p:SimpleNamespace(st_mtime_ns=2 if p.parent.name==latest else 1)):
    self.assertEqual(Path(shell._command()[0]).parent.name,latest)
 def test_nudge_actions_cross_real_child_stdio(self):
  actions=[];shell=NativeBubbleShell(actions.append,lambda _:None,executable=self.worker())
  try:
   self.assertTrue(shell.start({}));end=time.time()+3
   while len(actions)<2 and time.time()<end:time.sleep(.02)
   self.assertEqual([a['action'] for a in actions],['nudge_reply','nudge_defer'])
  finally:self.assertTrue(shell.stop())
 def worker(self):
  f=tempfile.NamedTemporaryFile('w',suffix='.py',delete=False);f.write(WORKER);f.close();self.addCleanup(lambda:os.unlink(f.name));return f.name
 def test_handshake_snapshot_and_safe_shutdown(self):
  actions=[]; crashes=[]; shell=NativeBubbleShell(actions.append,crashes.append,executable=self.worker())
  self.assertTrue(shell.start({'queue':[]})); end=time.time()+2
  while not shell.is_ready and time.time()<end: time.sleep(.02)
  self.assertTrue(shell.is_ready);self.assertFalse(crashes);shell.stop();shell.stop();self.assertIsNone(shell.pid)
 def test_private_actions_and_bad_attachment(self):
  self.assertIn('input_activity',ALLOWED_ACTIONS);self.assertNotIn('arbitrary_command',ALLOWED_ACTIONS)
  self.assertIn('composer_state',ALLOWED_ACTIONS)
  self.assertIn('speech_history_state',ALLOWED_ACTIONS)
  source=(Path(__file__).resolve().parents[1]/'native-bubble-shell'/'src'/'main.rs').read_text(encoding='utf-8')
  self.assertIn('"composer_state"',source)
  self.assertIn('"speech_history_state"',source)
  with self.assertRaises(ValueError):validate_attachments(['data:image/png;base64,AA=='])
  self.assertGreater(MAX_DECODED_PIXELS,1)
