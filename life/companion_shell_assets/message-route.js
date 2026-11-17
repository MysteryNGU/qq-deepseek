/** Delivery affects presentation only; every message remains in shared history. */
export function desktopTarget(message){const targets=message.delivery?.targets??['all'];return targets.includes('all')||targets.includes('desktop')}
export function routeLabel(message){
 const source=message.source?.kind;
 const origin=({desktop:'桌面',mobile:'手机',qq:'QQ',internal:'自主活动'})[source];
 if(message.role==='user')return origin?`来自${origin}`:'';
 const targets=message.delivery?.targets;
 if(!targets)return '';
 const names=targets.includes('all')?['全部设备']:[...new Set(targets.map(t=>t==='desktop'?'桌面':t.startsWith('mobile:')?'手机':t))];
 return `发送到${names.join('、')}`;
}

/** Mirror durable rows from live events and reconnect snapshots without replaying notifications. */
export function createMessageMirror({events,nextCursor,images,beats,publish}){
 const seen=new Set();
 return row=>{
  const id=row.id?String(row.id):null;if(id&&seen.has(id))return;
  if(id){seen.add(id);if(seen.size>1200)seen.delete(seen.values().next().value)}
  const assistant=row.role==='assistant',text=row.content??row.text??'',label=routeLabel(row);
  // A desktop typed event precedes the durable core ID assigned to it.
  const local=!assistant&&row.source?.kind!=='mobile'&&row.source?.kind!=='qq'?events.find(e=>e.meta?.localInput&&!e.meta.coreId&&e.meta.text===text):null;
  if(local){local.meta.coreId=id;local.meta.routeLabel=label||'来自桌面';return}
  const meta=assistant?{kind:'say',beats:beats(String(text)),routeLabel:label,coreId:id}:{via:'chat',text,routeLabel:label,coreId:id};
  const cursor=nextCursor();
  const event={id:String(cursor),cursor,source:'desktop-pet',type:assistant?'desktop-pet.self':'desktop-pet.message',
   ts:typeof row.created==='number'?new Date(row.created*1000).toISOString():row.created||new Date().toISOString(),
   text,blobs:images(row),meta,contextDelivery:'observe'};
  events.push(event);if(events.length>600)events.shift();publish(event);
 };
}
