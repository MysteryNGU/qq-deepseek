/** Play authored parameter curves only; never edit geometry, textures or clothing. */
export function createMotionPlayer(core,motions){
 let current=null;
 function restore(){if(current)for(const curve of current.curves)core.setParameterValueById(curve.id,curve.base);current=null}
 return{
  start(id){
   const motion=motions[id]?.motion;if(!motion)return false;
   restore();const curves=[];
   for(const curve of motion.Curves){
    if(curve.Target!=='Parameter')continue;
    const index=core.getParameterIndex(curve.Id);if(index<0||index>=core.getParameterCount())continue;
    const s=curve.Segments,points=[[s[0],s[1]]];
    for(let i=2;i<s.length;i+=3){if(s[i]!==0)throw Error('Only authored linear parameter curves are supported');points.push([s[i+1],s[i+2]])}
    curves.push({id:curve.Id,base:core.getParameterValueById(curve.Id),points,
     min:core.getParameterMinimumValue(index),max:core.getParameterMaximumValue(index)});
   }
   current={motion,curves,time:0};return true;
  },
  step(dt){
   if(!current)return false;
   const a=current;a.time+=Math.max(0,dt);const duration=a.motion.Meta.Duration;
   if(a.time>=duration){restore();return true}
   const weight=Math.min(1,a.time/(a.motion.Meta.FadeInTime||a.motion.FadeInTime||.001),
    (duration-a.time)/(a.motion.Meta.FadeOutTime||a.motion.FadeOutTime||.001));
   for(const curve of a.curves){
    const p=curve.points;let right=p.findIndex(point=>point[0]>=a.time);if(right<0)right=p.length-1;
    const before=p[Math.max(0,right-1)],after=p[right];
    const fraction=after[0]===before[0]?1:(a.time-before[0])/(after[0]-before[0]);
    const value=before[1]+(after[1]-before[1])*Math.max(0,Math.min(1,fraction));
    core.setParameterValueById(curve.id,Math.max(curve.min,Math.min(curve.max,curve.base+(value-curve.base)*weight)));
   }
   return true;
  },
  stop:restore,
 };
}
