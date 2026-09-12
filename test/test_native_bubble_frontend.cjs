const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
function fixture(){
 let now=0,id=0,hover=false;const timers=new Map(),actions=[],classes=new Set();
 const context=vm.createContext({performance:{now:()=>now},setTimeout:(fn,ms)=>{timers.set(++id,{fn,at:now+ms});return id;},clearTimeout:i=>timers.delete(i),
  crypto:{randomUUID:()=>String(++id)},document:{body:{matches:()=>hover,classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)}},getElementById:()=>null},
  window:{__TAURI__:{core:{invoke:(name,p)=>{actions.push(p);return Promise.resolve();}},event:{listen:()=>{}},window:{getCurrentWindow:()=>({label:'speech'})}}}});
 const source=fs.readFileSync(path.join(__dirname,'../native-bubble-shell/frontend/app.js'),'utf8');
 vm.runInContext(source.slice(0,source.lastIndexOf('init().catch')),context);
 vm.runInContext('render=()=>{};resize=()=>{};',context);
 return {run:s=>vm.runInContext(s,context),actions,classes,timers,hover:v=>hover=v,advance(ms){const end=now+ms;for(;;){const next=[...timers].filter(([,v])=>v.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!next)break;now=next[1].at;timers.delete(next[0]);next[1].fn();}now=end;}};
}
test('new presentation cancels final fade and rejects a queued stale callback',()=>{
 const f=fixture();f.run('setPresentationFade({presentation_revision:1,fade_ms:10})');f.advance(10);
 const stale=[...f.timers.values()][0].fn;
 f.run('setPresentationFade({presentation_revision:2})');stale();f.advance(1000);
 assert.equal(f.actions.length,0);assert.equal(f.classes.has('fading'),false);
});
test('hover pauses dwell and final fade without resetting remaining time',()=>{
 const f=fixture();f.run('setPresentationFade({presentation_revision:1,fade_ms:100})');f.advance(40);f.run('pausePresentationFade()');f.advance(1000);
 assert.equal(f.actions.length,0);f.run('resumePresentationFade()');f.advance(60);assert.ok(f.classes.has('fading'));
 f.advance(100);f.run('pausePresentationFade()');assert.equal(f.classes.has('fading'),false);f.advance(1000);f.run('resumePresentationFade()');f.advance(399);assert.equal(f.actions.length,0);f.advance(1);assert.equal(f.actions.length,1);
});
test('presentation arriving while hovered waits until leave',()=>{
 const f=fixture();f.hover(true);f.run('setPresentationFade({presentation_revision:1,fade_ms:20})');f.advance(2000);assert.equal(f.actions.length,0);
 f.hover(false);f.run('resumePresentationFade()');f.advance(520);assert.equal(f.actions.length,1);
});
test('recent pending-to-sent starts expiry; off and pending never expire',()=>{
 const f=fixture();f.run("setRecentFade({request_id:'a',state:'active'})");f.run('pauseRecentFade();resumeRecentFade()');f.advance(9000);assert.equal(f.run('recentFade.hidden'),false);
 f.run("setRecentFade({request_id:'a',state:'sent',fade_ms:8000})");f.advance(7999);assert.equal(f.run('recentFade.hidden'),false);f.advance(1);assert.equal(f.run('recentFade.hidden'),true);
 f.run("setRecentFade({request_id:'b',state:'sent',fade_ms:null});resumeRecentFade()");f.advance(20000);assert.equal(f.run('recentFade.hidden'),false);
});
test('old recent expiry cannot hide a newly submitted card',()=>{
 const f=fixture();f.run("setRecentFade({request_id:'a',state:'sent',fade_ms:8})");const stale=[...f.timers.values()][0].fn;
 f.run("setRecentFade({request_id:'b',state:'waiting'})");stale();assert.equal(f.run('recentFade.hidden'),false);
});
test('approval clearing restarts same-revision presentation fade',()=>{
 const f=fixture();
 f.run(`document.body.classList.toggle=()=>{};
 window.AmberText={render:()=>{}};
 document.createElement=()=>({append(){},className:'',onclick:null});
 document.getElementById=id=>id==='content'?{after(){}}:null;
 state={speech:{text:'synthetic',presentation_revision:1,fade_ms:20},approval:{id:'approval',summary:'synthetic'}};
 renderPresentation();`);
 f.advance(1000);assert.equal(f.actions.length,0);
 f.run('state.approval=null;renderPresentation()');f.advance(520);
 assert.equal(f.actions.length,1);
});
