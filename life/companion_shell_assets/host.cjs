// Native host wraps upstream transparent body and its chat UI, with no cognition.
const {app,BrowserWindow,Menu}=require('electron');
const {runPetHost}=require('../../vendor/coopanion/packages/cortico-world-desktop-pet/host/electron-main.cjs');
const WebSocket=require('../../data/companion-runtime/coopanion-shell/node_modules/ws');
const arg=name=>process.argv.find(x=>x.startsWith(name+'='))?.slice(name.length+1);
const origin=arg('--shell-url'),token=arg('--shell-token');
if(process.env.COMPANION_CDP)app.commandLine.appendSwitch('remote-debugging-port',process.env.COMPANION_CDP);
const control=(op,args={})=>fetch(origin+'/api/shell/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({op,args})}).catch(()=>{});
let chat=null,dress=null,petWindow=null;
function openChat(){if(!chat){chat=new BrowserWindow({title:'小鱼 · 陪伴',width:1060,height:760,minWidth:720,minHeight:520,backgroundColor:'#f4f5f4',autoHideMenuBar:true,webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true}});chat.loadURL(origin);chat.on('close',e=>{if(!app.isQuitting){e.preventDefault();chat.hide()}})}chat.show();chat.focus()}
const url=arg('--pet-url')||'';
runPetHost({url,parentPid:Number(arg('--parent-pid')||0),tray:true});
app.on('browser-window-created',(_,win)=>win.webContents.on('did-finish-load',()=>{
 if(!win.webContents.getURL().includes('/pet'))return;petWindow=win;
 void win.webContents.executeJavaScript(`window.addEventListener('message',e=>{if(e.data?.t!=='frame'||!e.data.layout)return;if(![...document.querySelectorAll('.figure-frame')].some(f=>f.contentWindow===e.source))return;window.companionBodyLayout=e.data.layout})`);
}));
app.whenReady().then(()=>{Menu.setApplicationMenu(null);const ws=new WebSocket(origin.replace('http:','ws:')+'/host?token='+token);ws.on('message',raw=>{const m=JSON.parse(raw);if(m.type==='open_chat')openChat();else if(m.type==='body_probe'){
 if(petWindow&&!petWindow.isDestroyed())void petWindow.webContents.executeJavaScript(`({layout:window.companionBodyLayout?{x:window.companionBodyLayout.x,y:window.companionBodyLayout.y,mode:window.companionBodyLayout.mode}:null,screen:{width:innerWidth,height:innerHeight}})`).then(state=>{if(ws.readyState===1)ws.send(JSON.stringify({type:'body_state',...state,visible:petWindow.isVisible()}))}).catch(()=>{});
 }else if(m.type==='dress'){if(!dress){dress=new BrowserWindow({width:960,height:760,autoHideMenuBar:true,webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true}});dress.on('closed',()=>dress=null)}dress.loadURL(m.url);dress.show()}});});
app.on('before-quit',()=>{app.isQuitting=true;void control('exit')});
// Opt-in native capture for the repeatable GUI acceptance check.
if(process.env.COMPANION_CAPTURE_DIR)app.on('browser-window-created',(_,win)=>{
 win.webContents.on('did-finish-load',()=>{void win.webContents.executeJavaScript(`window.addEventListener('message',e=>{if(e.data?.t==='frame'&&e.data.layout)window.companionCaptureLayout=e.data.layout})`);setTimeout(async()=>{
  try{const pet=win.webContents.getURL().includes('/pet');let rect;
   if(pet)rect=await win.webContents.executeJavaScript(`(()=>{const b=window.companionCaptureLayout?.box;const rs=[...document.querySelectorAll('#bubble:not([hidden]),.bubble.said')].map(e=>e.getBoundingClientRect());if(b)rs.push({...b,right:b.x+b.w,bottom:b.y+b.h});if(!rs.length)return undefined;const x=Math.max(0,Math.floor(Math.min(...rs.map(r=>r.x))-24)),y=Math.max(0,Math.floor(Math.min(...rs.map(r=>r.y))-24));return{x,y,width:Math.min(innerWidth-x,Math.ceil(Math.max(...rs.map(r=>r.right))-x+24)),height:Math.min(innerHeight-y,Math.ceil(Math.max(...rs.map(r=>r.bottom))-y+24))}})()`);
   require('node:fs').writeFileSync(require('node:path').join(process.env.COMPANION_CAPTURE_DIR,pet?'body-bubble.png':'chat-window.png'),(await win.webContents.capturePage(rect)).toPNG());
  }catch(e){console.error('capture',String(e))}
 },12000)});
});
