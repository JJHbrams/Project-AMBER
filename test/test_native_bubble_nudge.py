import unittest
from unittest.mock import Mock,patch
from test_native_bubble_host import Shell,Provider
from overlay.bubble.native_host import NativeBubbleHost

class NudgeTests(unittest.TestCase):
    def setUp(self):
        self.provider=Provider();self.events=[];self.serial=0
        self.h=NativeBubbleHost(schedule=lambda *_:None,get_session=lambda:self.provider,
            get_anchor=lambda:(800,600,100,100),shell_factory=Shell,version='1',
            on_nudge_reply=lambda token:self.events.append('reply'),on_nudge_ignored=lambda:self.events.append('ignored'),
            on_nudge_accepted=lambda token:self.events.append('accepted'),on_composer_closed=lambda:self.events.append('closed'))
    def action(self,name,rid=None,**payload):
        self.serial+=1;self.h.action(dict(action_id=str(self.serial),action=name,request_id=rid,payload=payload))
    def show(self):self.h.show_nudge('synthetic nudge',dwell_ms=50)
    def reply(self):self.action('nudge_reply',presentation_revision=self.h._nudge['revision'])
    def submit(self,rid='r',text='yes'):self.action('submit',rid,text=text,attachments=[])
    def test_cold_nudge_does_not_open_composer_or_provider(self):
        self.show();self.assertFalse(self.h.composer_visible);self.assertFalse(self.provider.sent)
        self.assertFalse(self.h.rects['input']['visible']);self.assertFalse(self.h.rects['input']['focus'])
        self.assertTrue(self.h.rects['speech']['visible']);self.assertFalse(self.h.is_idle())
    def test_reply_context_is_private_and_committed_once_after_acceptance(self):
        self.show();self.reply();self.reply();self.assertEqual(self.events,['reply'])
        self.assertIn('speech',self.h._dismissed);self.assertFalse(self.h.speech['nudge'])
        self.assertFalse(self.h.rects['speech']['visible'])
        self.submit();self.assertEqual(self.events,['reply','accepted'])
        self.assertIn('synthetic nudge',self.provider.sent[0][0])
        self.assertEqual(self.h.cards['r']['summary'],'yes')
        self.submit('next','next');self.assertIsNone(self.h.payloads[self.h._key('next')].context)
    def test_rejection_and_version_do_not_consume_reply(self):
        self.show();self.reply();self.submit('empty','');self.submit('version','버전 뭐야')
        self.assertEqual(self.events,['reply']);self.assertIsNotNone(self.h._reply_context)
        with patch.object(self.h,'_prepare_card',side_effect=OSError()):self.submit('broken')
        self.assertEqual(self.events,['reply']);self.submit('valid');self.assertEqual(self.events,['reply','accepted'])
    def test_capacity_rejection_retains_context(self):
        self.submit('active')
        for i in range(5):self.submit(str(i))
        self.show();self.reply();self.submit('overflow')
        self.assertIsNotNone(self.h._reply_context);self.assertNotIn('accepted',self.events)
    def test_fade_exact_once_and_late_reply_replay(self):
        self.show();revision=self.h._nudge['revision']
        self.action('dismiss',window='speech',presentation_revision=revision)
        self.action('dismiss',window='speech',presentation_revision=revision)
        self.assertEqual(self.events,['ignored']);self.h.show();self.reply();self.submit()
        self.assertEqual(self.events,['ignored','reply','accepted'])
    def test_reply_close_not_ignored_and_collapse_defers(self):
        self.show();self.reply();self.action('close');self.assertEqual(self.events,['reply','closed'])
        self.show();self.assertTrue(self.h.defer_nudge());self.h.hide();self.h.stop()
        self.assertEqual(self.events,['reply','closed'])
    def test_crash_unanswered_settles_once(self):
        self.show();self.h._crashed('synthetic');self.h._crashed('synthetic')
        self.assertEqual(self.events,['ignored'])

    def test_main_native_reply_does_not_open_legacy_or_provider(self):
        from overlay.main import OverlayApp
        app=object.__new__(OverlayApp);native=Mock();app._initiative=Mock()
        app._initiative._cfg={};app._initiative.has_pending_outcome.return_value=True
        app._initiative.active_nudge_text.return_value='synthetic'
        app._ensure_native_bubble_host=lambda:native
        app._engage_nudge=Mock();app._ensure_bubble_session=Mock();click=Mock()
        app._initiative_show_nudge('synthetic',click)
        click.assert_not_called();native.on_nudge_reply('token')
        click.assert_called_once();app._engage_nudge.assert_not_called();app._ensure_bubble_session.assert_not_called()
        app._native_nudge_accepted('stale');app._initiative.notify_engaged.assert_not_called()
        app._native_nudge_accepted('token');app._native_nudge_accepted('token')
        app._initiative.notify_engaged.assert_called_once()
        app._initiative.notify_late_engaged.assert_not_called()

    def test_main_late_reply_keeps_prior_ignored_outcome(self):
        from overlay.main import OverlayApp
        app=object.__new__(OverlayApp);app._initiative=Mock()
        app._initiative.has_pending_outcome.return_value=False
        app._initiative.active_nudge_text.return_value='past'
        app._native_arm_nudge_reply();app._native_nudge_accepted()
        app._initiative.notify_late_engaged.assert_called_once()
        app._initiative.notify_engaged.assert_not_called()

    def test_main_converts_only_native_anchor_to_physical_coordinates(self):
        from overlay.main import OverlayApp
        app=object.__new__(OverlayApp)
        app._get_bubble_anchor_rect=lambda:(-1200,100,200,120)
        with patch('overlay.main.logical_rect_to_physical', return_value=(-1800,50,300,180)) as convert:
            self.assertEqual(app._get_native_bubble_anchor_rect(),(-1800,50,300,180))
        convert.assert_called_once_with((-1200,100,200,120))

if __name__=='__main__':unittest.main()
