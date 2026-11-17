"""Phone-only API for the existing character; no separate cognition loop."""
import asyncio
import collections
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from aiohttp import web
from PIL import Image


def private_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False),encoding='utf8')
    os.chmod(temporary,0o600);temporary.replace(path)


def create_pairing(home, *, replace=False, state_dir=None):
    path=Path(state_dir or (Path(home)/'data/companion'))/'mobile-pairing.json'
    if not path.exists() or replace:
        now=time.time();private_json(path,dict(code=f'{secrets.randbelow(1000000):06d}',created_at=now,expires_at=now+86400))
    return path


PAIRING_SECONDS=86400


def pairing_status(home, state_dir=None):
    """Owner-facing state of the current pairing code; never returns a used code."""
    path=Path(state_dir or (Path(home)/'data/companion'))/'mobile-pairing.json'
    try:value=json.loads(path.read_text('utf8'))
    except (OSError,ValueError):return dict(configured=False,can_pair=False,reason='还没有配对文件')
    now=time.time()
    if not value.get('code'):return dict(configured=True,can_pair=False,reason='配对码已用掉；要再配一台手机需要重新生成',used_at=value.get('used_at'))
    if now>=value.get('expires_at',0):return dict(configured=True,can_pair=False,reason='配对码已过期；要再配一台手机需要重新生成',expires_at=value.get('expires_at'))
    return dict(configured=True,can_pair=True,expires_at=value.get('expires_at'),seconds_left=round(value['expires_at']-now))


def create_pairing_code(home, state_dir=None):
    """Mint a new pairing code, invalidating only the *unused* previous one.

    Paired phones are keyed by token hash in mobile-devices.json, a separate file: issuing
    a new code never revokes or forgets a device that already holds a token, so a phone
    that lost its token can pair again while the others keep working. The old code stops
    working the moment this returns.
    """
    folder=Path(state_dir or (Path(home)/'data/companion'))
    path=folder/'mobile-pairing.json'
    now=time.time();code=f'{secrets.randbelow(1000000):06d}'
    private_json(path,dict(code=code,created_at=now,expires_at=now+PAIRING_SECONDS,issued='owner_regenerate',replaces_used=prior_used(path)))
    return code,path


def prior_used(path):
    try:return bool(json.loads(Path(path).read_text('utf8')).get('used_at'))
    except (OSError,ValueError):return False


HISTORY_LIMIT = 60
HISTORY_FIELDS = ('id', 'role', 'content', 'created', 'source', 'delivery')
IMAGE_FIELDS = ('handle', 'name', 'mime')
LIFE_ITEM_FIELDS = ('display_text', 'reflection', 'text', 'time_label', 'day_label', 'created',
                    'technical', 'before', 'after', 'reason', 'question', 'next_idea', 'goal')


def _trim_items(items, limit):
    rows = []
    for item in (items or [])[:limit]:
        if isinstance(item, dict):
            rows.append({key: item[key] for key in LIFE_ITEM_FIELDS if key in item})
    return rows


def mobile_view(snapshot, *, runtime=False, history_limit=HISTORY_LIMIT):
    """手机端只拿它真正渲染的字段。

    桌面 snapshot 是给桌面壳和调试用的，含项目、动作、观测等大块数据；
    直接整包发给手机，每次打开与每次刷新都要传 600 KB 以上。这里按手机
    实际用到的字段裁剪，运行详情所需的重数据只在 `runtime=True` 时附带。
    """
    history = []
    for row in (snapshot.get('history') or [])[-history_limit:]:
        if not isinstance(row, dict):
            continue
        item = {key: row[key] for key in HISTORY_FIELDS if key in row}
        refs = [ref for ref in (row.get('image_refs') or []) if isinstance(ref, dict)]
        item['image_refs'] = [{key: ref[key] for key in IMAGE_FIELDS if key in ref} for ref in refs]
        history.append(item)

    life = snapshot.get('life') or {}
    trimmed_life = {
        'now': life.get('now'),
        'absence_summary': life.get('absence_summary'),
        'generated_at': life.get('generated_at'),
        'today_preview': _trim_items(life.get('today_preview'), 12),
        'interest_preview': _trim_items(life.get('interest_preview'), 5),
        'ideas_changes': _trim_items(life.get('ideas_changes'), 6),
        'recollection': life.get('recollection'),
        'latest_letter': life.get('latest_letter'),
        'past_preview': _trim_items(life.get('past_preview'), 20),
        'past_has_more': bool(life.get('past_has_more')),
    }

    notes = [dict(text=note.get('text')) for note in (snapshot.get('notes') or [])
             if isinstance(note, dict) and note.get('kind') == 'self' and note.get('text')][-8:]
    projects = [{key: project[key] for key in ('id', 'origin', 'status', 'question', 'next_idea',
                                               'display_text', 'goal', 'ephemeral') if key in project}
                for project in (snapshot.get('projects') or [])
                if isinstance(project, dict) and project.get('origin') == 'self'
                and project.get('status') in ('active', 'waiting')][:3]

    view = dict(
        history=history,
        life=trimmed_life,
        notes=notes,
        projects=projects,
        phase=snapshot.get('phase'),
        town=dict(state=(snapshot.get('town') or {}).get('state')),
        active=snapshot.get('active'),
        name=snapshot.get('name'),
        next_wake=snapshot.get('next_wake'),
    )
    if runtime:
        view['observability'] = snapshot.get('observability')
    return view


class MobileAPI:
    def __init__(self,server,*,state_dir=None):
        self.server=server;self.home=server.home
        self.state_dir=Path(state_dir) if state_dir else (self.home/'data/companion')
        self.pairing_path=create_pairing(self.home,state_dir=self.state_dir)
        self.devices_path=self.pairing_path.with_name('mobile-devices.json')
        self.devices=json.loads(self.devices_path.read_text('utf8')) if self.devices_path.exists() else {}
        database=self.pairing_path.with_name('mobile.sqlite3')
        self.db=sqlite3.connect(database);os.chmod(database,0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS requests(device TEXT,id TEXT,digest TEXT,status TEXT,created REAL,updated REAL,PRIMARY KEY(device,id))')
        self.db.execute("UPDATE requests SET status='interrupted',updated=? WHERE status='accepted'",(time.time(),));self.db.commit()
        self.subscribers={};self.attempts={};self.closing=False
        from .companion_phone import PhoneControl
        self.phone=PhoneControl(self)
        from .companion_mobile_audio import MobileAudio
        self.audio=MobileAudio(self)
        server.app.router.add_post('/mobile/pair',self.pair)
        server.app.router.add_get('/mobile/state',self.state)
        server.app.router.add_post('/mobile/chat',self.chat)
        server.app.router.add_get('/mobile/events',self.events)
        server.app.router.add_get('/mobile/image/{handle}',self.image)
        server.app.router.add_post('/mobile/project',self.project)
        server.app.router.add_post('/mobile/rendered',self.rendered)
        server.app.router.add_get('/mobile/im',self.im_state)
        server.app.router.add_post('/mobile/im/config',self.im_config)
        server.app.router.add_post('/mobile/im/wechat/login',self.im_wechat_login)
        server.app.router.add_post('/mobile/im/wechat/status',self.im_wechat_status)
        server.app.router.add_post('/mobile/im/wechat/logout',self.im_wechat_logout)
        server.app.router.add_post('/mobile/im/test',self.im_test)
        server.app.router.add_get('/mobile/pairing',self.pairing_state)

    def device_name(self,device):
        info=next((value for value in self.devices.values() if value.get('id')==device),{})
        return str(info.get('name') or '')

    async def pairing_state(self,request):
        """Owner-only: whether a pairing code is usable, and mint a new one on request.

        Never returns a code that is still valid — the code reaches the owner by another
        channel, so this route cannot be used to lift it. A paired phone keeps working
        after a new code is issued; only the unused old code is replaced.
        """
        if request.query.get('action')=='regenerate':
            code,_=create_pairing_code(self.home,state_dir=self.state_dir)
            return web.json_response(dict(ok=True,status=pairing_status(self.home,state_dir=self.state_dir),
                regenerate=dict(code=code,note='旧配对码已作废；已配对的手机不受影响，继续用原 token')))
        return web.json_response(dict(ok=True,status=pairing_status(self.home,state_dir=self.state_dir)))

    # ── IM 通道：与桌面端读写同一份配置，手机端只是另一个入口 ──
    def _im(self):
        manager=getattr(self.server,'im',None)
        if manager is None:raise ValueError('IM 通道还没有启动')
        return manager

    def _im_snapshot(self):
        manager=getattr(self.server,'im',None)
        if manager is None:return dict(channels={},error='im_unavailable')
        try:return manager.snapshot()
        except Exception as error:return dict(channels={},error=type(error).__name__)

    async def im_state(self,request):
        return web.json_response(dict(ok=True,result=self._im_snapshot()))

    async def im_config(self,request):
        try:
            body=await request.json();channel=body.get('channel')
            if channel not in ('qq','wechat'):return web.json_response(dict(ok=False,error='未知通道'),status=400)
            options={key:body[key] for key in ('enabled','ws_url','token','clear_token') if key in body}
            await self._im().apply(channel,options)
            self.server.qq=self._im().qq
            return web.json_response(dict(ok=True,result=self._im_snapshot()))
        except ValueError as error:
            return web.json_response(dict(ok=False,error=str(error)),status=400)
        except Exception as error:
            return web.json_response(dict(ok=False,error='保存失败：'+type(error).__name__),status=400)

    async def im_wechat_login(self,request):
        try:return web.json_response(dict(ok=True,result=await self._im().wechat_login_begin()))
        except Exception as error:
            return web.json_response(dict(ok=False,error='取二维码失败：'+type(error).__name__),status=400)

    async def im_wechat_status(self,request):
        try:return web.json_response(dict(ok=True,result=await self._im().wechat_login_poll()))
        except Exception as error:
            return web.json_response(dict(ok=False,error='查询扫码状态失败：'+type(error).__name__),status=400)

    async def im_wechat_logout(self,request):
        try:
            await self._im().wechat_logout()
            self.server.qq=self._im().qq
            return web.json_response(dict(ok=True,result=self._im_snapshot()))
        except Exception as error:
            return web.json_response(dict(ok=False,error='退出登录失败：'+type(error).__name__),status=400)

    async def im_test(self,request):
        try:
            body=await request.json();channel=body.get('channel')
            return web.json_response(dict(ok=True,result=await self._im().test(channel)))
        except Exception as error:
            return web.json_response(dict(ok=False,error='发送测试消息失败：'+type(error).__name__+': '+str(error)[:120]),status=400)

    async def project(self,request):
        """Same core method as the desktop RPC; the phone never gets its own rules."""
        body=await request.json()
        return web.json_response(dict(ok=True,result=self.server.core.control_project(body)))

    async def rendered(self,request):
        from .companion_delivery import acknowledge_events
        body=await request.json()
        ids=body.get('event_ids') if isinstance(body,dict) else None
        if not isinstance(ids,list) or len(ids)>100 or any(not isinstance(x,str) for x in ids):
            raise web.HTTPBadRequest(text='event_ids must be at most 100 strings')
        core=self.server.core
        confirmed=acknowledge_events(core.repo,agent_id=core.life.agent_id,event_ids=ids,
            channel='mobile:'+request['mobile_device'])
        return web.json_response(dict(ok=True,event_ids=confirmed))

    async def authenticate(self,request,handler):
        if request.path=='/mobile/pair' and request.method=='POST':return await handler(request)
        if request.path=='/mobile/pairing':
            # Owner surface: the server token, never a phone token. A phone token cannot
            # read this route, so one paired phone cannot learn the other pairing code.
            if not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+str(getattr(self.server,'token',''))):
                raise web.HTTPUnauthorized()
            return await handler(request)
        if request.path=='/mobile/link':
            # Same owner surface as /mobile/pairing: phone link state is for the owner only,
            # and it must not be reachable with a phone token.
            if not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+str(getattr(self.server,'token',''))):
                raise web.HTTPUnauthorized()
            return await handler(request)
        header=request.headers.get('Authorization','')
        if not header.startswith('Bearer '):raise web.HTTPUnauthorized()
        token=header[7:]
        device=self.devices.get(hashlib.sha256(token.encode()).hexdigest())
        if not token.startswith('mob_') or not device or device.get('revoked'):raise web.HTTPUnauthorized()
        request['mobile_device']=device['id']
        return await handler(request)

    async def pair(self,request):
        address=request.remote or 'unknown';now=time.time()
        attempts=[at for at in self.attempts.get(address,[]) if now-at<60]
        if len(attempts)>=5:raise web.HTTPTooManyRequests(headers={'Retry-After':'60'})
        attempts.append(now);self.attempts[address]=attempts
        if len(self.attempts)>256:self.attempts={key:[at for at in values if now-at<60] for key,values in self.attempts.items() if any(now-at<60 for at in values)}
        try:
            data=await request.json()
            code=str(data.get('code',''));name=str(data.get('device_name','Android'))[:80]
            pairing=json.loads(self.pairing_path.read_text('utf8'))
        except (ValueError,AttributeError,OSError):raise web.HTTPBadRequest(text='Invalid pairing request')
        if not re.fullmatch(r'\d{6}',code) or not pairing.get('code') or now>=pairing.get('expires_at',0) or not hmac.compare_digest(code,pairing['code']):
            raise web.HTTPUnauthorized(text='Pairing code invalid or expired')
        token='mob_'+secrets.token_urlsafe(32);identity=uuid.uuid4().hex
        self.devices[hashlib.sha256(token.encode()).hexdigest()]=dict(id=identity,name=name,created_at=now)
        private_json(self.devices_path,self.devices)
        pairing.pop('code',None);pairing['used_at']=now;private_json(self.pairing_path,pairing)
        return web.json_response(dict(token=token,device_id=identity))

    async def state(self,request):
        runtime=request.query.get('runtime')=='1'
        snapshot=mobile_view(self.server.snapshot(include_image_data=False),runtime=runtime)
        device=request['mobile_device']
        source=self.device_source(device)
        snapshot['mobile_device']=dict(id=device,name=source.get('name','手机'))
        snapshot['mobile_requests']=[dict(id=row[0],status=row[1]) for row in self.db.execute('SELECT id,status FROM requests WHERE device=? ORDER BY created DESC LIMIT 30',(device,))]
        return web.json_response(snapshot,headers={'Cache-Control':'no-store'})

    def device_source(self,device):
        info=next((value for value in self.devices.values() if value['id']==device),{})
        return dict(kind='mobile',device_id=device,name=info.get('name','手机'))

    async def chat(self,request):
        try:
            data=await request.json();identity=str(uuid.UUID(str(data.get('id',''))))
            text=data.get('text','');images=data.get('images',[])
            if not isinstance(text,str) or len(text)>65536 or not isinstance(images,list) or len(images)>8 or not (text.strip() or images):raise ValueError()
            digest=hashlib.sha256(json.dumps(dict(text=text,images=images),sort_keys=True,separators=(',',':')).encode()).hexdigest()
        except (ValueError,AttributeError,TypeError):raise web.HTTPBadRequest(text='Invalid chat request')
        device=request['mobile_device']
        old=self.db.execute('SELECT digest,status FROM requests WHERE device=? AND id=?',(device,identity)).fetchone()
        if old:
            if not hmac.compare_digest(digest,old[0]):raise web.HTTPConflict(text='Message id already has different content')
            old_status=old[1]
            # 失败/被打断的消息允许**沿用同一个 id 重跑**：前端「原地重试」必须复用幂等键，
            # 否则去重失效、同一条话会被处理两遍。
            # 为什么只放行这两个状态：`completed` 的回复已经产出，重跑会重复回复；
            # `accepted` 说明可能有 `_chat` 正在跑或曾跑到一半，放行有双跑风险（启动时
            # 会被扫成 interrupted，届时自然落到这里，闭环是通的）。
            # 依据：`core.chat()` 的 future 只在认知成功时 set_result（companion.py:1843），
            # 所以 `failed` 一定意味着回复没产出，重跑不会重复。
            if old_status in {'failed','interrupted'}:
                self.db.execute('UPDATE requests SET status=?,updated=? WHERE device=? AND id=?',
                                ('accepted',time.time(),device,identity))
                self.db.commit()
                self.server.spawn(self._chat(device,identity,text,images))
                return web.json_response(dict(id=identity,accepted=True,retried=True,previous=old_status),status=202)
            # 其余状态（completed / accepted）**故意不重跑**：
            #   completed —— 回复已经产出，重跑会重复回复；
            #   accepted  —— 可能有 _chat 正在跑，重跑会双跑。这不是漏写：
            #     进程崩在 finally 之前时消息会停在 accepted，而启动时
            #     （本文件 `_recover` / :145 那条 UPDATE）会把它们扫成 interrupted，
            #     下次重试自然落进上面允许重跑的分支，闭环是通的。
            return web.json_response(dict(id=identity,accepted=True,duplicate=True,status=old_status),status=202)
        now=time.time()
        self.db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?)',(device,identity,digest,'accepted',now,now));self.db.commit()
        self.server.spawn(self._chat(device,identity,text,images))
        return web.json_response(dict(id=identity,accepted=True),status=202)

    async def _chat(self,device,identity,text,images):
        status='completed'
        try:
            if getattr(self.server.core,'supports_message_routing',False):
                await self.server.core.chat(text,images,source=self.device_source(device))
            elif images:await self.server.core.chat(text,images)
            else:await self.server.core.chat(text)
        except asyncio.CancelledError:status='interrupted';raise
        except Exception:
            status='failed';self.emit(dict(type='error',text='手机消息处理未完成，请查看消息状态。',request_id=identity))
        finally:
            self.db.execute('UPDATE requests SET status=?,updated=? WHERE device=? AND id=?',(status,time.time(),device,identity));self.db.commit()
            self.emit(dict(type='mobile_request',id=identity,status=status))

    def emit(self,event):
        if event.get('type') not in {'message','phase','status','error','activity','usage','project','action_result','mobile_request'}:return
        # Images remain authenticated, on-demand files. No repeated base64 stream.
        value={k:v for k,v in event.items() if k not in ('images','details','result','action')}
        if event.get('images'):value['has_images']=True
        for queue in list(self.subscribers):
            if queue.full():
                while not queue.empty():queue.get_nowait()
                queue.put_nowait(dict(type='resync'))
            else:queue.put_nowait(value)

    async def events(self,request):
        response=web.StreamResponse(headers={'Content-Type':'text/event-stream','Cache-Control':'no-cache','X-Accel-Buffering':'no'})
        await response.prepare(request)
        queue=asyncio.Queue(maxsize=128);device=request['mobile_device'];self.subscribers[queue]=device
        try:
            await response.write(b'event: ready\ndata: {"type":"ready"}\n\n')
            while not self.closing:
                try:event=await asyncio.wait_for(queue.get(),15)
                except asyncio.TimeoutError:
                    await response.write(b': heartbeat\n\n');continue
                if event is None:break
                encoded=json.dumps(event,ensure_ascii=False,separators=(',',':'))
                await response.write(('event: '+event['type']+'\ndata: '+encoded+'\n\n').encode())
        except (ConnectionError,RuntimeError):pass
        finally:
            self.subscribers.pop(queue,None)
            self.phone.disconnected(queue,device)
        return response

    async def image(self,request):
        handle=request.match_info['handle']
        if not re.fullmatch(r'[a-f0-9]{64}',handle):raise web.HTTPNotFound()
        path=self.home/'data/companion/images'/handle
        if not path.is_file() or not path.resolve().is_relative_to((self.home/'data/companion/images').resolve()):raise web.HTTPNotFound()
        try:
            with Image.open(path) as image:mime={'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp','GIF':'image/gif'}.get(image.format)
        except OSError:raise web.HTTPNotFound()
        if not mime:raise web.HTTPNotFound()
        return web.FileResponse(path,headers={'Content-Type':mime,'Cache-Control':'private, max-age=3600','X-Content-Type-Options':'nosniff'})

    async def stop(self):
        self.closing=True
        for queue in list(self.subscribers):
            if queue.full():queue.get_nowait()
            queue.put_nowait(None)

    def close(self):self.db.close()
