"""Scoped background media jobs over the existing WorkQueue and MediaService."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import secrets
import time

from .agent_session import SessionFailure, canonical, digest
from .contracts import Scope
from .local_media_config import LocalMediaConfig
from .media import validate_generated_media
from .media_process import MediaProcess
from .text import stable_id
from .media_adapters import AdapterRegistry,validate_frozen,frozen_hash

log = logging.getLogger(__name__)
JOB_KIND = 'local_media'
TERMINAL = {'done','failed','cancelled','unknown','quarantined'}


class LocalMedia:
    def __init__(self,life,media,voices,cfg,root,*,notify=None,backend=None):
        self.life,self.repo,self.jobs = life,life.repo,life.jobs
        self.media,self.voices,self.cfg = media,voices,cfg
        self.settings = LocalMediaConfig.parse(getattr(cfg,'local_media',None))
        self.config = self.settings.resolve(root)
        self.adapters=AdapterRegistry(self.config,root)
        self.voices.adapters=self.adapters
        self.root = Path(self.config['root'])
        self.project = Path(root).resolve()
        if self.project not in self.root.parents:
            raise ValueError('local_media_runtime_must_be_inside_project')
        self.notify = notify
        self.backend = backend or MediaProcess(self.config)
        self.task = None
        self.wake = asyncio.Event()
        self.active = None
        self.closing = False
        self.lock_file = None
        self.blocked_reason = None
        self.initiative = None
        self._resource_checked_at,self._resource_wait = -float('inf'),None
        self.resource_clock = time.monotonic
        self.voices.local_enabled = self.settings.enabled

    def capability(self,scope):
        return dict(enabled=self.settings.enabled,tool='life_media',actions=['submit','status','cancel'],
            kinds=['image','speech'],default_voice=self.voices.default(scope),gpu='serial',
            result='结果返回当前角色，不自动发送',read_kind='media_jobs',blocked_reason=self.blocked_reason,
            provider_refs=[i['ref'] for i in self.catalog(scope)])

    def import_presets(self,scope):
        path = Path(self.config['preset_manifest']) if self.config['preset_manifest'] else None
        if not self.settings.enabled or path is None:
            return
        if not path.is_file():
            raise ValueError('configured_voice_preset_manifest_missing')
        manifest = json.loads(path.read_text(encoding='utf-8'))
        provider=self.adapters.resolve('speech',manifest.get('provider_id'))
        for preset in manifest['presets']:
            import_key = 'local_media_preset_import:'+digest(dict(path=str(path),id=preset['id'],
                revision=preset['revision'],reference=preset['reference']['sha256'],delivery=preset['delivery']))
            if self.repo._one('SELECT 1 FROM meta WHERE k=?',(import_key,)):
                continue  # A forgotten reference is not silently reimported at every restart.
            event,_ = self.life.ingest(scope,'voice_preset_import','tool_observation','从本机已生成的参考与试听登记私人音色。',
                external_id='voice-preset:'+digest(dict(id=preset['id'],revision=preset['revision'],
                    reference=preset['reference']['sha256'],delivery=preset['delivery'])),
                payload=dict(voice_id=preset['id'],revision=preset['revision'],source=preset['reference'].get('source')))
            assets = {}
            for part in ('reference','sample'):
                item = preset[part]
                source = Path(item['path']).resolve()
                if self.project not in source.parents or source.is_symlink():
                    raise ValueError('voice_preset_material_outside_project')
                data = source.read_bytes()
                if len(data)>self.media.max_bytes or hashlib.sha256(data).hexdigest()!=item['sha256']:
                    raise ValueError('voice_preset_material_digest_mismatch')
                mime = validate_generated_media(data,'audio')
                assets[part] = self.media.register(scope,event.id,part,'audio',data,mime,
                    dict(voice_id=preset['id'],revision=preset['revision'],purpose=part))
            self.voices.register(dict(id=preset['id'],name=preset['name'],revision=preset['revision'],
                engine=provider['description']['engine'],provider_id=provider['provider_id'],engine_version=provider['model_version'],
                reference_asset_id=assets['reference']['id'],parameters=preset['delivery'],
                source=dict(kind='synthetic_reference',description=preset['reference'].get('source'),
                    source_voice=preset.get('source_voice'))),scope)
            with self.repo.tx():
                self.voices.verified_local(scope,preset['id'],preset['revision'],assets['sample']['id'])
                self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',(import_key,scope.scope_id))
        default = manifest.get('default_voice_id')
        choice_key = 'local_media_default_selection:'+digest(str(path))
        choice = digest(dict(id=default,revision=manifest.get('default_voice_revision')))
        old = self.repo._one('SELECT v FROM meta WHERE k=?',(choice_key,))
        if default and (old is None or old['v']!=choice):
            with self.repo.tx():
                voice = self.voices.require(default,scope=scope)
                if voice['revision']!=manifest['default_voice_revision']:
                    raise ValueError('default_voice_revision_mismatch')
                self.voices.set_default(scope,default,owner=True)
                self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',(choice_key,choice))
        manifest.update(production_catalog_imported=True,default_activation='active',catalog_scope=scope.scope_id)
        from .media_worker import atomic_json
        atomic_json(path,manifest)

    def sources_valid(self,scope,sources):
        if not sources:
            raise SessionFailure('invalid_input','生成任务需要真实来源事件')
        for eid in sources:
            event = self.repo.get_event(eid)
            if not event or event.redacted_at or event.scope.scope_id!=scope.scope_id:
                raise SessionFailure('stale_context','生成任务来源已失效或不属于当前会话')

    def prepare(self,scope,p):
        kind=p.get('kind')
        if kind not in {'image','speech'}:
            raise SessionFailure('invalid_input','生成类型为 image 或 speech')
        allowed={'action','kind','prompt','width','height','seed','reference_asset_ids','voice_id','voice_revision',
            'text','delivery','segments','intent_ref','base_revision','sources','handled_input_ids',
            'activity_changes','appraisal','provider_id','provider_options'}
        if set(p)-allowed:
            raise SessionFailure('invalid_input','存在不支持的媒体参数',fields=sorted(set(p)-allowed))
        seed=p.get('seed')
        if seed is not None and (type(seed) is not int or not 0<=seed<2**32):
            raise SessionFailure('invalid_input','seed 需要为 0～2^32-1 的整数')
        voice=None
        assets=dict(references=[])
        if kind=='speech':
            selected=self.voices.require(p['voice_id'],False,scope) if p.get('voice_id') else self.voices.default_record(scope)
            if selected:
                voice=self.voices.local_record(selected['id'],p.get('voice_revision',selected['revision']),scope)
                provider=self.adapters.resolve(kind,p.get('provider_id'),voice)
                if (voice['engine']!=provider['description']['engine'] or voice['engine_version']!=provider['model_version'] or
                        voice.get('provider_digest',provider['digest'])!=provider['digest']):
                    raise SessionFailure('stale_context','音色绑定的模型版本与所选后端不同，请重新试音')
        refs=p.get('reference_asset_ids',[])
        if not isinstance(refs,list) or len(refs)>20 or any(not isinstance(aid,str) for aid in refs):
            raise SessionFailure('invalid_input','参考图需要最多 20 个资产引用，实际数量由所选后端限制')
        for aid in refs:
            asset=self.media.require(scope,aid)
            if asset['kind']!='image':
                raise SessionFailure('invalid_input','参考图必须是当前范围内的图片资产')
            validate_generated_media(self.media.read_bytes(scope,aid),'image')
            assets['references'].append(dict(asset_id=aid,sha256=asset['digest'],mime=asset['mime']))
        return self.adapters.prepare(kind,p,assets,voice)

    def catalog(self,scope):
        if not self.settings.enabled:
            return []
        return self.adapters.catalog()

    def normalize_voice(self,record):
        return self.adapters.normalize_voice(record) if record.get('engine')!='edge-tts' else record

    async def refresh(self):
        """Activate installed declarations at the existing idle change point."""
        if self.active or self.blocked_reason or self.repo._one("SELECT 1 FROM work_jobs WHERE kind='local_media' AND status='running'"):
            raise SessionFailure('resource_wait','媒体执行或停止核对尚未结束；配置重载等待原作业停稳')
        from .role_config import load_config
        path=Path(getattr(self.cfg,'_source_path',self.project/'config.toml'))
        configured=load_config(path)
        if (configured.owner!=self.cfg.owner or configured.life_agent!=getattr(self.cfg,'life_agent','default') or
                configured.life_db!=getattr(self.cfg,'life_db','data/life.db')):
            raise SessionFailure('invalid_input','媒体重载不能改变角色身份或生活数据库')
        settings=LocalMediaConfig.parse(configured.local_media);config=settings.resolve(self.project)
        if Path(config['root'])!=self.root:
            raise SessionFailure('invalid_input','现有作业的受管目录不能通过媒体重载移动')
        registry=AdapterRegistry(config,self.project)
        self.settings,self.config,self.adapters=settings,config,registry
        self.cfg.local_media=configured.local_media
        self.voices.adapters=registry;self.voices.local_enabled=settings.enabled
        if isinstance(self.backend,MediaProcess):
            self.backend=MediaProcess(config)
        self._resource_checked_at=-float('inf');self._resource_wait=None
        await self.retire_changed_jobs()
        if settings.enabled and self.task is None:
            self.import_presets(self.life.scope('qq:private:'+str(self.cfg.owner),str(self.cfg.owner)))
            await self.start()
        self.wake.set()
        return dict(activated=True,component='media',providers=[dict(provider_id=p['provider_id'],
            adapter_version=p['adapter_version'],digest=p['digest'],model_version=p['model_version']) for p in registry.providers.values()],
            behavior_validated=False)

    def submit(self,scope,p,call_key,sources,intent_ref=None,*,root=None):
        if not self.settings.enabled:
            raise SessionFailure('unavailable','本地语音/生图后台尚未配置')
        self.sources_valid(scope,sources)
        jid = stable_id('mediajob',scope.scope_id,call_key)
        # An idempotent retry uses the originally frozen default voice and random seed.
        fingerprint = digest(dict(proposal=p,sources=sources,intent_ref=intent_ref))
        prior = self.repo._one('SELECT * FROM work_jobs WHERE id=?',(jid,))
        if prior:
            if json.loads(prior['payload']).get('proposal_hash')!=fingerprint:
                raise SessionFailure('idempotency_conflict','媒体请求编号已绑定另一份内容')
            return self.status(scope,jid)
        spec = self.prepare(scope,p)
        attribution = None
        if self.initiative and self.initiative.enabled:
            root = root or self.initiative.role.sessions.binding(scope)
            attribution,existing = self.initiative.prepare_job(scope,root,p)
            if existing:
                return self.status(scope,existing)
        if spec['seed'] is None:
            spec['seed'] = secrets.randbelow(2**32)
        if self.repo._one("SELECT COUNT(*) AS n FROM work_jobs WHERE kind=? AND status IN ('pending','running','waiting')",(JOB_KIND,))['n']>=self.settings.max_pending_jobs:
            raise SessionFailure('resource_wait','本地媒体队列已满，请等待现有任务')
        intent_revision = None
        if intent_ref:
            from .tasks import TaskService
            if TaskService(self.repo).blocker(intent_ref,scope.scope_id):
                raise SessionFailure('cancel_requested','相关打算当前不可执行')
            intent_revision = TaskService(self.repo).get(intent_ref,scope.scope_id).get('revision',1)
            if p.get('base_revision') is not None and p['base_revision']!=intent_revision:
                raise SessionFailure('stale_context','相关打算的版本已改变')
        payload = dict(scope=scope.__dict__,spec=spec,sources=list(sources),proposal_hash=fingerprint,
            request_hash=digest({k:v for k,v in spec.items() if k!='_adapter'}),frozen_spec_hash=frozen_hash(spec),
            intent_ref=intent_ref,intent_revision=intent_revision)
        if attribution:
            payload['initiative'] = attribution
        with self.repo.tx():
            if attribution and attribution['classification']=='autonomous':
                _,existing = self.initiative.prepare_job(scope,root,p)
                if existing:
                    return self.status(scope,existing)
            from .budget import Budget
            from types import SimpleNamespace
            if not Budget(SimpleNamespace(repo=self.repo),self.cfg).reserve(jid):
                raise SessionFailure('resource_wait','当前行动预算已用完，仍可聊天或查询已有任务')
            self.jobs.enqueue(scope,JOB_KIND,payload,key=jid,root_id=intent_ref or jid)
        self.wake.set()
        return self.status(scope,jid)

    def status(self,scope,jid):
        row = self.repo._one('SELECT * FROM work_jobs WHERE id=? AND kind=?',(jid,JOB_KIND))
        if not row or row['scope_id']!=scope.scope_id:
            raise SessionFailure('scope_mismatch','媒体任务不属于当前会话')
        payload,checkpoint = json.loads(row['payload']),json.loads(row['checkpoint'])
        self.sources_valid(scope,payload.get('sources',[]))
        state = {'pending':'queued','running':'generating','done':'completed'}.get(row['status'],row['status'])
        result = dict(job_id=jid,status=state,kind=payload['spec']['kind'],cancel_requested=bool(checkpoint.get('cancel_requested')),
            asset_id=checkpoint.get('asset_id'),result_event_id=checkpoint.get('event_id'),
            error=row['error'] or None,waiting_reason=checkpoint.get('waiting_reason'),
            created=row['created'],updated=row['updated'])
        provider=payload['spec'].get('_adapter')
        if provider:
            result.update(provider_id=provider['provider_id'],provider_revision=provider['adapter_version'],
                frozen_spec_hash=payload.get('frozen_spec_hash'))
        if result['asset_id']:
            self.media.require(scope,result['asset_id'])
        return result

    def list(self,scope):
        results = []
        for row in self.repo._all('SELECT id FROM work_jobs WHERE kind=? AND scope_id=? ORDER BY created DESC LIMIT 10',
                (JOB_KIND,scope.scope_id)):
            try:
                results.append(self.status(scope,row['id']))
            except SessionFailure:
                continue
        return dict(items=results,capability=self.capability(scope))

    def cancel(self,scope,jid):
        result = self.status(scope,jid)
        if result['status'] in {'completed','failed','cancelled','unknown','quarantined'}:
            return result
        with self.repo.tx():
            row = self.repo._one('SELECT checkpoint,status FROM work_jobs WHERE id=?',(jid,))
            checkpoint = dict(json.loads(row['checkpoint']),cancel_requested=True)
            self.repo._exec('UPDATE work_jobs SET checkpoint=?,updated=? WHERE id=?',
                (canonical(checkpoint),time.time(),jid))
            if row['status'] in {'pending','waiting'}:
                self.repo._exec("UPDATE work_jobs SET status='cancelled',error='explicit_cancel' WHERE id=?",(jid,))
        self.wake.set()
        return self.status(scope,jid)

    def check(self,lease,payload):
        if self.closing:
            return 'host_stopping'
        if not self.jobs.current(lease):
            return 'lease_or_source_epoch_lost'
        row = self.repo._one('SELECT checkpoint FROM work_jobs WHERE id=?',(lease.id,))
        if json.loads(row['checkpoint']).get('cancel_requested'):
            return 'explicit_cancel'
        if not self.adapters.current(payload['spec']):
            return 'adapter_changed_reissue_required'
        scope = Scope(**payload['scope'])
        try:
            self.sources_valid(scope,payload['sources'])
            if payload.get('intent_ref'):
                from .tasks import TaskService
                if TaskService(self.repo).blocker(payload['intent_ref'],scope.scope_id):
                    return 'intent_stopped'
                current = TaskService(self.repo).get(payload['intent_ref'],scope.scope_id)
                if payload.get('intent_revision') is not None and current.get('revision',1)!=payload['intent_revision']:
                    return 'intent_revision_changed'
            self.resolve_references(scope,payload['spec'])
        except SessionFailure:
            return 'source_or_reference_invalidated'
        return None

    def resolve_references(self,scope,spec):
        resolved = dict(spec)
        if spec.get('_adapter'):
            validate_frozen(spec,load=False)
        if spec.get('reference_asset_id'):
            frozen = self.voices.local_record(spec['voice_id'],spec['voice_revision'],scope)
            if frozen['reference_digest']!=spec['reference_sha256']:
                raise SessionFailure('stale_context','音色参考已改变')
            asset = self.media.require(scope,frozen['reference_asset_id'])
            resolved['reference_path'] = asset['storage_ref']
        if 'references' in spec:
            references = []
            for reference in spec['references']:
                asset = self.media.require(scope,reference['asset_id'])
                self.media.read_bytes(scope,asset['id'])
                if asset['digest']!=reference['sha256']:
                    raise SessionFailure('stale_context','参考图已改变')
                references.append(dict(reference,path=asset['storage_ref']))
            resolved['references'] = references
        return resolved

    async def run_one(self):
        if self.blocked_reason:
            return False
        lease = self.jobs.claim(kinds=(JOB_KIND,))
        if not lease:
            return False
        payload = lease.payload
        scope = Scope(**payload['scope'])
        self.active = lease
        heartbeat = None
        stopped = True
        execution_started = None
        initiative_admitted = False
        try:
            reason = self.check(lease,payload)
            if reason:
                await self.terminal(lease,payload,'cancelled',error=reason)
                return True
            if not self.jobs.acquire(lease,'gpu:0'):
                self.jobs.finish(lease,'pending',delay=2,checkpoint=dict(waiting_reason='gpu_busy'))
                return False
            if self.resource_clock()-self._resource_checked_at>=5:
                self._resource_wait = await self.backend.resources_ready()
                self._resource_checked_at = self.resource_clock()
                if self.initiative:
                    self.initiative.resource_snapshot = dict(at=time.time(),known=True,waiting_reason=self._resource_wait)
            waiting = self._resource_wait
            reason = self.check(lease,payload)
            if reason:
                await self.terminal(lease,payload,'cancelled',error=reason)
                return True
            if waiting:
                self.jobs.finish(lease,'pending',delay=5,checkpoint=dict(waiting_reason=waiting))
                return False
            if self.initiative:
                with self.repo.tx():
                    self.initiative.begin_job(scope,lease.id,payload)
                initiative_admitted = True
            folder = self.root/lease.id
            folder.mkdir(parents=True,exist_ok=True)
            async def renew():
                while True:
                    await asyncio.sleep(10)
                    if not self.jobs.renew(lease):
                        return
            heartbeat = asyncio.create_task(renew())
            request = dict(spec=self.resolve_references(scope,payload['spec']),request_hash=payload['request_hash'],
                frozen_spec_hash=payload.get('frozen_spec_hash'),frozen_spec=payload['spec'],job_id=lease.id)
            if self.initiative:
                with self.repo.tx():
                    self.initiative.started(lease.id)
            execution_started = time.monotonic()
            result = await self.backend.run(request,folder,lambda:self.check(lease,payload))
            reason = self.check(lease,payload)
            if reason:
                await self.terminal(lease,payload,'cancelled',error=reason)
            else:
                await self.complete(lease,payload,folder,result)
        except asyncio.CancelledError:
            # The process runner's finally has stopped its tree before this resource is released.
            if self.jobs.current(lease):
                await self.terminal(lease,payload,'cancelled',error='host_stopping')
            raise
        except SessionFailure as error:
            if error.code in {'resource_wait','retry_suspended'}:
                self.jobs.finish(lease,'pending' if error.code=='resource_wait' else 'waiting',delay=5,checkpoint=dict(waiting_reason=error.code))
                if error.code=='resource_wait' and self.initiative and payload.get('initiative'):
                    self.initiative.role.attention.wait_resource(scope,payload['initiative']['owner_ref'],error.code)
                return False
            elif error.code=='stop_unknown':
                stopped = False
                self.blocked_reason = 'owned_process_stop_unconfirmed'
                with self.repo.tx():
                    self.repo._exec("UPDATE work_jobs SET status='unknown',error='stop_unknown' WHERE id=? AND lease_token=?",(lease.id,lease.token))
                    self.repo._exec('UPDATE resource_leases SET until=? WHERE job_id=? AND token=?',(1e20,lease.id,lease.token))
                await self.publish(lease.id,payload,'unknown',error='stop_unknown')
            elif self.jobs.current(lease):
                await self.terminal(lease,payload,'cancelled' if error.code=='cancelled' else 'failed',
                    error=error.code+': '+str(error))
        except Exception as error:
            log.exception('本地媒体任务失败 job=%s',lease.id)
            if self.jobs.current(lease):
                await self.terminal(lease,payload,'failed',error=type(error).__name__+': '+str(error))
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat,return_exceptions=True)
            if stopped:
                with self.repo.tx():
                    # A lost source/lease cannot publish, but its stopped tree no longer owns this token.
                    self.repo._exec('DELETE FROM resource_leases WHERE job_id=? AND token=?',(lease.id,lease.token))
                    self.repo._exec("UPDATE work_jobs SET status='cancelled',lease_token='',lease_until=0,error='lease_or_source_lost' "
                        "WHERE id=? AND lease_token=? AND status='running'",(lease.id,lease.token))
            if self.initiative and initiative_admitted:
                with self.repo.tx():
                    row = self.repo._one('SELECT status,error FROM work_jobs WHERE id=?',(lease.id,))
                    self.initiative.finish_job(lease.id,row['status'],row['error'],stopped=stopped)
            if execution_started is not None:
                from .budget import Budget
                from types import SimpleNamespace
                Budget(SimpleNamespace(repo=self.repo),self.cfg).charge_time(lease.id,time.monotonic()-execution_started)
            self.active = None
        return True

    async def complete(self,lease,payload,folder,result):
        if result.get('request_hash')!=payload['request_hash'] or result.get('status')!='completed':
            raise SessionFailure('stale_context','引擎结果不对应本次请求')
        provider=payload['spec'].get('_adapter')
        if provider:
            validate_frozen(payload['spec'])
            if (frozen_hash(payload['spec'])!=payload.get('frozen_spec_hash') or
                    result.get('frozen_spec_hash')!=payload['frozen_spec_hash'] or result.get('job_id')!=lease.id or
                    result.get('provider_digest')!=provider['digest'] or result.get('provider_revision')!=provider['adapter_version'] or
                    result.get('actual_parameters')!=payload['spec']):
                raise SessionFailure('invalid_output','adapter 结果与冻结作业、版本或实际参数不一致')
        output=provider['description']['output'] if provider else dict(extension='.wav' if payload['spec']['kind']=='speech' else '.png')
        expected='output'+output['extension']
        path = folder/expected
        if result.get('output')!=expected or path.is_symlink() or not path.is_file() or path.stat().st_size>self.media.max_bytes:
            raise SessionFailure('invalid_output','引擎输出路径或容量无效')
        data = await asyncio.to_thread(path.read_bytes)
        if hashlib.sha256(data).hexdigest()!=result.get('sha256'):
            raise SessionFailure('invalid_output','引擎输出摘要不匹配')
        kind = 'audio' if payload['spec']['kind']=='speech' else 'image'
        mime = validate_generated_media(data,kind)
        if provider:
            from .media_worker import output_metadata
            if mime!=output['mime'] or output['kind']!=kind or result.get('media')!=output_metadata(path,provider['description']):
                raise SessionFailure('invalid_output','adapter 输出编码、尺寸或时长与实际文件不符')
        if kind=='image':
            import io
            from PIL import Image
            with Image.open(io.BytesIO(data)) as picture:
                if picture.size!=(payload['spec']['width'],payload['spec']['height']):
                    raise SessionFailure('invalid_output','图片尺寸与本次请求不符')
        scope = Scope(**payload['scope'])
        with self.jobs.commit(lease):
            self.sources_valid(scope,payload['sources'])
            event,_ = self.life.ingest(scope,'media_generated','tool_observation','本地媒体已生成，待主角色检查或分享。',
                external_id='media-generated:'+lease.id,source_event_ids=payload['sources'],
                payload=dict(job_id=lease.id,kind=kind,request_hash=payload['request_hash'],spec=payload['spec']))
            asset = self.media.register(scope,event.id,'output',kind,data,mime,
                dict(job_id=lease.id,request_hash=payload['request_hash'],
                    frozen_spec_hash=payload.get('frozen_spec_hash'),inspection='not_observed',
                    actual_parameters={k:v for k,v in payload['spec'].items() if k!='_adapter'},
                    provider_id=provider['provider_id'] if provider else None,
                    provider_revision=provider['adapter_version'] if provider else None))
            if kind=='audio':
                self.voices.verified_local(scope,payload['spec']['voice_id'],payload['spec']['voice_revision'],asset['id'])
            self.jobs.finish(lease,'done',checkpoint=dict(asset_id=asset['id']))
        await self.publish(lease.id,payload,'done',asset_id=asset['id'])

    async def terminal(self,lease,payload,status,error=''):
        if self.jobs.current(lease):
            self.jobs.finish(lease,status,error=error)
            await self.publish(lease.id,payload,status,error=error)

    async def publish(self,jid,payload,status,**result):
        scope = Scope(**payload['scope'])
        try:
            self.sources_valid(scope,payload['sources'])
        except SessionFailure:
            with self.repo.tx():
                row = self.repo._one('SELECT checkpoint FROM work_jobs WHERE id=?',(jid,))
                if row:
                    self.repo._exec('UPDATE work_jobs SET checkpoint=? WHERE id=?',
                        (canonical(dict(json.loads(row['checkpoint']),notified=True,result_suppressed='source_invalidated')),jid))
            return
        with self.repo.tx():
            summary = dict(job_id=jid,status=status,kind=payload['spec']['kind'],**result)
            parts = [dict(part_id='summary',ordinal=0,kind='text',text=canonical(summary))]
            if result.get('asset_id'):
                parts.append(dict(part_id='result',ordinal=1,kind='audio' if payload['spec']['kind']=='speech' else 'image',
                    asset_id=result['asset_id'],source_ref={}))
            event,_ = self.life.ingest(scope,'media_result','tool_observation',
                '本地媒体任务已有结果，请核对后决定检查、修改或分享。\n'+canonical(summary),
                external_id='media-result:'+jid,source_event_ids=payload['sources'],
                payload=dict(summary,parts=parts))
            row = self.repo._one('SELECT checkpoint FROM work_jobs WHERE id=?',(jid,))
            checkpoint = dict(json.loads(row['checkpoint']),event_id=event.id)
            self.repo._exec('UPDATE work_jobs SET checkpoint=? WHERE id=?',(canonical(checkpoint),jid))
        if self.notify:
            try:
                await self.notify(event)
            except Exception:
                log.exception('媒体结果已保存，等待再次投递 job=%s',jid)
                return
        with self.repo.tx():
            self.repo._exec('UPDATE work_jobs SET checkpoint=? WHERE id=?',
                (canonical(dict(checkpoint,notified=True)),jid))

    async def retire_changed_jobs(self):
        """End unstarted work on retired providers; never rebind it to a new version."""
        for row in self.repo._all("SELECT * FROM work_jobs WHERE kind=? AND status IN ('pending','waiting')",(JOB_KIND,)):
            payload=json.loads(row['payload'])
            if not payload or self.adapters.current(payload['spec']):
                continue
            reason='adapter_changed_reissue_required'
            with self.repo.tx():
                self.repo._exec("UPDATE work_jobs SET status='cancelled',error=?,updated=? WHERE id=? AND status IN ('pending','waiting')",
                    (reason,time.time(),row['id']))
                if self.initiative:
                    self.initiative.finish_job(row['id'],'cancelled',reason,stopped=True)
            await self.publish(row['id'],payload,'cancelled',error=reason)

    async def recover(self):
        await self.retire_changed_jobs()
        for row in self.repo._all('SELECT * FROM work_jobs WHERE kind=?',(JOB_KIND,)):
            payload = json.loads(row['payload'])
            if not payload:
                if hasattr(self.backend,'reconcile_process'):
                    await self.backend.reconcile_process(self.root/row['id'])
                    with self.repo.tx():
                        self.repo._exec('DELETE FROM resource_leases WHERE job_id=?',(row['id'],))
                        if self.initiative:
                            self.initiative.finish_job(row['id'],'cancelled',stopped=True)
                continue
            checkpoint = json.loads(row['checkpoint'])
            if row['status']=='running':
                folder = self.root/row['id']
                if hasattr(self.backend,'reconcile_process'):
                    await self.backend.reconcile_process(folder)
                marker = folder/'result.json'
                if marker.is_file():
                    # A result written before the host crashed can be validated without repeating inference.
                    with self.repo.tx():
                        self.repo._exec('UPDATE work_jobs SET lease_until=0,due=0 WHERE id=?',(row['id'],))
                    lease = self.jobs.claim(row['id'],kinds=(JOB_KIND,))
                    if lease:
                        try:
                            result = json.loads(marker.read_text(encoding='utf-8'))
                            reason = self.check(lease,payload)
                            if reason:
                                await self.terminal(lease,payload,'cancelled',error=reason)
                            elif result.get('status')=='completed':
                                await self.complete(lease,payload,folder,result)
                            else:
                                await self.terminal(lease,payload,'failed',error=str(result.get('error','engine_failed')))
                        except Exception as error:
                            await self.terminal(lease,payload,'unknown',error='result_reconciliation_failed:'+str(error))
                        if self.initiative:
                            with self.repo.tx():
                                current = self.repo._one('SELECT status,error FROM work_jobs WHERE id=?',(row['id'],))
                                self.initiative.finish_job(row['id'],current['status'],current['error'],stopped=True)
                    continue
                # A coordinator restart never blindly reruns a generation with an uncertain outcome.
                with self.repo.tx():
                    self.repo._exec("UPDATE work_jobs SET status='unknown',error='host_restart_outcome_unknown',lease_token='',lease_until=0 WHERE id=?",(row['id'],))
                    self.repo._exec('DELETE FROM resource_leases WHERE job_id=?',(row['id'],))
                await self.publish(row['id'],payload,'unknown',error='host_restart_outcome_unknown')
                if self.initiative:
                    with self.repo.tx():
                        self.initiative.finish_job(row['id'],'unknown','host_restart_outcome_unknown',stopped=True)
            elif row['status']=='unknown' and row['error']=='stop_unknown':
                if hasattr(self.backend,'reconcile_process'):
                    await self.backend.reconcile_process(self.root/row['id'])
                with self.repo.tx():
                    self.repo._exec('DELETE FROM resource_leases WHERE job_id=?',(row['id'],))
                    if self.initiative:
                        self.initiative.finish_job(row['id'],'unknown','stop_unknown',stopped=True)
            elif row['status'] in TERMINAL and not checkpoint.get('notified'):
                await self.publish(row['id'],payload,row['status'],asset_id=checkpoint.get('asset_id'),error=row['error'])

    async def start(self):
        if not self.settings.enabled or self.task:
            return
        self.root.mkdir(parents=True,exist_ok=True)
        self.lock_file = (self.root/'coordinator.lock').open('a+b')
        self.lock_file.seek(0)
        if not self.lock_file.read(1):
            self.lock_file.write(b'0'); self.lock_file.flush()
        self.lock_file.seek(0)
        try:
            import os
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.lock_file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.lock_file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close(); self.lock_file=None
            raise RuntimeError('local_media_coordinator_already_running')
        try:
            await self.recover()
        except SessionFailure as error:
            if error.code!='stop_unknown':
                raise
            self.blocked_reason='owned_process_stop_unconfirmed'
            with self.repo.tx():
                self.repo._exec("UPDATE work_jobs SET status='unknown',error='stop_unknown' WHERE kind=? AND status='running'",(JOB_KIND,))
                self.repo._exec("UPDATE resource_leases SET until=? WHERE job_id IN (SELECT id FROM work_jobs WHERE kind=? AND error='stop_unknown')",(1e20,JOB_KIND))
            log.exception('媒体进程停止情况不明，保留 GPU 占用；文字交互继续可用')
        async def pump():
            while not self.closing:
                try:
                    if await self.run_one():
                        continue
                    for row in self.repo._all("SELECT * FROM work_jobs WHERE kind=? AND status IN ('done','failed','cancelled','unknown') "
                            "AND payload!='{}' AND COALESCE(json_extract(checkpoint,'$.notified'),0)=0 ORDER BY updated LIMIT 3",(JOB_KIND,)):
                        payload = json.loads(row['payload'])
                        if payload:
                            checkpoint = json.loads(row['checkpoint'])
                            await self.publish(row['id'],payload,row['status'],asset_id=checkpoint.get('asset_id'),error=row['error'])
                except Exception:
                    log.exception('媒体执行泵暂未完成，保留队列')
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(),1)
                except asyncio.TimeoutError:
                    pass
        self.task = asyncio.create_task(pump(),name='local-media-pump')

    async def close(self):
        self.closing=True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
            self.task=None
        if self.lock_file:
            import os
            if os.name=='nt':
                import msvcrt
                self.lock_file.seek(0)
                msvcrt.locking(self.lock_file.fileno(),msvcrt.LK_UNLCK,1)
            self.lock_file.close();self.lock_file=None
