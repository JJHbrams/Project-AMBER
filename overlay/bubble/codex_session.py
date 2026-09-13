"""Private, owned Codex app-server connection with correlated terminal delivery.

The reader never waits for a request: RPC futures and notifications are routed
independently. Control acknowledgements are not treated as turn completion.
"""
from __future__ import annotations
import base64
from concurrent.futures import Future, TimeoutError
from dataclasses import asdict, dataclass
import json
import os
import subprocess
import threading
import uuid

from .provider_cli import codex_executable
from .turn_queue import TurnKey


@dataclass
class _Active:
    key: TurnKey
    turn_id: str | None = None
    unknown: bool = False


class CodexBubbleSession:
    provider = 'codex'

    def __init__(self, cwd, env_overrides=None, permission_level='auto', on_event=None,
                 on_approval_request=None, resume_session_id=None, on_session_id=None,
                 state_controller=None, on_title_checkpoint=None, stm_bridge=None,
                 thinking_tokens=0, bootstrap_prompt=None, **_):
        self._cwd=cwd
        self._env=dict(env_overrides or {})
        self._permission=permission_level
        self._emit_cb=on_event or (lambda _:None)
        self._approval_cb=on_approval_request
        self._resume=resume_session_id
        self._on_sid=on_session_id
        self._state=state_controller
        self._stm=stm_bridge
        self._bootstrap=bootstrap_prompt
        self._title_cb=on_title_checkpoint
        self._attempt_generation=1
        self._proc=None
        self._thread_id=None
        self._active=None
        self._pending={}
        self._early=[]
        self._seq=0
        self._lock=threading.RLock()
        self._write_lock=threading.Lock()
        self._alive=threading.Event()
        self._stopping=threading.Event()
        self._assistant=[]

    def start(self):
        if self.is_alive():return
        env={k:v for k,v in os.environ.items() if not k.startswith('ORCA_')}
        env.update(self._env)
        self._stopping.clear()
        self._proc=subprocess.Popen([codex_executable(),'app-server'],cwd=self._cwd,env=env,
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self._alive.set()
        threading.Thread(target=self._reader,args=(self._proc,),daemon=True,name='bubble-codex-rpc').start()
        try:
            self._rpc('initialize',{'clientInfo':{'name':'engram-bubble','version':'1'},'capabilities':{}})
            self._write({'method':'initialized','params':{}})
            params=self._thread_params()
            result=self._rpc('thread/resume' if self._resume else 'thread/start',params,timeout=15)
            sid=result.get('thread',{}).get('id')
            if not isinstance(sid,str) or not sid:raise RuntimeError('thread_start_invalid')
            self._thread_id=sid
            if self._on_sid:self._on_sid(sid)
            if self._state:self._state.bind_provider_session(sid)
            if self._stm:self._stm.open()
        except Exception:
            self.stop()
            raise RuntimeError('codex_session_start_failed') from None

    def _thread_params(self):
        """Build fresh and resume thread parameters from the same bubble policy."""
        params={'cwd':self._cwd,'approvalPolicy':'never' if self._permission=='auto' else 'untrusted',
            'sandbox':'workspace-write'}
        if self._bootstrap:
            params['developerInstructions']=self._bootstrap
        if self._resume:
            params['threadId']=self._resume
        return params

    def is_alive(self):
        return bool(self._alive.is_set() and self._proc and self._proc.poll() is None and not self._stopping.is_set())

    def retire_state(self):
        if self._state:
            checkpoint=self._state.retire()
            if checkpoint is not None and self._title_cb:self._title_cb(checkpoint)

    def stop(self,timeout=3):
        self._stopping.set();self._alive.clear();self.retire_state()
        proc=self._proc
        if proc is None:return True
        try:
            proc.stdin.close()
            try:proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:proc.wait(timeout)
                except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout)
        finally:
            proc.stdout.close()
            self._proc=None
            if self._stm:self._stm.close()
        return proc.poll() is not None

    def send(self,text):
        return self.send_rich(text,(),TurnKey(self._thread_id or 'pending',uuid.uuid4().hex,self._attempt_generation))

    def send_rich(self,text,attachments,key):
        if not self.is_alive() or not self._thread_id or (not text and not attachments):return False
        if not isinstance(key,TurnKey) or key.attempt_generation!=self._attempt_generation:return False
        with self._lock:
            if self._active is not None:return False
            self._active=_Active(key)
            self._early=[]
            self._assistant=[]
        content=[{'type':'text','text':text}] if text else []
        for a in attachments:
            content.append({'type':'image','url':f'data:{a.mime_type};base64,'+base64.b64encode(a.data).decode('ascii')})
        def begin():
            try:
                if self._stm:self._stm.record_user(text or '[이미지 첨부]')
                if self._state:self._state.event('submit')
                result=self._rpc('turn/start',{'threadId':self._thread_id,'input':content},timeout=30)
                tid=result.get('turn',{}).get('id')
                if not isinstance(tid,str) or not tid:raise RuntimeError('turn_start_invalid')
                with self._lock:
                    active=self._active
                    if active is None or active.key!=key:return
                    if active.turn_id is not None and active.turn_id!=tid:raise RuntimeError('turn_id_mismatch')
                    active.turn_id=tid
                    early,self._early=self._early,[]
                for event in early:self._notification(event)
            except Exception:self._unknown(key)
        threading.Thread(target=begin,daemon=True,name='bubble-codex-send').start()
        return True

    def interrupt(self,key):
        with self._lock:
            active=self._active
            if active is None or active.key!=key or active.unknown:return False
        def request():
            try:
                # turn/started can precede the start RPC response. Until either
                # has supplied the exact turn ID, no cancellation is guessed.
                if not active.turn_id:raise RuntimeError('turn_id_unavailable')
                self._rpc('turn/interrupt',{'threadId':self._thread_id,'turnId':active.turn_id},timeout=10)
                self._safe({'kind':'interrupt_ack'},key)
            except Exception:self._safe({'kind':'interrupt_failed'},key)
        threading.Thread(target=request,daemon=True,name='bubble-codex-interrupt').start()
        return True

    def _write(self,message):
        raw=(json.dumps(message,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8')
        if len(raw)>32*1024*1024:raise ValueError('message_too_large')
        with self._write_lock:
            if self._proc is None:raise RuntimeError('connection_closed')
            self._proc.stdin.write(raw);self._proc.stdin.flush()

    def _rpc(self,method,params,timeout=10):
        future=Future()
        with self._lock:
            self._seq+=1;identifier=self._seq;self._pending[identifier]=future
        try:
            self._write({'id':identifier,'method':method,'params':params})
            result=future.result(timeout)
            if not isinstance(result,dict):raise RuntimeError('invalid_rpc_response')
            return result
        finally:
            with self._lock:self._pending.pop(identifier,None)

    def _reader(self,proc):
        try:
            while not self._stopping.is_set():
                raw=proc.stdout.readline(4*1024*1024+1)
                if not raw:break
                if len(raw)>4*1024*1024 or not raw.endswith(b'\n'):break
                message=json.loads(raw)
                if not isinstance(message,dict):break
                if 'id' in message and 'method' not in message:
                    with self._lock:future=self._pending.get(message['id'])
                    if future and not future.done():
                        if 'error' in message:future.set_exception(RuntimeError('codex_rpc_failed'))
                        else:future.set_result(message.get('result'))
                elif 'id' in message:self._approval(message)
                else:self._notification(message)
        except (OSError,ValueError):pass
        finally:
            self._alive.clear()
            with self._lock:
                for f in self._pending.values():
                    if not f.done():f.set_exception(RuntimeError('codex_connection_closed'))
                key=self._active.key if self._active else None
            if key and not self._stopping.is_set():self._unknown(key)

    def _approval(self,message):
        method=message.get('method');params=message.get('params') or {}
        with self._lock:active=self._active
        valid=active is not None and params.get('threadId')==self._thread_id and params.get('turnId')==active.turn_id
        def reply():
            decision='decline'
            if valid and self._approval_cb and method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
                from .approval import ApprovalRequest
                request=ApprovalRequest(uuid.uuid4().hex,'Codex command' if 'commandExecution' in method else 'Codex file change',params,Future())
                try:
                    self._approval_cb(request)
                    result=request.future.result(60)
                    with self._lock:still_active=self._active is active and not active.unknown
                    if still_active and getattr(result,'behavior',None)=='allow':decision='accept'
                except Exception:pass
            try:
                if method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
                    self._write({'id':message['id'],'result':{'decision':decision}})
                elif method=='mcpServer/elicitation/request':
                    self._write({'id':message['id'],'result':{'action':'decline','content':None,'_meta':None}})
                else:self._write({'id':message['id'],'error':{'code':-32601,'message':'Unsupported approval request'}})
            except (OSError,ValueError,RuntimeError):pass
        threading.Thread(target=reply,daemon=True,name='bubble-codex-approval').start()

    def _notification(self,message):
        method=message.get('method');params=message.get('params') or {}
        with self._lock:
            active=self._active
            if active is None or params.get('threadId')!=self._thread_id:return
            tid=params.get('turnId') or (params.get('turn') or {}).get('id')
            if method=='turn/started' and not active.turn_id:active.turn_id=tid
            if not active.turn_id:
                if len(self._early)>=256:self._unknown(active.key)
                else:self._early.append(message)
                return
            if tid!=active.turn_id:return
            key=active.key
            if method=='turn/completed':
                status=(params.get('turn') or {}).get('status')
                if status not in ('completed','interrupted','failed'):
                    self._unknown(key);return
                self._active=None
                if self._stm and self._assistant:
                    try:self._stm.record_assistant(''.join(self._assistant))
                    except Exception:pass
                if self._state:self._state.event('turn_end',is_error=status=='failed')
                self._safe({'kind':'turn_end','terminal':True,'is_error':status=='failed','provider_status':status},key)
            elif method=='item/agentMessage/delta':
                text=params.get('delta','')
                if isinstance(text,str):
                    self._assistant.append(text)
                    self._safe({'kind':'speech','text':text,'delta':True,'id':params.get('itemId')},key)
            elif method in ('item/reasoning/summaryTextDelta','item/reasoning/textDelta'):
                self._safe({'kind':'thought','text':params.get('delta',''),'delta':True,'id':params.get('itemId')},key)
            elif method=='item/started':
                item=params.get('item') or {}
                if item.get('type') in ('commandExecution','fileChange','mcpToolCall'):
                    self._safe({'kind':'tool_use','tool_name':item['type']},key)

    def _unknown(self,key):
        with self._lock:
            if not self._active or self._active.key!=key:return
            self._active.unknown=True
        self._safe({'kind':'provider_unknown','text':'Codex 요청의 종료 상태를 확인할 수 없습니다.'},key)

    def _safe(self,event,key):
        if self._stopping.is_set():return
        try:self._emit_cb({**event,'request_key':asdict(key)})
        except Exception:pass
