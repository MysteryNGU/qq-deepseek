"""Independent character deployments owned by the local settings service.

Only public program files are copied. Memories, private skills, media and DSH
homes are never cloned from the primary deployment or another character.
"""
import json
import os
from pathlib import Path
import re
import shutil
import threading
import tempfile
import time
from urllib.parse import urlsplit

from .persona import compile_persona
from .persona_files import PersonaFiles
from .role_config import Config,load_config
from .settings import SettingsError,SettingsStore,atomic_write,encode_toml,fingerprint,restore,sanitize

IDENTIFIER = re.compile(r'[a-z][a-z0-9_-]{0,39}\Z')
EDITABLE = {'name','bot_qq','owner','ws_url','ws_token','persona'}
IDENTITY_FIELDS = {'name','bot_qq','owner','ws_url','ws_token','life_db','life_agent','persona',
                   'public_persona','unified_persona','dsh_home','dsh_tmp'}
BUILTIN_CHARACTERS = (('xiaoyu','原始小鱼','xiaoyu.md'),('kafka','卡夫卡','kafka.md'),('firefly','流萤','firefly.md'))
STATES = dict(stopped='已停止',starting='启动中',observed='运行中',evidence_pending='运行中',
              channel_offline='QQ 未连接',restarting='恢复中',blocked='启动失败，等待重试',
              failed='启动失败',observing='更新中',rolled_back='已回滚')
STATES.update(stopping='停止中',stop_failed='停止未完成')


def endpoint(value):
    try:
        url=urlsplit(value)
        if url.scheme not in {'ws','wss'} or not url.hostname or url.username or url.password:
            raise ValueError()
        host=url.hostname.lower()
        if host=='localhost':host='127.0.0.1'
        return (url.scheme,host,url.port or (443 if url.scheme=='wss' else 80),url.path.rstrip('/'),url.query)
    except (TypeError,ValueError):
        raise SettingsError('invalid_ws','QQ 连接地址需要 ws:// 或 wss:// 地址') from None


class CharacterManager:
    def __init__(self,root,*,runner_factory=None,monitor=True):
        self.root=Path(root).resolve()
        self.folder=self.root/'data/characters';self.folder.mkdir(parents=True,exist_ok=True)
        self.registry=self.folder/'registry.json'
        self.lock=threading.RLock();self.runners={};self.states={};self.closed=threading.Event()
        self.runner_factory=runner_factory or self._runner
        self.thread=None
        if monitor:
            self.thread=threading.Thread(target=self._monitor,name='character-supervisor',daemon=True)
            self.thread.start()

    def _read(self):
        return json.loads(self.registry.read_text('utf-8')) if self.registry.exists() else {}

    def _write(self,records):atomic_write(self.registry,json.dumps(records,ensure_ascii=False,indent=2))

    def deployment(self,identity):
        if not isinstance(identity,str) or not IDENTIFIER.fullmatch(identity):
            raise SettingsError('invalid_character','人物编号只允许小写字母、数字、下划线和短横线')
        path=self.folder/identity
        if any(p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (path,self.folder)):
            raise SettingsError('invalid_path','人物目录不能经过链接')
        return path

    def store(self,identity):
        if identity not in self._read():raise SettingsError('not_found','没有这个人物',404)
        return SettingsStore(self.deployment(identity))

    def templates(self):
        choices={'blank':'# 新人物 · 统一人格卡\n\n## 我是谁\n\n## 我的个性\n\n## 我怎么说话\n\n## 面对主人\n\n## 对主人说话的样子\n\n## 对主人的想法\n'}
        for key,_label,name in BUILTIN_CHARACTERS:
            path=self.root/'personas'/name
            if key=='kafka' and not path.exists():path=self.root/'personas/kafka-style.md'
            if path.is_file() and not path.is_symlink():choices[key]=path.read_text('utf-8')
        return choices

    def register_templates(self):
        """Register saved cards without inventing QQ bindings or starting them."""
        with self.lock:
            templates=self.templates();owner=SettingsStore(self.root).raw().get('owner',0)
            if type(owner) is not int or owner<=0:
                raise SettingsError('invalid_owner','先配置主人 QQ，再登记人物')
            for identity,name,_file in BUILTIN_CHARACTERS:
                if identity in templates and identity not in self._read():
                    self.create(dict(id=identity,name=name,owner=owner,bot_qq=0,
                        ws_url='',ws_token='',persona=templates[identity]))

    @staticmethod
    def missing_connection(raw):
        return [label for key,label in (('bot_qq','人物 QQ'),('ws_url','QQ 连接地址')) if not raw.get(key)]

    def snapshot(self):
        with self.lock:
            result=[]
            order={identity:i for i,(identity,_name,_file) in enumerate(BUILTIN_CHARACTERS)}
            for identity,record in sorted(self._read().items(),key=lambda item:order.get(item[0],len(order))):
                store=self.store(identity);raw=store.raw();path=store.root/'persona.md'
                state=self.states.get(identity,'starting' if record['wanted'] else 'stopped')
                missing=self.missing_connection(raw)
                if missing and not record['wanted']:state='unconfigured'
                status=store.status()
                if not record['wanted']:status.update(online=False,qq_connected=False,cognition_ready=False)
                result.append(dict(id=identity,name=raw['name'],bot_qq=raw['bot_qq'],owner=raw['owner'],
                    ws_url=raw['ws_url'],ws_token=sanitize(raw.get('ws_token',''),'ws_token'),
                    persona=path.read_text('utf-8'),revision=fingerprint(store.config_path),
                    persona_revision=fingerprint(path),wanted=record['wanted'],process_owned=identity in self.runners,state=state,
                    state_label='待配置 QQ' if state=='unconfigured' else STATES.get(state,state),
                    ready=not missing,missing_fields=missing,status=status,
                    storage_bytes=sum(p.stat().st_size for p in (store.root/'data').glob('life.db*') if p.is_file())))
            return dict(characters=result,templates=self.templates(),
                        primary_retired=(self.folder/'primary-retired.json').is_file())

    def _check_connections(self,identity,values):
        target=endpoint(values['ws_url']) if values.get('ws_url') else None
        for other in self._read():
            if other==identity:continue
            raw=self.store(other).raw()
            if (values.get('bot_qq') and raw['bot_qq']==values['bot_qq']) or (target and raw.get('ws_url') and endpoint(raw['ws_url'])==target):
                raise SettingsError('account_conflict','这个 QQ 或连接地址已经绑定其他人物',409)
        primary=SettingsStore(self.root).raw()
        if not (self.folder/'primary-retired.json').is_file() and (
            (target and endpoint(primary.get('ws_url') or Config.ws_url)==target) or
            (values.get('bot_qq') and primary.get('bot_qq',0)==values['bot_qq'])):
            raise SettingsError('account_conflict','这个 QQ 或连接地址属于原有主实例，请使用独立 QQ 连接',409)

    def _validate(self,identity,values):
        if set(values)!=EDITABLE:raise SettingsError('invalid_character','人物配置字段不完整或包含未知字段')
        if not isinstance(values['name'],str) or not 1<=len(values['name'].strip())<=80:
            raise SettingsError('invalid_name','人物名称需要 1～80 个字符')
        for key in ('bot_qq','owner'):
            if type(values[key]) is not int or values[key]<(1 if key=='owner' else 0):
                raise SettingsError('invalid_qq','主人 QQ 需要正整数，人物 QQ 可留空待配置')
        if not isinstance(values['ws_url'],str):raise SettingsError('invalid_ws','QQ 连接地址格式不正确')
        if not isinstance(values['ws_token'],str) or len(values['ws_token'])>4096:
            raise SettingsError('invalid_token','QQ 密钥格式不正确')
        if not isinstance(values['persona'],str) or not 1<=len(values['persona'].encode())<=180000:
            raise SettingsError('invalid_persona','人格卡不能为空，最大 180 KB')
        PersonaFiles.validate(values['persona'])
        compile_persona(Path('persona.md'),source_text=values['persona'])
        self._check_connections(identity,values)

    def _copy_program(self,destination):
        # Each deployment has its own source tree so root-relative locks, release
        # controllers, settings, generated skills and logs cannot collide.
        suffixes={'.py','.json','.js','.cjs','.html','.css','.toml','.txt'}
        for directory in ('life','tests'):
            source=self.root/directory
            for path in source.rglob('*'):
                if not path.is_file() or path.suffix not in suffixes or '__pycache__' in path.parts:continue
                if not path.resolve().is_relative_to(source.resolve()) or any(
                    p.is_symlink() or getattr(p,'is_junction',lambda:False)() for p in (path,*path.parents) if p.is_relative_to(source)):
                    continue
                target=destination/path.relative_to(self.root);target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(path,target)
        for relative in ('bot.py','supervise.py','lifesupervisor.py','requirements.txt','scripts/codex_consult.py'):
            source=self.root/relative
            if source.is_file() and not source.is_symlink():
                target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)

    def create(self,body):
        with self.lock:
            identity=body.get('id');destination=self.deployment(identity)
            records=self._read()
            if identity in records or destination.exists():raise SettingsError('exists','这个人物编号已经存在',409)
            values={k:body.get(k) for k in EDITABLE}
            self._validate(identity,values)
            primary=SettingsStore(self.root).raw()
            port=primary.get('local_embedding',{}).get('port',8231)
            used={self.store(other).raw().get('local_embedding',{}).get('port') for other in records
                  if self.store(other).raw().get('local_embedding',{}).get('enabled')}
            while port in used:port+=1
            with tempfile.TemporaryDirectory(prefix='.create-',dir=self.folder) as temporary:
                stage=Path(temporary)/'deployment'
                self._copy_program(stage)
                from .character_defaults import materialize,relocate_supporting_files
                config=materialize(self.root,stage,identity,embedding_port=port)
                config.update({k:values[k] for k in EDITABLE if k!='persona'})
                relocate_supporting_files(stage,destination,config)
                store=SettingsStore(stage)
                atomic_write(stage/'config.toml',store.validate('config',config))
                atomic_write(stage/'persona.md',values['persona'])
                os.replace(stage,destination)
            records[identity]=dict(wanted=False,created=time.time())
            self._write(records)
            return dict(created=True,id=identity)

    def update(self,identity,body):
        with self.lock:
            store=self.store(identity)
            if identity in self.runners or self._read()[identity]['wanted']:
                raise SettingsError('running','先停止这个人物，再修改 QQ 绑定或人格卡',409)
            path=store.root/'persona.md'
            if body.get('revision')!=fingerprint(store.config_path) or body.get('persona_revision')!=fingerprint(path):
                raise SettingsError('conflict','人物已在别处修改，请重新载入',409)
            values={k:body.get(k) for k in EDITABLE}
            values['ws_token']=restore(values['ws_token'],store.raw().get('ws_token',''))
            self._validate(identity,values)
            raw=store.raw();raw.update({k:v for k,v in values.items() if k!='persona'})
            text=store.validate('config',raw)
            # Keep both old bodies for a failed write; no memories are touched.
            old=path.read_text('utf-8')
            atomic_write(path,values['persona'])
            try:atomic_write(store.config_path,text)
            except BaseException:
                atomic_write(path,old);raise
            return dict(saved=True,id=identity)

    def save_settings(self,identity,body):
        with self.lock:
            if body.get('document')=='config' and set(body.get('changes') or {}) & IDENTITY_FIELDS:
                raise SettingsError('read_only','QQ 绑定和人物身份请在人物管理中停止后编辑')
            return self.store(identity).save(body.get('document'),body.get('base_revision'),body.get('changes'))

    def _runner(self,identity):
        from .release import ReleaseController,NativeWorker
        from .supervisor import HostSupervisor
        store=self.store(identity)
        config=load_config(store.config_path)
        env=dict(os.environ)
        for key in ('QQ_WS_URL','DEEPSEEK_API_KEY'):env.pop(key,None)
        return HostSupervisor(ReleaseController(store.root,store.root/'data/life-control',NativeWorker()),
            store.config_path,startup_timeout=180 if config.local_embedding.get('enabled') else 60,environment=env,quiet=True)

    def action(self,identity,action):
        with self.lock:
            store=self.store(identity);records=self._read()
            if action=='start':
                if identity in self.runners:return dict(queued=True,message='这个人物已经启动')
                raw=store.raw()
                missing=self.missing_connection(raw)
                if missing:raise SettingsError('not_configured','请先填写：'+'、'.join(missing),409)
                self._check_connections(identity,raw)
                if raw.get('life_agent')!=identity or raw.get('life_db')!='data/life.db' or raw.get('dsh_home')!='data/dsh-home':
                    raise SettingsError('identity_mismatch','人物存储身份发生变化，停止启动')
                runner=self.runner_factory(identity)
                runner.start();self.runners[identity]=runner;self.states[identity]='starting'
                records[identity]['wanted']=True
                try:self._write(records)
                except BaseException:
                    runner.stop();self.runners.pop(identity,None);raise
                return dict(queued=True,message='人物已启动，等待 QQ 与认知服务就绪')
            if action=='stop':
                records[identity]['wanted']=False;self._write(records)
                runner=self.runners.get(identity)
                self.states[identity]='stopping'
                try:
                    if runner:runner.stop()
                except Exception:
                    self.states[identity]='stop_failed'
                    raise SettingsError('stop_failed','停止尚未确认，请重试；原进程归属仍保留',409) from None
                self.runners.pop(identity,None)
                self.states[identity]='stopped'
                return dict(stopped=True,message='这个人物已停止，记忆保留')
            if action=='restart':
                if identity not in self.runners:raise SettingsError('offline','这个人物尚未启动',409)
                return store.request_restart()
            raise SettingsError('invalid_action','只支持启动、停止或重启')

    def tick(self):
        with self.lock:
            for identity,record in self._read().items():
                if not record['wanted']:continue
                try:
                    if identity not in self.runners:
                        self.action(identity,'start')
                    runner=self.runners[identity]
                    self.states[identity]=runner.tick()
                    if getattr(runner,'restart_controller',False):
                        runner.stop();self.runners.pop(identity,None)
                except Exception:
                    self.states[identity]='failed'  # Never expose exception text containing credentials.

    def _monitor(self):
        while not self.closed.wait(2):
            try:self.tick()
            except Exception:pass

    def close(self):
        self.closed.set()
        if self.thread:self.thread.join(5)
        with self.lock:
            for runner in self.runners.values():runner.stop()
            self.runners.clear()
