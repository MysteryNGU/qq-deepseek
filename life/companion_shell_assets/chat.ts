import {renderMonitor} from './monitor.js';
// Embed Coopanion's actual chat page and UI primitives; its cognition is never started.
import {chatPage} from '../../vendor/coopanion/console/features/chat/index.ts';
import {createConsoleUi} from '../../vendor/coopanion/console/base/ui/index.ts';
import {Lifecycle} from '../../vendor/coopanion/console/base/lifecycle.ts';
import {S,STEP} from '../../vendor/coopanion/console/features/chat/strings.ts';
Object.assign(STEP,{companion_execute:'做点事情'});
Object.assign(S,{idle:'在这里',thinking:(name:string)=>`${name}正在想…`,doing:()=> '正在忙一件事…',placeholder:(name:string)=>`想和${name}说什么…`});
function showNotice(value:unknown){
  const detail=String(value||'');
  const node=document.querySelector('#notice');
  if(node)node.textContent=/DecisionStateError|本轮结果未能保存/.test(detail)?'这次回复没有完成，可以稍后再试。':detail;
  const diagnostic=document.querySelector('#notice-detail');if(diagnostic)diagnostic.textContent=detail;
}
const error=(e:unknown)=>showNotice(e);
const lifecycle=new Lifecycle(error);
const ui=createConsoleUi({signal:lifecycle.signal,overlayHost:document.body,memo:{get:(k,f)=>{try{return JSON.parse(localStorage.getItem('xiaoyu.'+k)||'null')??f}catch{return f}},set:(k,v)=>localStorage.setItem('xiaoyu.'+k,JSON.stringify(v))}});
const root=document.querySelector('#chat') as HTMLElement;
await chatPage.mount({root,ui,lifecycle,signal:lifecycle.signal,router:{navigate:()=>show('life'),addLeaveGuard:()=>({dispose(){}})},capabilities:{},onError:error,mountPanel:async()=>({dispose(){}})});
let page='chat';
let nearbyOpen=false;
try{nearbyOpen=localStorage.getItem('xiaoyu.nearby')==='true'}catch{}
const narrowWindow=matchMedia('(max-width:980px)');
const nearbyButton=document.createElement('button');
nearbyButton.className='nearby-toggle';nearbyButton.textContent='她的近况';nearbyButton.setAttribute('aria-controls','nearby');
root.querySelector('.featureintro')!.append(nearbyButton);
root.querySelector('.featureintro')!.append(document.querySelector('#notice')!);
function syncNearby(){
  const visible=page==='chat'&&nearbyOpen&&!narrowWindow.matches;
  (document.querySelector('#nearby') as HTMLElement).hidden=!visible;
  document.querySelector('#main')!.classList.toggle('with-nearby',visible);
  nearbyButton.hidden=visible;nearbyButton.setAttribute('aria-expanded',String(visible));
  (document.querySelector('#nearby-setting') as HTMLInputElement).checked=nearbyOpen;
}
function setNearby(open:boolean){nearbyOpen=open;try{localStorage.setItem('xiaoyu.nearby',String(open))}catch{}syncNearby()}
nearbyButton.onclick=()=>narrowWindow.matches?show('life'):setNearby(true);
narrowWindow.addEventListener('change',syncNearby);
document.querySelector('#close-nearby')!.addEventListener('click',()=>setNearby(false));
document.querySelector('#nearby-setting')!.addEventListener('change',e=>setNearby((e.target as HTMLInputElement).checked));
syncNearby();
function show(id:string){
  page=id;document.querySelectorAll('.page-stack>section').forEach(e=>(e as HTMLElement).hidden=e.id!==id);
  document.querySelectorAll('nav button[data-page]').forEach(e=>e.classList.toggle('active',(e as HTMLElement).dataset.page===id));
  syncNearby();
}
document.querySelectorAll('nav button[data-page]').forEach(e=>e.addEventListener('click',()=>show((e as HTMLElement).dataset.page!)));
const control=async(op:string,args:unknown={})=>fetch('/api/shell/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({op,args})});
document.querySelector('#autonomy')!.addEventListener('change',e=>void control('active',{active:(e.target as HTMLInputElement).checked}));
document.querySelector('#dress')!.addEventListener('click',()=>void control('dress'));
document.querySelector('#connections')!.addEventListener('click',()=>void control('connections'));
document.querySelector('#exit')!.addEventListener('click',()=>void control('exit'));
document.querySelector('#models')!.addEventListener('change',e=>void control('model',{path:(e.target as HTMLSelectElement).value}));

const esc=(text:unknown)=>String(text??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c] as string));
const clean=(value:unknown)=>typeof value==='string'?value.trim():'';
const unique=(items:string[])=>[...new Set(items.filter(Boolean))];
const date=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'long',day:'numeric',weekday:'long'});
document.querySelectorAll('.life-date').forEach(e=>e.textContent=date.format(new Date()));

// Read saved life material without another model call or exposing tool logs.
let lifeVisible=12;
let lastLifeState:any;
const prose=(item:any)=>clean(item?.display_text)||clean(item?.reflection)||clean(item?.text);
const readable=(item:any)=>!item?.technical&&!!prose(item)&&!/^\s*[\[{]|Traceback|\b(?:action_id|source_ref|workspace_id)\b/.test(prose(item));
function renderLife(s:any){
  lastLifeState=s;
  const life=s.life||{};
  const phase=s.phase?.state;
  const here=clean(life.current_text)|| (phase==='model'?'正在想事情，等会儿再慢慢说。':phase==='tools'?'手上有件事正在做。':s.active?'这会儿安静待着。':'这会儿先歇一歇，还是可以和你说话。');
  const candidates=Array.isArray(life.interest_preview)?life.interest_preview:(s.projects||[]).filter((p:any)=>p.origin==='self'&&['active','waiting'].includes(p.status)&&!p.ephemeral);
  const interests=unique(candidates.map((p:any)=>clean(p.question)||clean(p.next_idea)||clean(p.goal))).slice(0,3);
  const today=(life.today_preview||[]).filter(readable);
  const notes=unique((s.notes||[]).filter((n:any)=>n.kind==='self').map((n:any)=>clean(n.text))).slice(0,8);
  const memory=prose(life.recollection),letter=prose(life.latest_letter);
  const changes=(life.ideas_changes||[]).map((i:any)=>[clean(i.before)&&'之前：'+clean(i.before),clean(i.after)&&'现在：'+clean(i.after),clean(i.reason)].filter(Boolean).join('\n')).filter(Boolean).slice(0,6);
  const paragraph=(text:string)=>`<p>${esc(text)}</p>`;
  const section=(title:string,content:string)=>content?`<section class="life-block"><h2>${esc(title)}</h2>${content}</section>`:'';
  const timeline=(items:any[])=>items.map(i=>`<article class="life-entry"><time>${esc(i.time_label||i.age||'')}</time><p>${esc(prose(i))}</p></article>`).join('');
  const past=(life.past_preview||[]).filter(readable);
  let previousDay='';
  const pastHtml=past.slice(0,Math.max(0,lifeVisible-today.length)).map((i:any)=>{
    const day=clean(i.day_label)||'往日';const heading=day===previousDay?'':`<h3 class="life-day">${esc(day)}</h3>`;previousDay=day;
    return heading+timeline([i]);
  }).join('');
  const body=section('这会儿',paragraph(here))+
    section('今天到现在',clean(life.absence_summary)?paragraph(clean(life.absence_summary)):'')+
    section('今天的几件小事',today.length?timeline(today.slice(0,lifeVisible)):paragraph('今天还没有留下新的经历。'))+
    section('最近惦记',interests.map(paragraph).join(''))+
    section('慢慢变了的想法',changes.map(paragraph).join(''))+
    section('最近记下的',notes.map(paragraph).join(''))+
    section('想起一件事',memory?paragraph(memory):'')+
    section('写给你',letter?paragraph(letter):'')+
    section('往日的生活',pastHtml)+
    (lifeVisible<today.length+past.length?'<button class="life-more" id="life-more">展开已保存近况</button>':'')+
    (life.past_has_more&&lifeVisible>=today.length+past.length?'<p class="life-footnote">这里显示了最近保存的近况，更早的经历仍留在记忆里。</p>':'');
  const host=document.querySelector('#life-body') as HTMLElement;
  if(host.dataset.signature!==body){host.dataset.signature=body;host.innerHTML=body;host.querySelector('#life-more')?.addEventListener('click',()=>{lifeVisible+=12;renderLife(lastLifeState)})}
  const nearby=document.querySelector('#nearby-body') as HTMLElement;
  const preview=section('这会儿',paragraph(here))+section('最近的小事',timeline(today.slice(0,2)))+section('最近惦记',interests.slice(0,1).map(paragraph).join(''));
  if(nearby.dataset.signature!==preview){nearby.dataset.signature=preview;nearby.innerHTML=preview}
}

async function refresh(){try{const s=await (await fetch('/api/shell/state')).json();
  showNotice(s.notice);
  const name=document.querySelector('#name');if(name)name.textContent=s.name||'小鱼';
  const autonomy=document.querySelector('#autonomy') as HTMLInputElement|null;if(autonomy)autonomy.checked=!!s.active;
  const log=document.querySelector('#activity-log');
  if(log)log.textContent=(s.activity||[]).map((e:any)=>[e.created||'',e.text].filter(Boolean).join(' ')).join('\n');
  const monitor=document.querySelector('#monitor');if(monitor)renderMonitor(monitor,s.observability);
  const legacy=document.querySelector('#legacy-state');
  if(legacy)legacy.textContent=['正在在意的事',...(s.projects||[]).map((p:any)=>`${p.goal} · ${p.status}\n${p.next_step||''}`),'\n自己的笔记',...(s.notes||[]).slice(0,12).map((n:any)=>n.text)].join('\n');
  renderLife(s);
  const picker=document.querySelector('#models') as HTMLSelectElement|null;
  if(picker){const options=[{path:'whale',name:'大肥鱼 · 会行走的 Q 版'},...(s.models||[])];const key=JSON.stringify(options);
    if(picker.dataset.key!==key){picker.replaceChildren(...options.map((m:any)=>{const o=document.createElement('option');o.value=m.path;o.textContent=m.name;return o}));picker.dataset.key=key}
    picker.value=s.model||'whale';}
}catch(e){error(e)}}
setInterval(refresh,1500);void refresh();
