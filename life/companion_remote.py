"""Authenticated single-owner transport; the server owns the character, not a UI."""
from __future__ import annotations
import asyncio
import base64
from concurrent.futures import Future
from dataclasses import dataclass, field
import hmac
import inspect
import json
import logging
import os
from pathlib import Path
import queue
import secrets
import threading
import time
import uuid
from urllib.parse import urlsplit

from aiohttp import web, ClientSession, ClientTimeout, WSMsgType

log = logging.getLogger(__name__)


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


#: 图片 mime → 落盘后缀；与浏览器适配器同一张表（不认识的后缀原样留 img）。
IMAGE_SUFFIX = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif'}


def browser_image_blobs(result, *, read_limit=32 * 1024 * 1024,
                        wire_limit=24 * 1024 * 1024):
    """把桌面端落盘的浏览器截图读成字节，随回执一起回传给云端。

    浏览器适配器把截图写在**桌面主机**上；云端进程读不到那个路径。computer 早就
    有 `_image_png` 回传，浏览器以前没有——于是云端只拿到一个打不开的路径，
    她「看得见路径却看不见画面」。这里补上同款回传：读不到或超过回传预算的，
    记一条 ``skipped`` 让云端说明原因，而不是悄悄少一张图。

    回传预算按 base64 后的体积算（WS 单条消息上限 40MB）：超过就不传字节，
    由云端按「桌面端有、云端读不到」如实写进回执。
    """
    blobs = []
    images = result.get('images') if isinstance(result, dict) and isinstance(result.get('images'), list) else []
    budget = wire_limit
    for index, item in enumerate(images):
        if not isinstance(item, dict) or not item.get('saved'):
            continue
        path = Path(str(item.get('path') or ''))
        try:
            if not path.is_file():
                blobs.append(dict(index=index, path=str(path), skipped='unreadable'))
                continue
            size = path.stat().st_size
            encoded_size = (size + 2) // 3 * 4
            if not size or size > read_limit:
                blobs.append(dict(index=index, path=str(path), skipped='too_large', bytes=size))
                continue
            if encoded_size > budget:
                blobs.append(dict(index=index, path=str(path), skipped='too_large', bytes=size))
                continue
            raw = path.read_bytes()
        except OSError:
            blobs.append(dict(index=index, path=str(path), skipped='unreadable'))
            continue
        budget -= encoded_size
        blobs.append(dict(index=index, path=str(path),
                          mime=str(item.get('mime') or 'image/png'), bytes=len(raw),
                          data=base64.b64encode(raw).decode('ascii')))
    return blobs


def adopt_remote_images(home, result, *, limit=32 * 1024 * 1024):
    """把桌面端回传的图片字节落到云端目录，并把回执里的路径换成云端可读的那份。

    判定标准是「云端进程用这个路径能不能读到文件」：有字节就写本地副本；
    没有字节（桌面端还不回传、或图片超过回传预算）就保留桌面路径并写明原因，
    由统一出口标成「截图在桌面端、云端读不到」——绝不静默返回打不开的路径。
    """
    if not isinstance(result, dict):
        return result
    blobs = result.pop('_image_blobs', None) or []
    images = ([item for item in result.get('images') or [] if isinstance(item, dict)]
              if isinstance(result.get('images'), list) else [])
    folder = Path(home) / 'data/companion/artifacts/browser'
    for blob in blobs:
        if not isinstance(blob, dict) or blob.get('skipped'):
            continue
        try:
            raw = base64.b64decode(str(blob.get('data') or ''), validate=True)
        except (ValueError, TypeError):
            continue
        if not raw or len(raw) > limit:
            continue
        mime = str(blob.get('mime') or 'image/png')
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (uuid.uuid4().hex + '.' + IMAGE_SUFFIX.get(mime, 'img'))
            path.write_bytes(raw)
        except OSError:
            continue
        try:
            index = int(blob.get('index'))
        except (TypeError, ValueError):
            index = -1
        entry = dict(mime=mime, saved=True, path=str(path), bytes=len(raw),
                     cloud_readable=True, source_path=str(blob.get('path') or ''),
                     coordinate_space='页面像素（由浏览器工具决定，可能已缩放）')
        if 0 <= index < len(images):
            images[index] = dict(images[index], **entry)
        else:
            images.append(entry)
    # 桌面端明确说「没回传」的那些：保留路径 + 原因，别让下游以为可读。
    for blob in blobs:
        if not isinstance(blob, dict) or not blob.get('skipped'):
            continue
        try:
            index = int(blob.get('index'))
        except (TypeError, ValueError):
            continue
        if not (0 <= index < len(images)):
            continue
        reason = ('图片太大，没有随回执回传' if blob.get('skipped') == 'too_large'
                  else '桌面端读不到这个文件，没有回传')
        images[index].update(saved=False, cloud_readable=False,
                             desktop_path=str(blob.get('path') or images[index].get('path') or ''),
                             reason=reason)
    if images:
        result['images'] = images
        result['image_paths'] = [str(item.get('path')) for item in images if item.get('saved')]
    return result


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
        """旁路（computer/browser/phone）也走执行器的统一出口。

        这三个 kind 由服务端直接分派给桌面/手机，不经过 ActionExecutor.run；
        以前它们各自补一套 provenance/visibility（或者干脆不补）。现在统一走
        ``run_with_receipt``：来源分级与缺口只有一份实现，不会再漂移。
        """
        from .companion_executor import run_with_receipt
        kind = str((action or {}).get('kind') or '')
        if kind == 'computer':
            return await run_with_receipt(kind, action,
                                          lambda: self.server.control('computer', action))
        if kind == 'browser':
            return await run_with_receipt(kind, action,
                                          lambda: self.server.control('browser', action))
        if kind == 'phone':
            return await run_with_receipt(kind, action,
                                          lambda: self.server.mobile.phone.run(action))
        return await self.original.run(action)
    async def close(self): await self.original.close()


class CompanionServer:
    """One service lifetime, independent of authenticated client lifetimes."""
    METHODS = {'chat', 'snapshot', 'set_active', 'configure_town', 'bilibili_call', 'update_body',
               'observe_body', 'control_project', 'acknowledge_rendered',
               'im_configure', 'im_wechat_login_begin', 'im_wechat_login_poll', 'im_wechat_logout',
               'im_test',
               # 桌面端下拉用：服务商与模型清单（不需要桌面租约）
               'providers',
               # 只读诊断：真实服务此刻认为哪些 kind 可用、不可用的原因是什么。
               # 2026-11-19：没有这个之前，只能靠本地构造 Companion 去猜，测的不是真实服务。
               'executor_kinds'}
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

    # 不该让快照缓存失效的事件类型：它们本身就是快照的结果或纯通知。
    # 2026-11-19：emit 无条件清缓存 + :441 每次 call 后 emit(snapshot) 把 CPU 顶到 96%。
    KEEP_CACHE_EVENTS = ('snapshot', 'remote_result', 'remote_cancel')

    def emit(self, event):
        if event.get('type') not in self.KEEP_CACHE_EVENTS:
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

    def executor_kinds(self):
        """只读：真实 executor 此刻报告的能力，以及每一项不可用的原因。

        2026-11-19 起因：验「她能不能用 computer」时没有可观测入口，
        只能本地构造 Companion 去测——构造出来的 executor 不是服务里的那个，
        于是得到一个看起来合理但与事实无关的答案。
        """
        executor = getattr(self.core, 'executor', None)
        source = 'remote' if type(executor).__name__ == 'RemoteExecutor' else 'local'
        try:
            kinds = list(executor.available_kinds()) if hasattr(executor, 'available_kinds') else ['command']
        except Exception as error:
            return dict(kinds=[], source=source, error=type(error).__name__,
                        desktop_connected=self.peer is not None)
        local_computer = False
        try:
            from .companion_computer import Computer
            local_computer = bool(Computer(self.home).available())
        except Exception:
            local_computer = False

        verified = self._verified_kinds()
        detail = {}
        for kind in kinds:
            how = verified.get(kind)
            detail[kind] = dict(available=True, verified=how is not None,
                                verified_how=how)
        if 'computer' not in kinds:
            if self.peer is None:
                reason = '桌面端未连（/ws 没有活动连接）'
            else:
                reason = '桌面端已连，但执行器未报可用'
            detail['computer'] = dict(available=False, verified=False,
                                      verified_how=None, reason=reason)
        if 'phone' not in kinds:
            try:
                connected = self.mobile.phone.context().get('connected')
            except Exception:
                connected = None
            detail['phone'] = dict(available=False, verified=False, verified_how=None,
                                   reason='手机端未连' if connected is False else '手机状态读取失败')
        # dsh 的边界是适配器事实，不在这里改判——可用就照报，只在没实测过时说清。
        if 'dsh' in detail and not detail['dsh']['verified']:
            detail['dsh']['reason'] = ('adapter 只支持 DeepSeek 官方端点；'
                                       '2026-11-18 实测 401，尚未在本端点跑通过')

        return dict(kinds=kinds, source=source,
                    desktop_connected=self.peer is not None,
                    runtimes=dict(computer_remote=self.peer is not None,
                                  computer_local=local_computer),
                    detail=detail)

    def _verified_kinds(self):
        """哪些 kind 真的留下过成功回执。

        判据只认台账：companion_actions 里该 kind 有 completed 记录。
        这不证明「每次都成功」，只证明「至少真的跑通过一次」——
        比「executor 认为可用」强一档，比「刚才试通了」弱一档，所以单独一列。
        """
        agent_id = getattr(getattr(self.core, 'life', None), 'agent_id', None)
        if not agent_id:
            return {}
        out = {}
        try:
            rows = self.core.repo._all(
                "SELECT action, status, updated FROM companion_actions "
                "WHERE agent=? AND status IN ('completed','succeeded') "
                "ORDER BY updated DESC LIMIT 500", (agent_id,))
        except Exception:
            return {}
        for row in rows:
            try:
                kind = json.loads(row['action'] or '{}').get('kind')
            except Exception:
                continue
            if not kind or kind in out:
                continue
            # status='completed' 只说明这条动作跑完了，不说明它做成了。
            # 2026-11-19：dsh 有两条 completed，result 里 return_code=0，
            # 内容却是「本机没有 ADB 通道」「无法核实」——那是没做成。
            try:
                body = json.loads(row['result'] or '{}')
            except Exception:
                body = {}
            if isinstance(body, dict):
                code = body.get('return_code', body.get('exit_code'))
                if isinstance(code, (int, float)) and code != 0:
                    continue
                inner = str(body.get('status') or '')
                if inner in ('failed', 'error', 'timeout'):
                    continue
            # 只说清它证明了什么：这个 kind 至少跑完过一次且没有非零退出。
            # 它不证明某次具体任务做成了（dsh 有两次 completed 内容是「没有 ADB」）。
            out[kind] = ('台账里有跑完且无非零退出的记录；'
                         '这不等于那一次的任务做成了（最近一次 %s）') % time.strftime(
                '%Y-%m-%d %H:%M', time.localtime(float(row['updated'] or 0)))
        return out

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
        if method=='executor_kinds': return self.executor_kinds()
        if method=='providers':
            # 桌面端只做「选服务商 → 选模型 → 填 key」，所以这里只给这些。
            # 不带 api_key、不带内部判据；空 models + needs_manual_model 表示要手填。
            from . import providers as provider_catalog
            return dict(providers=provider_catalog.catalog())
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
        # 通用分发：`control_project`、`configure_town` 这类是同步方法，
        # 以前在这里无条件 await，结果桌面一暂停/恢复项目就只拿到
        # `TypeError: object dict can't be used in 'await' expression`——
        # 项目管理明明是普通接口，却因为分发写死成异步而不可用。
        result = getattr(self.core, method)(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    async def rpc(self, request):
        method=None
        try:
            m=await request.json(); method=m.get('method'); result=await self.invoke(method,m.get('args',[]))
            return web.json_response(dict(ok=True,result=result))
        except Exception as error:
            # IM 通道的失败通常是主人刚填的地址/开关不对，说清楚才有用；其它操作仍只报类型。
            # 失败一律落日志、带完整堆栈：只回类型名的话，下次还是查不到根因
            # （2026-11-18 一次瞬时 ValueError 就是这么丢掉的）。
            # **脱敏**：只记方法名和异常本身，不记 headers、请求体、token 或密钥。
            # 调用方拿到的仍然只有类型名，堆栈不出服务端。
            log.exception('rpc %s 失败', method)
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
        # 2026-11-19 止血：桌面端连着时每次 call 后都发全量 snapshot，
        # 而 snapshot() 里 _projects 被重复计算 5 次以上（每次 2ms 全表查询），
        # 事件循环被占满，/alive 超时、CPU 99%。先改成不发快照——
        # 桌面端自己有增量状态，不需要每次全量推送。
        if not getattr(self, 'SKIP_SNAPSHOT_EMIT', True):
            self.emit(dict(type='snapshot',snapshot=self.snapshot(include_image_data=True)))
        async def call(m):
            try:
                result=await self.invoke(m.get('method'),m.get('args',[]),desktop=True)
                if self.peer is peer:
                    self.emit(dict(type='remote_result',id=m.get('id'),ok=True,result=result))
                    # 同上：每次 call 后的全量快照也是热路径，先不发。
                    if not getattr(self, 'SKIP_SNAPSHOT_EMIT', True):
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
                        # 浏览器截图在桌面主机上：把回传的字节落成云端副本，
                        # 没回传就保持桌面路径（统一出口会写明云端读不到）。
                        result=adopt_remote_images(self.home,result)
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
        # 浏览器适配器是可选的：没装就保持 None，execute() 会如实说不可用。
        try:
            from .companion_browser_adapter import BrowserAdapter
            self.browser=BrowserAdapter(self.home)
        except Exception:
            self.browser=None
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
            elif m['kind']=='browser':
                # 浏览器走本机适配器（MCP stdio），只读流程回放与页面原语都在这边。
                # 适配器没装或没起来时如实说不可用，不退回别的通道。
                browser = getattr(self, 'browser', None)
                if browser is None:
                    result = dict(status='failed', failed=True,
                                  text='本机没有浏览器适配器：这一端没装 browser 能力，不是网页打不开。')
                else:
                    result = await browser.run(m['args'])
                # 截图落在**本机**磁盘上，云端进程读不到：把字节一起回传（与 computer 同一套）。
                if isinstance(result, dict):
                    blobs = browser_image_blobs(result)
                    if blobs:
                        result['_image_blobs'] = blobs
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
