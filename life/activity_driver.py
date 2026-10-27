"""Continue explicit activities after a settled turn, with durable stop fences."""
from dataclasses import dataclass, fields
import json
import time

from .agent_session import SessionFailure, canonical, digest
from .contracts import Scope
from .life_access import RecallError


@dataclass(frozen=True)
class ActivityConfig:
    enabled: bool = True
    step_delay_seconds: int = 5
    no_progress_limit: int = 3
    self_step_limit: int = 8

    @classmethod
    def parse(cls, raw=None):
        raw = {} if raw is None else raw
        if not isinstance(raw, dict) or set(raw)-{f.name for f in fields(cls)}:
            raise ValueError('invalid_activity_config')
        result = cls(**raw)
        if (type(result.enabled) is not bool or any(type(v) is not int or v < 1
                for k,v in result.__dict__.items() if k != 'enabled') or result.step_delay_seconds > 3600):
            raise ValueError('invalid_activity_config')
        return result


class ActivityDriver:
    def __init__(self, role):
        self.role, self.repo = role, role.repo
        self.config = ActivityConfig.parse(getattr(role.cfg, 'activity', {}))

    def configure(self, scope, task, proposal, bind):
        mode = proposal.get('execution_mode', task.get('execution_mode'))
        if mode is None:
            mode = 'continuous' if bind and task.get('kind') == 'ongoing' and task.get('origin') == 'user' else 'manual'
        if mode not in {'manual', 'continuous'} or mode == 'continuous' and not bind:
            raise SessionFailure('invalid_input', 'execution_mode 为 manual 或 continuous；持续执行必须绑定当前任务')
        state = dict(task.get('activity', {}))
        if not state or proposal.get('action') == 'resume':
            state.update(steps=0, stalled=0, pending=None, hold=None)
        return self.role.tasks.ensure(scope, task['ref'], None, execution_mode=mode, activity=state)

    def accessible_elsewhere(self, scope, task):
        actor=scope.participant_id
        if not actor or scope.conversation_id!='qq:private:'+actor or task['scope_id']==scope.scope_id:return False
        origins=[self.repo.get_event(ref) for ref in task.get('origin_source_event_ids',[])]
        if not origins or any(not e or e.actor_id!=actor or e.source_kind!='user_report' for e in origins):return False
        if task.get('parent_ref') or task.get('intent_id') or task.get('want_id') or task.get('merged_into'):return False
        ctx=self.role.life.life_access.bind(scope)
        try:
            for ref in set(task.get('source_event_ids',[])+task.get('origin_source_event_ids',[])):
                self.role.life.life_access.load(ctx,ref,track=False)
        except RecallError:return False
        return True

    def related(self, scope):
        result=[]
        for row in self.repo._all("SELECT v FROM meta WHERE k LIKE 'action_task:%' AND json_extract(v,'$.status') IN ('active','paused')"):
            task=json.loads(row['v'])
            if self.accessible_elsewhere(scope,task):
                result.append({k:task.get(k) for k in ('ref','scope_id','goal','checkpoint','status','revision')})
        return result[:20]

    def transfer(self,scope,p,root):
        task=self.role.tasks.get(p.get('ref',''))
        if not task or not self.accessible_elsewhere(scope,task):raise SessionFailure('scope_mismatch','只能把本人有权读取的群聊事项接到与本人的私聊，不能搬运他人或私密场景材料')
        if task.get('revision',1)!=p.get('base_revision'):raise SessionFailure('stale_context','事项版本已变化')
        if task['status'] not in {'active','paused'} or task.get('pause_owner')=='user':raise SessionFailure('cancel_requested','等待答复或用户停止的事项须在原场景明确恢复')
        evidence=self.role._sources(scope,p,root)
        latest=self.repo._one("SELECT input_id FROM agent_inputs WHERE session_id=? AND state IN ('consumed','settled') ORDER BY ordinal DESC LIMIT 1",(root['session_id'],))
        if not any((event:=self.repo.get_event(ref)) and event.source_kind=='user_report' and event.actor_id==scope.participant_id
                and latest and latest['input_id']==ref
                and self.repo._one("SELECT 1 FROM agent_inputs WHERE session_id=? AND input_id=? AND state IN ('consumed','settled')",(root['session_id'],ref)) for ref in evidence):
            raise SessionFailure('invalid_input','转移需要当前私聊中本人的真实已读请求')
        if (len(self.role.tasks.dependents(task['ref'],task['scope_id']))>1 or self.repo._one("SELECT 1 FROM role_operations WHERE intent_ref=? AND status IN ('dispatched','unknown')",(task['ref'],))
                or self.repo._one("SELECT 1 FROM work_jobs WHERE root_id=? AND status IN ('pending','running','waiting')",(task['ref'],))):
            raise SessionFailure('outcome_unknown','原事项仍有子任务或未完成操作，先核对，不能在新场景重复执行')
        old_scope=task['scope_id'];state=dict(task.get('activity',{}));state.update(pending=None,hold=None)
        task.update(scope_id=scope.scope_id,conversation_id=scope.conversation_id,revision=task.get('revision',1)+1,
            source_event_ids=list(evidence),activity=state,
            transferred_from=old_scope,updated_at=time.time())
        self.repo._exec('UPDATE meta SET v=? WHERE k=?',(canonical(task),'action_task:'+task['ref']))
        self.repo._exec("UPDATE role_wakes SET state='cancelled' WHERE intent_ref=? AND scope_id=?",(task['ref'],old_scope))
        self.repo._exec('UPDATE agent_sessions SET intent_ref=NULL,intent_revision=NULL WHERE scope_id=? AND intent_ref=?',(old_scope,task['ref']))
        self.repo._exec('UPDATE agent_sessions SET intent_ref=?,intent_revision=? WHERE session_id=?',(task['ref'],task['revision'],root['session_id']))
        return dict(intent=task,transferred=True)

    def bound(self, scope, root):
        binding = self.role.sessions.require(root['session_id'])
        task = self.role.tasks.get(binding['intent_ref'], scope.scope_id) if binding['intent_ref'] else None
        return task if (self.config.enabled and task and task.get('execution_mode') == 'continuous'
                        and task['status'] == 'active') else None

    def delivered(self, scope, root, content):
        task = self.bound(scope, root)
        if not task:return
        state = dict(task.get('activity', {})); hashes = state.get('delivered_hashes', [])
        key = digest(content)
        if key not in hashes:
            state['delivered_hashes'] = [*hashes, key][-64:]
            state['delivery_progress'] = state.get('delivery_progress', 0)+1
            self.role.tasks.ensure(scope, task['ref'], None, activity=state)

    def finish(self, scope, root, proposal):
        task = self.bound(scope, root)
        if not task:return
        state = dict(task.get('activity', {}))
        turn = self.role.turn_number(root['session_id'])
        cycle = root['session_id']+':'+str(turn)
        if state.get('last_cycle') == cycle:return
        results = self.repo._one("SELECT COUNT(DISTINCT tool||':'||request_hash||':'||COALESCE(result,'')) AS n FROM role_operations WHERE scope_id=? AND intent_ref=? "
            "AND status='succeeded' AND cancel_requested=0", (scope.scope_id, task['ref']))['n']
        progress = [state.get('delivery_progress', 0), results]
        stalled = 0 if progress != state.get('progress', [0, 0]) else state.get('stalled', 0)+1
        state.update(progress=progress, stalled=stalled, steps=state.get('steps', 0)+1, last_cycle=cycle)
        mode = (proposal.get('next_attention') or {}).get('mode', 'adaptive')
        if stalled >= self.config.no_progress_limit or (task.get('origin') != 'user' and state['steps'] >= self.config.self_step_limit):
            reason = 'activity_no_progress' if stalled >= self.config.no_progress_limit else 'activity_self_step_limit'
            self.role.tasks.transition(scope.scope_id, task['ref'], 'paused', cause=reason)
            state.update(pending=None, hold=reason)
        elif mode in {'none', 'event'}:
            state.update(pending=None, hold='explicit_'+mode)
        else:
            delay = proposal.get('wake_after_seconds', (proposal.get('next_attention') or {}).get('after_seconds', self.config.step_delay_seconds))
            if not isinstance(delay, (int,float)) or isinstance(delay,bool) or not 0 <= delay <= 86400:
                raise SessionFailure('invalid_input', '持续活动的接续间隔无效')
            state.update(pending=dict(session_id=root['session_id'], epoch=self.repo.forget_epoch(scope.scope_id),
                cycle=cycle, delay=delay), hold='turn_finishing')
        self.role.tasks.ensure(scope, task['ref'], None, activity=state)

    def settle(self):
        if not self.config.enabled:return
        with self.repo.tx():
            rows = self.repo._all("SELECT v FROM meta WHERE k LIKE 'action_task:%' AND json_extract(v,'$.activity.pending') IS NOT NULL")
            for row in rows:
                task = json.loads(row['v']); state = dict(task['activity']); pending = state['pending']
                binding = self.repo._one("SELECT * FROM agent_sessions WHERE scope_id=? AND status='current' AND parent_session_id IS NULL", (task['scope_id'],))
                if not binding:continue
                scope = Scope(**json.loads(binding['scope_json']))
                if self.role.tasks.blocker(task['ref'],scope.scope_id) or pending['epoch'] != self.repo.forget_epoch(scope.scope_id):
                    state.update(pending=None, hold='stopped_or_forgotten')
                else:
                    last = self.repo._one("SELECT payload FROM agent_events WHERE session_id=? AND json_extract(payload,'$.type') IN ('turn/start','turn/end') ORDER BY seq DESC LIMIT 1", (binding['session_id'],))
                    if self.role.sessions.pending(binding['session_id']) or last and json.loads(last['payload'])['type'] == 'turn/start':continue
                    if self.repo._one("SELECT 1 FROM role_operations WHERE scope_id=? AND intent_ref=? AND status IN ('dispatched','unknown')", (scope.scope_id,task['ref'])):continue
                    if self.repo._one("SELECT 1 FROM work_jobs WHERE scope_id=? AND root_id=? AND status IN ('running','pending','waiting')", (scope.scope_id,task['ref'])):continue
                    if binding['intent_ref'] and binding['intent_ref'] != task['ref']:continue
                    # Binding is durable across restarts; no copied conversation or fabricated user input.
                    self.repo._exec('UPDATE agent_sessions SET intent_ref=?,intent_revision=? WHERE session_id=?',
                        (task['ref'],task.get('revision',1),binding['session_id']))
                    plan = self.role.schedule(scope,task['ref'],pending['delay'],
                        '继续已确认的持续任务 '+task['ref']+'：先读取任务和检查点，执行下一步；已有发送不重复。遇到等待、停止或阻碍如实保存状态。',
                        kind='task_due',sources=task.get('source_event_ids',[]))
                    self.repo._exec("UPDATE role_wakes SET state='cancelled' WHERE scope_id=? AND kind='idle_probe' AND state='pending'", (scope.scope_id,))
                    state.update(pending=None, hold=None, plan_id=plan, scheduled_at=time.time())
                self.role.tasks.ensure(scope,task['ref'],None,activity=state)
