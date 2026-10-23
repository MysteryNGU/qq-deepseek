"""OneBot transport and lifecycle for the sole DSH role cognition."""
from __future__ import annotations

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time
import uuid
import hashlib

import websockets

from .asr import WhisperASR
from .expression import Expressions
from .media import MediaService,accept_onebot,onebot_segments
from .role_agent import RoleAgent
from .runtime import LifeConfig,LifeRuntime
from .drives import DriveConfig
from .services.dsh_bridge import DshBridge
from .voice_catalog import VoiceCatalog

log = logging.getLogger(__name__)
dialogue = logging.getLogger('dialogue')
ROLE_RUNTIME_VERSION = '2026.10.23.7'


def runtime_version():
    return ROLE_RUNTIME_VERSION


def source_signature(root):
    checksum = hashlib.sha256()
    for path in sorted([*root.glob('*.py'),*(root/'life').rglob('*.py')]):
        if not path.is_symlink() and root in path.resolve().parents:
            checksum.update(path.relative_to(root).as_posix().encode()+b'\0'+path.read_bytes())
    return checksum.hexdigest()


def setup_logging(root):
    folder = Path(root)/'data'
    folder.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s',
        handlers=[logging.StreamHandler(),RotatingFileHandler(folder/'runtime.log',maxBytes=8*1024*1024,
                                                            backupCount=2,encoding='utf-8')],force=True)
    dialogue.handlers.clear()
    handler = RotatingFileHandler(folder/'conversation.log',maxBytes=8*1024*1024,backupCount=2,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    dialogue.addHandler(handler)
    dialogue.propagate = False


class RoleHost:
    def __init__(self,cfg,root,*,transport=None,bridge=None):
        self.cfg,self.root = cfg,Path(root).resolve()
        behavior_path = self.root/'life-dynamics.json'
        behavior = json.loads(behavior_path.read_text('utf-8')) if behavior_path.is_file() else {}
        self.life = LifeRuntime(self.root/cfg.life_db,cfg.life_agent,
            LifeConfig(drives=DriveConfig.from_mapping(cfg.life_dynamics),relationship_owner=str(cfg.owner),relations=behavior.get('relations')))
        self.life.runtime_info = dict(version=runtime_version(),source_signature=source_signature(self.root))
        from .persona import ensure_audience_sections
        ensure_audience_sections(self.root/cfg.persona,self.root/'personas/public.md')
        self.life.persona.activate(self.root/cfg.persona)
        self.bridge = bridge or DshBridge(cfg,self.root)
        self.ws = None
        self._rpc = {}
        self._tasks = set()
        self.stop = asyncio.Event()
        self.exit_code = 0
        self.boot_id = uuid.uuid4().hex
        self.boot_ready = False
        self.boot_event_id = None
        self.accept_after = time.time()
        self.started_role = False
        self.media = MediaService(self.life,self.root/'data'/'media',transport or self.call,
            local_roots=[self.root/'napcat',self.root/'napcat-onekey',*cfg.media_local_roots],
            max_bytes=cfg.media_max_mb*1024*1024,
            asr=WhisperASR(self.root/'tools'/'asr-models',cfg.asr_model,cfg.asr_language))
        self.voices = VoiceCatalog(self.life,self.media)
        self.expressions = Expressions(self.life,self.media,self.voices,transport or self.call,
            typing_cps=cfg.typing_chars_per_second,paste_threshold=cfg.paste_threshold)
        self.role = RoleAgent(self.life,self.bridge,cfg,self.root,self.send,self.media,self.voices)
        self.life.delivery.recover()
        self._health_baseline = self.life.repo._one('SELECT COUNT(*) AS n FROM life_calls')['n']

    async def send(self,scope,logical,proposal,sources):
        if not self.boot_ready and (scope.conversation_id!='qq:private:'+str(self.cfg.owner) or self.boot_event_id not in sources):
            from .agent_session import SessionFailure
            raise SessionFailure('input_pending','启动时先处理启动事件，自然打招呼；不补回以前未读消息')
        result = await self.expressions(scope,logical,proposal,sources)
        if result.get('sent_confirmed'):
            text = proposal if isinstance(proposal,str) else proposal.get('text') or json.dumps(proposal.get('parts'),ensure_ascii=False)
            # Each confirmed segment is already an experience. This is only a log marker.
            with self.life.repo.tx():
                key = 'expression_logged:'+logical
                created = not self.life.repo._one('SELECT 1 FROM meta WHERE k=?',(key,))
                self.life.repo._exec('INSERT OR IGNORE INTO meta(k,v) VALUES(?,?)',(key,'true'))
            if created:
                dialogue.info('[她][%s] %s',scope.conversation_id,text)
            if not self.boot_ready and scope.conversation_id=='qq:private:'+str(self.cfg.owner):
                self.boot_ready = True
                self.life.startup_greeted = True
                self.accept_after = time.time()
                log.info('启动招呼已证实送达；现在接收新消息')
        return result

    async def call(self,action,params):
        if self.ws is None:
            raise ConnectionError('onebot_disconnected')
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._rpc[echo] = future
        try:
            await self.ws.send(json.dumps(dict(action=action,params=params,echo=echo),ensure_ascii=False))
            return await asyncio.wait_for(future,30)
        finally:
            self._rpc.pop(echo,None)

    def spawn(self,coroutine,name,*,report=True):
        task = asyncio.create_task(coroutine,name=name)
        self._tasks.add(task)
        def done(result):
            self._tasks.discard(result)
            if not result.cancelled() and result.exception() and report:
                log.error('%s 未完成',name,exc_info=(type(result.exception()),result.exception(),result.exception().__traceback__))
        task.add_done_callback(done)
        return task

    def allowed(self,raw):
        actor = int(raw.get('user_id') or 0)
        if raw.get('message_type')=='group':
            return int(raw.get('group_id') or 0) in self.cfg.allow_groups
        return actor==self.cfg.owner or actor in self.cfg.friends or self.cfg.allow_all_private

    async def receive(self,raw):
        if raw.get('post_type')!='message' or not self.allowed(raw):
            return
        if float(raw.get('time') or 0)<int(self.accept_after):
            return
        text = ''.join(str(p.get('data',{}).get('text','')) for p in onebot_segments(raw.get('message',[])) if p.get('type')=='text')
        owner = int(raw['user_id'])==self.cfg.owner and raw.get('message_type')!='group'
        if owner and text.strip() in {'/重启','/状态','/清空记忆'}:
            await self.command(text.strip(),raw)
            return
        if not self.boot_ready:
            return
        event,created = accept_onebot(self.life,raw)
        if not created:
            return
        dialogue.info('[收到][%s][%s] %s',event.scope.conversation_id,event.actor_id,event.text or '[媒体消息]')
        await self.role.ingest(event)

    async def command(self,text,raw):
        event,created = accept_onebot(self.life,raw)
        if not created:
            return
        if text=='/状态':
            body = '运行中；认知：DSH；数据库：life.db。'
        elif text=='/清空记忆':
            binding = self.role.sessions.binding(event.scope)
            await asyncio.to_thread(self.bridge.session_control,binding['session_id'],binding['generation'])
            sessions = [r['session_id'] for r in self.life.repo._all('SELECT session_id FROM agent_sessions WHERE scope_id=?',(event.scope.scope_id,))]
            ids = [r['id'] for r in self.life.repo._all('SELECT id FROM events WHERE agent_id=? AND conversation_id=?',event.scope.private_key)]
            identity = self.life.tombstones.append(event.scope,[],ids)
            self.life.tombstones.apply(identity,event.scope,[],ids)
            self.role.sessions.retire(event.scope)
            await asyncio.to_thread(self.bridge.purge_sessions,sessions)
            await asyncio.to_thread(self.bridge.hello)
            await self.role.recover()
            event,_ = self.life.ingest(event.scope,'command_result','inference','清空完成')
            body = '当前私聊记忆已清空。'
        else:
            body = '正在重启。'
        await self.expressions(event.scope,'command:'+event.id,dict(text=body),[event.id])
        if text=='/重启':
            self.exit_code = 42
            self.stop.set()

    async def startup(self):
        if self.started_role:
            return
        self.started_role = True
        try:
            await self.role.start()
            log.info('主角色已启动 provider=%s model=%s profile=sdk persona=%s',
                     self.bridge.provider,self.bridge.model,self.life.persona.current().digest)
            scope = self.life.scope('qq:private:'+str(self.cfg.owner),str(self.cfg.owner))
            event,_ = self.life.ingest(scope,'startup','inference',
                '程序刚启动。请以自己的自然语气跟主人打个招呼。启动招呼以前的消息已忽略，不追补未读。',
                external_id='startup:'+self.boot_id)
            self.boot_event_id = event.id
            self.life.startup_event_id = event.id
            self.life.startup_greeted = False
            await self.role.ingest(event)
            self.role.schedule(scope,None,self.role.resources.idle_base_seconds,'空闲时觉察自己的状态与想法，自行选择接下来做什么，也可以休息。',kind='idle_probe')
        except BaseException:
            self.started_role = False
            raise

    async def health(self):
        target = os.environ.get('QQ_WORKER_HEALTH')
        next_warning = 0
        while not self.stop.is_set():
            if target:
                try:
                    completed = self.life.repo._one("SELECT COUNT(*) AS n FROM life_calls WHERE json_extract(receipt,'$.ok')=1")['n']
                    path = Path(target)
                    path.parent.mkdir(parents=True,exist_ok=True)
                    temporary = path.with_suffix('.writing')
                    temporary.write_text(json.dumps(dict(token=os.environ.get('QQ_WORKER_TOKEN',''),pid=os.getpid(),
                        at=time.time(),completed_turns=max(0,completed-self._health_baseline),
                        channel_connected=self.ws is not None,cognition_ready=self.role.running,
                        runtime=self.life.runtime_info)),encoding='utf-8')
                    temporary.replace(path)
                    next_warning = 0
                except Exception:
                    # Windows readers can briefly deny replacement. Keep the
                    # heartbeat task alive and retry; a prolonged failure still
                    # expires normally in the independent supervisor.
                    if time.monotonic()>=next_warning:
                        log.warning('健康状态暂未写入，下轮重试',exc_info=True)
                        next_warning = time.monotonic()+60
            await asyncio.sleep(2)

    async def run(self):
        self.spawn(self.health(),'worker-health')
        failures,next_report = 0,0
        try:
            while not self.stop.is_set():
                reader,stopped = None,None
                try:
                    headers = {'Authorization':'Bearer '+self.cfg.ws_token} if self.cfg.ws_token else None
                    async with websockets.connect(self.cfg.ws_url,additional_headers=headers,proxy=None,max_size=32*1024*1024) as ws:
                        self.ws = ws
                        log.info('QQ通道已%s','恢复连接' if failures else '连接')
                        failures,next_report = 0,0
                        reader = self.spawn(self.read(ws),'onebot-read',report=False)
                        startup = self.spawn(self.startup(),'role-startup')
                        stopped = asyncio.create_task(self.stop.wait())
                        done,_ = await asyncio.wait([reader,startup,stopped],return_when=asyncio.FIRST_COMPLETED)
                        if startup in done:
                            await startup
                            done,_ = await asyncio.wait([reader,stopped],return_when=asyncio.FIRST_COMPLETED)
                        if reader in done:
                            await reader
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    failures += 1
                    if time.monotonic()>=next_report:
                        log.warning('QQ通道暂不可用，保留已接收输入，继续重连：[%s] %s',
                                    type(error).__name__,str(error)[:160],exc_info=failures==1)
                        next_report = time.monotonic()+60
                finally:
                    waits = [task for task in (reader,stopped) if task is not None]
                    for task in waits:
                        task.cancel()
                    await asyncio.gather(*waits,return_exceptions=True)
                    self.ws = None
                    for future in self._rpc.values():
                        if not future.done():
                            future.set_exception(ConnectionError('onebot_disconnected'))
                if not self.stop.is_set():
                    await asyncio.sleep(min(15,3*2**min(max(0,failures-1),3)))
        finally:
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*list(self._tasks),return_exceptions=True)
            await self.role.close()
            self.life.close()
        return self.exit_code

    async def read(self,ws):
        async for data in ws:
            try:
                raw = json.loads(data)
                echo = raw.get('echo')
                if echo in self._rpc:
                    future = self._rpc[echo]
                    if not future.done():
                        future.set_result(raw)
                elif raw.get('post_type')=='message':
                    self.spawn(self.receive(raw),'onebot-input')
            except Exception:
                log.exception('平台事件解析失败')


# Bound methods cannot carry instance attributes; advertise plan support on the
# function so RoleAgent sees it through the bound method as well.
RoleHost.send.accepts_plan = True
