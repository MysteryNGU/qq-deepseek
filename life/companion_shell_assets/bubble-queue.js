/** Pace display only. Durable messages and conversational cancellation belong to Companion. */
export function createBubbleQueue({send,durationMs,connected=()=>true,onError=()=>{},
 clock=Date.now,schedule=setTimeout,cancel=clearTimeout}){
 const pending=[],seen=new Set(),ids=[];
 let timer=null,busy=false,closed=false,nextAt=0;
 function wait(ms){timer=schedule(()=>{timer=null;pump()},ms)}
 function pump(){
  if(closed||busy||timer!==null||!pending.length)return;
  if(!connected()){wait(250);return}
  const delay=nextAt-clock();if(delay>0){wait(delay);return}
  const {message,interrupted}=pending.shift();busy=true;
  nextAt=clock()+(interrupted?0:Math.max(0,durationMs(message)));
  Promise.resolve().then(()=>send(message,{interrupted})).catch(onError).finally(()=>{busy=false;pump()});
 }
 return{
  enqueue(message){
   if(closed)return;
   if(message.id){if(seen.has(message.id))return;seen.add(message.id);ids.push(message.id);if(ids.length>600)seen.delete(ids.shift())}
   pending.push({message,interrupted:false});pump();
  },
  interrupt(){
   if(timer!==null)cancel(timer);timer=null;nextAt=0;
   for(const entry of pending)entry.interrupted=true;
   pump();
  },
  close(){closed=true;if(timer!==null)cancel(timer);timer=null;pending.length=0},
 };
}
