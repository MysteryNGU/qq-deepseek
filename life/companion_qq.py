"""Owner-only OneBot forward WebSocket transport for the single Companion core."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

import aiohttp
from PIL import Image

from .companion_media import MAX_BYTES, MAX_TOTAL, MIMES, _validate, save_images, wire_images


def load_qq_settings(home):
    folder=Path(home)/'data/companion'
    try:data=json.loads((folder/'qq.json').read_text('utf-8'))
    except FileNotFoundError:data={}
    url=data.get('ws_url','ws://127.0.0.1:3001')
    parsed=urlsplit(url)
    if parsed.scheme not in ('ws','wss') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('QQ连接须为不含密码的WebSocket地址')
    if type(data.get('enabled',False)) is not bool:raise ValueError('QQ开关须为布尔值')
    return dict(enabled=data.get('enabled',False),ws_url=url,has_token=(folder/'qq-token').is_file())


def configure_qq(home, *, enabled=True, ws_url='ws://127.0.0.1:3001', token=''):
    parsed=urlsplit(ws_url)
    if parsed.scheme not in ('ws','wss') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('QQ连接须为不含密码的WebSocket地址')
    if type(enabled) is not bool:raise ValueError('QQ开关须为布尔值')
    folder=Path(home)/'data/companion';folder.mkdir(parents=True,exist_ok=True)
    if token:
        p=folder/'qq-token.tmp';p.write_text(token.strip(),encoding='utf-8');os.chmod(p,0o600);p.replace(folder/'qq-token')
    p=folder/'qq.json.tmp';p.write_text(json.dumps(dict(enabled=enabled,ws_url=ws_url)),encoding='utf-8');p.replace(folder/'qq.json')
    return load_qq_settings(home)


class CompanionQQ:
    def __init__(self, core, cfg, home):
        self.core,self.cfg,self.home=core,cfg,Path(home)
        self.repo=core.repo;self.agent=core.life.agent_id
        self.config=load_qq_settings(home)
        self.state='disabled' if not self.config['enabled'] else 'disconnected'
        self.error='';self.ws=None;self.http=None;self._closed=False;self._tasks=set();self._chat_tasks=set()
        self._pending={};self._ready=asyncio.Event();self._outbox=asyncio.Event();self._verified=0
        with self.repo.tx():
            self.repo._exec('CREATE TABLE IF NOT EXISTS companion_qq_delivery('
                'agent TEXT,direction TEXT,id TEXT,status TEXT,payload TEXT,updated REAL,PRIMARY KEY(agent,direction,id))')
            self.repo._exec("UPDATE companion_qq_delivery SET status='unknown' WHERE agent=? AND status IN ('sending','processing')",(self.agent,))

    def snapshot(self):
        rows=self.repo._all('SELECT direction,status,COUNT(*) n FROM companion_qq_delivery WHERE agent=? GROUP BY direction,status',(self.agent,))
        return dict(**self.config,state=self.state,error=self.error,owner=str(self.cfg.owner),
                    account=str(self._verified or getattr(self.cfg,'bot_qq',0)),deliveries=[dict(r) for r in rows])

    async def start(self):
        if self._tasks or self._closed or not self.config['enabled']:return
        if not getattr(self.cfg,'bot_qq',0):
            self.state='needs_account';self.error='请先设置该人物的bot_qq';return
        self.http=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),trust_env=False)
        self._tasks={asyncio.create_task(self._connect()),asyncio.create_task(self._send_loop())}

    def _store(self,direction,identity,status,payload):
        with self.repo.tx():
            self.repo._exec('INSERT OR IGNORE INTO companion_qq_delivery VALUES(?,?,?,?,?,?)',
                (self.agent,direction,str(identity),status,json.dumps(payload,ensure_ascii=False),time.time()))

    def _set(self,direction,identity,status):
        with self.repo.tx():self.repo._exec('UPDATE companion_qq_delivery SET status=?,updated=? WHERE agent=? AND direction=? AND id=?',
            (status,time.time(),self.agent,direction,str(identity)))

    def emit(self,event):
        if self._closed or not self.config['enabled'] or event.get('type')!='message' or event.get('role')!='assistant' or not event.get('id'):return
        text=event.get('text',event.get('content',''))
        if not text and not event.get('images'):return
        images=save_images(self.home,event.get('images'))
        self._store('out',event['id'],'queued',dict(text=text,images=images))
        self._outbox.set()

    async def _rpc(self,action,params,timeout=20):
        if not self.ws or self.ws.closed:raise ConnectionError('qq_disconnected')
        identity=uuid.uuid4().hex;future=asyncio.get_running_loop().create_future();self._pending[identity]=future
        try:
            await self.ws.send_json(dict(action=action,params=params,echo=identity))
            reply=await asyncio.wait_for(future,timeout)
            if reply.get('status')!='ok' or reply.get('retcode',0)!=0:raise ValueError('qq_api_rejected')
            return reply.get('data') or {}
        finally:self._pending.pop(identity,None)

    async def _connect(self):
        path=self.home/'data/companion/qq-token'
        while not self._closed:
            reader=None
            try:
                token=path.read_text('utf-8').strip() if path.is_file() else ''
                async with self.http.ws_connect(self.config['ws_url'],headers={'Authorization':'Bearer '+token} if token else {},heartbeat=25,max_msg_size=2*1024*1024) as ws:
                    self.ws=ws;self.state='verifying';buffer=[]
                    reader=asyncio.create_task(self._read(ws,buffer))
                    info=await self._rpc('get_login_info',{})
                    if str(info.get('user_id'))!=str(self.cfg.bot_qq):raise ValueError('qq_account_mismatch')
                    self._verified=int(info['user_id']);self.state='connected';self.error='';self._ready.set();self._outbox.set()
                    for event in buffer:self._dispatch(event)
                    await reader
            except asyncio.CancelledError:raise
            except Exception as error:self.error=str(error) if isinstance(error,ValueError) else type(error).__name__
            finally:
                self._ready.clear();self._verified=0;self.ws=None
                if reader:reader.cancel();await asyncio.gather(reader,return_exceptions=True)
                for future in list(self._pending.values()):
                    if not future.done():future.set_exception(ConnectionError('qq_connection_lost'))
            if not self._closed:self.state='disconnected';await asyncio.sleep(3)

    async def _read(self,ws,buffer):
        async for message in ws:
            if message.type!=aiohttp.WSMsgType.TEXT:continue
            try:event=json.loads(message.data)
            except ValueError:continue
            future=self._pending.get(str(event.get('echo')))
            if future and not future.done():future.set_result(event);continue
            if not self._verified:
                if len(buffer)<100:buffer.append(event)
            else:self._dispatch(event)

    def _dispatch(self,event):
        if (event.get('post_type')!='message' or event.get('message_type')!='private'
            or str(event.get('user_id'))!=str(self.cfg.owner) or str(event.get('self_id'))!=str(self._verified)
            or event.get('message_id') is None):return
        identity=str(event['message_id'])
        if self.repo._one('SELECT id FROM companion_qq_delivery WHERE agent=? AND direction=? AND id=?',(self.agent,'in',identity)):return
        self._store('in',identity,'processing',event)
        task=asyncio.create_task(self._receive(identity,event));self._chat_tasks.add(task);task.add_done_callback(self._chat_tasks.discard)

    async def _receive(self,identity,event):
        try:
            segments=event.get('message',[])
            if isinstance(segments,str):segments=[dict(type='text',data=dict(text=segments))]
            text=''.join(str(s.get('data',{}).get('text','')) for s in segments if s.get('type')=='text')
            images=[];total=0
            for segment in segments:
                if segment.get('type')!='image':continue
                if len(images)>=8:raise ValueError('too_many_images')
                url=segment.get('data',{}).get('url','')
                if urlsplit(url).scheme not in ('http','https'):raise ValueError('qq_image_url_missing')
                async with self.http.get(url) as response:
                    response.raise_for_status();chunks=[];size=0
                    async for chunk in response.content.iter_chunked(65536):
                        size+=len(chunk)
                        if size>MAX_BYTES or total+size>MAX_TOTAL:raise ValueError('qq_image_too_large')
                        chunks.append(chunk)
                raw=b''.join(chunks);total+=len(raw)
                with Image.open(io.BytesIO(raw)) as image:mime=MIMES.get(image.format)
                _validate(raw,mime)
                images.append(dict(name='qq-image',mime=mime,base64=base64.b64encode(raw).decode('ascii')))
            if not text.strip() and not images:self._set('in',identity,'ignored');return
            if getattr(self.core,'supports_message_routing',False):
                await self.core.chat(text,images,source=dict(kind='qq',name='QQ'))
            else:await self.core.chat(text,images)
            self._set('in',identity,'received')
        except asyncio.CancelledError:
            self._set('in',identity,'unknown');raise
        except Exception as error:
            self._set('in',identity,'failed');self.error='incoming_'+type(error).__name__

    async def _send_loop(self):
        while not self._closed:
            await self._ready.wait()
            row=self.repo._one("SELECT * FROM companion_qq_delivery WHERE agent=? AND direction='out' AND status='queued' ORDER BY updated LIMIT 1",(self.agent,))
            if not row:
                self._outbox.clear();await self._outbox.wait();continue
            try:
                payload=json.loads(row['payload']);segments=[]
                if payload['text']:segments.append(dict(type='text',data=dict(text=payload['text'])))
                for image in wire_images(self.home,payload.get('images')):
                    segments.append(dict(type='image',data=dict(file='base64://'+image['base64'])))
                if not segments:raise ValueError('qq_empty_message')
            except (OSError,ValueError,KeyError):
                self._set('out',row['id'],'failed');self.error='outgoing_media_unavailable';continue
            self._set('out',row['id'],'sending')
            try:
                receipt=await self._rpc('send_private_msg',dict(user_id=int(self.cfg.owner),message=segments))
                if receipt.get('message_id') is None:raise RuntimeError('qq_receipt_missing')
                payload['qq_message_id']=receipt['message_id']
                with self.repo.tx():self.repo._exec('UPDATE companion_qq_delivery SET payload=? WHERE agent=? AND direction=? AND id=?',
                    (json.dumps(payload,ensure_ascii=False),self.agent,'out',row['id']))
                self._set('out',row['id'],'sent')
            except asyncio.CancelledError:self._set('out',row['id'],'unknown');raise
            except ValueError:self._set('out',row['id'],'failed');self.error='outgoing_rejected'
            except Exception:self._set('out',row['id'],'unknown');self.error='outgoing_unconfirmed'

    async def close(self):
        if self._closed:return
        self._closed=True;self._ready.clear()
        tasks=self._tasks|self._chat_tasks
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if self.http:await self.http.close()
        self.state='closed'
