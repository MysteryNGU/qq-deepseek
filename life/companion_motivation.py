"""Expose existing drive state; settle only the character's evidenced assessments."""
import json

from .drives import DriveEvidence
from .memory_policy import reset_boundary
from .text import estimate_tokens,stable_id
from .motivation import ACTIONS

# Ordinary activity choices reuse the existing action vocabulary and executor.
ORDINARY={'exploration','competence','relatedness','coherence','self_direction','resource_reserve'}
CHOICES={'type':'array','maxItems':2,'items':{'type':'object','additionalProperties':False,
    'required':['dimension','choice','reason','project'],'properties':{
        'dimension':{'enum':sorted(ORDINARY)},'choice':{'enum':['act','express','defer']},
        'reason':{'type':'string','maxLength':240},'project':{'type':'string'}}}}
ATTENTION_INSTRUCTIONS='''motivation.attention是本轮已经越过表达阈值、值得处理的普通内驱，不是背景装饰。先接住用户正在说的事，再结合真实兴趣、未完成承诺和可用工具选择一件：自然表达、实际行动，或明确暂缓。用motivation_choices记录dimension、choice(act/express/defer)、reason、project（无项目填空串），每个候选最多一项。act必须同时给出同项目可执行actions，不用“我打算”替代行动；express必须有实际聊天内容，不能机械报数、突然换话题或重复催促。忙、话题不合适或没有材料就defer，理由只记内部、不发流程气泡。不凭高压力编经历。不为了满足多个驱动堆多个任务。行动后根据真实回执填写drive_feedback，发起行动和说过话本身不会自动扣压力。'''

TASK_DIMENSIONS={'exploration','competence','coherence','self_direction'}
INTERACTION_DIMENSIONS={'relatedness','intimacy'}
FIELD={'type':'array','maxItems':2,'items':{'type':'object','additionalProperties':False,
    'required':['source_ref','dimension','reason'],'properties':{
        'source_ref':{'type':'string'},'dimension':{'enum':sorted(TASK_DIMENSIONS|INTERACTION_DIMENSIONS)},
        'reason':{'type':'string','minLength':1,'maxLength':240}}}}
INSTRUCTIONS='''motivation.drives各项是[pressure,phase]，只表示已有系统的模拟内部倾向，不是人体感觉或意识测量。可结合自己的项目选择探索、继续、休息、改主意，也可以不联系主人；压力不是必须执行的命令。仅在本轮真实体验确实有所满足时填drive_feedback:[{source_ref,dimension,reason}]，最多两项，无则[]。reason是你的评估理由，不是客观证明。source_ref用实际已完成action_id可反馈exploration/competence/coherence/self_direction；用本轮真实用户source_ref可反馈relatedness/intimacy。仅调用模型、写计划/检查点、发出回复或accepted回执都不算满足，不必每轮申请反馈。不以此更新专项身体体验。motivation.libido是性欲的子状态：pressure是累积压力，stimulation是当下爽度（不推进会按半小时半衰），threshold是本场到顶所需爽度，climaxes是本场次数，satiety是刚满足后的钝感，can_express只表示压力是否越过表达门槛。爽度高不等于必须发生什么，可以说想要、可以停、也可以不接；不能说没发生的身体反应。motivation.scene是专项场景的真实状态：context=closed表示现在不在场景里，arousal是该场景内的身体反应，不是爽度本身；没有场景时不要凭空描述身体反应。'''


class MotivationError(ValueError):
    retryable=True


class CompanionMotivation:
    def __init__(self,core):
        self.core=core;self.life=core.life;self.repo=core.repo;self.scope=core.scope

    def _visible(self,identity):
        return self.repo._one('SELECT * FROM events WHERE id=? AND agent_id=? AND conversation_id=? '
            'AND audience_policy_id=? AND redacted_at IS NULL AND received_at>=?',
            (identity,self.life.agent_id,self.scope.conversation_id,self.scope.audience_policy_id,reset_boundary(self.repo,self.scope)))

    def attention_state(self):
        return self.core._state('ordinary_attention:'+self.scope.scope_id,{})

    def attention(self,view=None):
        view=self.life.drive_view(self.scope,include_private=True) if view is None else view
        now=self.life.clock();previous=self.attention_state();candidates=[]
        epoch=self.repo.forget_epoch(self.scope.scope_id)
        for dimension in sorted(ORDINARY):
            drive=view.get(dimension,{})
            if (drive.get('pressure',0)<drive.get('expression_threshold',1)
                    or drive.get('recovery_until',0)>now):continue
            old=previous.get(dimension,{})
            same=old.get('cycle')==drive.get('cycle_id',0) and old.get('epoch')==epoch
            if same and old.get('next_due',0)>now:continue
            project=old.get('project') if same else None
            if project:
                ref=self.core._project_ref(project)
                if self.core.followthrough._busy(ref) or ref in self.core.town.pending_project_refs():continue
                task=self.core.tasks.get(ref,self.scope.scope_id) or {}
                if task.get('status')=='active' and task.get('continuation',{}).get('state')=='scheduled':continue
            candidates.append(dict(dimension=dimension,pressure=round(drive['pressure'],2),
                cycle=drive.get('cycle_id',0),epoch=epoch,options=ACTIONS[dimension],
                last_considered=old.get('at',0) if same else 0))
        # Give unattended needs a turn instead of letting a permanent 1.0 starve them.
        return sorted(candidates,key=lambda c:(c['last_considered'],-c['pressure'],c['dimension']))[:2]

    def record_choice(self,source,decision,candidates,*,spoken):
        """Record only a committed round. This never awards drive satisfaction."""
        if not candidates:return
        choices={c['dimension']:c for c in decision.get('motivation_choices',[])}
        previous=self.attention_state();now=self.life.clock()
        for candidate in candidates:
            dimension=candidate['dimension'];choice=choices.get(dimension,{})
            project=choice.get('project','');selected=choice.get('choice')
            action=bool(project and any(a.get('kind')!='body' and a.get('project')==project
                                       for a in decision.get('actions',[])))
            status=('acting' if selected=='act' and action else
                    'queued_expression' if selected=='express' and spoken else
                    'deferred' if selected=='defer' and choice.get('reason','').strip() else 'unresolved')
            previous[dimension]=dict(cycle=candidate['cycle'],epoch=candidate['epoch'],status=status,
                reason=choice.get('reason','')[:240],project=project if status=='acting' else '',
                at=now,next_due=now+max(900,self.core._idle_seconds()),source_ref=source.id)
        self.core._save('ordinary_attention:'+self.scope.scope_id,previous)

    def expression_result(self,turn_ids,*,delivered):
        if not turn_ids:return
        previous=self.attention_state();changed=False
        for row in previous.values():
            if row['status']=='queued_expression' and row['source_ref'] in turn_ids:
                row['status']='expressed' if delivered else 'interrupted'
                changed=True
        if changed:self.core._save('ordinary_attention:'+self.scope.scope_id,previous)

    def context(self,*,attention=None):
        view=self.life.drive_view(self.scope,include_private=True)
        packet=dict(drives={dimension:[round(row['pressure'],2),row['phase']] for dimension,row in view.items()},recent_feedback=[])
        candidates=self.attention(view) if attention is None else attention
        if candidates:packet['attention']=candidates
        epoch=self.repo.forget_epoch(self.scope.scope_id)
        recent=[dict(dimension=d,status=r['status'],reason=r['reason'],project=r['project'])
                for d,r in sorted(self.attention_state().items(),key=lambda item:item[1]['at'],reverse=True)
                if r.get('epoch')==epoch and self.life.clock()-r['at']<86400]
        if recent:packet['recent_choices']=recent[:2]
        # 性欲不是只有压力：爽度、余韵和阈值决定她现在想不想要、还能不能继续。
        libido=view.get('libido') or {}
        if libido:
            packet['libido']=dict(
                pressure=round(float(libido.get('pressure',0.0)),2),
                phase=libido.get('phase'),
                stimulation=round(float(libido.get('stimulation',0.0)),3),
                peak=round(float(libido.get('stimulation_peak',0.0)),3),
                climaxes=int(libido.get('scene_climaxes',0) or 0),
                threshold=round(float(libido.get('climax_threshold',0.0)),2),
                satiety=round(float(libido.get('satiety',0.0)),2),
                can_express=bool(libido.get('can_express')),
                recovery_until=float(libido.get('recovery_until',0.0) or 0.0))
        # 专项亲密场景的真实状态（只读）：她现在在不在场景里、身体反应到哪一步。
        scene=getattr(self.life,'experience',None)
        if scene is not None and hasattr(scene,'read'):
            try:
                state=scene.read(self.scope) or {}
                packet['scene']=dict(
                    context=state.get('experience_context'),
                    arousal=round(float(state.get('arousal') or 0.0),3),
                    valence=round(float(state.get('valence') or 0.0),3),
                    revision=state.get('state_revision'),
                    mode=state.get('mode'),
                    stop_latched=bool(state.get('stop_latched')),
                    permission_request=state.get('permission_request'))
                candidate=scene.private.candidate(self.scope) if hasattr(scene,'private') else None
                if candidate:
                    packet['scene']['candidate']=dict(
                        candidate_id=candidate.get('candidate_id'),
                        eligible=bool(candidate.get('eligible')))
            except Exception:
                pass
        for row in self.repo.drive_settlements(self.scope.scope_id,limit=12):
            if row.get('revoked_at') or row['effect_kind'] not in {'verified_task_satisfaction','interaction_satisfaction'}:continue
            if not row['source_ids'] or not all(self._visible(identity) for identity in row['source_ids']):continue
            packet['recent_feedback'].append(dict(dimension=row['dimension'],kind=row['effect_kind'],
                delta=round(row['after_value']-row['before_value'],3),reason=str(row['payload'].get('note',''))[:120]))
            if len(packet['recent_feedback'])==2:break
        # Keep state visible without restoring the old full drive parameter packet.
        while packet['recent_feedback'] and estimate_tokens(json.dumps(packet,ensure_ascii=False,separators=(',',':')))>320:
            longest=max(packet['recent_feedback'],key=lambda item:len(item['reason']))
            if len(longest['reason'])>20:longest['reason']=longest['reason'][:len(longest['reason'])//2]
            else:packet['recent_feedback'].pop()
        return packet

    def _evidence(self,source,item):
        dimension=item.get('dimension');ref=item.get('source_ref');reason=item.get('reason')
        if dimension not in TASK_DIMENSIONS|INTERACTION_DIMENSIONS or not isinstance(ref,str) or not ref or len(ref)>128:
            raise MotivationError('drive_feedback需要真实source_ref和支持的dimension。')
        if not isinstance(reason,str) or not reason.strip() or len(reason)>240:
            raise MotivationError('drive_feedback需要不超过240字的本轮评估理由。')
        kind='interaction_satisfaction' if dimension in INTERACTION_DIMENSIONS else 'verified_task_satisfaction'
        previous=self.repo._one('SELECT 1 FROM drive_settlements WHERE scope_id=? AND root_id=? AND dimension=? AND effect_kind=? AND revoked_at IS NULL',
            (self.scope.scope_id,ref,dimension,kind))
        if previous:return None
        if dimension in INTERACTION_DIMENSIONS:
            event=self._visible(ref)
            if not event or ref!=source.id or event['source_kind']!='user_report' or json.loads(event['payload']).get('role')!='user':
                raise MotivationError('连接/亲密反馈只能引用本轮真实用户输入；发出回复、自述或旧消息不算新互动。')
            sources=[ref]
        else:
            action=self.repo._one('SELECT * FROM companion_actions WHERE id=? AND agent=?',(ref,self.life.agent_id))
            if not action or not self.core.tasks.get(action['project_ref'],self.scope.scope_id) or not self._visible(action['source']):
                raise MotivationError('任务反馈必须引用当前场景真实行动action_id。')
            receipt=json.loads(action['result'] or '{}')
            if action['status'] not in {'completed','succeeded'} or receipt.get('status') not in {'completed','succeeded'} or receipt.get('completion') in {'accepted','queued'}:
                raise MotivationError('行动尚未真实成功完成；不能把失败、执行中或accepted回执结算为满足。')
            candidates=self.repo._all("SELECT id,payload FROM events WHERE agent_id=? AND conversation_id=? AND kind='tool_result' "
                "AND source_kind='tool_observation' AND json_extract(payload,'$.action_id')=? ORDER BY ingest_seq DESC LIMIT 4",
                (self.life.agent_id,self.scope.conversation_id,ref))
            events=[row for row in candidates if self._visible(row['id']) and json.loads(row['payload']).get('result',{}).get('status') in {'completed','succeeded'}]
            if not events:raise MotivationError('该行动没有可见的实际工具结果事件，不能结算满足。')
            sources=[events[0]['id']]
        return DriveEvidence(id=stable_id('companion-drive',ref,dimension,kind),root_id=ref,dimension=dimension,kind=kind,
            confidence=1.0,confirmed=True,real_participant=dimension in INTERACTION_DIMENSIONS,relationship_allows=True,
            participant_id=self.scope.participant_id,sources=sources,note=reason.strip())

    def _prepare(self,source,feedback):
        if not isinstance(feedback,list) or len(feedback)>2 or any(not isinstance(item,dict) for item in feedback):
            raise MotivationError('drive_feedback最多两项，无反馈用空数组。')
        return [evidence for item in feedback if (evidence:=self._evidence(source,item)) is not None]

    def before_commit(self,source,feedback):
        self._prepare(source,feedback)

    def commit_feedback(self,source,feedback):
        with self.repo.tx():
            evidence=self._prepare(source,feedback)
            if not evidence:return []
            _,settlements=self.life.apply_drive_evidence(self.scope,evidence)
            return settlements
