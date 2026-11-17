/** Observe the pinned World's real page events, without changing its source or touch debounce. */
import {randomUUID} from 'node:crypto';
export const ROAM_MODES=Object.freeze({
 free:'常走动：身体可自行散步、小跑、跳跃或休息。',
 calm:'多待着：较少自行走动，不自行小跑，仍可能短距离走路或做小动作。',
 off:'只做被要求的动作：不自行漫游；有行走能力时仍可主动指定pet_move_to。',
});
const number=x=>typeof x==='number'&&Number.isFinite(x);
const fraction=x=>number(x)?Math.round(Math.max(0,Math.min(1,x))*1000)/1000:null;
export function createBodyBridge(world,cfg,emit){
 const session=randomUUID(),calls=new Map(),walkOwners=new Map(),touchPositions=new Map();
 let sequence=0,invoking=null,grabbed=false;
 let position={x:null,y:null},screen={width:null,height:null},visible=null,motion=null;
 const onPage=world.onPage.bind(world),sendPet=world.server.sendPet.bind(world.server);
 const currentPosition=()=>({x:position.x??fraction(world.standX),y:position.y??fraction(world.standY)});
 function state(){return{figure:world.figureShown?.split(':')[0]??null,connected:!!world.server.petConnected,
  visible:!!world.server.petConnected&&!world.petHidden&&visible!==false,
  roam:world.quiet?.roam??cfg.roam,roam_preference:cfg.roam,
  position:currentPosition(),scale:cfg.window.scale}};
 function observe(event){
  if(!event.type?.startsWith('desktop-pet.')||['desktop-pet.self','desktop-pet.message','desktop-pet.speech'].includes(event.type))return;
  const meta={...(event.meta||{})},touch=meta.touch;
  if(touch){const points=touchPositions.get(touch.kind),point=points?.shift()||currentPosition();meta.touch={...touch,...point};if(!points?.length)touchPositions.delete(touch.kind)}
  emit({type:'body_event',event:{id:`body:${session}:${++sequence}`,type:event.type,text:event.text||'',
   meta,ts:event.ts||new Date().toISOString(),source:'desktop-pet'}});
 }
 world.server.sendPet=message=>{
  if(invoking&&['walk','move'].includes(message.t)){
   const call=calls.get(invoking);if(call){call.walkId=String(message.id);walkOwners.set(call.walkId,invoking)}
  }
  return sendPet(message);
 };
 world.onPage=message=>{
  if(['arrived','interrupted'].includes(message.t)){
   position={x:fraction(message.x)??position.x,y:fraction(message.y)??position.y};
   const owner=walkOwners.get(String(message.walkId)),call=calls.get(owner);
   if(call)call.outcome={status:message.t==='arrived'?'completed':'interrupted',
    walk_id:String(message.walkId),position:currentPosition(),...(message.by?{by:String(message.by)}:{})};
  }else if(message.t==='position')position={x:fraction(message.x),y:fraction(message.y)};
  else if(message.t==='touch'&&cfg.touch.enabled){
   const kind=String(message.kind);
   if(number(message.x)&&number(world.screen?.w))position={...position,x:fraction(message.x/world.screen.w)};
   if(kind==='drop'||kind==='throw'){
    const points=touchPositions.get(kind)||[];points.push(currentPosition());touchPositions.set(kind,points);
   }else touchPositions.set(kind,[currentPosition()]);
   if(kind==='grab'&&!grabbed){grabbed=true;observe({type:'desktop-pet.touch',text:'伙伴把我拿起来了。',
    meta:{touch:{kind:'grab',count:1,woke:false,crashed:false}}})}
   if(kind==='drop'||kind==='throw')grabbed=false;
  }
  return onPage(message);
 };
 return{
  observe,
  layout(value){
   if(number(value.screen?.width)&&number(value.screen?.height))screen={width:value.screen.width,height:value.screen.height};
   if(value.layout&&number(screen.width)&&number(screen.height)){
    position={x:fraction(value.layout.x/screen.width),y:fraction(value.layout.y/screen.height)};
    motion=typeof value.layout.mode==='string'?value.layout.mode:null;
   }
   if(typeof value.visible==='boolean')visible=value.visible;
  },
  async context(){
   const base=state(),pack=world.packs().find(p=>p.id===base.figure);
   // A figure is changing: wait for the actual page's report before publishing its vocabulary.
   if(base.figure&&world.currentPack()?.id!==base.figure)return null;
   const vars=await world.envPromptVars(),vocab=base.figure?world.vocab():[];
   return{...base,name:pack?.manifest.name.zh||pack?.manifest.name.en||base.figure,
    can_walk:!!pack&&pack.manifest.can.walk!==false,roam_modes:ROAM_MODES,
    position:{...base.position,unit:'screen_fraction'},
    screen:{width:screen.width??world.screen?.w??null,height:screen.height??world.screen?.h??null},
    target_range:{x:[0,1]},motion,
    body:base.figure?vars['pet.body']:'尚未收到形象绘制报告。',vocab:base.figure?vars['pet.vocab']:'',
    self:vars['pet.self'],actions:vocab.map(word=>word.id)};
  },
  async invoke(tool,args,ctx,id){
   const key=String(id),call={walkId:null,outcome:null};calls.set(key,call);
   const previous=invoking;let pending;
   try{invoking=key;pending=tool.handler(args,ctx)}finally{invoking=previous}
   try{
    const receipt=await pending;
    if(receipt.failed)return{...receipt,status:'failed',code:'body_operation_failed',state:state()};
    if(tool.name==='pet_move_to'){
     if(call.outcome)return{...receipt,...call.outcome,state:state()};
     return{...receipt,status:'failed',failed:true,
      code:!world.server.petConnected?'body_window_gone':call.walkId?'body_timeout':'body_receipt_unverified',state:state()};
    }
    return{...receipt,status:'completed',completion:['pet_act','pet_say','pet_ask'].includes(tool.name)?'accepted':'completed',state:state()};
   }finally{calls.delete(key);if(call.walkId)walkOwners.delete(call.walkId)}
  },
  close(){world.onPage=onPage;world.server.sendPet=sendPet;calls.clear();walkOwners.clear()},
 };
}
