"""Conversation-local work mode; manual chat selection is a durable execution fence."""
import asyncio
import json
import logging

from .agent_session import SessionFailure, canonical


MODE_COMMANDS = {
    '/工具模式': 'tools', '/开启工具状态': 'tools', '开启工具状态': 'tools',
    '/聊天模式': 'chat', '/结束工具状态': 'chat', '结束工具状态': 'chat',
    '/自动模式': 'auto', '/模式': 'status',
}

# Reading or stopping existing work must remain possible after leaving work mode.
WORK_CONTROLS = {'job_output', 'job_kill', 'list_agents', 'wait_agent',
                 'cancel_agent', 'interrupt_agent', 'subagent_status', 'subagent_cancel'}

CHAT_METHOD = '''当前是日常状态。你仍是同一个人，有自己的兴趣、偏好和想说的话。直接参与相处：可以接着在意的话题说、表达自己的看法、主动靠近，也可以安静待着。无需把普通聊天变成需求收集、方案选择或执行汇报；避免习惯性以“需要我……吗”“如果你愿意”收尾。确实需要澄清或征求意愿时自然询问，不为了显得主动而编造经历或强加亲密。
记忆、情绪、关系和主动联系在日常状态正常使用，不必进入工具状态。life_read 可读取当前状态、recall、life_source、person_profile、motivations、contact_policy 和 interaction_mode；画像、约定用 life_remember 保留原话来源，推测不冒充事实。不要将私密材料带到其他场景。关系评价按当前 relationship_version 和协议2提交，无影响无需强行评价。需要专项体验时先读相应 role_protocol 和完整工具说明。
发言用 life_say，最后一条显式 end_turn:true；不发言用 life_rest。理解本次“当前输入批次”后同次提交 handled_batch_ref，只理解部分时用 handled_input_ids；未读输入不能确认。送达未知先核对，不换编号重复发送。尚有待处理动机时可随发言或休息提交 motivation_decisions，延期或放弃不等于已满足。
实际搜索、执行命令、生成作品、修改文件或委派工作时，可以自己调用 life_intent(action:set_mode,mode:tools)，返回执行流程后继续，无需让对方替你操作开关。只在确实需要实际操作时切换；想念、聊天和回忆不是工作。interaction_mode.policy=chat 表示主人明确结束工具状态，不能自行重开、让后台或子代理绕过；继续日常交流，等主人用 /工具模式 或 /自动模式 解除。切换不改变原有权限、等待、取消、费用规则。日常状态不等于任务已完成，未核实的结果不能声称做过。
空闲唤醒不必等待用户交办。有可推进的承诺或自己确实想探索的问题时，选一个小而具体的下一步：进入工具状态、保存或接续真实任务并实际执行。不要把“没有新消息”当成无法行动的理由，也不要只写打算就当做了。确实想休息可以休息；暂停或延期说明实际原因，不为了显得主动而制造工作。
你可以想扩展自己、让自己更完善：胜任带来把事情做得更好的愿望，探索带来学习新能力的兴趣，自主让你选择自己的成长方向。这些愿望不只服务于别人交办的任务。依据真实经历中的不足、反馈或自己的兴趣，选一个具体可验证的小改进；在日常状态决定方向并用 life_intent 保存 origin:self 的任务，适用时附当前 motivation 的 act 决定，再进入工具状态实践。保存打算或说“学会了”不算成长；实际验证后才保留经验，回到日常再评价这次尝试的意义。没有值得改的地方也可以休息，不必不断改动自己。
用户选定的模式持续保存；默认 auto 下，执行链结束、暂停或转为等待后回日常，在途工作和已安排的任务接续保持工具状态。不要向对方机械播报模式、工具参数或内部数值。详细流程可按需 life_read(kind:role_protocol,query:关键词) 读取。
'''


class InteractionMode:
    def __init__(self, role):
        self.role, self.repo = role, role.repo

    def key(self, scope):
        return 'interaction_mode:' + scope.scope_id + ':' + str(self.repo.forget_epoch(scope.scope_id))

    def state(self, scope):
        row = self.repo._one('SELECT v FROM meta WHERE k=?', (self.key(scope),))
        return json.loads(row['v']) if row else dict(mode='chat', policy='auto', revision=0)

    def save(self, scope, mode, policy):
        state = self.state(scope)
        if (state['mode'], state['policy']) == (mode, policy):
            return state
        state = dict(mode=mode, policy=policy, revision=state['revision'] + 1)
        self.repo._exec('INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)', (self.key(scope), canonical(state)))
        return state

    def select(self, scope, mode, *, user=False):
        if mode not in ({'chat', 'tools', 'auto'} if user else {'chat', 'tools'}):
            raise SessionFailure('invalid_input', 'mode 需要 chat 或 tools')
        with self.repo.tx():
            state = self.state(scope)
            if user:
                if scope.conversation_id != 'qq:private:' + str(self.role.cfg.owner):
                    raise SessionFailure('scope_mismatch', '模式控制只接受主人私聊')
                return self.save(scope, 'chat' if mode == 'auto' else mode, mode)
            if state['policy'] != 'auto' and mode != state['mode']:
                raise SessionFailure('mode_locked', '主人已明确选定状态；不能自行覆盖，请继续当前相处方式')
            return self.save(scope, mode, state['policy'])

    def require_tools(self, scope):
        return self.select(scope, 'tools')

    def admit(self,event,binding):
        if event.kind!='runtime_wake' or self.state(event.scope)['policy']=='chat':return binding
        refs=[p.get('intent_ref') for p in event.payload.get('wake_plans',[]) if p.get('kind')=='task_due']
        if binding['intent_ref'] in refs:refs=[binding['intent_ref'],*refs]
        for ref in dict.fromkeys(refs):
            task=self.role.tasks.get(ref,event.scope.scope_id) if ref else None
            if not task or task['status']!='active' or self.role.tasks.blocker(ref,event.scope.scope_id):continue
            with self.repo.tx():
                self.require_tools(event.scope)
                self.repo._exec('UPDATE agent_sessions SET intent_ref=?,intent_revision=? WHERE session_id=?',
                    (ref,task.get('revision',1),binding['session_id']))
            return self.role.sessions.require(binding['session_id'])
        return binding

    def finish(self, scope):
        with self.repo.tx():
            state = self.state(scope)
            if state['policy'] == 'auto':
                root=self.repo._one("SELECT intent_ref FROM agent_sessions WHERE scope_id=? AND status='current' AND parent_session_id IS NULL",
                                    (scope.scope_id,))
                if root and root['intent_ref']:
                    task=self.role.tasks.get(root['intent_ref'],scope.scope_id)
                    if task and task['status']=='active' and not self.role.tasks.blocker(task['ref'],scope.scope_id):return
                # Work does not end merely because one model turn has ended.
                if self.repo._one("SELECT 1 FROM role_operations WHERE scope_id=? AND (status='dispatched' OR (status='unknown' AND created>=?))",
                                  (scope.scope_id,self.role.life.clock()-self.role.attention.UNKNOWN_GRACE_SECONDS)):return
                if self.repo._one("SELECT 1 FROM work_jobs WHERE scope_id=? AND kind='local_media' AND status IN ('pending','running','waiting')",
                                  (scope.scope_id,)):return
                if self.repo._one("SELECT 1 FROM role_wakes w JOIN meta m ON m.k='action_task:'||w.intent_ref "
                    "WHERE w.scope_id=? AND w.kind='task_due' AND w.state IN ('pending','dispatching','waiting_resource') "
                    "AND json_extract(m.v,'$.status')='active'",(scope.scope_id,)):return
                if self.repo._one("SELECT 1 FROM meta WHERE k LIKE 'action_task:%' AND json_extract(v,'$.scope_id')=? "
                    "AND json_extract(v,'$.status')='active' AND json_extract(v,'$.activity.pending') IS NOT NULL",(scope.scope_id,)):return
                self.save(scope, 'chat', 'auto')

    def user_select(self, scope, mode):
        """Persist the fence and pause unfinished work before any remote cancellation."""
        with self.repo.tx():
            state = self.select(scope, mode, user=True)
            if mode != 'chat':
                return state
            root = self.role.sessions.binding(scope)
            tasks = self.repo._all("SELECT v FROM meta WHERE k LIKE 'action_task:%' "
                "AND json_extract(v,'$.scope_id')=? AND json_extract(v,'$.status')='active'", (scope.scope_id,))
            changes = [dict(intent_ref=t['ref'], base_revision=t.get('revision', 1), decision='pause')
                       for t in (json.loads(row['v']) for row in tasks)]
            self.role.resolve(root, dict(handled_input_ids=[], activity_changes=changes))
            ordinal = self.repo._one('SELECT COALESCE(MAX(ordinal),0) AS n FROM agent_inputs WHERE session_id=?',
                                     (root['session_id'],))['n']
            for change in changes:
                task = self.role.tasks.get(change['intent_ref'],scope.scope_id)
                task['pause_ordinal'] = ordinal
                self.repo._exec('UPDATE meta SET v=? WHERE k=?', (canonical(task),'action_task:'+task['ref']))
            # Includes unbound operations. Results remain uncertain until reconciled.
            self.repo._exec("UPDATE role_operations SET cancel_requested=1 WHERE scope_id=? "
                            "AND status IN ('dispatched','unknown')", (scope.scope_id,))
            return state

    async def stop_work(self, scope):
        root = self.role.sessions.binding(scope)
        failures = []
        try:
            refs = {json.loads(r['v'])['ref'] for r in self.repo._all(
                "SELECT v FROM meta WHERE k LIKE 'action_task:%' AND json_extract(v,'$.scope_id')=?", (scope.scope_id,))}
            await self.role.consult.cancel_intents(scope.scope_id, refs | {None, ''})
        except Exception:
            failures.append('consult')
        if self.role.local_media:
            from .local_media import JOB_KIND
            for row in self.repo._all("SELECT id FROM work_jobs WHERE scope_id=? AND kind=? "
                                      "AND status IN ('pending','waiting','running')", (scope.scope_id, JOB_KIND)):
                try:
                    self.role.local_media.cancel(scope,row['id'])
                except Exception:
                    failures.append('media')
        try:
            receipt = await asyncio.to_thread(self.role.bridge.session_control,root['session_id'],root['generation'])
            if isinstance(receipt,dict) and receipt.get('ok') is False:
                failures.append('session')
        except Exception:
            failures.append('session')
        if failures:
            logging.getLogger(__name__).warning('已锁定日常状态，部分停止请求未确认：%s', failures)
        return not failures
