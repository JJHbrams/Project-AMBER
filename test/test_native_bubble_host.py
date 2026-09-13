import unittest
from overlay.bubble.native_host import NativeBubbleHost, version_question
from overlay.chat_window import logical_rect_to_physical


class Shell:
    def __init__(self,*args,**kwargs):self.is_ready=True;self.pid=123;self.messages=[]
    def publish_snapshot(self,s):self.messages.append(s);return True
    def set_geometry(self,g):return True
    def stop(self):self.pid=None;return True


class Provider:
    _attempt_generation=1
    def __init__(self):self.sent=[];self.interrupts=[];self.accept=True
    def is_alive(self):return True
    def send_rich(self,text,attachments,key):self.sent.append((text,attachments,key));return self.accept
    def interrupt(self,key):self.interrupts.append(key);return self.accept


class HostTests(unittest.TestCase):
    def test_all_manual_bubbles_follow_anchor_translation_and_scale(self):
        anchor=[800,500,100,100];self.host.get_anchor=lambda:tuple(anchor)
        for label in ('input','speech','thought'):
            self.host._geometry(dict(window=label,x=600,y=300,width=320,height=180,scale=1,origin='user_drag'))
        anchor[:]=[900,550,200,200];self.host.refresh_positions()
        for label in ('input','speech','thought'):
            self.assertEqual((self.host.rects[label]['x'],self.host.rects[label]['y']),(500,150))

    def test_style_points_dpi_limits_zero_cap_and_auto_font(self):
        from unittest.mock import patch
        self.host.rects['speech']={'scale':2}
        with patch('overlay.bubble.native_host.geometry.default_bubble_width',return_value=800),patch('overlay.bubble.native_host.geometry.get_monitor_work_rect',return_value=(0,0,1600,1000)),patch('overlay.bubble.native_host.terminal_font_size',return_value=12):
            self.host.cfg={'font_family':'Arial','font_size':18,'speech_max_height_ratio':0}
            style=self.host._presentation_style(0,0,100)
            self.assertEqual(style['speech']['font_size'],24)
            self.assertEqual(style['speech']['max_width'],400)
            self.assertEqual(style['speech']['max_height'],500)
            self.host.cfg['font_size']=0
            self.assertEqual(self.host._presentation_style(0,0,100)['input']['font_size'],16)

    def test_font_reload_preserves_queue_and_provider(self):
        self.submit('active');self.submit('waiting')
        before=self.host.queue.snapshot();provider=self.host.provider
        self.host.update_cfg({'font_family':'Arial','font_size':18})
        self.assertEqual(self.host.queue.snapshot(),before)
        self.assertIs(self.host.provider,provider)

    def test_presentation_size_stale_and_manual_guards(self):
        self.host._present('speech','hello');revision=self.host.presentation_revision['speech']
        self.action('presentation_size',window='speech',width=240,height=160,presentation_revision=revision-1)
        self.assertNotIn('speech',self.host._presentation_sizes)
        self.action('presentation_size',window='speech',width=240,height=160,presentation_revision=revision)
        self.assertEqual(self.host._presentation_sizes['speech'],{'width':240,'height':160})
        self.host.manual_size.add('speech')
        self.action('presentation_size',window='speech',width=250,height=170,presentation_revision=revision)
        self.assertEqual(self.host._presentation_sizes['speech']['width'],240)

    def test_speech_history_is_bounded_private_and_new_presentation_resets_only_its_size(self):
        h=self.host
        self.submit('active')
        key=self.provider.sent[-1][2]
        event={'request_key':{'session_id':key.session_id,'request_id':key.request_id,'attempt_generation':key.attempt_generation},'kind':'speech','id':'same','delta':False,'text':'first answer'}
        h.handle_event(event)
        h._geometry(dict(window='speech',x=10,y=20,width=700,height=500,scale=1,origin='user_resize'))
        revision=h.presentation_revision['speech']
        event.update(delta=True,text=' more');h.handle_event(event)
        self.assertIn('speech',h.manual_size)
        self.assertGreater(h.presentation_revision['speech'],revision)
        h._present('speech','second answer')
        self.assertIn('speech',h.manual_size)
        self.assertEqual(h.snapshot()['speech_history'][0]['text'],'first answer more')
        for i in range(25):h._present('speech',f'answer {i}')
        self.assertEqual(len(h.speech_history),20)
        self.assertNotIn('first answer',self.provider.sent[0][0])

    def test_reopened_idle_submission_does_not_stick(self):
        self.host.hide()
        self.host.show()
        self.submit('fresh')
        self.assertEqual(len(self.provider.sent),1)
        self.assertEqual(self.provider.sent[0][2].request_id,'fresh')

    def test_fresh_held_submission_preserves_old_waiting(self):
        self.submit('active')
        self.submit('old')
        self.host.queue.hold()
        self.terminal(self.provider.sent[0][2])
        self.submit('fresh')
        self.assertEqual([p[2].request_id for p in self.provider.sent],['active','fresh'])
        self.assertEqual([p.key.request_id for p in self.host.queue.snapshot().waiting],['old'])
        self.assertTrue(self.host.queue.snapshot().held)

    def test_manual_preferences_survive_passive_ack_and_dpi(self):
        h=self.host
        for origin in ('user_drag','user_resize'):
            h._geometry(dict(window='input',x=-800,y=-100,width=600,height=400,scale=1,origin=origin))
        h._geometry(dict(window='input',x=0,y=0,width=450,height=250,scale=2,origin='dpi'))
        h.refresh_positions()
        self.assertEqual((h.rects['input']['x'],h.rects['input']['y']),(-800,-100))
        self.assertEqual((h.rects['input']['width'],h.rects['input']['height']),(1200,800))

    def test_negative_monitor_anchor_is_not_clamped_to_primary(self):
        self.host.get_anchor=lambda:(-1000,-300,100,100)
        self.host.refresh_positions()
        self.assertEqual(self.host.rects['input']['x'],-1465)
        self.assertEqual(self.host.rects['input']['y'],-300)

    def test_passive_and_dpi_do_not_persist_manual_geometry(self):
        h=self.host
        for origin,scale in [('passive',1),('dpi',2)]:
            h._geometry(dict(window='input',x=100,y=200,width=700,height=500,scale=scale,origin=origin))
        self.assertFalse(h.manual_position);self.assertFalse(h.manual_size)
        h.refresh_positions();self.assertEqual(h.rects['input']['width'],920)

    def test_user_geometry_components_are_independent(self):
        h=self.host
        h._geometry(dict(window='input',x=100,y=200,width=700,height=500,scale=1,origin='user_drag'))
        h.refresh_positions();self.assertEqual(h.rects['input']['x'],100);self.assertEqual(h.rects['input']['width'],460)
        h._geometry(dict(window='speech',x=10,y=20,width=700,height=500,scale=1,origin='user_resize'))
        h.refresh_positions();self.assertEqual(h.rects['speech']['x'],335);self.assertEqual(h.rects['speech']['width'],700)

    def test_invalid_geometry_and_auto_height_cannot_replace_manual_size(self):
        h=self.host;h._geometry(None);h._geometry(dict(window='input',x=float('nan')))
        h.visible=True;h.manual_size.add('input');h.rects['input']={'height':600,'scale':1}
        self.action('resize_input',height=240);self.assertEqual(h.rects['input']['height'],600)

    def test_blank_composer_timeout_hides_input_only(self):
        h=self.host;h.cfg['composer_idle_close_ms']=10;h.speech={'text':'answer'};h.thought={'text':'thinking'}
        h.show();timer=next(callback for delay,callback in self.timers if delay==10);timer()
        self.assertFalse(h.composer_visible);self.assertTrue(h.visible)
        self.assertEqual(h.speech['text'],'answer');self.assertEqual(h.thought['text'],'thinking')

    def test_composer_guard_restarts_timeout_and_rejects_stale_callback(self):
        h=self.host;h.cfg['composer_idle_close_ms']=10;h.show();stale=next(callback for delay,callback in self.timers if delay==10)
        h._composer_state({'draft_nonempty':True,'attachments':0,'composing':False,'readers':0,'pending':False,'editing':False,'queue_open':False,'hovered':False})
        stale();self.assertTrue(h.composer_visible)
        h._composer_state({'draft_nonempty':False,'attachments':0,'composing':False,'readers':0,'pending':False,'editing':False,'queue_open':False,'hovered':False})
        fresh=[callback for delay,callback in self.timers if delay==10][-1];fresh();self.assertFalse(h.composer_visible)

    def test_nudge_reply_blocks_idle_and_manual_close_rolls_back_once(self):
        events=[];h=NativeBubbleHost(schedule=lambda delay,fn:self.timers.append((delay,fn)),get_session=lambda:self.provider,
            get_anchor=lambda:(800,600,100,100),shell_factory=Shell,cfg={'composer_idle_close_ms':10},
            on_composer_closed=lambda:events.append('closed'),geometry_state_getter=lambda:{},geometry_state_updater=lambda _:None)
        # This was an ordinary blank-composer callback.  It must be stale after
        # the user engages a nudge reply, and no replacement idle timer is
        # allowed while the reply context/token remains active.
        h.show();stale=next(callback for delay,callback in self.timers if delay==10)
        h.show_nudge('synthetic');h.action({'action_id':'reply','action':'nudge_reply','request_id':None,'payload':{'presentation_revision':h.presentation_revision['speech']}})
        self.assertEqual(len([1 for delay,_ in self.timers if delay==10]),1)
        stale();self.assertTrue(h.composer_visible);self.assertIsNotNone(h._reply_context);self.assertIsNotNone(h._reply_token)
        h.hide();h.hide()
        self.assertEqual(events,['closed']);self.assertIsNone(h._reply_context);self.assertIsNone(h._reply_token)

    def test_composer_idle_close_zero_disables(self):
        h=self.host;h.cfg['composer_idle_close_ms']=0;h.show();self.assertFalse(any(delay==0 for delay,_ in self.timers))

    def test_idle_timeout_reload_invalidates_old_callback(self):
        h=self.host;h.cfg['composer_idle_close_ms']=20;h.show()
        old=next(callback for delay,callback in self.timers if delay==20)
        h.update_cfg({**h.cfg,'composer_idle_close_ms':10})
        old();self.assertTrue(h.composer_visible)
        next(callback for delay,callback in self.timers if delay==10)()
        self.assertFalse(h.composer_visible)

    def test_history_activity_and_approval_guard_idle_close(self):
        h=self.host;h.cfg['composer_idle_close_ms']=10;h.show();first=next(callback for delay,callback in self.timers if delay==10)
        h.set_history_open(True);first();self.assertTrue(h.composer_visible)
        h.set_history_open(False);history_clear=[callback for delay,callback in self.timers if delay==10][-1]
        h._input_activity(True);history_clear();self.assertTrue(h.composer_visible)
        h._input_activity(False);[callback for delay,callback in self.timers if delay==10][-1]()
        self.assertFalse(h.composer_visible)
        h.show();speech_stale=[callback for delay,callback in self.timers if delay==10][-1]
        h.set_speech_history_open(True);speech_stale();self.assertTrue(h.composer_visible)
        h.set_speech_history_open(False);[callback for delay,callback in self.timers if delay==10][-1]()
        self.assertFalse(h.composer_visible)
        h.show();h.approval=type('Approval',(),{'id':'approval','allow':lambda self:None,'deny':lambda self:None})()
        h._composer_generation+=1;h._composer_idle_armed=False;h._arm_composer_idle_close()
        timer_count=len(self.timers)
        h.action({'action_id':'approval-close','action':'approval','request_id':None,'payload':{'approval_id':'approval','allow':True}})
        self.assertGreater(len(self.timers),timer_count)
        self.timers[-1][1]()
        self.assertFalse(h.composer_visible)

    def test_stop_and_crash_clear_native_speech_history_guard_only(self):
        h=self.host;h.cfg['composer_idle_close_ms']=10;h.show();h.set_speech_history_open(True);h._input_activity(True);h._history_open=True
        h.stop();self.assertFalse(h._speech_history_open);self.assertFalse(h._composer_input_active);self.assertTrue(h._history_open)
        h.set_history_open(False);h.shell.pid=123;h.show();[callback for delay,callback in self.timers if delay==10][-1]();self.assertFalse(h.composer_visible)
        h.show();h.set_speech_history_open(True);h._input_activity(True);h._history_open=True;h._crashed(1)
        self.assertFalse(h.composer_visible);self.assertFalse(h._speech_history_open);self.assertFalse(h._composer_input_active);self.assertTrue(h._history_open)

    def test_hide_reopen_invalidates_stale_idle_callback_and_nudge_rolls_back(self):
        events=[];h=NativeBubbleHost(schedule=lambda delay,fn:self.timers.append((delay,fn)),get_session=lambda:self.provider,
            get_anchor=lambda:(800,600,100,100),shell_factory=Shell,cfg={'composer_idle_close_ms':10},
            on_composer_closed=lambda:events.append('closed'),geometry_state_getter=lambda:{},geometry_state_updater=lambda _:None)
        h.show();stale=next(callback for delay,callback in self.timers if delay==10);h._input_activity(True);h.hide();self.assertFalse(h._composer_input_active)
        h.show();stale();self.assertTrue(h.composer_visible)
        [callback for delay,callback in self.timers if delay==10][-1]();self.assertFalse(h.composer_visible)
        h.show()
        h.show_nudge('synthetic');h.action({'action_id':'reply-rollback','action':'nudge_reply','request_id':None,'payload':{'presentation_revision':h.presentation_revision['speech']}})
        self.assertTrue(h._nudge['replied']);h._close_composer_only()
        self.assertFalse(h._nudge['replied']);self.assertTrue(h.speech['nudge']);self.assertEqual(events.count('closed'),3)

    def test_manual_geometry_round_trips_anchor_position_and_logical_size(self):
        store={}
        def update(mutator): mutator(store)
        h=NativeBubbleHost(schedule=lambda *_:None,get_session=lambda:self.provider,
            get_anchor=lambda:(100,200,200,100),shell_factory=Shell,
            geometry_state_getter=lambda:store,geometry_state_updater=update)
        h._geometry({'window':'thought','x':0,'y':150,'width':600,'height':300,'scale':2,'origin':'user_drag'})
        h._geometry({'window':'thought','x':0,'y':150,'width':600,'height':300,'scale':2,'origin':'user_resize'})
        self.assertEqual(store['native_bubble_geometry']['thought']['position'],{'dx':-0.5,'dy':-0.5})
        self.assertEqual(store['native_bubble_geometry']['thought']['size'],{'width':300.0,'height':150.0})
        restored=NativeBubbleHost(schedule=lambda *_:None,get_session=lambda:self.provider,
            get_anchor=lambda:(-500,400,400,200),shell_factory=Shell,
            geometry_state_getter=lambda:store,geometry_state_updater=update)
        restored.refresh_positions()
        self.assertEqual((restored.rects['thought']['x'],restored.rects['thought']['y']),(-700,300))
        self.assertEqual((restored.rects['thought']['width'],restored.rects['thought']['height']),(300,150))
        restored._geometry({'window':'thought','x':-700,'y':300,'width':600,'height':300,'scale':2,'origin':'dpi'})
        self.assertEqual((restored.rects['thought']['width'],restored.rects['thought']['height']),(600,300))

    def test_malformed_geometry_and_passive_dpi_are_not_persisted(self):
        store={'native_bubble_geometry':{'input':{'position':{'dx':'bad','dy':0},'size':{'width':float('nan'),'height':20}},'speech':[1]}}
        writes=[]
        h=NativeBubbleHost(schedule=lambda *_:None,get_session=lambda:self.provider,
            get_anchor=lambda:(0,0,100,100),shell_factory=Shell,
            geometry_state_getter=lambda:store,geometry_state_updater=lambda update:writes.append(update))
        self.assertFalse(h.manual_position);self.assertFalse(h.manual_size)
        h._geometry({'window':'input','x':1,'y':2,'width':300,'height':200,'scale':2,'origin':'passive'})
        h._geometry({'window':'input','x':1,'y':2,'width':300,'height':200,'scale':2,'origin':'dpi'})
        self.assertEqual(writes,[])

    def test_native_tail_uses_anchor_center_but_input_keeps_start_target(self):
        self.host.get_anchor=lambda:(-400,100,200,120)
        self.host.refresh_positions()
        self.assertNotIn('tail_target',self.host.rects['input'])
        self.assertEqual(self.host.rects['speech']['tail_target'],[-300,160])
        self.assertEqual(self.host.rects['thought']['tail_target'],[-300,160])

    def test_stale_dismiss_cannot_hide_new_version_answer(self):
        self.submit('a','버전 뭐야');old=self.host.presentation_revision['speech']
        self.submit('b','AMBER 버전')
        self.action('dismiss',window='speech',presentation_revision=old)
        self.assertNotIn('speech',self.host._dismissed)

    def test_terminal_fade_revision_and_latest_pending_recent(self):
        self.submit('a');self.submit('b');self.host.queue.hold()
        old=self.host.presentation_revision['speech'];self.terminal(self.provider.sent[0][2])
        self.assertGreater(self.host.presentation_revision['speech'],old)
        self.assertEqual(self.host.cards['a']['fade_ms'],8000)
        self.assertEqual(self.host.snapshot()['recent']['request_id'],'b')

    def test_thumbnail_failure_does_not_enqueue(self):
        from unittest.mock import patch
        with patch.object(self.host,'_prepare_card',side_effect=OSError('synthetic')):
            self.submit('rejected')
        self.assertFalse(self.host.queue.snapshot().waiting)
        self.assertFalse(self.host.payloads)
        self.assertFalse(self.provider.sent)

    def test_thumbnail_failure_preserves_original_edit(self):
        from unittest.mock import patch
        self.submit('active');self.submit('pending','original');self.action('edit','pending')
        key=self.host.edit
        with patch.object(self.host,'_prepare_card',side_effect=OSError('synthetic')):
            self.action('save_edit','rejected',edit_request_id='pending',text='replacement',attachments=[])
        self.assertEqual(self.host.edit,key)
        self.assertEqual(self.host.payloads[key].text,'original')

    def setUp(self):
        self.provider=Provider();self.timers=[];self.calls=0
        def session():self.calls+=1;return self.provider
        self.host=NativeBubbleHost(schedule=lambda delay,f:self.timers.append((delay,f)),get_session=session,
            get_anchor=lambda:(800,600,100,100),shell_factory=Shell,version='1.2.3.4',
            geometry_state_getter=lambda:{},geometry_state_updater=lambda _update:None)
        self.counter=0
    def action(self,name,rid=None,**payload):
        self.counter+=1
        self.host.action({'action_id':str(self.counter),'action':name,'request_id':rid,'payload':payload})
    def submit(self,rid,text='hello'):
        self.action('submit',rid,text=text,attachments=[])
    def terminal(self,key,**extra):
        self.host.handle_event({'kind':'turn_end','terminal':True,'request_key':{'session_id':key.session_id,
            'request_id':key.request_id,'attempt_generation':key.attempt_generation},**extra})
    def test_fifo_requires_terminal_not_ack(self):
        self.submit('a');self.submit('b');self.submit('c')
        self.assertEqual(len(self.provider.sent),1)
        key=self.provider.sent[0][2]
        self.host.handle_event({'kind':'interrupt_ack','request_key':{'session_id':key.session_id,'request_id':key.request_id,'attempt_generation':1}})
        self.assertEqual(len(self.provider.sent),1)
        self.terminal(key)
        self.assertEqual([k.request_id for _,_,k in self.provider.sent],['a','b'])
    def test_lightning_selected_once_remainder_held(self):
        for rid in ('a','b','c'):self.submit(rid)
        key=self.provider.sent[0][2]
        self.action('send_now','c');self.action('send_now','c')
        self.assertEqual(self.provider.interrupts,[key])
        self.action('delete','c')
        self.assertIsNotNone(self.host._key('c'))
        self.terminal(key,provider_status='interrupted')
        self.assertEqual([k.request_id for _,_,k in self.provider.sent],['a','c'])
        self.terminal(self.provider.sent[-1][2])
        self.assertEqual(len(self.provider.sent),2)
        self.action('resume_queue')
        self.assertEqual(self.provider.sent[-1][2].request_id,'b')
    def test_timeout_does_not_send_selected(self):
        self.submit('a');self.submit('b');self.action('send_now','b')
        for delay,callback in self.timers:
            if delay==15000:callback()
        self.assertEqual(len(self.provider.sent),1)
        self.assertTrue(self.host.queue.snapshot().held)
        self.action('resume_queue')
        self.assertEqual(len(self.provider.sent),1)
    def test_edit_and_delete_waiting(self):
        self.submit('a');self.submit('b');self.action('edit','b')
        self.terminal(self.provider.sent[0][2])
        self.assertEqual(len(self.provider.sent),1)
        self.action('save_edit','edit-ack',edit_request_id='b',text='changed',attachments=[])
        self.assertEqual(self.provider.sent[-1][0],'changed')
        self.assertEqual(self.host.accepted,'edit-ack')
        self.submit('c');self.action('delete','c')
        self.assertIsNone(self.host._key('c'))
    def test_version_is_host_owned_offline_and_targeted(self):
        self.submit('a','버전 뭐야?')
        self.assertEqual(self.calls,0)
        self.assertIn('1.2.3.4',self.host.speech['text'])
        for text in ('Claude 버전','프로젝트 버전 뭐야','AMBER 버전과 코드를 알려줘'):
            self.assertFalse(version_question(text))
    def test_capacity_retains_rejected_draft(self):
        for i in range(7):self.submit(str(i))
        self.assertEqual(self.host.rejected,'6')
        self.assertEqual(self.host.accepted,'5')
        self.assertEqual(len(self.host.queue.snapshot().waiting),5)
    def test_stale_terminal_cannot_dispatch(self):
        self.submit('a');self.submit('b');key=self.provider.sent[0][2]
        self.host.handle_event({'kind':'turn_end','terminal':True,'request_key':{'session_id':key.session_id,'request_id':key.request_id,'attempt_generation':0}})
        self.assertEqual(len(self.provider.sent),1)
    def test_terminal_releases_original_payload(self):
        self.submit('a');key=self.provider.sent[0][2];self.terminal(key)
        self.assertNotIn(key,self.host.payloads)
        self.assertEqual(self.host.cards['a']['state'],'sent')
    def test_replayed_action_id_not_duplicated(self):
        msg={'action_id':'same','action':'submit','request_id':'a','payload':{'text':'x','attachments':[]}}
        self.host.action(msg);self.host.action(msg)
        self.assertEqual(len(self.provider.sent),1)

    def test_collapse_confirmed_provider_stop_releases_active_holds_waiting(self):
        self.submit('a');self.submit('b')
        self.host.hide();self.host.provider_stopped(self.provider)
        self.assertIsNone(self.host.queue.snapshot().active)
        self.assertEqual(self.host.cards['a']['state'],'interrupted')
        self.assertEqual(len(self.provider.sent),1)
        self.action('resume_queue')
        self.assertEqual(self.provider.sent[-1][2].request_id,'b')

    def test_late_interrupted_terminal_after_timeout_is_not_sent(self):
        self.submit('a');key=self.provider.sent[0][2]
        self.action('interrupt','a');self.host._interrupt_failed(key)
        self.terminal(key,provider_status='interrupted')
        self.assertEqual(self.host.cards['a']['state'],'interrupted')


class NativeCoordinateTests(unittest.TestCase):
    def test_logical_rect_to_physical_preserves_negative_monitor_origin(self):
        from unittest.mock import patch
        with patch('overlay.chat_window._get_monitor_info', return_value=((-1280, 0, 0, 720), (-1920, -100, 0, 980), 1.5)):
            self.assertEqual(logical_rect_to_physical((-1180, 100, 200, 100)), (-1770, 50, 300, 150))


if __name__=='__main__':unittest.main()
