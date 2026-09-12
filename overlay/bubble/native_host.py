"""Private bubble UI controller and authoritative turn dispatcher.

All methods except shell callbacks run on the host UI thread. Provider callbacks
must be marshalled there too. The public renderer must never receive snapshots.
"""
from __future__ import annotations
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
import base64
import math
from io import BytesIO
import re
import time
import uuid

from .native_shell import NativeBubbleShell
from .rich_input import validate_attachments
from .turn_queue import TurnQueue, TurnKey, TurnState
from . import geometry
from overlay.chat_window import terminal_font_size

LABELS={'waiting':'대기','editing':'수정 중','active':'처리 중','interrupt_requested':'중단 확인 중',
        'sent':'보냄','failed':'실패','interrupted':'중단됨','unknown':'상태 확인 필요'}


@dataclass(repr=False)
class Payload:
    text: str = field(repr=False)
    attachments: tuple = field(repr=False)
    context: str | None = field(default=None,repr=False)


def version_question(text):
    normalized=re.sub(r'[\s?!？！.]+',' ',text.strip().lower()).strip()
    return normalized in {'버전 뭐야','amber 버전','engram 버전','현재 버전','앰버 버전'}


class NativeBubbleHost:
    def __init__(self, *, schedule, get_session, get_anchor, on_dispatch=lambda _:None,
                 on_history=lambda:None, on_activity=lambda _:None, on_fallback=lambda _:None,
                 on_nudge_reply=lambda token:None, on_nudge_defer=lambda:None, on_nudge_ignored=lambda:None,
                 on_nudge_accepted=lambda token:None, on_composer_closed=lambda:None,
                 cfg=None, terminal_cfg=None, shell_factory=NativeBubbleShell, version=None):
        self.schedule,self.get_session,self.get_anchor=schedule,get_session,get_anchor
        self.on_dispatch,self.on_history,self.on_activity=on_dispatch,on_history,on_activity
        self.on_fallback=on_fallback
        self.on_nudge_reply,self.on_nudge_defer,self.on_nudge_ignored=on_nudge_reply,on_nudge_defer,on_nudge_ignored
        self.on_nudge_accepted,self.on_composer_closed=on_nudge_accepted,on_composer_closed
        self._reply_context=None
        self._reply_token=None
        self.cfg=dict(cfg or {})
        self.terminal_cfg=dict(terminal_cfg or {})
        if version is None:
            try:
                from core.install.versioning import resolve_version
                version=resolve_version().version
            except (ValueError,OSError): version=''
        self.version=version
        self.session_id=uuid.uuid4().hex
        self.generation=None
        self.provider=None
        self._can_rebind=False
        self.queue=TurnQueue()
        self.payloads={}
        self.cards=OrderedDict()
        self.actions=OrderedDict()
        self.priority=None
        self.edit=None
        self.accepted=None
        self.rejected=None
        self.notice=''
        self.visible=False
        self.composer_visible=False
        self._nudge=None
        self.rects={}
        self._sent_geometry={}
        self._dismissed=set()
        # Position and size are independent user choices.  A DPI or host
        # reflow must not turn either one into a sticky manual override.
        self.manual_position=set()
        self.manual_size=set()
        self._manual_rects={}
        self._input_height=185
        self._presentation_sizes={}
        self.speech={'text':''}
        self.thought={'text':''}
        self.blocks={}
        self.presentation_revision={'speech':0,'thought':0}
        self.approval=None
        self._publish_pending=False
        self._interrupt_token=None
        self.shell=shell_factory(lambda a:self.schedule(0,lambda:self.action(a)),
            lambda c:self.schedule(0,lambda:self._crashed(c)),
            on_ready=lambda:self.schedule(0,self._ready),
            on_geometry=lambda r:self.schedule(0,lambda:self._geometry(r)))

    def _present(self, kind, text, **extra):
        if kind=='speech' and self._nudge and not self._nudge['settled']:
            self._settle_nudge('ignored')
        if kind=='speech' and not extra.get('nudge'):self._nudge=None
        self.presentation_revision[kind]+=1
        value={'text':text,'presentation_revision':self.presentation_revision[kind]}
        value.update(extra);setattr(self,kind,value)
        self._dismissed.discard(kind)

    def show(self, *, composer=True):
        self.visible=True
        self.composer_visible=bool(composer)
        for kind in ('speech','thought'):
            self.presentation_revision[kind]+=1
            getattr(self,kind)['presentation_revision']=self.presentation_revision[kind]
        if self._nudge:self._nudge['revision']=self.presentation_revision['speech']
        self._dismissed.clear();self._sent_geometry.clear()
        if self.shell.pid is None:
            return self.shell.start(self.snapshot())
        self.refresh_positions(focus=True)
        self.publish()
        return True

    def hide(self):
        if self.composer_visible:
            if self._reply_context and self._nudge:
                self._nudge['replied']=False;self.speech['nudge']=True
            self._reply_context=None;self._reply_token=None;self.on_composer_closed()
        self.visible=False
        self.composer_visible=False
        self.queue.hold()
        self._sent_geometry.clear()
        for label in ('input','speech','thought'):
            self.shell.set_geometry({'window':label,'visible':False})
        self.on_activity(False)
        self.publish()

    def stop(self):
        self._settle_nudge('ignored')
        if self.composer_visible:
            self._reply_context=None;self._reply_token=None;self.on_composer_closed()
        self.composer_visible=False
        self.queue.hold()
        self.visible=False
        return self.shell.stop()

    def open_composer(self):
        """Show a blank composer without replacing a nudge presentation."""
        self.composer_visible=True;self.visible=True
        if self.shell.pid is None:return self.shell.start(self.snapshot())
        self.refresh_positions(focus=True);self.publish();return True

    def is_idle(self):
        if self.queue.snapshot().active or self.queue.snapshot().waiting:return False
        return not self.visible or (not self.composer_visible and not self.approval and
            all(k in self._dismissed or not getattr(self,k).get('text') for k in ('speech','thought')))

    def show_nudge(self, text, *, dwell_ms=None):
        if not isinstance(text,str) or not text.strip():return False
        self._present('speech',text.strip(),nudge=True,
            fade_ms=int(dwell_ms if dwell_ms is not None else self.cfg.get('speech_dwell_ms',20000)))
        self._nudge={'revision':self.presentation_revision['speech'],'settled':False,'text':text.strip()}
        return self.show(composer=False)

    def defer_nudge(self):
        """Launcher collapse is deferral, not a negative user response."""
        pending=bool(self._nudge and (not self._nudge['settled'] or self._reply_context))
        if self._nudge:self._nudge['settled']=True
        self._reply_context=None;self._reply_token=None;self.composer_visible=False
        return pending

    def _settle_nudge(self, outcome):
        nudge=self._nudge
        if not nudge or nudge['settled']:return
        nudge['settled']=True
        if outcome=='ignored':self.on_nudge_ignored()
        elif outcome=='defer':self.on_nudge_defer()

    def _ready(self):
        self._sent_geometry.clear()
        self.refresh_positions(focus=self.visible)
        self.publish()

    def _crashed(self, code):
        self._settle_nudge('ignored')
        self.visible=False
        self.queue.hold()
        self.notice='말풍선 연결이 종료되었습니다. 대기 요청은 보류했습니다.'
        self.on_fallback(code)

    def provider_stopped(self,session):
        """Called only after the exact detached provider reports verified stop."""
        if session is not self.provider:return
        active=self.queue.snapshot().active
        if active:
            self.queue.confirmed_interrupt(active.key)
            self._card(active.key,'interrupted')
            self.payloads.pop(active.key,None)
        self.queue.hold();self.queue.release_priority();self.priority=None
        self._interrupt_token=None;self.provider=None;self._can_rebind=True
        self.notice='대기 요청은 보존했습니다. 재개하면 새 연결에서 이어서 보냅니다.'
        self.publish()

    def _session(self):
        session=self.get_session()
        if not session or not session.is_alive():raise ValueError('provider unavailable')
        generation=session._attempt_generation
        if self.generation is None:self.generation=generation
        if self.provider is not None and session is not self.provider:
            raise ValueError('previous provider stop is not confirmed')
        if generation!=self.generation:
            if not self._can_rebind or self.queue.snapshot().active:raise ValueError('provider attempt changed')
            old=self.queue.snapshot();fresh=TurnQueue();new_payloads={};new_edit=None
            for item in old.waiting:
                p=self.payloads[item.key]
                key=fresh.enqueue(self.session_id,item.key.request_id,generation,text_private=p.text,image_sizes=[len(a.data) for a in p.attachments])
                new_payloads[key]=p
                if item.key==self.edit:fresh.begin_edit(key);new_edit=key
            fresh.hold();self.queue=fresh;self.payloads=new_payloads;self.edit=new_edit;self.generation=generation
        self.provider=session;self._can_rebind=False
        return session

    def _geometry(self, rect):
        if not isinstance(rect,dict):return
        label=rect.get('window')
        if label not in ('input','speech','thought'): return
        values={key:rect.get(key) for key in ('x','y','width','height','scale')}
        if not all(isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) for value in values.values()): return
        if values['width']<=0 or values['height']<=0 or values['scale']<=0:return
        old=self.rects.get(label,{})
        origin=rect.get('origin')
        if origin not in ('passive','dpi','user_drag','user_resize'):return
        preferred=self._manual_rects.setdefault(label,{})
        if origin=='user_drag':
            ax,ay,aw,ah=self.get_anchor()
            if aw<=0 or ah<=0:return
            self.manual_position.add(label);preferred.update(dx=(values['x']-ax)/aw,dy=(values['y']-ay)/ah)
        elif origin=='user_resize':
            self.manual_size.add(label)
            preferred.update(width=values['width']/values['scale'],height=values['height']/values['scale'])
            self.publish()
        # Only explicit completed native interactions become manual.  Passive
        # move/resize and DPI reports are informational and never persist.
        self.rects[label]={**old,**values}
        if old.get('scale')!=values['scale']:
            self.refresh_positions()

    def refresh_positions(self, focus=False):
        if not self.shell.is_ready: return
        x,y,w,h=self.get_anchor()
        style=self._presentation_style(x,y,w)
        if getattr(self,'_last_style',None)!=style:
            self._last_style=style
            self.publish()
        defaults={'input':(x-465,y,460,self._input_height),
                  'speech':(x-465,y-230,460,220),
                  'thought':(x-20,y-190,250,170)}
        for label,(px,py,pw,ph) in defaults.items():
            rect=self.rects.get(label,{})
            show=self.visible and label not in self._dismissed and ((label=='input' and self.composer_visible) or bool(getattr(self,label,{}).get('text')) or (label=='speech' and self.approval is not None))
            scale=rect.get('scale',1)
            preferred=self._manual_rects.get(label,{})
            natural=self._presentation_sizes.get(label,{})
            pw=natural.get('width',pw);ph=natural.get('height',ph)
            if label!='input' and label not in self.manual_size:
                limits=style[label]
                pw=max(limits['min_width'],min(limits['max_width'],pw))
                ph=max(limits['min_height'],min(limits['max_height'],ph))
                px=x-int(pw*scale)-5 if label=='speech' else x-20
                py=y-int(ph*scale)-(10 if label=='speech' else 20)
            geometry={'window':label,
                'x':int(x+preferred.get('dx',0)*w) if label in self.manual_position else px,
                'y':int(y+preferred.get('dy',0)*h) if label in self.manual_position else py,
                'width':int(preferred.get('width',pw)*scale) if label in self.manual_size else int(pw*scale),
                'height':int(preferred.get('height',ph)*scale) if label in self.manual_size else int(ph*scale),
                'visible':show,'focus':focus and label=='input' and self.composer_visible}
            if label!='input':geometry['tail_target']=[x+w/2,y+h/2]
            if label in style:geometry['presentation_style']=style[label]
            self.rects[label]={**geometry,'scale':scale}
            if self._sent_geometry.get(label)!=geometry:
                self._sent_geometry[label]=geometry
                self.shell.set_geometry(geometry)

    def _presentation_style(self, x, y, char_w):
        physical_width=geometry.default_bubble_width(x,y,char_w,self.cfg)
        _,top,_,bottom=geometry.get_monitor_work_rect(x,y)
        configured=self.cfg.get('font_size') or 0
        pt=max(8,int(configured)) if configured else max(8,round(terminal_font_size(x,y,self.terminal_cfg)))
        # Tk uses positive point sizes. CSS logical pixels = points * 96/72.
        px=pt*96/72
        family=str(self.cfg.get('font_family') or 'Noto Sans KR Medium')
        result={'input':{'font_family':family,'font_size':px}}
        for label,min_width,min_height,default_ratio in (('speech',220,140,.55),('thought',180,120,.30)):
            scale=self.rects.get(label,{}).get('scale',1)
            ratio=float(self.cfg.get(f'{label}_max_height_ratio',default_ratio))
            # Zero means no configured cap, still bounded by the visible work area.
            cap=(bottom-top)*(min(1,ratio) if ratio>0 else 1)/scale
            result[label]={'font_family':family,'font_size':px,
                'max_width':max(min_width,int(physical_width/scale)),
                'max_height':max(min_height,int(cap)),
                'min_width':min_width,'min_height':min_height,
                'manual_size':label in self.manual_size}
        return result

    def update_cfg(self, cfg, terminal_cfg=None):
        self.cfg=dict(cfg or {})
        if terminal_cfg is not None:self.terminal_cfg=dict(terminal_cfg or {})
        self._presentation_sizes.clear();self._sent_geometry.clear();self.refresh_positions();self.publish()

    def _key(self, request_id):
        return next((key for key in self.payloads if key.request_id==request_id),None)

    def _card(self,key,state):
        card=self.cards.get(key.request_id)
        if card:
            card['state']=state
            card['status_label']=LABELS.get(state,state)

    def _prepare_card(self,payload):
        from PIL import Image
        thumbnails=[]
        for attachment in payload.attachments:
            with Image.open(BytesIO(attachment.data)) as source:
                source.thumbnail((96,96));buf=BytesIO();source.convert('RGB').save(buf,format='JPEG',quality=70)
            thumbnails.append('data:image/jpeg;base64,'+base64.b64encode(buf.getvalue()).decode('ascii'))
        return {'state':'waiting','status_label':'대기',
            'summary':payload.text.replace('\n',' ')[:160] or f'이미지 {len(payload.attachments)}개',
            'image_count':len(payload.attachments),'thumbnails':thumbnails}
    def _remember(self,key,card):
        self.cards[key.request_id]=dict(card,request_id=key.request_id)
        # Completed cards retain only bounded summaries; original images stay
        # with pending payloads and are released on terminal/cancel.
        completed=[k for k,c in self.cards.items() if c['state'] in ('sent','failed','interrupted')]
        for k in completed[:-10]: self.cards.pop(k,None)

    def snapshot(self):
        snap=self.queue.snapshot()
        cards=[dict(c,locked=self.priority is not None) for c in self.cards.values()]
        edit=None
        if self.edit in self.payloads:
            p=self.payloads[self.edit]
            edit={'request_id':self.edit.request_id,'text':p.text,'attachments':[
                f'data:{a.mime_type};base64,'+base64.b64encode(a.data).decode('ascii') for a in p.attachments]}
        approval={'id':self.approval.id,'summary':f'{self.approval.tool_name} 실행을 허용할까요?'} if self.approval else None
        recent=cards[-1] if cards else None
        x,y,w,_=self.get_anchor()
        return {'version':self.version,'queue':cards,'recent':recent,'presentation_style':self._presentation_style(x,y,w),
            'busy':snap.active is not None,'held':snap.held,'edit':edit,'approval':approval,
            'accepted_request_id':self.accepted,'rejected_request_id':self.rejected,
            'notice':self.notice,'speech':self.speech,'thought':self.thought}

    def publish(self):
        if self._publish_pending: return
        self._publish_pending=True
        def flush():
            self._publish_pending=False
            if self.shell.is_ready:self.shell.publish_snapshot(self.snapshot())
        self.schedule(30,flush)

    def action(self,message):
        action_id=message.get('action_id')
        if action_id in self.actions:return
        self.actions[action_id]=True
        if len(self.actions)>512:self.actions.popitem(last=False)
        name=message.get('action');data=message.get('payload') or {};rid=message.get('request_id')
        key=self._key(rid)
        self.notice=''
        try:
            if name=='submit':self.submit(rid,data.get('text',''),data.get('attachments',[]))
            elif name=='edit' and key and self.queue.begin_edit(key):self.edit=key;self._card(key,'editing')
            elif name=='save_edit':
                key=self._key(data.get('edit_request_id'))
                if key!=self.edit:raise ValueError('edit no longer available')
                payload=self._validated(data.get('text',''),data.get('attachments',[]))
                payload.context=self.payloads[key].context
                if payload.context and len(payload.context)+len(payload.text)+40>65536:raise ValueError('context exceeds turn limit')
                card=self._prepare_card(payload)
                if not self.queue.update_edit(key,payload.text,[len(a.data) for a in payload.attachments]):raise ValueError('edit lost')
                self.queue.save_edit(key);self.payloads[key]=payload;self._remember(key,card);self.edit=None;self.accepted=rid;self._dispatch()
            elif name=='cancel_edit' and key==self.edit:
                self.queue.cancel_edit(key);self._card(key,'waiting');self.edit=None;self._dispatch()
            elif name=='delete' and key and self.queue.delete(key):
                self.payloads.pop(key,None);self.cards.pop(rid,None)
                if self.edit==key:self.edit=None
                self._dispatch()
            elif name=='send_now' and key and self.queue.reserve_priority(key):
                self.priority=key
                active=self.queue.snapshot().active
                if active:self._interrupt(active.key)
                else:self._dispatch(priority=key)
            elif name=='interrupt' and key:self._interrupt(key)
            elif name=='resume_queue':
                self._session()
                if self.queue.resume():self._dispatch()
            elif name=='close':
                self._settle_nudge('ignored');self.hide()
            elif name=='history':self.on_history()
            elif name=='input_activity':self.on_activity(bool(data.get('active')))
            elif name=='resize_input':
                height=data.get('height')
                if isinstance(height,(int,float)) and math.isfinite(height) and self.visible and 'input' not in self.manual_size:
                    rect=self.rects.get('input',{})
                    scale=rect.get('scale',1)
                    if not isinstance(scale,(int,float)) or not math.isfinite(scale) or scale<=0:raise ValueError('invalid scale')
                    self._input_height=max(170,min(430,height))
                    if int(self._input_height*scale)!=rect.get('height'):
                        # A resize can arrive before native geometry. Recompute
                        # the complete anchored rect rather than emitting an
                        # incomplete height-only geometry payload.
                        self._sent_geometry.pop('input',None)
                        self.refresh_positions()
            elif name=='presentation_size':
                label=data.get('window');width,height=data.get('width'),data.get('height')
                if label not in ('speech','thought') or data.get('presentation_revision')!=self.presentation_revision[label] or label in self.manual_size:return
                if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (width,height)):raise ValueError('invalid presentation size')
                x,y,char_w,_=self.get_anchor();limits=self._presentation_style(x,y,char_w)[label]
                if not (limits['min_width']<=width<=limits['max_width'] and limits['min_height']<=height<=limits['max_height']):raise ValueError('presentation size out of range')
                candidate={'width':round(width),'height':round(height)}
                if self._presentation_sizes.get(label)!=candidate:self._presentation_sizes[label]=candidate;self._sent_geometry.pop(label,None);self.refresh_positions()
            elif name=='dismiss' and data.get('window') in ('speech','thought'):
                label=data['window']
                if data.get('presentation_revision')!=self.presentation_revision[label] or (label=='speech' and self.approval):return
                if label=='speech' and self._nudge and self._nudge.get('revision')==data.get('presentation_revision'):
                    self._settle_nudge('ignored')
                self._dismissed.add(label)
                self._sent_geometry.pop(label,None)
                self.shell.set_geometry({'window':label,'visible':False})
            elif name=='approval' and self.approval and data.get('approval_id')==self.approval.id:
                (self.approval.allow if data.get('allow') is True else self.approval.deny)()
                self.approval=None
            elif name=='nudge_reply' and self._nudge and not self._nudge.get('replied'):
                # A reply to a previously faded nudge is a late engagement,
                # not a second ignored outcome.
                if data.get('presentation_revision')!=self._nudge['revision']:return
                self._nudge['settled']=True;self._nudge['replied']=True
                self._reply_context=self._nudge['text']
                self._reply_token=uuid.uuid4().hex
                self.speech.pop('fade_ms',None)
                self.presentation_revision['speech']+=1;self.speech['presentation_revision']=self.presentation_revision['speech']
                self._nudge['revision']=self.presentation_revision['speech']
                self.speech['nudge']=False;self._dismissed.add('speech')
                self.on_nudge_reply(self._reply_token);self.open_composer()
            elif name=='nudge_defer':
                if not self._nudge or data.get('presentation_revision')!=self._nudge['revision']:return
                self._settle_nudge('ignored')
                self._dismissed.add('speech');self.refresh_positions()
        except (ValueError,OverflowError,TypeError,RuntimeError,OSError):
            self.rejected=rid
            self.notice='요청을 적용하지 못했습니다. 입력과 대기 상태를 확인해주세요. (대기 최대 5건, 이미지 개별 5 MiB)'
        self.publish()

    def _validated(self,text,images):
        if not isinstance(text,str) or len(text)>65536:raise ValueError('invalid text')
        attachments=validate_attachments(images)
        if not text.strip() and not attachments:raise ValueError('empty input')
        return Payload(text.strip(),attachments)

    def submit(self,rid,text,images):
        if not isinstance(rid,str) or not rid or len(rid)>128:raise ValueError('invalid request id')
        payload=self._validated(text,images)
        if not payload.attachments and version_question(payload.text):
            self._present('speech',f'실행 중인 AMBER (ENGRAM) 버전은 {self.version}입니다.' if self.version else '실행 버전 정보를 확인할 수 없습니다.')
            self.accepted=rid;self.refresh_positions();return
        card=self._prepare_card(payload)
        session=self._session()
        generation=session._attempt_generation
        context=self._reply_context
        token=self._reply_token
        if context:
            prepared=f'[방금 내가 사용자에게 먼저 건넨 말] {context}\n\n{payload.text}'
            if len(prepared)>65536:raise ValueError('context exceeds turn limit')
            payload=Payload(payload.text,payload.attachments,context)
        key=self.queue.enqueue(self.session_id,rid,generation,text_private=payload.text,image_sizes=[len(a.data) for a in payload.attachments])
        self.payloads[key]=payload;self._remember(key,card);self.accepted=rid
        if context and token==self._reply_token:
            self._reply_context=None;self._reply_token=None
            try:self.on_nudge_accepted(token)
            except Exception:pass  # Accepted queue ownership must not be reversed by reporting.
        snap=self.queue.snapshot()
        if snap.held and snap.active is None:
            # Collapse retains old waiting work. A later fresh submit may run
            # only itself; never resume old held entries implicitly.
            self._dispatch(priority=key)
        elif snap.held:
            self.notice='기존 요청 상태를 확인 중입니다. 새 요청은 보내지 않았습니다.'
        else:
            self._dispatch()

    def _dispatch(self,priority=None):
        key=self.queue.dispatch_priority_after_terminal(priority) if priority else self.queue.dispatch_next()
        if key is None:return
        if priority:self.priority=None
        p=self.payloads[key];session=self.provider
        provider_text=f'[방금 내가 사용자에게 먼저 건넨 말] {p.context}\n\n{p.text}' if p.context else p.text
        if not session or not session.send_rich(provider_text,p.attachments,key):
            self.queue.mark_unknown(key);self._card(key,'unknown');self.notice='전송 여부를 확인할 수 없어 대기열을 보류했습니다.';return
        self._card(key,'active');self._present('speech','');self._present('thought','생각 중…');self.blocks.clear()
        self.on_dispatch(p.text);self.refresh_positions();self.publish()

    def _interrupt(self,key):
        if not self.queue.request_interrupt(key):return
        self._card(key,'interrupt_requested');token=self._interrupt_token=object()
        session=self.provider
        if not session or not session.interrupt(key):self._interrupt_failed(key);return
        def timeout():
            active=self.queue.snapshot().active
            if self._interrupt_token is token and active and active.key==key:self._interrupt_failed(key)
        self.schedule(15000,timeout)

    def _interrupt_failed(self,key):
        self.queue.mark_unknown(key);self._card(key,'unknown');self.priority=None;self.queue.release_priority()
        self.notice='중단 완료를 확인하지 못했습니다. 새 요청은 보내지 않았습니다.';self.publish()

    def handle_event(self,event):
        raw=event.get('request_key')
        active=self.queue.snapshot().active
        if raw:
            try:key=TurnKey(**raw)
            except (TypeError,ValueError):return False
            if not active or key!=active.key:return False
        elif active:
            return False
        else:
            return True
        kind=event.get('kind')
        if kind in ('interrupt_failed','provider_unknown'):
            self._interrupt_failed(key);return True
        if kind=='interrupt_ack':return True
        if kind in ('speech','thought'):
            self._dismissed.discard(kind)
            target=getattr(self,kind)
            block=event.get('id')
            text=event.get('text') or ''
            target['text']=((target.get('text','') if self.blocks.get(kind)==block else '')+text) if event.get('delta') else text
            target['text']=target['text'][-32000:];target.pop('fade_ms',None);self.blocks[kind]=block
            self.presentation_revision[kind]+=1;target['presentation_revision']=self.presentation_revision[kind]
        elif kind=='tool_use':
            self._present('thought',str(event.get('tool_name') or '도구 실행 중')[:160])
        if event.get('terminal') or kind=='turn_end':
            self._interrupt_token=None
            interrupted=event.get('provider_status')=='interrupted'
            # Claude's control protocol reports ResultMessage as terminal; a
            # requested stop may race a normal completion. Do not invent status.
            if interrupted:
                self.queue.confirmed_interrupt(key);self._card(key,'interrupted')
            elif event.get('is_error') or kind=='error':self.queue.terminal_failure(key);self._card(key,'failed')
            else:self.queue.terminal_success(key);self._card(key,'sent')
            self.payloads.pop(key,None)
            completed=[rid for rid,c in self.cards.items() if c['state'] in ('sent','failed','interrupted')]
            for rid in completed[:-10]:self.cards.pop(rid,None)
            if event.get('text') and not self.speech.get('text'):
                self._present('speech',str(event['text'])[:32000])
            if self.cfg.get('speech_fade',True):
                self.speech['fade_ms']=int(self.cfg.get('speech_dwell_ms',20000));self.presentation_revision['speech']+=1;self.speech['presentation_revision']=self.presentation_revision['speech']
            if self.cfg.get('thought_fade',True):
                self.thought['fade_ms']=int(self.cfg.get('thought_dwell_ms',0));self.presentation_revision['thought']+=1;self.thought['presentation_revision']=self.presentation_revision['thought']
            for card in self.cards.values():
                if card['state'] in ('sent','failed','interrupted'):
                    card['fade_ms']=int(self.cfg.get('echo_dwell_ms',8000)) if self.cfg.get('echo_fade',True) else None
            chosen=self.priority
            if chosen and not event.get('is_error'):self._dispatch(priority=chosen)
            elif chosen and interrupted:self._dispatch(priority=chosen)
            elif chosen:self.priority=None;self.queue.release_priority()
            else:self._dispatch()
        self.refresh_positions();self.publish();return True

    def show_approval(self,request):
        self.approval=request
        self._present('speech','도구 실행에 확인이 필요합니다.')
        self.refresh_positions();self.publish()
