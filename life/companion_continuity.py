"""Evidence-backed project continuity without another model loop or task store."""
import json
from pathlib import Path
import time

from .memory_policy import reset_boundary

PROJECT_FIELDS={
    'checkpoint':{'type':'string','maxLength':1200},
    'blocked_on':{'type':'array','maxItems':5,'items':{'enum':['town','phone','desktop','user','other']}},
    'evidence_refs':{'type':'array','maxItems':8,'items':{'type':'string'}},
    # 结束一件事时说明为什么结束：做完了、做到这里为止、还是改了目标。
    'close_reason':{'type':'string','maxLength':400},
    # 她自己选的复核时间（秒）；不是程序定时轮询，是她决定的“过多久再看一眼”。
    'review_after':{'type':'number','minimum':0},
}

INSTRUCTIONS='''实际连接状态current_facts优先于旧笔记和旧项目阻碍。fact_changes是真实观察证据；review_projects只表示需要复核，不能因连接变化就自动宣布整个项目完成。请在正常本轮认知中更新相关项目checkpoint、next_step、blocked_on和evidence_refs，说明哪些旧判断不再成立、还有什么没做。完成用同项目真实行动回执、可核验当前条件或本次用户确认作证；阻碍消失并不等于目标完成。own_projects是你在做的事，interests是你自己形成的兴趣（带依据和数量），两者不是一回事：事情做完兴趣可以留着，没事做也不等于没兴趣。可以继续、休息、改变方向或放弃，不必新造兴趣、凑行动或强行研究指定话题。用户暂停或取消的项目不能自行复活。checkpoint是你的进展判断，progress中的action才是已发生的执行证据。'''


class ContinuityError(ValueError):
    def __init__(self,message,*,retryable=True):
        super().__init__(message)
        self.retryable=retryable


class Continuity:
    def __init__(self,core):
        self.core=core;self.repo=core.repo;self.tasks=core.tasks;self.scope=core.scope
        self._review_context={}
        self._town_sources=set()

    @staticmethod
    def _facts(value):
        town=value.get('town',value.get('town_connection',{})) or {}
        phone=value.get('phone',value.get('phone_context',{})) or {}
        desktop=value.get('desktop',value.get('body',{})) or {}
        devices=[dict(device_id=str(d.get('device_id',''))[:80],operations=sorted(set(str(op)[:50] for op in d.get('operations',[]))))
                 for d in phone.get('devices',[]) if d.get('enabled',True)]
        return dict(town=dict(enabled=bool(town.get('enabled')),has_code=bool(town.get('has_code')),state=str(town.get('state','unknown'))[:40]),
                    phone=dict(connected=bool(phone.get('connected')),devices=sorted(devices,key=lambda d:d['device_id'])),
                    desktop=dict(connected=bool(desktop.get('connected'))))

    def _state(self):
        return self.core._state('continuity',dict(revision=0,facts={},changes=[]))

    def _visible_event(self,identity):
        return self.repo._one('SELECT * FROM events WHERE id=? AND agent_id=? AND conversation_id=? '
            'AND audience_policy_id=? AND redacted_at IS NULL AND received_at>=?',
            (identity,self.core.life.agent_id,self.scope.conversation_id,self.scope.audience_policy_id,reset_boundary(self.repo,self.scope)))

    def context(self,facts,source=None):
        self._town_sources={item['source'] for item in facts.get('town_events',[]) or []
                            if isinstance(item,dict) and isinstance(item.get('source'),str)}
        current=self._facts(facts);state=self._state()
        domains=[key for key in current if current[key]!=state['facts'].get(key)]
        if domains:
            with self.repo.tx():
                event,_=self.core.life.ingest(self.scope,'companion_connection_observation','tool_observation',
                    '实际连接状态更新：'+','.join(domains),payload=dict(current_facts=current,changed_domains=domains))
                change=dict(revision=state['revision']+1,evidence_ref=event.id,domains=domains,
                    before={key:state['facts'].get(key) for key in domains},after={key:current[key] for key in domains})
                state=dict(revision=change['revision'],facts=current,changes=[*state['changes'],change][-8:])
                self.core._save('continuity',state)
            self.core._audit('activity','真实连接条件已更新，相关旧项目等待复核。',evidence_ref=event.id,domains=domains)
        changes=[item for item in state['changes'] if self._visible_event(item['evidence_ref'])]
        projects=self.core._projects();reviews=[];own_projects=[];interests=[]
        for project in projects:
            if project.get('ephemeral'):continue
            blocked=project.get('blocked_on',[])
            applicable=[item for item in changes if item['revision']>project.get('reviewed_fact_revision',0)
                and (set(blocked)&set(item['domains']) or not blocked)]
            if project['status'] in {'active','waiting'} and applicable and (blocked or project['status']=='waiting') and project.get('pause_owner')!='user':
                reviews.append(dict(ref=project['ref'],key=project.get('key',project['ref']),status=project['status'],
                    old_next_step=str(project.get('next_step',''))[:240],blocked_on=blocked,
                    evidence_refs=[item['evidence_ref'] for item in applicable[-2:]],
                    reason='connection_conditions_changed' if blocked else 'legacy_conditions_need_review'))
            if project.get('origin')=='self' and project['status'] in {'active','waiting','paused'}:
                progress=self._visible_progress(project)
                latest=dict(progress[-1]) if progress else None
                if latest:
                    for key in ('text','output'):
                        if key in latest:latest[key]=latest[key][:180]
                own_projects.append(dict(ref=project['ref'],key=project.get('key',project['ref']),goal=str(project.get('goal',''))[:240],
                    status=project['status'],checkpoint=str(project.get('checkpoint',''))[:180],next_step=str(project.get('next_step',''))[:160],
                    blocked_on=blocked,last_progress=latest,
                    user_stopped=project.get('pause_owner')=='user'))
        reviews.sort(key=lambda p:p['status']!='waiting')
        shown=reviews[:2]
        projects_by_ref={project['ref']:project for project in projects}
        # Keep the full old step: core applies task changes before record_changes.
        # Only evidence actually supplied in this turn can acknowledge a review.
        self._review_context={item['ref']:dict(
            old_next_step=str(projects_by_ref[item['ref']].get('next_step','')),
            revision=max(change['revision'] for change in changes if change['evidence_ref'] in item['evidence_refs']),
            evidence_refs=list(item['evidence_refs'])) for item in shown}
        own_projects.sort(key=lambda p:(p['user_stopped'],p['status']!='active',p['status']=='paused',(p['last_progress'] or {}).get('at',0)))
        # 兴趣和待办是两回事：兴趣来自她自己的成长记录（带依据、有生命周期），
        # 项目是正在做的事。过去把两者混成一个字段，才出现"任务结束兴趣也没了"。
        store=getattr(self.core,'growth',None)
        if store is not None:
            try:
                interests=store.material(scope=self.scope,clues=(),limit=2)
            except Exception:
                self.core._audit('error','成长记录读取失败，这一轮不展示兴趣。')
                interests=[]
        else:
            interests=[dict(ref=item['ref'],key=item['key'],goal=item['goal'],status=item['status']) for item in own_projects[:2]]
        return dict(current_facts=current,fact_revision=state['revision'],source_ref=source.id if source is not None else None,
                    fact_changes=[{key:item[key] for key in ('revision','evidence_ref','domains')} for item in changes[-2:]],
                    review_projects=shown,own_projects=own_projects[:2],interests=interests[:2])

    def _visible_progress(self,project):
        rows=[]
        for entry in project.get('progress',[]):
            if entry.get('source_event') and self._visible_event(entry['source_event']):rows.append(entry)
        return rows[-12:]

    def _evidence(self,identity,project_ref):
        action=self.repo._one('SELECT * FROM companion_actions WHERE id=? AND agent=?',
            (identity,self.core.life.agent_id))
        if action and action['project_ref']!=project_ref:
            payload=json.loads(action['action'])
            # 只读查询类动作可以跨项目当证据：memory 的 used/skill 的 list 都不改任何东西，
            # 它们的结果（"这个能力今天用过没有"）本来就是给任意项目做判断用的。
            # 2026-11-18 实测：她把 memory op=used 的真实结果（web 今天 0 次）引到
            # "查今天有没有用网页搜索"这个项目里，被判成"证据不属于此项目"，
            # 于是每轮降级→项目更新被丢→她重试，9 分钟里同一件事做了 8 次。
            readonly = payload.get('kind') in {'memory','skill'}
            if not readonly and (payload.get('kind')!='body' or self.core._project_ref(payload.get('project',''))!=project_ref):
                action=None
        if action and action['status'] not in {'pending','running'} and action['result'] and self._visible_event(action['source']):
            result=json.loads(action['result'])
            return dict(kind='action',success=action['status'] in {'completed','succeeded'}
                and result.get('status') in {'completed','succeeded'} and result.get('completion') not in {'accepted','queued'})
        event=self._visible_event(identity)
        if event and event['kind']=='companion_connection_observation':
            return dict(kind='facts',payload=json.loads(event['payload']))
        if event and event['kind']=='town_delegation_result' and event['source_kind']=='tool_observation':
            payload=json.loads(event['payload'])
            if payload.get('project_ref')==project_ref:
                return dict(kind='external',terminal=payload.get('terminal') is True)
        if event and event['source_kind']=='user_report':return dict(kind='user')
        # 小镇回执落在交办表里，不在这张动作表；不认它，她会因为"引用自己的真实
        # 经历"被判成伪造证据。
        town=self.repo._one('SELECT project_ref,state,receipt FROM companion_town_delegations '
                            'WHERE id=? AND scope LIKE ?',(identity,self.core.life.agent_id+'|town:%'))
        if town and (not town['project_ref'] or town['project_ref']==project_ref):
            done=self._town_done(town['receipt'])
            return dict(kind='external',terminal=done,success=done)
        town_scope=getattr(self.core.town,'scope',None)
        if not event and identity in self._town_sources and town_scope is not None:
            observation=self.repo._one("SELECT id FROM events WHERE id=? AND agent_id=? AND conversation_id=? "
                "AND audience_policy_id=? AND kind='town_event' AND source_kind='tool_observation' "
                "AND redacted_at IS NULL AND received_at>=?",
                (identity,self.core.life.agent_id,town_scope.conversation_id,town_scope.audience_policy_id,
                 reset_boundary(self.repo,town_scope)))
            if observation:return dict(kind='observation')
        raise ContinuityError('证据引用不存在、不属于此项目或尚未取得真实结果：'+str(identity))

    @staticmethod
    def _town_done(receipt):
        """小镇回执是否真的做成了：只看回执里的 state，不看交办时的受理标志。"""
        try:
            payload=json.loads(receipt or '{}') or {}
        except (TypeError,ValueError):
            return False
        inner=payload.get('receipt') or {}
        if not isinstance(inner,dict):
            return False
        return str(inner.get('state') or '') in {'done','succeeded','completed'} and bool(inner.get('ok',True))

    def _refs(self,change,project_ref):
        refs=list(dict.fromkeys(change.get('evidence_refs',[])))
        if not refs and change.get('status')=='done':
            # Older decisions can reuse actual receipts without inventing new fields.
            for row in self.repo._all("SELECT id FROM companion_actions WHERE agent=? AND (project_ref=? OR json_extract(action,'$.kind')='body') "
                "AND status IN ('completed','succeeded') ORDER BY updated DESC LIMIT 32",(self.core.life.agent_id,project_ref)):
                try:
                    if self._evidence(row['id'],project_ref).get('success'):return [row['id']]
                except ContinuityError:pass
        return refs

    def before_commit(self,source,changes,*,purpose='activity'):
        for change in changes:
            ref=self.core._project_ref(change['key']);old=self.tasks.get(ref,self.scope.scope_id) or {}
            status=change.get('status',old.get('status','active'))
            if purpose!='dialogue' and status=='active' and old.get('status') in {'paused','cancelled'} and (
                    old.get('pause_owner')=='user' or old.get('origin')=='user'):
                raise ContinuityError('用户暂停或取消的项目需要用户明确恢复，不能自行激活。',retryable=False)
            refs=self._refs(change,ref);evidence=[(identity,self._evidence(identity,ref)) for identity in refs]
            if status!='done' or old.get('status')=='done':continue
            pending=self.repo._one("SELECT id FROM companion_actions WHERE agent=? AND project_ref=? AND status IN ('pending','running') LIMIT 1",(self.core.life.agent_id,ref))
            if pending:raise ContinuityError('项目还有未取得回执的行动；请保持active，不能提前完成。')
            valid=False
            for identity,item in evidence:
                if item['kind']=='action' and item['success']:valid=True
                if item['kind']=='user' and purpose=='dialogue' and identity==source.id:valid=True
                if item['kind']=='observation' and change.get('checkpoint','').strip():valid=True
                if item['kind']=='external' and item['terminal'] and change.get('checkpoint','').strip():valid=True
                if item['kind']=='facts' and change.get('checkpoint','').strip():
                    observed=item['payload'].get('current_facts',{});current=self._state()['facts']
                    # Validate the observation, not a keyword-based interpretation
                    # of the goal. The normal model turn explains sufficiency.
                    if observed==current:valid=True
            if not valid:raise ContinuityError('完成项目需要同项目真实成功回执、注明关联条件的当前事实，或本轮用户确认；请保持active/waiting，使用checkpoint记录进展并引用evidence_refs。')

    def record_changes(self,source,changes):
        with self.repo.tx():
            state=self._state()
            for change in changes:
                ref=self.core._project_ref(change['key']);project=self.tasks.get(ref,self.scope.scope_id)
                if not project:continue
                updates={key:change[key] for key in ('checkpoint','blocked_on') if key in change}
                refs=self._refs(change,ref)
                if refs or 'evidence_refs' in change:updates['evidence_refs']=refs
                if change.get('checkpoint','').strip():
                    entry=dict(kind='checkpoint',text=change['checkpoint'][:1200],evidence_refs=refs,source_event=source.id,at=time.time())
                    progress=project.get('progress',[])
                    if not progress or progress[-1].get('text')!=entry['text'] or progress[-1].get('evidence_refs')!=refs:
                        updates['progress']=[*progress,entry][-12:]
                    revisions=[item['revision'] for item in state['changes'] if item['evidence_ref'] in refs]
                    reviewed=self._review_context.get(ref)
                    if reviewed and isinstance(change.get('next_step'),str) and change['next_step'].strip()!=reviewed['old_next_step'].strip():
                        # Context evidence acknowledges revision, never completion
                        # or an explicit model citation.
                        revisions.append(reviewed['revision'])
                        updates['review_context_refs']=list(reviewed['evidence_refs'])
                    if revisions:updates['reviewed_fact_revision']=max(project.get('reviewed_fact_revision',0),*revisions)
                if updates:self.tasks.ensure(self.scope,ref,None,**updates)

    def record_action(self,row,result):
        actual=self.repo._one('SELECT * FROM companion_actions WHERE id=? AND agent=?',(row['id'],self.core.life.agent_id))
        if not actual or actual['status'] in {'pending','running'} or not actual['result']:return
        project=self.tasks.get(actual['project_ref'],self.scope.scope_id)
        if not project or project.get('ephemeral'):return
        progress=project.get('progress',[])
        if any(item.get('action_id')==actual['id'] for item in progress):return
        receipt=json.loads(actual['result']);artifacts=[]
        for name in ('output_path','image_path'):
            value=receipt.get(name)
            if isinstance(value,str):
                path=Path(value);path=path if path.is_absolute() else self.core.home/path
                if path.is_file():artifacts.append(str(path.resolve()))
        entry=dict(kind='action',action_id=actual['id'],status=actual['status'],output=str(receipt.get('output',''))[:600],
            artifacts=artifacts,source_event=actual['source'],at=actual['updated'])
        self.tasks.ensure(self.scope,project['ref'],None,progress=[*progress,entry][-12:],last_progress_at=actual['updated'])
