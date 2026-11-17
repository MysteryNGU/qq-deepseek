/** AGPL Coopanion World embedded as a body. No Cortico Bot, model or memory is started. */
import {DesktopPetWorld} from '../../vendor/coopanion/packages/cortico-world-desktop-pet/src/world.ts';
import {DESKTOP_PET_DEFAULTS} from '../../vendor/coopanion/packages/cortico-world-desktop-pet/src/config.ts';
import {estimateSeconds,parseScript} from '../../vendor/coopanion/packages/cortico-world-desktop-pet/src/script.ts';
import {createBubbleQueue} from './bubble-queue.js';
import {desktopTarget,createMessageMirror} from './message-route.js';
import {applyMonitorPhase} from './monitor-state.js';
import {chatItem} from '../../vendor/coopanion/packages/cortico-world-desktop-pet/src/chat.ts';
import {createBodyBridge} from './body-bridge.js';
import {WebSocketServer} from '../../data/companion-runtime/coopanion-shell/node_modules/ws/wrapper.mjs';
import {createServer} from 'node:http';
import {createInterface} from 'node:readline';
import {mkdirSync,readFileSync,writeFileSync,existsSync} from 'node:fs';
import {resolve,join,extname} from 'node:path';
import {randomUUID,createHash} from 'node:crypto';
const home=resolve(process.argv[2]);const root=resolve(import.meta.dirname,'../..');
const data=join(home,'data/companion');mkdirSync(data,{recursive:true});
const imageFolder=join(data,'images');mkdirSync(imageFolder,{recursive:true});
const imageBlobs=new Map<string,any>();
function storeImages(items:any[]=[]){
 if(!Array.isArray(items)||items.length>8)throw Error('一次最多发送8张图片');
 let total=0;
 return items.map(item=>{
  const bytes=item.bytes?Buffer.from(item.bytes):Buffer.from(String(item.base64||''),'base64');total+=bytes.length;
  if(!['image/png','image/jpeg','image/webp','image/gif'].includes(item.mime)||!bytes.length||bytes.length>8*1024*1024||total>16*1024*1024)throw Error('图片类型或大小不符合要求');
  const handle=createHash('sha256').update(bytes).digest('hex');
  writeFileSync(join(imageFolder,handle),bytes);
  const blob={handle,bytes,mime:item.mime,name:String(item.name||'image').slice(0,120),fallbackText:item.fallbackText||'图片'};
  const {bytes:ignored,...metadata}=blob;imageBlobs.set(handle,metadata);return blob;
 });
}
function readImage(handle:string){const meta=imageBlobs.get(handle);return meta?{...meta,bytes:readFileSync(join(imageFolder,handle))}:null}
const wireImages=(blobs:any[])=>blobs.map(b=>({mime:b.mime,name:b.name,base64:Buffer.from(b.bytes).toString('base64')}));
function historyImages(row:any){
 const loaded=storeImages(row.images||[]).map(({bytes,...b}:any)=>b);
 const handles=new Set(loaded.map((b:any)=>b.handle));
 for(const ref of row.image_refs||[]){if(handles.has(ref.handle)||!/^[a-f0-9]{64}$/.test(ref.handle)||!existsSync(join(imageFolder,ref.handle)))continue;
  const meta={handle:ref.handle,mime:ref.mime,name:ref.name,fallbackText:'图片'};imageBlobs.set(ref.handle,meta);loaded.push(meta);handles.add(ref.handle)}
 return loaded;
}
const emit=(data:unknown)=>process.stdout.write('@companion-pet '+JSON.stringify(data)+'\n');
const log:any={child:()=>log};for(const level of ['debug','info','warn','error'])log[level]=(...args:unknown[])=>process.stderr.write(level+' '+args.map(x=>typeof x==='string'?x:JSON.stringify(x)).join(' ')+'\n');
function merge(into:any,patch:any){for(const[k,v]of Object.entries(patch))if(v&&typeof v==='object'&&!Array.isArray(v))merge(into[k]??={},v);else into[k]=v;}
const cfg=structuredClone(DESKTOP_PET_DEFAULTS);
cfg.enabled=true;cfg.port=0;cfg.asr.enabled=false;cfg.typeKey.enabled=false;cfg.cua.enabled=false;
cfg.skin.figure='whale';cfg.skin.scheme='deepseek-maid';cfg.roam='free';cfg.sound=false;cfg.doubleClickChat=true;cfg.hoverButtons='chat,roam,dress,size,hide';
let savedAppearance=false;
try{const saved=JSON.parse(readFileSync(join(data,'world.json'),'utf8'));merge(cfg,saved);savedAppearance=typeof saved.skin?.figure==='string'}catch{}
// Voice and computer execution are owned by Companion's executors, never by a second agent.
cfg.asr.enabled=false;cfg.cua.enabled=false;
cfg.window.electronFile=resolve(root,'data/companion-runtime/coopanion-shell/node_modules/electron/dist/electron.exe');
let snapshot:any={name:process.env.COMPANION_NAME||'小鱼',active:false,history:[],notes:[],projects:[],usage:{},activity:[]};
let cursor=0;const events:any[]=[];const hosts=new Set<any>();const token=randomUUID();
const broadcast=(m:any)=>{for(const ws of hosts)if(ws.readyState===1)ws.send(JSON.stringify(m))};
const web=resolve(root,'data/companion-runtime/coopanion-shell/web');
const mime:any={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.png':'image/png'};
const http=createServer(async(req,res)=>{try{
 const url=new URL(req.url||'/',origin);if(!/^(127\.0\.0\.1|localhost)(:\d+)?$/.test(req.headers.host||'')){res.writeHead(421).end();return}
 if(req.method==='POST'){
  if(req.headers.origin&&req.headers.origin!==origin){res.writeHead(403).end();return}
  let raw='';for await(const part of req){raw+=part;if(raw.length>65536){res.writeHead(413).end();return}}
  const m=JSON.parse(raw||'{}');if(url.pathname==='/api/shell/control'){await control(m.op,m.args||{});json(res,{ok:true});return}
  res.writeHead(404).end();return;
 }
 if(url.pathname==='/api/shell/state'){json(res,snapshot);return}
 if(url.pathname==='/api/shell/monitor'){json(res,snapshot.observability||null);return}
 if(url.pathname==='/api/blob'){const handle=url.searchParams.get('handle')||'';const blob=readImage(handle);if(!/^[a-f0-9]{64}$/.test(handle)||!blob){res.writeHead(404).end();return}res.writeHead(200,{'Content-Type':blob.mime,'X-Content-Type-Options':'nosniff','Cache-Control':'private, max-age=3600'});res.end(blob.bytes);return}
 if(url.pathname==='/api/config'){json(res,{groups:[]});return}
 if(url.pathname==='/api/avatar'){res.setHeader('Content-Type','image/png');res.end(readFileSync(join(root,'vendor/coopanion/packages/cortico-world-desktop-pet/web/whale/thumbs/deepseek.png')));return}
 const file=({'/':'index.html','/chat.js':'chat.js','/styles.css':'styles.css','/shell.css':'shell.css','/chat.css':'chat.css','/companion.css':'companion.css'} as any)[url.pathname];
 if(file){res.setHeader('Content-Type',mime[extname(file)]);res.end(readFileSync(join(web,file)));return}
 res.writeHead(404).end();
 }catch(e){json(res,{error:String(e)},500)}});
function json(res:any,value:any,status=200){res.writeHead(status,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'});res.end(JSON.stringify(value))}
await new Promise<void>(done=>http.listen(0,'127.0.0.1',done));
const origin=`http://127.0.0.1:${(http.address() as any).port}`;
const wss=new WebSocketServer({noServer:true,maxPayload:24*1024*1024});
http.on('upgrade',(req,socket,head)=>{
 const url=new URL(req.url||'/',origin);
 if(req.headers.origin&&req.headers.origin!==origin){socket.destroy();return}
 if(url.pathname==='/host'&&url.searchParams.get('token')===token){wss.handleUpgrade(req,socket,head,ws=>{hosts.add(ws);ws.on('message',raw=>{try{const m=JSON.parse(raw.toString());if(m.type==='body_state'){bodyBridge.layout(m);void publishBodyContext()}}catch(e){log.warn('身体状态未能读取',String(e))}});ws.on('close',()=>{hosts.delete(ws);setTimeout(()=>{if(!closing&&!hosts.size){log.warn('桌宠窗口已退出，结束同伴服务');emit({type:'closed'})}},300)})});return}
 if(url.pathname==='/ws/providers/world%3Adesktop-pet/panels/chat')wss.handleUpgrade(req,socket,head,ws=>world.webUi('zh').stream('chat',{get open(){return ws.readyState===1},send:text=>ws.send(text),close:()=>ws.close(),onClose:fn=>ws.on('close',fn),onMessage:fn=>ws.on('message',raw=>fn(raw.toString()))}));
 else socket.destroy();
});
process.env.CORTICO_DESKTOP_PET_HOST=JSON.stringify([cfg.window.electronFile,resolve(import.meta.dirname,'host.cjs'),`--shell-url=${origin}`,`--shell-token=${token}`]);
const world:any=new DesktopPetWorld({cfg,timezone:'Asia/Shanghai',persist:(patch:any)=>{merge(cfg,patch);writeFileSync(join(data,'world.json'),JSON.stringify(cfg));if(patch.skin?.figure)reportAppearance();},runtimesRoot:()=>resolve(root,'data/companion-runtime'),modelsDir:()=>join(data,'models'),packRoots:()=>[resolve(root,'data/companion-runtime/coopanion-shell/figures'),join(data,'figures')],packDir:()=>join(data,'figures'),botName:()=>snapshot.name,language:()=>'zh',controls:{openChat:()=>broadcast({type:'open_chat'}),openSettings:()=>broadcast({type:'open_chat'}),quit:()=>emit({type:'exit'}),quitLabel:'退出伙伴'}});
function reportAppearance(){const pack=world.packs().find((p:any)=>p.id===cfg.skin.figure);emit({type:'appearance',figure:cfg.skin.figure,name:pack?.manifest.name.zh||pack?.manifest.name.en||cfg.skin.figure})}
const bodyBridge=createBodyBridge(world,cfg,emit);
await world.start({store:{latestCursor:()=>cursor,range:({limit=300,toCursor=Infinity}:any)=>events.filter(e=>e.cursor<=toCursor).slice(-limit)},log,modelFacts:{accepts:(mime:string)=>mime.startsWith('image/')},blob:readImage,pushEvent:async(event:any,opts:any={})=>{
 const blobs=storeImages(event.blobs||[]);
 if(['desktop-pet.message','desktop-pet.speech'].includes(event.type))event={...event,meta:{...event.meta,routeLabel:'来自桌面',localInput:true}};
 const e={...event,blobs:blobs.map(({bytes,...b}:any)=>b),cursor:++cursor,id:String(cursor),contextDelivery:opts.deliver===false?'observe':'deliver'};events.push(e);if(events.length>600)events.shift();
 if(['desktop-pet.message','desktop-pet.speech'].includes(e.type)){bubbles.interrupt();world.sayEnds=[];world.server.sendPet({t:'companion-interrupt'});emit({type:'input',text:e.meta?.text||'',images:wireImages(blobs),cursor:e.cursor});}
 else if(e.type!=='desktop-pet.self'){emit({type:'activity',text:e.text,event:e.type});bodyBridge.observe(e)}return e;
}});
let contextSignature='';
async function publishBodyContext(){try{const context=await bodyBridge.context();if(!context)return;const signature=JSON.stringify(context);if(signature!==contextSignature){contextSignature=signature;emit({type:'body_context',context})}}catch(e){log.warn('身体上下文未能更新',String(e))}}
const bodyTimer=setInterval(()=>{if(hosts.size)broadcast({type:'body_probe'});else void publishBodyContext()},2000);
async function control(op:string,args:any){if(op==='exit')emit({type:'exit'});else if(op==='connections')emit({type:'connections'});else if(op==='active')emit({type:'active',active:!!args.active});else if(op==='model')emit({type:'model',path:args.path});else if(op==='dress')broadcast({type:'dress',url:world.server.origin+'/dress'});else if(op==='open_chat')broadcast({type:'open_chat'});else throw Error('Unknown control')}
const tools=()=>world.tools();const controls=new Map<string,AbortController>();
const mirrorMessage=createMessageMirror({events,nextCursor:()=>++cursor,images:historyImages,
 beats:(text:string)=>parseScript(text,world.vocab()).beats.map((b:any)=>({text:b.text})),
 publish:(event:any)=>{const item=chatItem(event);if(item)world.chat.broadcast({t:'item',item})}});

const bubbles=createBubbleQueue({connected:()=>world.server.petConnected,
 durationMs:(m:any)=>200+1000*estimateSeconds(parseScript(String(m.text||''),world.vocab()).beats,cfg.window.bubbleSpeed),
 send:async(m:any,{interrupted}:any)=>{
  const blobs=storeImages(m.images||[]);
  if(interrupted||!desktopTarget(m))return;
  // Durable chat was mirrored on receipt, independently of this paced desktop presentation.
  const receipt=await world.say({script:String(m.text||''),images:blobs.map(b=>b.handle)},false);emit({type:'receipt',message_id:m.id,turn_id:m.turn_id,part:m.part,...receipt})},
 onError:(error:unknown)=>{snapshot.notice='气泡显示遇到问题；消息仍保留在对话历史中。';log.warn(String(error))}});
const input=createInterface({input:process.stdin,crlfDelay:Infinity});let closing=false;
async function close(){if(closing)return;closing=true;clearInterval(bodyTimer);bubbles.close();input.close();for(const ws of hosts)ws.close();wss.close();http.closeAllConnections();await world.stop();bodyBridge.close();await new Promise<void>(done=>http.close(()=>done()));emit({type:'closed'});process.exit(0)}
input.on('line',async line=>{try{const m=JSON.parse(line);
 if(m.type==='close')await close();
 else if(m.type==='say'){if(desktopTarget(m))bubbles.enqueue(m)}
 else if(m.type==='history_message'){mirrorMessage(m.message);if(m.message.role==='user'){bubbles.interrupt();world.sayEnds=[];world.server.sendPet({t:'companion-interrupt'})}}
 else if(m.type==='settled')world.onEventsSettled(events.filter(e=>e.cursor===m.cursor),'delivered');
 else if(m.type==='type')world.server.sendPet({t:'type'});
 else if(m.type==='appearance'){
  // world.json owns the full dress, including imported figures and their schemes.
  if(m.restore&&savedAppearance){reportAppearance();return}
  const changed=cfg.skin.figure!==m.figure;cfg.skin.figure=m.figure;
  if(changed){cfg.skin.scheme=m.figure==='whale'?'deepseek-maid':'';cfg.roam=world.currentPack()?.manifest.can.walk?'free':'off';cfg.window.scale=cfg.figureScales[m.figure]||(m.figure==='whale'?1:2.1)}
  writeFileSync(join(data,'world.json'),JSON.stringify(cfg));reportAppearance();
 }
 else if(m.type==='phase'){applyMonitorPhase(snapshot,m.phase);if(m.phase.state==='model')snapshot.notice='';world.onRunPhase(m.phase)}
 else if(m.type==='notice'){snapshot.notice=String(m.text||'');broadcast({type:'notice',text:snapshot.notice})}
 else if(m.type==='snapshot'){
  const incomingHistory=m.snapshot.history||[];
  snapshot={...m.snapshot,history:incomingHistory.map(({images,...row}:any)=>row),notice:snapshot.notice||''};
  for(const row of incomingHistory)mirrorMessage(row);
 }
 else if(m.type==='control_cancel')controls.get(String(m.id))?.abort();
 else if(m.type==='control'){const tool=tools().find((t:any)=>t.name===m.op);if(!tool){emit({type:'control_result',id:m.id,status:'failed',failed:true,code:'body_unknown_operation',text:'Unknown body operation'});return}const controller=new AbortController();controls.set(String(m.id),controller);try{emit({type:'control_result',id:m.id,...await bodyBridge.invoke(tool,m.args||{},{signal:controller.signal},m.id)})}catch(e){emit({type:'control_result',id:m.id,status:'failed',failed:true,code:'body_operation_error',text:String(e)})}finally{controls.delete(String(m.id))}}
 }catch(e){emit({type:'error',text:String(e)})}});
input.on('close',()=>void close());process.on('SIGTERM',()=>void close());process.on('SIGINT',()=>void close());
emit({type:'ready',origin,petOrigin:world.server.origin,home});
