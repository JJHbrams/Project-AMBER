"""Isolated Windows native bubble QA. Never opens real providers/config/STM.

--exercise drives the actual child WebViews using CDP and synthetic content.
--demo leaves the same fake-provider bubble visible for 60 seconds.
CDP is loopback and scoped to this child. All waits and owned shutdown are bounded.
"""
from __future__ import annotations
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import urllib.request

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


async def run(args):
    from dataclasses import asdict
    from overlay.bubble.native_host import NativeBubbleHost
    from overlay.bubble.native_shell import NativeBubbleShell
    loop=asyncio.get_running_loop()
    def schedule(delay,callback):
        loop.call_soon_threadsafe(lambda:loop.call_later(delay/1000,callback))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    # Only this isolated Python host and its child inherit the debugging flag.
    os.environ['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS']=f'--remote-debugging-port={port} --remote-debugging-address=127.0.0.1'
    os.environ['WEBVIEW2_USER_DATA_FOLDER']=tempfile.mkdtemp(prefix='amber-compact-webview-')
    events=[];crashes=[]
    class Provider:
        _attempt_generation=1
        def __init__(self):self.sent=[];self.active=None
        def is_alive(self):return True
        def send_rich(self,text,attachments,key):
            if self.active is not None:return False
            self.active=key;self.sent.append((key,len(attachments)))
            host.handle_event({'kind':'speech','text':'[합성 QA] 입력과 이미지 전달을 확인하고 있어요.','request_key':asdict(key)})
            def finish():
                if self.active==key:
                    self.active=None
                    host.handle_event({'kind':'turn_end','terminal':True,'provider_status':'completed','request_key':asdict(key)})
            schedule(10000,finish)
            return True
        def interrupt(self,key):
            if self.active!=key:return False
            host.handle_event({'kind':'interrupt_ack','request_key':asdict(key)})
            def finish():
                if self.active==key:
                    self.active=None
                    host.handle_event({'kind':'turn_end','terminal':True,'provider_status':'interrupted','request_key':asdict(key)})
            schedule(200,finish)
            return True
    provider=Provider()
    def factory(on_action,on_crash,**kw):
        def action(m):events.append(m['action']);on_action(m)
        return NativeBubbleShell(action,on_crash,executable=args.exe,**kw)
    anchor=[850,550,120,120]
    host=NativeBubbleHost(schedule=schedule,get_session=lambda:provider,get_anchor=lambda:tuple(anchor),
        on_fallback=crashes.append,shell_factory=factory,version='SYNTHETIC QA')
    async def until(predicate,seconds=8):
        deadline=loop.time()+seconds
        while loop.time()<deadline:
            if predicate():return
            if crashes:raise RuntimeError('native_start_failed')
            await asyncio.sleep(.025)
        raise TimeoutError('native_check_timeout')
    try:
        if not host.show():raise RuntimeError('native_spawn_failed')
        await until(lambda:host.shell.is_ready)
        await asyncio.sleep(.25)
        if sys.platform=='win32':
            import ctypes
            from ctypes import wintypes
            user32=ctypes.windll.user32
            styles=[];window_handles={}
            callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
            user32.GetWindowLongW.argtypes=[wintypes.HWND,ctypes.c_int]
            user32.GetWindowLongW.restype=ctypes.c_long
            @callback_type
            def inspect_window(hwnd,_):
                pid=wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
                if pid.value==host.shell.pid:
                    title=ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd,title,256)
                    if title.value in ('Engram input','Engram speech','Engram thought'):
                        window_handles[title.value]=hwnd
                        styles.append((user32.GetWindowLongW(hwnd,-16),user32.GetWindowLongW(hwnd,-20)))
                return True
            user32.EnumWindows(inspect_window,0)
            assert len(styles)==3,'native_windows_missing'
            print(json.dumps({'native_styles':styles}))
            assert all(not (s & 0x00C00000) and not (s & 0x00030000) for s,e in styles),'native_caption_or_minmax'
            assert all(not (e & 0x00040000) for s,e in styles),'native_appwindow_taskbar'
            print(json.dumps({'native_caption_minmax_disabled':True,'native_appwindow_disabled':True,'window_count':len(styles)}))
        if args.exercise:
            import websockets
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/json',timeout=2) as response:targets=json.load(response)
            for target in targets:
                async with websockets.connect(target['webSocketDebuggerUrl'],max_size=4*1024*1024) as ws:
                    seq=0
                    async def evaluate(expression):
                        nonlocal seq
                        seq+=1
                        await ws.send(json.dumps({'id':seq,'method':'Runtime.evaluate','params':{'expression':expression,'returnByValue':True,'awaitPromise':True}}))
                        while True:
                            message=json.loads(await asyncio.wait_for(ws.recv(),5))
                            if message.get('id')==seq:
                                if 'exceptionDetails' in message.get('result',{}):raise RuntimeError('webview_script_failed')
                                return message['result']['result'].get('value')
                    async def capture(name):
                        nonlocal seq
                        if not args.capture_dir:return
                        seq+=1
                        await ws.send(json.dumps({'id':seq,'method':'Page.captureScreenshot','params':{'format':'png'}}))
                        while True:
                            message=json.loads(await asyncio.wait_for(ws.recv(),5))
                            if message.get('id')==seq:
                                folder=Path(args.capture_dir);folder.mkdir(parents=True,exist_ok=True)
                                (folder/f'{name}.png').write_bytes(base64.b64decode(message['result']['data']))
                                return
                    label=await evaluate('window.__TAURI__.window.getCurrentWindow().label')
                    if label in ('input','speech'):
                        tip_button='flip' if label=='input' else 'dismiss'
                        await evaluate(f"document.getElementById('{tip_button}').dispatchEvent(new PointerEvent('pointerover',{{bubbles:true}}))")
                        assert await evaluate("(()=>{const t=document.getElementById('bubble-tooltip'),r=t.getBoundingClientRect();return !t.hidden&&r.left>=39&&r.right<=innerWidth-39&&r.top>=29&&r.bottom<=innerHeight-47})()"),'tooltip_clipped'
                        await capture(label+'-tooltip')
                        if label=='input':
                            await evaluate("document.getElementById('draft').focus()")
                            assert await evaluate("getComputedStyle(document.getElementById('draft')).outlineStyle==='none'"),'textarea_focus_outline'
                            await capture('input-focus')
                    await capture(label)
                    if label=='speech':
                        # Actual WebView timers + private host revision guard.
                        # Hover functions are driven synthetically, not claimed as OS mouse QA.
                        host._present('speech','synthetic old fade',fade_ms=20)
                        host.refresh_positions();host.publish();await asyncio.sleep(.1)
                        await evaluate('resumePresentationFade()')
                        await asyncio.sleep(.08)
                        host._present('speech','synthetic replacement')
                        host.refresh_positions();host.publish();await asyncio.sleep(.7)
                        assert 'speech' not in host._dismissed,'stale_fade_closed_replacement'
                        assert await evaluate("document.querySelector('#content').textContent.includes('synthetic replacement')"),'new_speech_missing'
                        if args.restoration:
                            original_cfg=dict(host.cfg)
                            host.update_cfg({**original_cfg,'font_family':'Arial','font_size':18,'speech_max_height_ratio':.20})
                            host._present('speech','짧은 응답');host.refresh_positions();host.publish()
                            await asyncio.sleep(.6)
                            short_size=await evaluate('[innerWidth,innerHeight]')
                            assert await evaluate("Math.abs(parseFloat(getComputedStyle(document.querySelector('#content')).fontSize)-24)<.1"),'configured_font_size'
                            assert await evaluate("getComputedStyle(document.querySelector('#content')).fontFamily.includes('Arial')"),'configured_font_family'
                            await capture('restored-short')
                            host._present('speech',('긴 응답은 설정된 최대 크기에서 줄바꿈하고 스크롤해야 합니다.\n'*100))
                            host.refresh_positions();host.publish();await asyncio.sleep(.7)
                            long_size=await evaluate('[innerWidth,innerHeight]')
                            assert long_size[1]>short_size[1],'response_did_not_grow'
                            assert await evaluate("(()=>{const c=document.querySelector('#content');return c.scrollHeight>c.clientHeight})()"),'long_response_not_scrollable'
                            await capture('restored-long')
                            host._present('speech','짧은 응답');host.refresh_positions();host.publish();await asyncio.sleep(.6)
                            assert await evaluate('[innerWidth,innerHeight]')==short_size,'short_response_did_not_shrink'
                            print(json.dumps({'auto_size_short':short_size,'auto_size_long':long_size,'configured_font_px':24}))
                            host.update_cfg(original_cfg);host.publish();await asyncio.sleep(.5)
                            # Real OS header drag, then move the synthetic character anchor.
                            hwnd=window_handles['Engram speech'];user32.SetForegroundWindow.argtypes=[wintypes.HWND];user32.SetForegroundWindow(hwnd)
                            user32.SetWindowPos.argtypes=[wintypes.HWND,wintypes.HWND,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_uint]
                            user32.SetWindowPos(hwnd,wintypes.HWND(-1),0,0,0,0,3)
                            await asyncio.sleep(.2)
                            before=dict(host.rects['speech']);scale=before['scale']
                            point=wintypes.POINT();user32.GetCursorPos(ctypes.byref(point))
                            grip=await evaluate("(()=>{const r=document.querySelector('header strong').getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()")
                            actual_before=wintypes.RECT();user32.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
                            user32.GetWindowRect(hwnd,ctypes.byref(actual_before))
                            x=int(actual_before.left+grip[0]*scale);y=int(actual_before.top+grip[1]*scale)
                            try:
                                user32.SetCursorPos(x,y);await asyncio.sleep(.1);user32.mouse_event(2,0,0,0,0);await asyncio.sleep(.15)
                                for step in range(1,6):
                                    user32.SetCursorPos(x+step*8,y+step*5);await asyncio.sleep(.07)
                            finally:
                                user32.mouse_event(4,0,0,0,0);user32.SetCursorPos(point.x,point.y)
                                user32.SetWindowPos(hwnd,wintypes.HWND(-2),0,0,0,0,3)
                            await until(lambda:'speech' in host.manual_position)
                            dragged=dict(host.rects['speech']);anchor[0]+=100;anchor[1]+=50
                            host.refresh_positions();host.publish();await asyncio.sleep(.3)
                            native_rect=wintypes.RECT();user32.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
                            user32.GetWindowRect(hwnd,ctypes.byref(native_rect))
                            assert abs(native_rect.left-dragged['x']-100)<=1 and abs(native_rect.top-dragged['y']-50)<=1,'manual_bubble_did_not_follow_anchor'
                            print(json.dumps({'relative_anchor_follow':True,'delta':[100,50]}))
                        if args.os_resize:
                            # Actual Windows mouse input on this test's owned speech window only.
                            hwnd=window_handles['Engram speech']
                            user32.SetForegroundWindow.argtypes=[wintypes.HWND]
                            user32.SetForegroundWindow(hwnd)
                            await asyncio.sleep(.2)
                            grip=await evaluate("(()=>{const r=document.querySelector('.resize-handle').getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()")
                            before=dict(host.rects['speech']);scale=before['scale']
                            point=wintypes.POINT();user32.GetCursorPos(ctypes.byref(point))
                            x=int(before['x']+grip[0]*scale);y=int(before['y']+grip[1]*scale)
                            try:
                                user32.SetCursorPos(x,y);await asyncio.sleep(.1)
                                user32.mouse_event(2,0,0,0,0);await asyncio.sleep(.15)
                                for step in range(1,6):
                                    user32.SetCursorPos(x+step*12,y+step*8);await asyncio.sleep(.08)
                            finally:
                                user32.mouse_event(4,0,0,0,0)
                                user32.SetCursorPos(point.x,point.y)
                            await until(lambda:'speech' in host.manual_size)
                            after=host.rects['speech']
                            assert after['width']>before['width'] and after['height']>before['height'],'os_resize_not_applied'
                            print(json.dumps({'os_speech_resize':True,'before':[before['width'],before['height']],'after':[after['width'],after['height']]}))
                            await capture('speech-resized')
                        host.show_nudge('synthetic native initiative',dwell_ms=20000)
                        await asyncio.sleep(.12)
                        assert not host.composer_visible,'nudge_opened_composer'
                        assert await evaluate("document.body.classList.contains('nudge')"),'native_nudge_style'
                        await capture('nudge')
                        await evaluate("[...document.querySelectorAll('button')].find(b=>b.textContent==='답장').click()")
                        await until(lambda:host.composer_visible)
                        assert host._reply_context=='synthetic native initiative','reply_context_missing'
                        continue
                    if label=='thought' and args.restoration:
                        saved_cfg=dict(host.cfg)
                        host.update_cfg({**saved_cfg,'font_size':12,'thought_max_height_ratio':.15})
                        host._present('thought','짧은 생각');host.refresh_positions();host.publish();await asyncio.sleep(.4)
                        small=await evaluate('[innerWidth,innerHeight]')
                        host._present('thought','생각 길이에 맞춘 자동 크기 검증\n'*80);host.refresh_positions();host.publish();await asyncio.sleep(.5)
                        large=await evaluate('[innerWidth,innerHeight]')
                        assert large[1]>small[1],'thought_did_not_grow'
                        assert await evaluate("(()=>{const c=document.querySelector('#content');return c.scrollHeight>c.clientHeight})()"),'thought_not_scrollable'
                        await capture('restored-thought')
                        print(json.dumps({'thought_short':small,'thought_long':large}))
                        host.update_cfg(saved_cfg)
                    if label!='input':continue
                    await evaluate("document.querySelector('#draft').value='synthetic first';document.querySelector('#send').click()")
                    await until(lambda:len(provider.sent)==1)
                    await until(lambda:host.accepted==provider.sent[0][0].request_id)
                    await asyncio.sleep(.15)
                    assert await evaluate("document.querySelector('#draft').value===''"),'draft_ack'
                    await evaluate("document.querySelector('#draft').value='synthetic second';document.querySelector('#send').click()")
                    await until(lambda:len(host.queue.snapshot().waiting)==1)
                    await asyncio.sleep(.15)
                    await evaluate("document.querySelector('#draft').value='synthetic third';document.querySelector('#send').click()")
                    await until(lambda:len(host.queue.snapshot().waiting)==2)
                    await asyncio.sleep(.15)
                    await capture('compact-input')
                    input_size=await evaluate('[innerWidth,innerHeight]')
                    assert await evaluate('window.innerHeight<=430'),'compact_height_regression'
                    assert await evaluate("getComputedStyle(document.querySelector('#heading')).display==='none'"),'heading_not_removed'
                    assert await evaluate("document.querySelector('#send').getBoundingClientRect().left >= document.querySelector('#draft').getBoundingClientRect().right"),'send_not_beside_editor'
                    await evaluate("document.querySelector('#flip').click()")
                    await asyncio.sleep(.3)
                    assert await evaluate("document.querySelector('#inputFace').inert && !document.querySelector('#queueFace').inert"),'flip_inert'
                    assert await evaluate('[innerWidth,innerHeight]')==input_size,'flip_changed_size'
                    assert await evaluate("getComputedStyle(document.querySelector('#queue')).overflowY==='auto'"),'queue_not_scrollable'
                    await capture('queue')
                    # The selected waiting item owns the lightning action. Its
                    # duplicate event must not cause duplicate provider dispatch.
                    await evaluate("document.querySelectorAll('.card')[1].click()")
                    await capture('queue-actions')
                    await evaluate("document.querySelector('.bolt').click();document.querySelector('.bolt').click()")
                    assert len(provider.sent)==1,'interrupt_ack_is_not_terminal'
                    await until(lambda:len(provider.sent)==2)
                    assert host.queue.snapshot().held,'remainder_not_held'
                    assert len(host.queue.snapshot().waiting)==1,'wrong_priority_count'
                    await evaluate("document.querySelector('#back').click()")
                    await asyncio.sleep(.3)
                    assert await evaluate("!document.querySelector('#inputFace').inert && document.querySelector('#queueFace').inert"),'flip_restore'
                    assert not (host.manual_position-({'speech'} if args.restoration else set())) and not (host.manual_size-({'speech'} if args.os_resize else set())),'passive_geometry_marked_manual'
                    # Exercise completed recent-slot expiry without deleting queue history.
                    key=provider.active;provider.active=None
                    host.cfg['echo_dwell_ms']=50
                    host.handle_event({'kind':'turn_end','terminal':True,'provider_status':'completed','request_key':asdict(key)})
                    waiting=host.queue.snapshot().waiting[0].key
                    host.action({'action_id':'qa-delete-waiting','action':'delete','request_id':waiting.request_id,'payload':{}})
                    await asyncio.sleep(.1)
                    await evaluate('resumeRecentFade()')
                    await asyncio.sleep(.12)
                    assert await evaluate("document.querySelector('#recent').hidden"),'recent_not_expired'
                    assert host.cards[key.request_id]['state']=='sent','queue_history_lost'
                    host.hide();host.show();await asyncio.sleep(.15)
                    await evaluate("document.querySelector('#draft').value='synthetic reopen';document.querySelector('#send').click()")
                    await until(lambda:len(provider.sent)==3)
                    assert host.queue.snapshot().active is not None,'reopened_submit_stuck'
            assert len(provider.sent)==3,'no_input_target'
        if args.demo:await asyncio.sleep(min(60,args.seconds))
        print(json.dumps({'status':'PASS','native_windows':3,'exercise':args.exercise,
            'provider':'synthetic','dispatch_count':len(provider.sent),'held':host.queue.snapshot().held,
            'actions':sorted(set(events))}))
    finally:
        stopped=host.stop()
        print(json.dumps({'owned_child_reaped':stopped,'remaining_pid':host.shell.pid}))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--exe');parser.add_argument('--exercise',action='store_true')
    parser.add_argument('--capture-dir',help='Save only the isolated synthetic WebView surfaces as PNG')
    parser.add_argument('--demo',action='store_true');parser.add_argument('--seconds',type=int,default=60)
    parser.add_argument('--os-resize',action='store_true',help='Exercise the owned speech resize grip using the Windows mouse')
    parser.add_argument('--restoration',action='store_true',help='Verify content sizing, configured fonts and relative anchor follow')
    args=parser.parse_args()
    try:asyncio.run(run(args))
    except Exception as error:
        print(json.dumps({'status':'FAIL','reason':type(error).__name__,'assertion':str(error) if isinstance(error,(AssertionError,RuntimeError)) else None}))
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
