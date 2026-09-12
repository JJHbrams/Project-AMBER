import threading
import unittest
from unittest.mock import Mock
from overlay.bubble.codex_session import CodexBubbleSession, _Active
from overlay.bubble.turn_queue import TurnKey
from overlay.bubble.rich_input import Attachment


class Tests(unittest.TestCase):
    def setUp(self):
        self.out=[];self.s=CodexBubbleSession('.',on_event=self.out.append)
        self.k=TurnKey('s','r',1);self.s._thread_id='thread';self.s._active=_Active(self.k,'turn')
    def event(self,method,**params):
        self.s._notification({'method':method,'params':{'threadId':'thread','turnId':'turn',**params}})
    def test_stale_thread_is_ignored(self):
        self.event('turn/completed',threadId='other',turn={'id':'turn','status':'completed'})
        self.assertEqual(self.out,[])
    def test_stale_nested_turn_is_ignored(self):
        self.s._notification({'method':'turn/completed','params':{'threadId':'thread','turn':{'id':'old','status':'completed'}}})
        self.assertEqual(self.out,[])
    def test_terminal_interrupt_is_not_rpc_ack(self):
        self.s._rpc=Mock(return_value={})
        self.assertTrue(self.s.interrupt(self.k))
        for _ in range(50):
            if self.out:break
            threading.Event().wait(.01)
        self.assertEqual(self.out[0]['kind'],'interrupt_ack');self.assertIsNotNone(self.s._active)
        self.event('turn/completed',turn={'id':'turn','status':'interrupted'})
        self.assertEqual(self.out[-1]['provider_status'],'interrupted')
        self.assertFalse(self.out[-1]['is_error']);self.assertIsNone(self.s._active)
    def test_delta_has_text_and_boolean_delta(self):
        self.event('item/agentMessage/delta',delta='hello',itemId='item')
        self.assertEqual(self.out[0]['text'],'hello');self.assertIs(self.out[0]['delta'],True)
    def test_unknown_is_not_terminal_and_cannot_be_resent(self):
        self.s._unknown(self.k)
        self.assertEqual(self.out[0]['kind'],'provider_unknown')
        self.assertNotIn('terminal',self.out[0]);self.assertTrue(self.s._active.unknown)
    def test_no_active_unknown_is_safe(self):
        self.s._active=None;self.s._unknown(self.k);self.assertEqual(self.out,[])
    def test_rpc_futures_are_independently_correlated(self):
        captured=[]
        def write(m):
            captured.append(m);self.s._pending[m['id']].set_result({'method':m['method']})
        self.s._write=write
        self.assertEqual(self.s._rpc('initialize',{}),{'method':'initialize'})
        self.assertEqual(self.s._rpc('thread/start',{}),{'method':'thread/start'})
        self.assertEqual([m['id'] for m in captured],[1,2]);self.assertEqual(self.s._pending,{})
    def test_image_payload_and_early_delta(self):
        self.s._active=None;self.s.is_alive=lambda:True
        captured=[];finished=threading.Event()
        def rpc(method,params,**kw):
            captured.append((method,params))
            self.s._notification({'method':'item/agentMessage/delta','params':{'threadId':'thread','turnId':'t2','delta':'seen'}})
            finished.set();return {'turn':{'id':'t2'}}
        self.s._rpc=rpc
        self.assertTrue(self.s.send_rich('x',(Attachment('image/png',b'abc'),),self.k))
        self.assertTrue(finished.wait(2))
        for _ in range(50):
            if self.out:break
            threading.Event().wait(.01)
        self.assertEqual(captured[0][1]['input'][1]['url'],'data:image/png;base64,YWJj')
        self.assertEqual(self.out[0]['text'],'seen')


if __name__=='__main__':unittest.main()
