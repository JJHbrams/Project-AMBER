/* Private native UI: no network, no filesystem, no public renderer content. */
'use strict';
const bridge=window.__TAURI__, invoke=bridge.core.invoke, listen=bridge.event.listen;
const label=bridge.window.getCurrentWindow().label, $=id=>document.getElementById(id);
const icons={edit:'<path d="m3 17 1-4L14 3l4 4L8 17zm12-15 2-2 4 4-2 2z"/>',delete:'<path d="M6 3h4V1h4v2h4v2H4V3zm0 4h12l-1 13H7zm3 2v8h2V9zm4 0v8h2V9z"/>',send_now:'<path d="M12 0 3 12h7l-1 10 11-14h-8l3-8z"/>'};
let state={},composing=false,flipPending=null,queueOpen=false,attachments=[],pending=null,editing=null,draftBackup=null,selected=null,readers=0;
let rememberedSelection=[0,0], activityTimer=null,presentationKey=null;
let lastComposerState=null,lastComposerVisibilityRevision=null;
let historyOpen=false,selectedSpeechHistory=null;
let presentationFade={dwell:null,final:null,generation:0,deadline:0,remaining:0,phase:null,revision:null};
let recentFade={timer:null,generation:0,deadline:0,remaining:0,key:null,hidden:false};
const action=(name,payload={},requestId=null)=>invoke('shell_action',{action:name,actionId:crypto.randomUUID(),requestId,payload}).catch(()=>notice('연결을 확인하고 있습니다. 입력은 유지됩니다.'));
function composerState(){if(label!=='input')return;const payload={draft_nonempty:!!$('draft')?.value.trim(),attachments:attachments.length,composing,readers,pending:!!pending,editing:!!editing,queue_open:queueOpen,hovered:document.body.matches(':hover')},serialized=JSON.stringify(payload);if(serialized===lastComposerState)return;lastComposerState=serialized;action('composer_state',payload);}
function notice(text){if($('notice'))$('notice').textContent=text;}
function button(name,tip,requestId,payload={}){const b=document.createElement('button');b.setAttribute('aria-label',tip);b.dataset.tip=tip;b.innerHTML=icons[name]?`<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name]}</svg>`:name==='interrupt'?'■':'×';if(name==='send_now')b.className='bolt';b.onclick=()=>{b.disabled=true;action(name,payload,requestId);};return b;}
function flip(on){if(composing){flipPending=on;return;}queueOpen=on;if(!on&&$('draft'))requestAnimationFrame(()=>{$('draft').focus();$('draft').setSelectionRange(...rememberedSelection);});else if($('draft'))rememberedSelection=[$('draft').selectionStart,$('draft').selectionEnd];$('rotor').classList.toggle('flipped',on);$('inputFace').inert=on;$('queueFace').inert=!on;$('inputFace').setAttribute('aria-hidden',String(on));$('queueFace').setAttribute('aria-hidden',String(!on));resize();composerState();if(on)setTimeout(()=>$('queue').focus(),250);}
function previews(){const nodes=attachments.map((url,i)=>{const wrap=document.createElement('div');wrap.className='preview';const img=document.createElement('img');img.src=url;img.alt=`첨부 이미지 ${i+1}`;const b=button('remove','첨부 이미지 제거');b.disabled=!!pending;b.onclick=()=>{attachments.splice(i,1);previews();resize();};wrap.append(img,b);return wrap;});$('previews').replaceChildren(...nodes);composerState();}
function resize(){if(label!=='input')return;const d=$('draft');d.style.minHeight='0';d.style.height='0px';const editorHeight=Math.min(150,Math.max(52,d.scrollHeight));d.style.minHeight='52px';d.style.height=editorHeight+'px';const compact=185+editorHeight-52+(attachments.length?64:0);action('resize_input',{height:Math.min(430,compact)});}
function submit(){if(composing||pending||readers||(!$('draft').value.trim()&&!attachments.length))return;const id=crypto.randomUUID();pending={id,text:$('draft').value,attachments:[...attachments]};$('draft').disabled=true;$('send').disabled=true;composerState();action(editing?'save_edit':'submit',{text:pending.text,attachments:pending.attachments,edit_request_id:editing},id);}
function clearPresentationFade(){
 const f=presentationFade;f.generation++;clearTimeout(f.dwell);clearTimeout(f.final);
 f.dwell=f.final=null;f.phase=null;document.body.classList.remove('fading');
}
function runPresentationFade(){
 const f=presentationFade,generation=f.generation,revision=f.revision;
 if(f.phase==='dwell')f.dwell=setTimeout(()=>{
  if(generation!==f.generation)return;f.dwell=null;f.phase='final';f.remaining=500;
  f.deadline=performance.now()+500;runPresentationFade();
 },Math.max(0,f.remaining));
 else if(f.phase==='final'){
  document.body.classList.add('fading');
  f.final=setTimeout(()=>{if(generation!==f.generation)return;f.final=null;f.phase=null;
   action('dismiss',{window:label,presentation_revision:revision});
  },Math.max(0,f.remaining));
 }
}
function setPresentationFade(p){
 clearPresentationFade();const f=presentationFade;f.revision=p.presentation_revision;
 f.remaining=Math.max(0,Number(p.fade_ms)||0);
 if(p.fade_ms==null||state.approval)return;
 f.phase='dwell';f.deadline=performance.now()+f.remaining;
 if(!document.body.matches(':hover'))runPresentationFade();
}
function pausePresentationFade(){
 const f=presentationFade;if(!f.phase)return;
 if(f.dwell!==null||f.final!==null)f.remaining=Math.max(0,f.deadline-performance.now());
 f.generation++;clearTimeout(f.dwell);clearTimeout(f.final);f.dwell=f.final=null;
 document.body.classList.remove('fading');
}
function resumePresentationFade(){
 const f=presentationFade;if(f.phase&&f.dwell===null&&f.final===null){
  f.deadline=performance.now()+f.remaining;runPresentationFade();
 }
}
function clearRecentFade(){clearTimeout(recentFade.timer);recentFade.timer=null;recentFade.generation++;}
function runRecentFade(){
 const f=recentFade,generation=f.generation;f.deadline=performance.now()+f.remaining;
 f.timer=setTimeout(()=>{if(generation!==f.generation)return;f.timer=null;f.hidden=true;render();resize();},f.remaining);
}
function setRecentFade(recent){
 const f=recentFade,key=recent&&`${recent.request_id}:${recent.state}:${recent.fade_ms}`;
 if(key===f.key)return;clearRecentFade();f.key=key;f.hidden=false;
 f.eligible=!!recent&&['sent','failed','interrupted'].includes(recent.state)&&recent.fade_ms!=null;
 f.remaining=Math.max(0,Number(recent&&recent.fade_ms)||0);
 if(f.eligible&&!document.body.matches(':hover'))runRecentFade();
}
function pauseRecentFade(){
 const f=recentFade;if(f.timer===null)return;
 f.remaining=Math.max(0,f.deadline-performance.now());clearRecentFade();
}
function resumeRecentFade(){
 const f=recentFade;if(f.eligible&&!f.hidden&&f.timer===null)runRecentFade();
}
let appliedStyleKey=null,measurePending=false,lastMeasurement=null;
function applyPresentationStyle(){
 const style=state.presentation_style?.[label];if(!style)return;
 const key=JSON.stringify(style);if(key===appliedStyleKey)return;
 appliedStyleKey=key;lastMeasurement=null;
 document.body.style.fontFamily=JSON.stringify(style.font_family)+', sans-serif';
 document.body.style.fontSize=style.font_size+'px';
 if(label==='input')requestAnimationFrame(resize);
}
function schedulePresentationMeasure(){
 if(label==='input'||historyOpen||measurePending)return;
 measurePending=true;
 requestAnimationFrame(()=>{
  measurePending=false;
  const limits=state.presentation_style?.[label],p=state[label]||{};
  if(!limits||limits.manual_size||(!p.text&&!state.approval))return;
  const face=document.querySelector('#bubble .face.front, #bubble>.face');if(!face)return;
  const clone=face.cloneNode(true);
  clone.removeAttribute('id');clone.querySelectorAll('[id]').forEach(e=>e.removeAttribute('id'));
  Object.assign(clone.style,{position:'fixed',left:'-10000px',top:'0',right:'auto',bottom:'auto',visibility:'hidden',pointerEvents:'none',height:'auto',minHeight:'0',maxHeight:'none',width:'max-content',maxWidth:(limits.max_width-24)+'px',transform:'none',transition:'none'});
  const content=clone.querySelector('.content');
  Object.assign(content.style,{flex:'none',height:'auto',maxHeight:'none',minHeight:'0',overflow:'visible',maxWidth:'100%',overflowWrap:'anywhere'});
  document.body.append(clone);
  const width=Math.min(limits.max_width,Math.max(limits.min_width,Math.ceil(clone.getBoundingClientRect().width+24)));
  clone.style.width=(width-24)+'px';
  const height=Math.min(limits.max_height,Math.max(limits.min_height,Math.ceil(clone.getBoundingClientRect().height+44)));
  clone.remove();
  const payload={window:label,width,height,presentation_revision:p.presentation_revision};
  const key=JSON.stringify([appliedStyleKey,payload]);
  if(key!==lastMeasurement){lastMeasurement=key;action('presentation_size',payload);}
 });
}
function render(){
 applyPresentationStyle();
 if(label!=='input'){renderPresentation();schedulePresentationMeasure();return;}
 if(pending&&(state.accepted_request_id===pending.id||state.rejected_request_id===pending.id)){if(state.accepted_request_id===pending.id){if(editing&&draftBackup){$('draft').value=draftBackup.text;attachments=draftBackup.attachments;editing=null;draftBackup=null;}else{$('draft').value='';attachments=[];}}pending=null;$('draft').disabled=false;$('send').disabled=false;previews();resize();}
 if(state.edit&&state.edit.request_id!==editing){draftBackup={text:$('draft').value,attachments:[...attachments]};editing=state.edit.request_id;$('draft').value=state.edit.text;attachments=[...(state.edit.attachments||[])];previews();flip(false);resize();}
 if(editing&&!state.edit&&!pending){if(draftBackup){$('draft').value=draftBackup.text;attachments=draftBackup.attachments;}editing=null;draftBackup=null;previews();}
 $('cancelEdit').hidden=!editing;$('heading').textContent=editing?'예약 메시지 수정':state.busy?'추가 요청을 입력해주세요…':'무엇을 도와드릴까요?';$('keyHint').textContent=`Shift+Enter 줄바꿈 · Enter ${editing?'저장':state.busy?'예약':'전송'}`;$('send').dataset.tip=editing?'수정 저장':state.busy?'현재 응답 뒤에 예약':'메시지 전송';$('send').setAttribute('aria-label',$('send').dataset.tip);$('version').textContent=state.version?`AMBER ${state.version}`:'';
 notice(state.notice||'');const items=state.queue||[];$('count').textContent=items.filter(i=>i.state==='waiting'||i.state==='editing').length;$('resume').hidden=!state.held;$('resume').disabled=!!state.busy;
 const recent=state.recent;setRecentFade(recent);const showRecent=recent&&!recentFade.hidden;$('recent').hidden=!showRecent;if(showRecent){const summary=document.createElement('span');summary.className='summary';summary.textContent=`♧ ${recent.status_label} · ${recent.summary}`;const nodes=[summary];if(['waiting','editing'].includes(recent.state))nodes.push(button('delete','예약 취소',recent.request_id));if(recent.state==='active')nodes.push(button('interrupt','현재 응답 중단',recent.request_id));if(recent.state==='interrupt_requested'){const s=document.createElement('small');s.textContent='확인 중';nodes.push(s);}$('recent').replaceChildren(...nodes);}
 if(!items.some(i=>i.request_id===selected))selected=items.find(i=>i.state==='waiting')?.request_id||items.at(-1)?.request_id;let index=items.findIndex(i=>i.request_id===selected);const visible=items;$('queue').replaceChildren(...visible.map(item=>{const card=document.createElement('article');card.className='card'+(item.request_id===selected?' selected':'');const p=document.createElement('span');p.className='summary';p.textContent=`♧ ${item.status_label} · ${item.summary}`;card.append(p);for(const url of (item.thumbnails||[])){const img=document.createElement('img');img.className='queue-thumb';img.src=url;img.alt='첨부 이미지';card.append(img);}card.onclick=()=>{selected=item.request_id;render();};if(item.request_id===selected&&item.state==='waiting')for(const [name,tip] of [['edit','메시지 수정'],['delete','예약 취소'],['send_now','현재 응답을 중단하고 즉시 전송']]){const b=button(name,tip,item.request_id);b.disabled=!!item.locked;b.onclick=e=>{e.stopPropagation();b.disabled=true;action(name,{},item.request_id);};card.append(b);}return card;}));$('queue').querySelector('.selected')?.scrollIntoView({block:'nearest'});if(!items.length){const p=document.createElement('p');p.className='queue-empty';p.textContent='보낸 메시지와 대기 중인 요청이 여기에 표시됩니다.';$('queue').append(p);}
}
function flipSpeechHistory(on,preserveSelection=false){
 const changed=historyOpen!==on;historyOpen=on;if(changed)action('speech_history_state',{open:historyOpen});if(!on&&!preserveSelection)selectedSpeechHistory=null;
 $('rotor').classList.toggle('flipped',on);$('speechLive').inert=on;$('speechHistory').inert=!on;
 $('speechLive').setAttribute('aria-hidden',String(on));$('speechHistory').setAttribute('aria-hidden',String(!on));
 if(on||selectedSpeechHistory!==null)pausePresentationFade();else resumePresentationFade();
 if(on)setTimeout(()=>$('speechHistoryList').focus(),250);
 renderPresentation();
}
function renderSpeechHistory(){
 const items=state.speech_history||[],list=$('speechHistoryList');if(!list)return;
 const nodes=[];const latest=document.createElement('button');latest.className='history-card'+(selectedSpeechHistory===null?' selected':'');latest.textContent='최신 응답으로 돌아가기';latest.onclick=()=>{selectedSpeechHistory=null;flipSpeechHistory(false);};nodes.push(latest);
 for(const item of items){const card=document.createElement('button');card.className='history-card'+(item.id===selectedSpeechHistory?' selected':'');card.textContent=item.summary||'응답';card.onclick=()=>{selectedSpeechHistory=item.id;flipSpeechHistory(false,true);};nodes.push(card);}
 if(!items.length){const empty=document.createElement('p');empty.className='queue-empty';empty.textContent='최근 완료 응답이 아직 없습니다.';nodes.push(empty);}list.replaceChildren(...nodes);
}
function renderPresentation(){const live=state[label]||{};const archived=label==='speech'&&selectedSpeechHistory!==null?(state.speech_history||[]).find(item=>item.id===selectedSpeechHistory):null;const p=archived?{...live,text:archived.text,nudge:false,fade_ms:null}:live;window.AmberText.render($('content'),p.text||'');document.body.classList.toggle('nudge',label==='speech'&&!!p.nudge);if(label==='speech')renderSpeechHistory();const key=JSON.stringify([live.presentation_revision,live.fade_ms,state.approval?.id]);if(!historyOpen&&selectedSpeechHistory===null&&key!==presentationKey){presentationKey=key;setPresentationFade(live);}const approval=archived?null:state.approval;if(approval)clearPresentationFade();let old=$('approval');if(old)old.remove();if(label==='speech'&&p.nudge&&!approval){const choices=document.createElement('div');choices.id='approval';choices.className='choices';for(const [actionName,text] of [['nudge_reply','답장'],['nudge_defer','나중에']]){const b=document.createElement('button');b.textContent=text;b.onclick=()=>{b.disabled=true;action(actionName,{presentation_revision:live.presentation_revision});};choices.append(b);}$('content').after(choices);}else if(label==='speech'&&approval){const box=document.createElement('div');box.id='approval';box.className='approval';const text=document.createElement('p');text.textContent=approval.summary;const choices=document.createElement('div');choices.className='choices';for(const [v,t] of [[false,'거부'],[true,'허용']]){const b=document.createElement('button');b.textContent=t;b.onclick=()=>{b.disabled=true;action('approval',{approval_id:approval.id,allow:v});};choices.append(b);}box.append(text,choices);$('content').after(box);}}
function geometry(rect){if(rect.window!==label||!rect.tail_points)return;const scale=rect.scale||1;const p=rect.tail_points.map(v=>v.map(n=>n/scale));let d=`M ${p[0]} L ${p[1]} L ${p[2]} Z`;if(label==='input'){const l=12,t=12,r=rect.width/scale-12,b=rect.height/scale-32,k=30;const a=p[0][0],z=p[2][0],tx=p[1][0],ty=p[1][1];d=`M ${l+k} ${t} H ${r-k} Q ${r} ${t} ${r} ${t+k} V ${b-k} Q ${r} ${b} ${r-k} ${b} H ${z} Q ${(z+tx)/2} ${b+5} ${tx} ${ty} Q ${(a+tx)/2} ${b+5} ${a} ${b} H ${l+k} Q ${l} ${b} ${l} ${b-k} V ${t+k} Q ${l} ${t} ${l+k} ${t} Z`;$('bubbleBody').setAttribute('display','none');}if(label==='thought'){const base=[(p[0][0]+p[2][0])/2,(p[0][1]+p[2][1])/2];d=[[.35,6],[.85,3]].map(([t,r])=>{const x=base[0]+(p[1][0]-base[0])*t,y=base[1]+(p[1][1]-base[1])*t;return `M ${x-r} ${y} a ${r} ${r} 0 1 0 ${2*r} 0 a ${r} ${r} 0 1 0 ${-2*r} 0`;}).join(' ');}$('tailPath').setAttribute('d',d);}
async function init(){
 document.body.classList.add(label);
 document.fonts.ready.then(()=>{lastMeasurement=null;schedulePresentationMeasure();});
 document.fonts.addEventListener('loadingdone',()=>{lastMeasurement=null;schedulePresentationMeasure();});
 const tooltip=document.createElement('div');tooltip.id='bubble-tooltip';tooltip.setAttribute('role','tooltip');tooltip.hidden=true;document.body.append(tooltip);
 let tipOwner=null;
 const hideTip=()=>{if(tipOwner)tipOwner.removeAttribute('aria-describedby');tipOwner=null;tooltip.hidden=true;};
 const showTip=owner=>{
  hideTip();if(!owner||!owner.dataset.tip)return;
  tipOwner=owner;owner.setAttribute('aria-describedby',tooltip.id);tooltip.textContent=owner.dataset.tip;tooltip.hidden=false;
  const r=owner.getBoundingClientRect(),t=tooltip.getBoundingClientRect();
  const x=Math.max(40,Math.min(r.right-t.width,innerWidth-40-t.width));
  const preferred=r.top-t.height-7>=30?r.top-t.height-7:r.bottom+7;
  const y=Math.max(30,Math.min(preferred,innerHeight-48-t.height));
  tooltip.style.left=x+'px';tooltip.style.top=y+'px';
 };
 document.addEventListener('pointerover',e=>showTip(e.target.closest('[data-tip]')));
 document.addEventListener('pointerout',e=>{if(tipOwner&&e.relatedTarget instanceof Node&&tipOwner.contains(e.relatedTarget))return;hideTip();});
 document.addEventListener('focusin',e=>showTip(e.target.closest('[data-tip]')));
 document.addEventListener('focusout',hideTip);
 document.addEventListener('pointerdown',hideTip);
 document.addEventListener('keydown',e=>{if(e.key==='Escape')hideTip();});
 window.addEventListener('resize',hideTip);
 const resizeHandle=document.createElement('button');resizeHandle.className='resize-handle';resizeHandle.setAttribute('aria-label','말풍선 크기 조절');resizeHandle.dataset.tip='모서리를 끌어 말풍선 크기 조절';resizeHandle.textContent='↘';resizeHandle.onpointerdown=e=>{e.preventDefault();invoke('shell_resize').catch(()=>{});};document.body.append(resizeHandle);
 if(label!=='input'){if(label==='speech')$('bubble').innerHTML=`<div id="rotor"><section id="speechLive" class="face front"><header class="drag"><strong>AMBER</strong><span><button id="speechFlip" aria-label="최근 응답 보기" data-tip="최근 응답 보기">⟳</button><button id="dismiss" aria-label="말풍선 닫기" data-tip="말풍선 닫기">×</button></span></header><div id="content" class="content"></div></section><section id="speechHistory" class="face back" inert aria-hidden="true"><header class="drag"><strong>최근 응답</strong><button id="speechBack" aria-label="최신 응답으로 돌아가기" data-tip="최신 응답으로 돌아가기">⟳</button></header><div id="speechHistoryList" class="speech-history" tabindex="0"></div></section></div>`;else $('bubble').innerHTML=`<section class="face"><header class="drag"><strong>생각 중…</strong><button id="dismiss" aria-label="말풍선 닫기" data-tip="말풍선 닫기">×</button></header><div id="content" class="content"></div></section>`;$('utility').hidden=true;$('dismiss').onclick=()=>action('dismiss',{window:label,presentation_revision:presentationFade.revision});if(label==='speech'){$('speechFlip').onclick=()=>flipSpeechHistory(true);$('speechBack').onclick=()=>flipSpeechHistory(false);}}
 else{$('flip').onclick=()=>flip(true);$('back').onclick=()=>flip(false);$('send').onclick=submit;$('resume').onclick=()=>action('resume_queue');$('history').onclick=()=>action('history');$('close').onclick=()=>action('close');$('cancelEdit').onclick=()=>action('cancel_edit',{},editing);$('draft').oncompositionstart=()=>composing=true;$('draft').oncompositionend=()=>{composing=false;if(flipPending!==null){const on=flipPending;flipPending=null;flip(on);}};$('draft').onkeydown=e=>{clearTimeout(activityTimer);action('input_activity',{active:true});activityTimer=setTimeout(()=>action('input_activity',{active:false}),1000);if(e.key==='Enter'&&!composing&&!e.isComposing&&e.keyCode!==229&&!e.shiftKey){e.preventDefault();submit();}};$('draft').oninput=()=>{resize();clearTimeout(activityTimer);action('input_activity',{active:true});activityTimer=setTimeout(()=>action('input_activity',{active:false}),1000);};$('draft').addEventListener('paste',async e=>{const files=[...e.clipboardData.files].filter(f=>/^image\/(png|jpeg)$/.test(f.type));if(!files.length)return;e.preventDefault();if(pending)return;for(const file of files){if(attachments.length+readers>=4||file.size>5*1024*1024){notice('이미지는 최대 4개, 개별 5 MiB까지 첨부할 수 있습니다.');break;}readers++;composerState();try{const url=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=reject;r.readAsDataURL(file);});attachments.push(url);}catch{notice('이미지를 읽지 못했습니다.');}finally{readers--;}previews();resize();composerState();}});$('queue').onwheel=e=>{e.preventDefault();const items=state.queue||[];const n=items.findIndex(i=>i.request_id===selected);selected=items[Math.max(0,Math.min(items.length-1,n+(e.deltaY>0?1:-1)))]?.request_id;render();};}
 document.addEventListener('pointerdown',e=>{if(e.button!==0||e.target.closest('button,textarea,input,select,a,.editor,.recent,.card,#queue,.content,.previews,#utility'))return;invoke('shell_drag').catch(()=>{});});
 document.body.onmouseenter=()=>{pausePresentationFade();pauseRecentFade();action('hover',{window:label,active:true});composerState();};document.body.onmouseleave=()=>{resumePresentationFade();resumeRecentFade();action('hover',{window:label,active:false});composerState();};
 await listen('host-message',e=>{const m=e.payload;if(m.type==='snapshot'){state=m.state||{};render();if(label==='input'&&state.composer_visibility_revision!==lastComposerVisibilityRevision){lastComposerVisibilityRevision=state.composer_visibility_revision;lastComposerState=null;composerState();}}else if(m.type==='presentation'){state={...state,...m.event};render();}});
 await listen('host-geometry',e=>geometry(e.payload.rect||{}));
 await invoke('shell_ready');render();if(label==='input'){composerState();document.addEventListener('input',composerState);document.addEventListener('compositionstart',composerState);document.addEventListener('compositionend',composerState);}
}
init().catch(()=>{document.body.textContent='말풍선 연결을 시작하지 못했습니다.';});
