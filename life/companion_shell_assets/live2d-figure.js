/** Cubism stays in a sandboxed Coopanion figure pack; original model files are untouched. */
import {createMotionPlayer} from './live2d-motion.js';
function script(url){return new Promise((ok,bad)=>{const s=document.createElement('script');s.src=String(url);s.onload=ok;s.onerror=()=>bad(Error('Could not load '+url));document.head.append(s)})}
export async function createLive2DBody(base,{host,asset}){
 await script(asset('lib/live2dcubismcore.min.js'));
 await script(asset('lib/pixi.min.js'));
 await script(asset('lib/unsafe-eval.min.js'));
 await script(asset('lib/cubism4.min.js'));
 const modelSettings=await (await fetch(asset('companion-model.json'))).json();
 const app=new PIXI.Application({width:host.bounds().W,height:host.bounds().H,transparent:true,backgroundAlpha:0,autoStart:false,antialias:true,resolution:devicePixelRatio});
 app.view.style.cssText='position:absolute;inset:0;width:100%;height:100%;pointer-events:none';host.root.append(app.view);
 const model=await PIXI.live2d.Live2DModel.from(new URL(modelSettings.file,base).href,{autoInteract:false,autoUpdate:false});
 // The SDK's default "breath" also swings the head on three unrelated cycles
 // (up to 7.5 degrees). Keep breathing; gaze and authored actions control the head.
 const breath=model.internalModel.breath;
 if(breath)breath.setParameters(breath.getParameters().filter(p=>!['ParamAngleX','ParamAngleY','ParamAngleZ'].includes(p.parameterId)));
 app.stage.addChild(model);model.anchor.set(.5,1);
 const core=model.internalModel.coreModel;
 const motions=modelSettings.motions?(await (await fetch(asset(modelSettings.motions))).json()).motions:{};
 const player=createMotionPlayer(core,motions);
 const natural={w:model.width,h:model.height};
 let x=host.start?.x??host.bounds().W*.75,y=host.start?.y??host.bounds().floorY;
 let drag=null,down=null,disposed=false,grabbed=false,talkTime=0,pendingDt=0;
 function applyFrameParameters(){
  const dt=pendingDt;pendingDt=0;
  player.step(dt);
  if(talkTime>0){talkTime=Math.max(0,talkTime-dt);core.setParameterValueById('ParamMouthOpenY',Math.min(.35,talkTime*2))}
 }
 model.internalModel.on('beforeModelUpdate',applyFrameParameters);
 const size=()=>{const h=256*host.bounds().S*1.8;return{h,w:h*natural.w/natural.h}};
 function lookAt(p){
  const h=size().h,limit=(v,max)=>Math.max(-max,Math.min(max,v));
  // Host pointers are stage pixels; aim from the face, not the model's waist.
  // The SDK focus controller supplies acceleration/smoothing before authored motions.
  model.internalModel.focusController.focus(p?limit((p.x-x)/(h*.65),.65):0,
   p?limit((y-h*.82-p.y)/(h*.65),.45):0);
 }
 const clamp=()=>{const {W,H}=host.bounds(),s=size();x=Math.max(s.w/2,Math.min(W-s.w/2,x));y=Math.max(s.h,Math.min(H,y))};
 function render(){clamp();const s=size();model.scale.set(s.h/natural.h);model.position.set(x,y);app.renderer.render(app.stage)}
 function resize(){const b=host.bounds();app.renderer.resize(b.W,b.H);render()}
 function layout(){const s=size();return{x,y,facing:1,mode:drag?'drag':'idle',busy:!!drag,moving:false,pressing:!!drag,cursor:drag?'grabbing':'grab',box:{x:x-s.w/2,y:y-s.h,w:s.w,h:s.h},hit:[{x,y:y-s.h*.55,r:Math.max(s.w/2,s.h*.4)}],bubble:{x,y:y-s.h+10},side:{x:x+s.w/2,y:y-s.h/2,reach:12}}}
 render();
 function release(p){const moved=grabbed;if(moved){player.start('put-down');host.emit('touch',{kind:'drop',x})}
  else if(p&&down&&Math.hypot(p.x-down.x,p.y-down.y)<8){player.start('surprised');host.emit('touch',{kind:'poke'})}
  down=null;drag=null;grabbed=false;
 }
 return{words:Object.keys(motions),z:'#87bfff',layout,resize,
  step(dt,draw){if(disposed)return;pendingDt+=dt;model.update(dt*1000);if(draw)render()},
  do(word){return player.start(word)},walk(){return false},move(){return false},
  set(state){if(state.bounds)resize()},
  place(a,_f,b){x=a;if(typeof b==='number')y=b;clamp()},shift(dx,dy){x+=dx;y+=dy;clamp()},
  pointer(type,p){if(type==='move'&&!drag)lookAt(p);else if(type==='leave'||type==='cancel')lookAt(null);
   if(type==='down'){down={x:p.x,y:p.y};drag={x:x-p.x,y:y-p.y}}else if(type==='move'&&drag){
   if(!grabbed&&Math.hypot(p.x-down.x,p.y-down.y)>=8){grabbed=true;player.start('picked-up');host.emit('touch',{kind:'grab'})}
   x=p.x+drag.x;y=p.y+drag.y;clamp()
  }else if(type==='up')release(p);else if(type==='cancel')release()},
  drop(){release()},talk(){talkTime=.18},cue(){},setScheme(){},dispose(){disposed=true;model.internalModel.off('beforeModelUpdate',applyFrameParameters);player.stop();model.destroy();app.destroy(true,{children:true,texture:true,baseTexture:true})}
 };
}
