"""Durable ordinary-drive decisions; a desire is neither an action nor a reward."""
import json
import math

from .agent_session import SessionFailure,canonical
from .text import stable_id


ACTIONS={
    'exploration':['提出一个具体问题并查证','探索一个已有兴趣'],
    'competence':['练习一项技能','解决一个可验证的小问题'],
    'relatedness':['分享合适的普通经历','关心联系人；未回复时不追问'],
    'intimacy':['非露骨地表达关心或陪伴意愿；尊重拒绝'],
    'libido':['选择非露骨的陪伴或约会邀请','独处休整；冲动不表示对方同意'],
    'coherence':['推进承诺','核对未完成事项与真实结果'],
    'self_direction':['选择自己的兴趣活动','重新安排可自主决定的计划'],
    'resource_reserve':['安排休息','减少同时执行的事项'],
}


class Motivation:
    def __init__(self,role):
        self.role,self.life,self.repo=role,role.life,role.repo
        self.next_check=0
        self.repo.db.execute('''CREATE TABLE IF NOT EXISTS motivation_decisions(
            id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,forget_epoch INTEGER NOT NULL,
            dimension TEXT NOT NULL,cycle_id INTEGER NOT NULL,revision INTEGER NOT NULL,
            status TEXT NOT NULL,pressure REAL NOT NULL,reason TEXT NOT NULL,
            intent_ref TEXT,created REAL NOT NULL,updated REAL NOT NULL,next_due REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0)''')

    def owner(self,scope):
        return scope.conversation_id=='qq:private:'+str(self.role.cfg.owner)

    def rows(self,scope):
        return self.repo._all('SELECT * FROM motivation_decisions WHERE scope_id=? AND forget_epoch=? ORDER BY pressure DESC,created',
            (scope.scope_id,self.repo.forget_epoch(scope.scope_id)))

    def view(self,scope):
        if not self.owner(scope):return dict(candidates=[],scope='owner_autonomy_only')
        items=[]
        for row in self.rows(scope):
            if row['status'] not in {'pending','selected','deferred','declined'}:continue
            task=self.role.tasks.get(row['intent_ref'],scope.scope_id) if row['intent_ref'] else None
            progress=None
            if task:
                counts={r['status']:r['n'] for r in self.repo._all('SELECT status,COUNT(*) n FROM role_operations WHERE scope_id=? AND intent_ref=? GROUP BY status',
                    (scope.scope_id,task['ref']))}
                progress=dict(task_status=task['status'],operations=counts,
                    satisfaction='requires_evidence_appraisal',blocker=self.role.tasks.blocker(task['ref'],scope.scope_id))
                outcome=self.repo._one('SELECT v FROM meta WHERE k=?',('motivation_result:'+row['id']+':'+task['ref'],))
                progress['result']=json.loads(outcome['v']) if outcome else None
            items.append(dict(ref=row['id'],revision=row['revision'],dimension=row['dimension'],pressure=row['pressure'],
                status=row['status'],reason=row['reason'],intent_ref=row['intent_ref'],next_due=row['next_due'],
                action_options=ACTIONS[row['dimension']],execution=progress))
        return dict(candidates=items[:8],read_kind='motivations',meaning='motivation_is_not_execution_or_satisfaction',
            initiation_candidate=self.life.experience.private.candidate(scope))

    def status(self,scope):
        counts={r['status']:r['n'] for r in self.repo._all('SELECT status,COUNT(*) n FROM motivation_decisions WHERE scope_id=? AND forget_epoch=? GROUP BY status',
            (scope.scope_id,self.repo.forget_epoch(scope.scope_id)))}
        explicit=bool(self.repo._one('SELECT 1 FROM meta WHERE k=?',('attention_explicit:'+scope.scope_id,)))
        return dict(states=counts,dimensions=list(ACTIONS),explicit_wait=explicit,
            background_allowed=self.role.spending.background_allowed(),cooldown_seconds=self.role.resources.motivation_cooldown_seconds)

    def record_result(self,scope,task,p,root):
        rows=[r for r in self.rows(scope) if r['status']=='selected' and r['intent_ref']==task['ref']]
        if not rows:return
        sources=p.get('result_sources',[])
        if not isinstance(sources,list) or not sources or len(sources)>8:
            raise SessionFailure('motivation_result_required','动机任务完成前须提供 result_sources，引用真实工具结果或已确认交流；计划和念头不算结果')
        context=self.life.life_access.bind(scope,session_id=root['session_id'],generation=root['generation'])
        refs=[]
        for source in sources:
            resource,_=self.life.life_access.load(context,source)
            event=self.repo.get_event(resource['native_id']) if resource['kind']=='event' else None
            if not event or event.scope.scope_id!=scope.scope_id or event.received_at<max(r['updated'] for r in rows):
                raise SessionFailure('motivation_result_required','结果必须发生在选择该行动之后，并属于当前场景')
            operation=self.repo._one("SELECT status FROM role_operations WHERE id=? AND intent_ref=? AND scope_id=?",
                (event.payload.get('operation_id'),task['ref'],scope.scope_id))
            delivered=self.repo._one("SELECT status FROM deliveries WHERE id=? AND scope_id=?",
                (event.payload.get('delivery_id'),scope.scope_id))
            actual_tool=event.source_kind=='tool_observation' and operation and operation['status']=='succeeded'
            actual_contact=event.kind=='delivered_segment' and event.source_kind=='self_output' and delivered and delivered['status']=='confirmed'
            if not actual_tool and not (actual_contact and all(r['dimension'] in {'relatedness','intimacy','libido'} for r in rows)):
                raise SessionFailure('motivation_result_required','没有对应的成功执行回执；自述、旧材料和未知结果不能证明完成')
            refs.append(resource['resource_id'])
        for row in rows:
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('motivation_result:'+row['id']+':'+task['ref'],
                canonical(dict(evidence_refs=refs,recorded_at=self.life.clock(),state='result_recorded',
                    satisfaction='not_automatically_awarded'))))

    def scan(self,scope,now=None):
        """Local calculation only. Budget and busy checks remain in wake dispatch."""
        if not self.owner(scope):return []
        now=self.life.clock() if now is None else now
        state=self.life.drive_view(scope,now)
        initiation=self.life.experience.private.candidate(scope,now)
        if initiation:
            state['libido']=dict(state['libido'],pressure=initiation['pressure'])
        epoch=self.repo.forget_epoch(scope.scope_id)
        with self.repo.tx():
            for dim,options in ACTIONS.items():
                drive=state.get(dim,{})
                pressure=float(drive.get('pressure',0))
                threshold=float(drive.get('expression_threshold',1))
                identity=stable_id('motivation',scope.scope_id,str(epoch),dim,str(drive.get('cycle_id',0)))
                self.repo._exec("UPDATE motivation_decisions SET status='withdrawn',revision=revision+1,reason='cycle_changed' WHERE scope_id=? AND dimension=? AND status='pending' AND id!=?",(scope.scope_id,dim,identity))
                old=self.repo._one('SELECT * FROM motivation_decisions WHERE id=?',(identity,))
                if pressure<threshold or drive.get('recovery_until',0)>now:
                    if old and old['status']=='pending':
                        self.repo._exec("UPDATE motivation_decisions SET status='withdrawn',revision=revision+1,updated=?,reason='pressure_below_threshold_or_recovering' WHERE id=?",(now,identity))
                    continue
                if old:
                    if old['status']=='selected':
                        task=self.role.tasks.get(old['intent_ref'],scope.scope_id)
                        if task and task['status'] in {'active','waiting','paused'}:continue
                        self.repo._exec("UPDATE motivation_decisions SET status='deferred',reason='activity_closed_review_result',updated=?,next_due=?,revision=revision+1 WHERE id=?",
                            (now,now+self.role.resources.motivation_cooldown_seconds,identity))
                        continue
                    if old['status']=='pending' or old['next_due']>now:continue
                    self.repo._exec("UPDATE motivation_decisions SET status='pending',pressure=?,revision=revision+1,updated=?,reason='',intent_ref=NULL WHERE id=?",(pressure,now,identity))
                else:
                    self.repo._exec('INSERT INTO motivation_decisions VALUES(?,?,?,?,?,1,?,?,?,NULL,?,?,?,0)',
                        (identity,scope.scope_id,epoch,dim,drive.get('cycle_id',0),'pending',pressure,'',now,now,now))
        return [dict(r) for r in self.rows(scope) if r['status']=='pending'][:3]

    def tick(self,now):
        with self.repo.tx():
            self._tick(now)

    def _tick(self,now):
        if now<self.next_check:return
        self.next_check=now+30
        scope=self.life.scope('qq:private:'+str(self.role.cfg.owner),str(self.role.cfg.owner))
        # Explicit sleep/event waits and budget pauses do not get overridden by drives.
        if self.repo._one('SELECT 1 FROM meta WHERE k=?',('attention_explicit:'+scope.scope_id,)):
            return
        # Refresh local candidates only. The persisted idle timer admits them
        # together at the next 10–60 minute wake, never on a separate fast loop.
        self.scan(scope,now)

    def decide(self,scope,p,task=None):
        with self.repo.tx():
            return self._decide(scope,p,task)

    def _decide(self,scope,p,task=None):
        ref=p.get('ref')
        row=self.repo._one('SELECT * FROM motivation_decisions WHERE id=? AND scope_id=? AND forget_epoch=?',
            (ref,scope.scope_id,self.repo.forget_epoch(scope.scope_id)))
        if not row or row['status']!='pending' or p.get('base_revision')!=row['revision']:
            raise SessionFailure('stale_context','动机已变化；先读取 motivations')
        decision=p.get('decision')
        reason=p.get('reason','')
        if decision not in {'act','defer','decline'} or not isinstance(reason,str) or not reason.strip() or len(reason)>240:
            raise SessionFailure('motivation_decision_required','选择 act/defer/decline，并给出具体原因')
        if decision=='act' and (not task or task['status']!='active' or self.role.tasks.blocker(task['ref'],scope.scope_id)):
            raise SessionFailure('motivation_activity_required','行动选择必须绑定一个可执行的真实 intent，等待中的事项不算已行动')
        now=self.life.clock()
        attempts=row['attempts']+1
        cooldown=min(self.role.resources.idle_max_seconds,
            self.role.resources.motivation_cooldown_seconds*2**min(attempts-1,4))
        delay=p.get('after_seconds',cooldown)
        if type(delay) not in {int,float} or not math.isfinite(delay) or not cooldown<=delay<=604800:
            raise SessionFailure('invalid_input','延期须在当前冷却与七天之间')
        self.repo._exec('UPDATE motivation_decisions SET status=?,reason=?,intent_ref=?,revision=revision+1,updated=?,next_due=?,attempts=? WHERE id=?',
            ('selected' if decision=='act' else 'deferred' if decision=='defer' else 'declined',reason,
             task['ref'] if task else None,now,now+delay,attempts,ref))
        if decision=='act':
            # Saving a goal is not sufficient: schedule its execution through
            # the existing task/wait/cancellation fences.
            self.role.schedule(scope,task['ref'],1,'继续执行已选择的动机行动，并核对结果。',kind='task_due')
        return dict(ref=ref,decision=decision,revision=row['revision']+1,execution='pending' if task else 'not_started')

    def finish(self,scope,root,p):
        proposals=p.get('motivation_decisions',[])
        if not isinstance(proposals,list) or len(proposals)>8:
            raise SessionFailure('invalid_input','motivation_decisions 最多八项')
        for proposal in proposals:
            if not isinstance(proposal,dict) or proposal.get('decision')=='act':
                raise SessionFailure('motivation_activity_required','act 使用 life_intent 的 motivation 参数；结束时只提交 defer/decline')
            self.decide(scope,proposal)
        turn=self.role.turn_number(root['session_id'])
        rows=self.repo._all('SELECT e.payload FROM agent_inputs i JOIN events e ON e.id=i.input_id WHERE i.session_id=? AND i.claimed_turn IS ?',
            (root['session_id'],turn))
        required={ref for r in rows for ref in json.loads(r['payload']).get('motivation_refs',[])}
        if required:
            pending=[dict(ref=r['id'],revision=r['revision'],dimension=r['dimension']) for r in self.rows(scope) if r['status']=='pending' and r['id'] in required]
            if pending:raise SessionFailure('motivation_decision_required','本轮动机尚无行动或延期决定；不要用新念头代替决定',candidates=pending)
        task_refs={w.get('intent_ref') for r in rows for w in json.loads(r['payload']).get('wake_plans',[]) if w.get('kind')=='task_due'}
        for candidate in self.rows(scope):
            if candidate['status']!='selected' or candidate['intent_ref'] not in task_refs:continue
            task=self.role.tasks.get(candidate['intent_ref'],scope.scope_id)
            if not task or self.role.tasks.blocker(task['ref'],scope.scope_id):continue
            if self.repo._one("SELECT 1 FROM role_operations WHERE scope_id=? AND intent_ref=? AND status IN ('dispatched','unknown')",(scope.scope_id,task['ref'])):
                continue  # The existing result/reconciliation path owns this wait.
            started=self.repo._one('SELECT MIN(e.received_at) n FROM agent_inputs i JOIN events e ON e.id=i.input_id WHERE i.session_id=? AND i.claimed_turn IS ?',
                (root['session_id'],turn))['n']
            progress=self.repo._one("SELECT 1 FROM role_operations WHERE scope_id=? AND intent_ref=? AND status='succeeded' AND created>=?",
                (scope.scope_id,task['ref'],started or self.life.clock()))
            if progress:
                self.role.schedule(scope,task['ref'],1,'本轮已有执行结果；继续下一步，或核对结果后完成/暂停该任务。',kind='task_due')
                continue
            blocker=p.get('activity_blocker','')
            if not isinstance(blocker,str) or not blocker.strip() or len(blocker)>240:
                raise SessionFailure('motivation_progress_required','已选择的动机任务尚无实际执行；请推进，或用 activity_blocker 说明具体阻碍，或显式等待/暂停任务')
            attempts=candidate['attempts']+1
            delay=self.role.resources.idle_delay(0)
            self.repo._exec('UPDATE motivation_decisions SET reason=?,attempts=?,next_due=? WHERE id=?',
                (blocker,attempts,self.life.clock()+delay,candidate['id']))
            self.role.schedule(scope,task['ref'],delay,'复核已记录的行动阻碍；条件未变则继续延期，不能声称已执行。',kind='task_due')
