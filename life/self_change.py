"""Recoverable persona, dynamics, skill and native code-release changes."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, fields, replace
import hashlib
import json
import math
import os
from pathlib import Path
import time

from .agent_session import SessionFailure, canonical, digest
from .drives import DriveConfig, DriveDynamics, validate_drive_config
from .state import AffectConfig


SCHEMA = '''CREATE TABLE IF NOT EXISTS self_changes(
 id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, scope_id TEXT NOT NULL,
 phase TEXT NOT NULL, manifest TEXT NOT NULL, receipt TEXT, created REAL NOT NULL,
 updated REAL NOT NULL);'''


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


class SelfChange:
    def __init__(self, runtime, cfg, root):
        self.life, self.repo, self.cfg, self.root = runtime, runtime.repo, cfg, Path(root).resolve()
        self.repo.db.executescript(SCHEMA)
        self.lock = asyncio.Lock()
        self.behavior_path = self.root / 'life-dynamics.json'
        database = Path(getattr(cfg,'life_db','data/life.db'))
        self.control_dir = (database if database.is_absolute() else self.root/database).parent/'life-control'

    def behavior(self, patch=None,*,candidate=None):
        current = candidate if candidate is not None else (json.loads(self.behavior_path.read_text('utf-8')) if self.behavior_path.exists() else {})
        if not isinstance(current,dict) or set(current)-{'drives','affect','relations'}:
            raise SessionFailure('invalid_input','动力文件包含不支持的顶层字段')
        result = json.loads(canonical(current))
        for kind, values in (patch or {}).items():
            if kind not in {'drives', 'affect','relations'} or not isinstance(values, dict):
                raise SessionFailure('invalid_input', '只支持 drives、affect 和 relations 参数')
            result.setdefault(kind, {})
            if kind=='relations':
                from .relationships import RelationConfig
                for key,value in values.items():
                    result[kind][key] = dict(result[kind].get(key,{}),**value) if key in {'magnitudes','sensitivity'} and isinstance(value,dict) else value
                RelationConfig.from_mapping(result[kind],self.life.relationships.registry)
                continue
            for dimension, parameters in values.items():
                if kind == 'drives':
                    if dimension not in self.life.config.drives.dimensions or not isinstance(parameters, dict):
                        raise SessionFailure('invalid_input', '未知欲望维度或参数格式')
                    allowed = {f.name for f in fields(DriveDynamics)} - {'dimension'}
                    if set(parameters)-allowed:
                        raise SessionFailure('invalid_input', '存在不支持的欲望参数')
                    merged = dict(result[kind].get(dimension, {}), **parameters)
                    base = self.life.config.drives.dimensions[dimension]
                    validate_drive_config(replace(base, **merged))
                    result[kind][dimension] = merged
                else:
                    if dimension not in {'half_life','baseline','event_cap','cause_threshold'}:
                        raise SessionFailure('invalid_input', '存在不支持的情绪参数')
                    result[kind][dimension] = parameters
        # Validate the full persisted configuration, not only the last patch.
        for dimension,parameters in result.get('drives',{}).items():
            if dimension not in self.life.config.drives.dimensions or not isinstance(parameters,dict):
                raise SessionFailure('invalid_input','未知欲望维度')
            try:
                validate_drive_config(replace(self.life.config.drives.dimensions[dimension],**parameters))
            except (TypeError,ValueError) as error:
                raise SessionFailure('invalid_input',str(error)) from error
        from .relationships import RelationConfig
        RelationConfig.from_mapping(result.get('relations'),self.life.relationships.registry)
        affect = result.get('affect', {})
        for name, values in affect.items():
            if name not in {'half_life','baseline','event_cap','cause_threshold'}:
                raise SessionFailure('invalid_input','未知情绪参数')
            items = values.values() if isinstance(values, dict) else [values]
            for value in items:
                number = float(value)
                if not math.isfinite(number) or number < 0 or (name != 'half_life' and number > 1) or (name=='half_life' and number==0):
                    raise SessionFailure('invalid_input', '情绪参数超出支持范围')
            if isinstance(values, dict) and set(values)-set(self.life.config.affect.baseline):
                raise SessionFailure('invalid_input', '未知情绪维度')
        return result

    def load_behavior(self,scope=None):
        data = self.behavior()
        from .relationships import RelationConfig
        relations = RelationConfig.from_mapping(data.get('relations'),self.life.relationships.registry)
        drives = replace(self.life.config.drives,
            dimensions={dim: replace(value, **data.get('drives',{}).get(dim,{}))
                        for dim,value in self.life.config.drives.dimensions.items()})
        affect = data.get('affect',{})
        old = self.life.config.affect
        affect_config = replace(old,
            half_life=dict(old.half_life, **affect.get('half_life',{})),
            baseline=dict(old.baseline, **affect.get('baseline',{})),
            event_cap=float(affect.get('event_cap',old.event_cap)),
            cause_threshold=float(affect.get('cause_threshold',old.cause_threshold)))
        if scope and (affect_config != old or drives != self.life.config.drives):
            self.life.configure_dynamics(scope,affect_config,drives)
        else:
            self.life.config.affect,self.life.config.drives = affect_config,drives
        if relations!=self.life.relationships.config:
            if scope:
                self.life.configure_relationships(scope,relations)
            else:
                self.life.relationships.config = relations
        return file_hash(self.behavior_path)

    def path(self, relative):
        target = (self.root / relative).resolve()
        if self.root not in target.parents:
            raise SessionFailure('scope_mismatch','修改路径必须在项目内')
        persona = (self.root / self.cfg.persona).resolve()
        if target not in {persona,self.behavior_path} and not (
                target.parent == (self.root/'skills').resolve() and target.suffix=='.md'):
            raise SessionFailure('scope_mismatch','此入口只修改当前人格、动力参数和 skill；代码使用 code 目标')
        if target.is_symlink():
            raise SessionFailure('scope_mismatch','不通过符号链接修改人格或方法')
        return target

    async def apply(self, scope, proposal, change_id, *, require_skill_baseline=False):
        if scope.participant_id != str(self.cfg.owner):
            raise SessionFailure('scope_mismatch','全局自我修改属于主人会话与自主生活')
        if not isinstance(proposal,dict):
            raise SessionFailure('invalid_input','自我修改需要结构化参数')
        target = proposal.get('target')
        if target not in {None,'persona','persona_public','dynamics','skill','code','dsh','worker','plugin'}:
            raise SessionFailure('invalid_input','未知修改目标；Bot Python 文件使用 target=code、patch 和 probes')
        if target in {None,'persona','persona_public'} and not proposal.get('files') and 'content' not in proposal:
            raise SessionFailure('invalid_input','人格修改需要 content 或 files；Bot 代码使用 target=code、patch 和 probes')
        if target=='skill' and (not proposal.get('name') or 'content' not in proposal):
            raise SessionFailure('invalid_input','方法修改需要 name 和 content')
        if target=='code' and (not isinstance(proposal.get('patch'),dict)
                or not ({'file','old','new'}.issubset(proposal['patch']) or 'changes' in proposal['patch'])):
            raise SessionFailure('invalid_input','代码修改需要 patch: {file,old,new} 和独立 probes')
        checksum = digest(proposal)
        async with self.lock:
            from .expression import expression_guard
            guard = expression_guard.get()
            if guard:
                guard()
            prior = self.repo._one('SELECT * FROM self_changes WHERE id=?',(change_id,))
            if prior:
                if prior['request_hash'] != checksum:
                    raise SessionFailure('idempotency_conflict','变更编号对应不同内容')
                if prior['receipt']:
                    return json.loads(prior['receipt'])
                if proposal.get('target')=='code':
                    return await self.code_change(scope,proposal,change_id,checksum)
                return self.activate(dict(prior))
            if proposal.get('target') == 'code':
                return await self.code_change(scope,proposal,change_id,checksum)
            if proposal.get('target') in {'dsh','worker','plugin'}:
                from .dsh_changes import queue_change
                return queue_change(self,scope,proposal,change_id,checksum)
            if proposal.get('target')=='persona_public':
                from .persona import replace_public_persona
                candidates = [dict(path=self.cfg.persona,content=replace_public_persona(
                    (self.root/self.cfg.persona).read_text('utf-8'),str(proposal['content'])))]
            elif proposal.get('target') == 'dynamics':
                candidates = [dict(path='life-dynamics.json',content=canonical(self.behavior(proposal.get('patch',{}))))]
            elif proposal.get('target') == 'skill':
                name = str(proposal['name'])
                if not name.endswith('.md'):
                    name += '.md'
                path=self.path('skills/'+name)
                current=file_hash(path)
                if require_skill_baseline:
                    if current is None:
                        if proposal.get('create') is not True or proposal.get('base_hash') is not None:
                            raise SessionFailure('stale_context','创建方法需要 create:true、base_hash:null，预期文件不存在')
                    elif proposal.get('create') or not isinstance(proposal.get('base_hash'),str) or proposal['base_hash']!=current:
                        raise SessionFailure('stale_context','方法已存在或 hash 已改变；读取最新方法后合并',current_hash=current)
                elif 'base_hash' in proposal and proposal['base_hash']!=current:
                    raise SessionFailure('stale_context','方法 hash 已改变；请重新读取',current_hash=current)
                metadata=proposal.get('metadata')
                methods=getattr(self.life,'method_store',None)
                if metadata is not None and methods:
                    methods.validate_metadata(metadata)
                item=dict(path='skills/'+name,content=str(proposal['content']),base_hash=current)
                if not require_skill_baseline and 'base_hash' not in proposal:
                    item['legacy_skill_write']=True
                if metadata is not None:
                    item['method_metadata']=metadata
                candidates=[item]
            elif proposal.get('files'):
                candidates = proposal['files']
            else:
                candidates = [dict(path=self.cfg.persona,content=str(proposal['content']),
                                   base_hash=proposal.get('base_hash'))]
            manifest = []
            for item in candidates:
                path = self.path(item['path'])
                current = file_hash(path)
                is_method=path.parent==(self.root/'skills').resolve()
                if is_method and require_skill_baseline and proposal.get('target')!='skill':
                    valid=(item.get('create') is True and item.get('base_hash') is None) if current is None else (
                        not item.get('create') and isinstance(item.get('base_hash'),str) and item['base_hash']==current)
                    if not valid:
                        raise SessionFailure('stale_context','所有方法写入都需要实际 base_hash，创建须显式 create:true')
                if is_method and 'method_metadata' in item and getattr(self.life,'method_store',None):
                    self.life.method_store.validate_metadata(item['method_metadata'])
                expected = item.get('base_hash',current)
                if expected is not None and expected != current:
                    raise SessionFailure('stale_context','文件已改变，请重新读取',path=item['path'],current_hash=current)
                content = str(item['content'])
                if path == self.behavior_path:
                    self.behavior(candidate=json.loads(content))
                if path == (self.root/self.cfg.persona).resolve():
                    from .persona import compile_persona
                    if '<!-- persona:private -->' in path.read_text('utf-8') and '<!-- persona:private -->' not in content:
                        raise SessionFailure('invalid_input','当前人格含私密分区，完整修改须保留分区；普通成长可用 persona_public 修改公共部分')
                    compile_persona(path,source_text=content)
                manifest.append(dict(path=path.relative_to(self.root).as_posix(),base_hash=current,
                    content=content,target_hash=hashlib.sha256(content.encode()).hexdigest()))
                if 'method_metadata' in item:
                    manifest[-1]['method_metadata']=item['method_metadata']
                if item.get('legacy_skill_write') or (is_method and not require_skill_baseline and 'base_hash' not in proposal and 'base_hash' not in item):
                    manifest[-1]['legacy_skill_write']=True
            with self.repo.tx():
                self.repo._exec('INSERT INTO self_changes VALUES(?,?,?,?,?,?,?,?)',
                    (change_id,checksum,scope.scope_id,'prepared',canonical(manifest),None,time.time(),time.time()))
            return self.activate(dict(self.repo._one('SELECT * FROM self_changes WHERE id=?',(change_id,))))

    def activate(self, change):
        manifest = json.loads(change['manifest'])
        scope_id = change['scope_id']
        self.life.advance_time(self.life.scope(scope_id.split('|',1)[1],str(self.cfg.owner)))
        with self.repo.tx():
            self.repo._exec("UPDATE self_changes SET phase='activating',updated=? WHERE id=?",(time.time(),change['id']))
        for item in manifest:
            path = self.path(item['path'])
            current = file_hash(path)
            if current not in {item['base_hash'],item['target_hash']}:
                raise SessionFailure('stale_context','激活期间文件被另一个写入者修改',path=item['path'])
            if current != item['target_hash']:
                path.parent.mkdir(parents=True,exist_ok=True)
                temporary = path.with_name(path.name+'.activating')
                with temporary.open('w',encoding='utf-8',newline='\n') as output:
                    output.write(item['content']); output.flush(); os.fsync(output.fileno())
                os.replace(temporary,path)
        spec = self.life.persona.activate(self.root/self.cfg.persona)
        from .persona import public_persona
        public_persona(self.root/self.cfg.persona,self.root/'personas/public.md')
        behavior_hash = self.load_behavior(self.life.scope(scope_id.split('|',1)[1],str(self.cfg.owner)))
        effective = {item['path']:file_hash(self.path(item['path'])) for item in manifest}
        if any(effective[item['path']]!=item['target_hash'] for item in manifest):
            raise SessionFailure('stale_context','实际加载版本与候选不一致')
        method_versions=[]
        methods=getattr(self.life,'method_store',None)
        if methods:
            for item in manifest:
                if item['path'].startswith('skills/'):
                    method_versions.append(methods.reconcile(Path(item['path']).name,item.get('method_metadata'),item['target_hash']))
        receipt = dict(change_id=change['id'],phase='applied',persona_hash=spec.digest,
            behavior_hash=behavior_hash,effective=effective,behavior_validated=False)
        if method_versions:
            receipt['methods']=[dict(ref='method:bot:'+m['id'],hash=m['body_hash'],version=m['version'],visibility=m['visibility']) for m in method_versions if m]
        if any(item.get('legacy_skill_write') for item in manifest):
            receipt['compatibility_notice']='旧方法写入未提供基线；升级会话后更新需 base_hash，新建需 create:true、base_hash:null。'
        with self.repo.tx():
            self.repo._exec("UPDATE self_changes SET phase='applied',receipt=?,updated=? WHERE id=?",
                (canonical(receipt),time.time(),change['id']))
        return receipt

    def recover(self):
        with self.repo.tx():
            self.repo._exec("UPDATE self_changes SET phase='outcome_unknown',receipt=? WHERE phase='dsh_checking'",
                (canonical(dict(phase='outcome_unknown',applied=False,behavior_validated=False,
                    reason='宿主在 DSH 修改期间退出；需核对原修改记录和源码，禁止盲重试。')),))
        for row in self.repo._all("SELECT * FROM self_changes WHERE phase IN ('prepared','activating') ORDER BY created"):
            self.activate(dict(row))
        self.load_behavior()

    def history(self,scope,limit=20):
        rows = self.repo._all('SELECT id,phase,receipt FROM self_changes WHERE scope_id=? ORDER BY created DESC LIMIT ?',
                              (scope.scope_id,limit))
        items = []
        for row in rows:
            receipt = json.loads(row['receipt']) if row['receipt'] else dict(change_id=row['id'],phase=row['phase'])
            if receipt.get('candidate_id'):
                from .release import ReleaseController,NativeWorker
                controller = ReleaseController(self.root,self.control_dir,NativeWorker())
                deployment = controller.deployment(receipt['candidate_id'])
                receipt['deployment'] = deployment
                receipt['runtime'] = getattr(self.life,'runtime_info',{})
                receipt['applied'] = bool(deployment and deployment['phase'] in {'observing','observed'})
                # Healthy startup and goal-specific behavior evidence are different.
            items.append(receipt)
        return items

    async def code_change(self,scope,proposal,change_id,checksum):
        from .release import ReleaseController, NativeWorker, Probe
        from .release_origin import capture
        origin = capture(self,scope)
        controller = ReleaseController(self.root,self.control_dir,NativeWorker())
        # Independent test code remains outside the candidate's writable edit.
        probes = [Probe(**value) for value in proposal.get('probes',[])]
        if not probes:
            raise SessionFailure('invalid_input','代码升级需要可执行的检查目标；先设计并运行具体探针')
        # The controller also owns a durable proposal identity, so a crash
        # between preparation and this receipt cannot create another candidate.
        with controller._db() as database:
            found = database.execute("SELECT id FROM candidates WHERE json_extract(manifest,'$.proposal_id')=?",(change_id,)).fetchone()
        try:
            identity = found[0] if found else await asyncio.to_thread(controller.prepare,change_id,proposal['patch'])
        except (ValueError, PermissionError) as error:
            raise SessionFailure('invalid_candidate', '候选补丁未生成：'+str(error),
                applied=False, hint='核对候选路径与补丁基线；这不是项目目录的系统写权限错误。') from error
        with self.repo.tx():
            self.repo._exec("INSERT INTO self_changes VALUES(?,?,?,'code_prepared',?,NULL,?,?) ON CONFLICT(id) DO NOTHING",
                (change_id,checksum,scope.scope_id,canonical(proposal),time.time(),time.time()))
        report = await asyncio.to_thread(controller.evaluate,identity,probes,origin=origin)
        receipt = dict(change_id=change_id,candidate_id=identity,phase=report['verdict'],report=report,
                       applied=False,behavior_validated=False)
        if report['verdict']!='publishable':
            receipt['activation_blocked'] = dict(code='candidate_not_publishable',
                message='候选尚未激活，当前运行未改变；局部探针通过不等于完整回归与发布通过。')
        if report['verdict']=='publishable':
            from .expression import expression_guard
            guard = expression_guard.get()
            try:
                if guard:
                    guard()
                # This short local commit cannot yield to a newer input between
                # eligibility and publishing the queued deployment.
                controller.request_publish(identity,origin=origin)
                receipt['phase']='queued_for_supervisor'
            except SessionFailure as error:
                receipt['activation_blocked'] = error.receipt()
        with self.repo.tx():
            self.repo._exec('UPDATE self_changes SET phase=?,receipt=?,updated=? WHERE id=?',
                (receipt['phase'],canonical(receipt),time.time(),change_id))
        return receipt
