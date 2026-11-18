"""Authenticated single-owner transport; the server owns the character, not a UI."""
from __future__ import annotations
import asyncio
import base64
from concurrent.futures import Future
from dataclasses import dataclass, field
import hmac
import json
import os
from pathlib import Path
import queue
import secrets
import threading
import time
import uuid
from urllib.parse import urlsplit

from aiohttp import web, ClientSession, ClientTimeout, WSMsgType


@dataclass
class RemoteConfig:
    url: str = ''
    enabled: bool = False
    token: str = field(default='', repr=False)

    @classmethod
    def load(cls, home):
        folder = Path(home)/'data/companion'
        path = folder/'remote.json'
        value = json.loads(path.read_text('utf8')) if path.exists() else {}
        token = folder/'remote-token'
        return cls(url=value.get('url', ''), enabled=bool(value.get('enabled', False)),
                   token=token.read_text('utf8').strip() if token.exists() else '')

    def public(self):
        return dict(url=self.url, enabled=self.enabled, has_token=bool(self.token))


def normalize_url(url):
    url = url.strip().rstrip('/')
    if url.startswith('ws://'): url = 'http://'+url[5:]
    if url.startswith('wss://'): url = 'https://'+url[6:]
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise ValueError('远端地址须为 http(s)://主机:端口，不包含密码或路径')
    if parsed.scheme=='http' and parsed.hostname not in ('localhost','127.0.0.1','::1'):
        raise ValueError('远端使用 HTTPS，或先通过 SSH 隧道连接本机端口')
    return url


def write_secret(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(value, encoding='utf8')
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def configure_remote(home, url='', token='', *, enabled=True):
    folder = Path(home)/'data/companion'; folder.mkdir(parents=True, exist_ok=True)
    existing = RemoteConfig.load(home)
    address = normalize_url(url or existing.url) if (url or existing.url) else ''
    secret = token.strip() or existing.token
    if enabled and (not address or not secret): raise ValueError('请填写远端地址和连接密钥')
    if token.strip(): write_secret(folder/'remote-token', secret)
    (folder/'remote.json').write_text(json.dumps(dict(url=address, enabled=bool(enabled))), encoding='utf8')
    return RemoteConfig.load(home).public()


async def remote_call(config, method, *args):
    async with ClientSession(timeout=ClientTimeout(total=120), trust_env=False) as client:
        async with client.post(normalize_url(config.url)+'/rpc', headers={'Authorization':'Bearer '+config.token},
                               json=dict(method=method,args=list(args))) as response:
            if response.status==401: raise ValueError('远端连接密钥无效')
            if response.status!=200: raise RuntimeError('远端请求失败')
            result = await response.json()
            if not result.get('ok'): raise RuntimeError(result.get('error','远端操作失败'))
            return result.get('result')


class RemoteExecutor:
    def __init__(self, server, original): self.server, self.original = server, original
    def available_kinds(self):
        kinds=[k for k in (self.original.available_kinds() if hasattr(self.original,'available_kinds') else ['command']) if k!='computer']
        return kinds+(['computer'] if self.server.peer is not None else [])+(['phone'] if self.phone_context()['connected'] else [])
    def phone_context(self):return self.server.mobile.phone.context()
    def known_mobile_destinations(self):
        return {'mobile:'+device['id'] for device in self.server.mobile.devices.values() if not device.get('revoked')}
    def available_destinations(self):
        destinations=[dict(target='desktop',source=dict(kind='desktop',name='桌面'))] if self.server.peer is not None else []
        for device in sorted(set(self.server.mobile.subscribers.values())):
            destinations.append(dict(target='mobile:'+device,source=self.server.mobile.device_source(device)))
        if self.server.qq is not None and self.server.qq.state=='connected':
            destinations.append(dict(target='qq',source=dict(kind='qq',name='QQ')))
        return destinations
    async def run(self, action):
        if action.get('kind')=='computer': return await self.server.control('computer', action)
        if action.get('kind')=='phone': return await self.server.mobile.phone.run(action)
        return await self.original.run(action)
    async def close(self): await self.original.close()


class CompanionServer:
    """One service lifetime, independent of authenticated client lifetimes."""
    METHODS = {'chat', 'snapshot', 'set_active', 'configure_town', 'bilibili_call', 'update_body',
               'observe_body', 'control_project', 'acknowledge_rendered',
               'im_configure', 'im_wechat_login_begin', 'im_wechat_login_poll', 'im_wechat_logout',
               'im_test'}
    def __init__(self, cfg, home, token, companion_factory=None):
        self.cfg,self.home,self.token = cfg,Path(home),token
        if not token: raise ValueError('Server token required')
        self.factory=companion_factory
        self.core=None; self.qq=None; self.im=None; self.peer=None; self.pending={}; self.tasks=set(); self.connecting=False
        self._snapshot_cache=None
        self.app=web.Application(client_max_size=40*1024*1024, middlewares=[self.auth])
        self.app.router.add_get('/health', self.health)
        self.app.router.add_get('/alive', self.alive)
        self.app.router.add_get('/mobile/link', self.mobile_link)
        self.app.router.add_post('/rpc', self.rpc)
        self.app.router.add_get('/ws', self.websocket)
        from .companion_mobile import MobileAPI
        self.mobile=MobileAPI(self)
        self.app.on_startup.append(self.start)
        self.app.on_shutdown.append(self.shutdown)
        self.app.on_cleanup.append(self.close)

    @web.middleware
    async def auth(self, request, handler):
        if request.path=='/alive':
            # Liveness only: no state, no token, no owner data. The watchdog must not hold
            # the owner credential, so this one path answers without it.
            return await handler(request)
        if request.path.startswith('/mobile/'):
            return await self.mobile.authenticate(request,handler)
        if not hmac.compare_digest(request.headers.get('Authorization',''), 'Bearer '+self.token):
            raise web.HTTPUnauthorized()
        # Browser callers must be same-origin; native clients omit Origin.
        origin=request.headers.get('Origin')
        if origin and origin!=f'{request.scheme}://{request.host}': raise web.HTTPForbidden()
        return await handler(request)

    def emit(self, event):
        self._snapshot_cache = None
        self.mobile.emit(event)
        targets=event.get('delivery',{}).get('targets',['all'])
        if self.im is not None: self.im.emit(event,targets)
        elif self.qq is not None and ('all' in targets or 'qq' in targets): self.qq.emit(event)
        if self.peer is not None:
            try: self.peer[1].put_nowait(event)
            except asyncio.QueueFull:
                self.spawn(self.peer[0].close(code=1013, message=b'Reconnect for snapshot'))

    def spawn(self, coro):
        task=asyncio.create_task(coro); self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def start(self, _app):
        if self.factory is None:
            from .companion import Companion
            self.factory=Companion
        self.core=self.factory(self.cfg,self.home,on_event=self.emit)
        if hasattr(self.core.executor,'attach_core'):
            self.core.executor.attach_core(self.core)
        self.core.executor=RemoteExecutor(self,self.core.executor)
        self.im_factory=self._im_manager
        from .companion_im import ImManager
        self.im=ImManager(self.core,self.cfg,self.home)
        self.qq=self.im.qq
        await self.core.start()
        await self.im.start()
        self.qq=self.im.qq

    def _im_manager(self):
        """还没起通道管理器时，问一次通道配置的真实状态（只读，不连网）。"""
        from .companion_im import ImManager
        return ImManager(self.core,self.cfg,self.home)

    async def shutdown(self, _app):
        await self.mobile.stop()
        if self.peer: await self.peer[0].close()

    async def close(self, _app):
        if self.im is not None: await self.im.close()
        elif self.qq is not None: await self.qq.close()
        if self.peer: await self.peer[0].close()
        for future in list(self.pending.values()):
            if not future.done(): future.set_exception(ConnectionError('Desktop disconnected'))
        for task in list(self.tasks): task.cancel()
        await asyncio.gather(*list(self.tasks),return_exceptions=True)
        if self.core: await self.core.close()
        self.mobile.close()

    async def health(self, request): return web.json_response(dict(ok=True, desktop_connected=self.peer is not None))

    async def alive(self, request):
        """Unauthenticated liveness probe: process is serving, nothing more.

        It carries no state, no token and no owner data, so an external watchdog can
        ask "is this process up" without holding the owner credential. Business
        health stays on the authenticated /health.
        """
        return web.json_response(dict(alive=True, pid=os.getpid()),
                                 headers={'Cache-Control': 'no-store'})

    async def mobile_link(self, request):
        """Read-only phone link state for the owner; behind the server token like `/mobile/pairing`.

        Answers "is the phone connected, and if not why" without dispatching an action, writing
        last_seen, or carrying a token.
        """
        from .companion_phone_view import phone_link
        return web.json_response(dict(ok=True, phone_link=phone_link(self.mobile)),
                                 headers={'Cache-Control': 'no-store'})

    def snapshot(self, include_image_data=False, *, history_limit=None, history_before=None):
        # 构造一次 snapshot 要 0.8–2.5 秒；同一轮里桌面、手机、状态页会重复要它。
        # 缓存 2 秒并在 emit 时失效，避免为了"刚发的一条消息"每次都重算整包。
        # 带分页参数的那一页不进缓存：它是另一份数据，混进来会把首屏串成旧页。
        paged = history_limit is not None or history_before is not None
        if not include_image_data and not paged:
            cached = getattr(self, '_snapshot_cache', None)
            if cached is not None and time.monotonic() - cached[0] < 2.0:
                return cached[1]
        options=dict(include_image_data=include_image_data) if getattr(self.core,'supports_snapshot_images',False) else {}
        if paged:
            # 只有真支持分页的那一版 core 才传：老 core 收到多余参数会直接 TypeError。
            try:
                accepts = 'history_limit' in self.core.snapshot.__code__.co_varnames
            except AttributeError:
                accepts = False
            if accepts:
                options['history_limit'] = history_limit
                options['history_before'] = history_before
        qq=self.im.qq.snapshot() if self.im is not None and self.im.qq is not None else dict(enabled=False,state='disabled')
        im=self.im.snapshot() if self.im is not None else dict(channels=dict(qq=qq),error='')
        value=dict(self.core.snapshot(**options), qq=qq, im=im)
        if not include_image_data and not paged:
            self._snapshot_cache = (time.monotonic(), value)
        return value

    async def invoke(self, method, args, *, desktop=False):
        if method not in self.METHODS or not isinstance(args,list): raise ValueError('Unsupported remote operation')
        if method in ('update_body','observe_body') and not desktop: raise ValueError('Desktop lease required')
        if method=='snapshot': return self.snapshot()
        if method.startswith('im_'):
            if self.im is None: raise ValueError('IM 通道还没有启动')
            try:
                if method=='im_configure':
                    channel,options=args[0],(args[1] if len(args)>1 else {})
                    if channel not in ('qq','wechat'): raise ValueError('未知通道')
                    result=await self.im.apply(channel,options)
                    self.qq=self.im.qq
                    return result
                if method=='im_wechat_login_begin': return await self.im.wechat_login_begin()
                if method=='im_wechat_login_poll': return await self.im.wechat_login_poll()
                if method=='im_wechat_logout':
                    result=await self.im.wechat_logout(); self.qq=self.im.qq; return result
                if method=='im_test':
                    channel=args[0] if args else 'wechat'
                    return await self.im.test(channel,*(args[1:2]))
            except Exception as error:
                raise ValueError(f'IM 操作未完成：{type(error).__name__}: {error}')
        # 非 IM 的方法继续走下面的通用分发；此前这里无条件抛出，
        # 使桌面 RPC 的 chat 分支与所有普通方法都不可达。
        if method=='chat':
            if not 1<=len(args)<=2:raise ValueError('Chat accepts text and optional images only')
            if getattr(self.core,'supports_message_routing',False):
                return await self.core.chat(*args,source=dict(kind='desktop',name='桌面'))
        return await getattr(self.core,method)(*args)

    async def rpc(self, request):
        method=None
        try:
            m=await request.json(); method=m.get('method'); result=await self.invoke(method,m.get('args',[]))
            return web.json_response(dict(ok=True,result=result))
        except Exception as error:
            # IM 通道的失败通常是主人刚填的地址/开关不对，说清楚才有用；其它操作仍只报类型。
            detail=str(error) if isinstance(method,str) and method.startswith('im_') else ''
            return web.json_response(dict(ok=False,error='远端操作未完成：'+type(error).__name__+('：'+detail if detail else '')))

    async def control(self, kind, args):
        peer=self.peer
        if peer is None: raise ConnectionError('Desktop offline')
        key=uuid.uuid4().hex; future=asyncio.get_running_loop().create_future(); self.pending[key]=future
        try:
            await peer[1].put(dict(type='remote_control', id=key, kind=kind, args=args))
            return await asyncio.wait_for(future,180)
        except asyncio.CancelledError:
            if self.peer is peer: self.emit(dict(type='remote_cancel',id=key))
            raise
        finally: self.pending.pop(key,None)

    def body_control(self, op, args):
        result=Future()
        async def deliver():
            try:
                value=await self.control('body',dict(op=op,args=args))
                if not result.done(): result.set_result(value)
            except Exception:
                if not result.done(): result.set_exception(ConnectionError('Desktop control unavailable'))
        task=self.spawn(deliver())
        result.add_done_callback(lambda f: task.cancel() if f.cancelled() else None)
        return result

    async def websocket(self, request):
        if self.peer is not None or self.connecting: raise web.HTTPConflict(text='A desktop is already connected')
        self.connecting=True
        ws=web.WebSocketResponse(heartbeat=20,max_msg_size=40*1024*1024)
        try: await ws.prepare(request)
        finally: self.connecting=False
        peer=(ws,asyncio.Queue(maxsize=256)); self.peer=peer
        await self.core.attach_body(self.body_control)
        async def send():
            while True: await ws.send_json(await peer[1].get())
        sender=asyncio.create_task(send()); calls=set()
        self.emit(dict(type='snapshot',snapshot=self.snapshot(include_image_data=True)))
        async def call(m):
            try:
                result=await self.invoke(m.get('method'),m.get('args',[]),desktop=True)
                if self.peer is peer:
                    self.emit(dict(type='remote_result',id=m.get('id'),ok=True,result=result))
                    self.emit(dict(type='snapshot',snapshot=self.snapshot()))
            except Exception as error:
                if self.peer is peer: self.emit(dict(type='error',text='远端操作未完成：'+type(error).__name__))
        try:
            async for message in ws:
                if message.type!=WSMsgType.TEXT: continue
                m=message.json()
                if m.get('type')=='call':
                    task=self.spawn(call(m));calls.add(task);task.add_done_callback(calls.discard)
                elif m.get('type')=='control_result':
                    future=self.pending.get(m.get('id'))
                    if future is not None and not future.done():
                        result=m.get('result',{})
                        image=result.pop('_image_png',None)
                        if image:
                            raw=base64.b64decode(image,validate=True)
                            if len(raw)>24*1024*1024: raise ValueError('Image too large')
                            folder=self.home/'data/companion/artifacts';folder.mkdir(parents=True,exist_ok=True)
                            path=folder/(uuid.uuid4().hex+'.png');path.write_bytes(raw);result['image_path']=str(path)
                        future.set_result(result)
        finally:
            sender.cancel();await asyncio.gather(sender,return_exceptions=True)
            if self.peer is peer:
                self.peer=None
                for future in list(self.pending.values()):
                    if not future.done(): future.set_exception(ConnectionError('Desktop disconnected'))
                await self.core.update_body(dict(connected=False))
            # Work and cognition belong to the server, not to this WebSocket.
        return ws


async def run_remote_service(cfg, home, host='127.0.0.1', port=8765, token_file=None):
    path=Path(token_file) if token_file else Path(home)/'data/companion/server-token'
    if not path.exists(): write_secret(path,secrets.token_urlsafe(32))
    server=CompanionServer(cfg,home,path.read_text('utf8').strip())
    runner=web.AppRunner(server.app,access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner,host,port).start()
        await asyncio.Event().wait()
    finally: await runner.cleanup()


class RemoteServiceThread:
    """Desktop adapter: never creates a second character on connection failure."""
    def __init__(self,cfg,home,*,config=None):
        self.home=Path(home);self.config=config or RemoteConfig.load(home)
        self.events=queue.Queue();self.ready=False;self.stopping=False;self.loop=None;self.ws=None
        self.control=None;self.body_context=None;self.controls={}
        self.thread=threading.Thread(target=self._run,name='companion-remote',daemon=True);self.thread.start()

    def _run(self):
        try: asyncio.run(self._serve())
        except asyncio.CancelledError: pass

    async def _serve(self):
        from .companion_computer import Computer
        from .companion_tunnel import ManagedTunnel
        self.loop=asyncio.get_running_loop();self.stop=asyncio.Event();self.task=asyncio.current_task();self.computer=Computer(self.home)
        try:tunnel=ManagedTunnel(self.home,self.config)
        except (ValueError,OSError):
            self.events.put(dict(type='error',text='SSH连接配置无效，请检查本机ssh.json和密钥路径。'))
            await self.computer.close()
            return
        try:
            while not self.stopping:
                try:
                    await tunnel.ensure()
                    async with ClientSession(trust_env=False,timeout=ClientTimeout(total=None,sock_connect=10)) as session:
                        async with session.ws_connect(normalize_url(self.config.url)+'/ws',headers={'Authorization':'Bearer '+self.config.token},heartbeat=20,max_msg_size=40*1024*1024) as ws:
                            self.ws=ws;self.ready=True
                            self.events.put(dict(type='connection',connected=True))
                            if self.body_context: await self.send_call('update_body',[self.body_context])
                            async for message in ws:
                                if message.type!=WSMsgType.TEXT: continue
                                m=message.json()
                                if m.get('type')=='remote_control':
                                    task=asyncio.create_task(self.execute(m));self.controls[m['id']]=task
                                elif m.get('type')=='remote_cancel':
                                    task=self.controls.get(m['id'])
                                    if task: task.cancel()
                                elif m.get('type')!='remote_result': self.events.put(m)
                except asyncio.CancelledError: raise
                except Exception:
                    if not self.stopping:self.events.put(dict(type='error',text='远端连接中断，正在重连；未启动另一份角色。'))
                finally:
                    self.ready=False;self.ws=None
                    tasks=list(self.controls.values())
                    for task in tasks: task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True);self.controls.clear()
                if not self.stopping:
                    try: await asyncio.wait_for(self.stop.wait(),3)
                    except asyncio.TimeoutError: pass
        finally:
            await tunnel.close()
            await self.computer.close()

    async def execute(self,m):
        ws=self.ws
        try:
            if m['kind']=='computer':
                result=await self.computer.run(m['args'])
                if result.get('image_path'):
                    result['_image_png']=base64.b64encode(Path(result.pop('image_path')).read_bytes()).decode('ascii')
            elif self.control:
                result=await asyncio.wrap_future(self.control(m['args']['op'],m['args']['args']))
            else: result=dict(status='failed',failed=True,text='桌面身体尚未连接')
            if self.ws is ws and not ws.closed: await ws.send_json(dict(type='control_result',id=m['id'],result=result))
        except asyncio.CancelledError: raise
        except Exception:
            if self.ws is ws and not ws.closed: await ws.send_json(dict(type='control_result',id=m['id'],result=dict(status='failed',failed=True,text='本机操作未完成')))
        finally:self.controls.pop(m['id'],None)

    async def send_call(self,method,args):
        if self.ws and not self.ws.closed: await self.ws.send_json(dict(type='call',id=uuid.uuid4().hex,method=method,args=args))

    def call(self,method,*args):
        if method=='attach_body': self.control=args[0];return True
        if method=='update_body':self.body_context=args[0]
        if self.stopping or not self.ready or not self.loop:return False
        future=asyncio.run_coroutine_threadsafe(self.send_call(method,list(args)),self.loop)
        return True

    def close(self):
        self.stopping=True
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.stop.set)
            self.loop.call_soon_threadsafe(self.task.cancel)
