import json
import time
import unittest
from unittest.mock import patch

from life.agent_session import SessionFailure
from life.motivation import Motivation
from life.persona_files import PersonaFiles
from life.self_change import SelfChange,file_hash
from tests.role_fixture import Fixture


class MotivationAndPeopleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        self.role,self.life,self.repo=self.f.role,self.f.life,self.f.life.repo
        self.scope,self.root=self.f.scope,self.f.binding

    def high(self,dimension='exploration'):
        state=self.life.drive_state(self.scope)
        state.drives[dimension].tension=.95
        state.drives[dimension].recovery_until=0
        state.drives[dimension].phase='high'
        self.repo.save_state(self.life._drive_aggregate(self.scope),state.to_dict())
        return self.role.motivations.scan(self.scope)[0]

    def trigger(self,ref):
        event,_=self.life.ingest(self.scope,'runtime_wake','inference','考虑一个具体行动',
            payload=dict(wake_kind='motivation',wake_plans=[dict(kind='motivation')],motivation_refs=[ref]))
        self.role.sessions.enqueue(self.root,event)
        self.role.sessions.apply_event(self.root['session_id'],dict(type='step/start',seq=0,time=time.time(),data=dict(turn=1,step=1)))
        with self.repo.tx():
            self.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=1,handled=1 WHERE session_id=?",(self.root['session_id'],))

    def profile(self,scope=None,actor='1',**changes):
        scope=scope or self.scope
        event,_=self.life.ingest(scope,'message','user_report','请叫我小林，我喜欢星空图鉴。',actor_id=actor)
        return dict(kind='person_profile',actor_id=actor,field='address',topic_key='address.name',
            text='称呼小林',origin='explicit_statement',sources=[event.id],base_revision=0,**changes)

    async def test_high_drive_schedules_one_wake_without_reward_or_model_call(self):
        self.high()
        before=self.life.drive_state(self.scope).drives['exploration'].tension
        now=time.time();self.role.motivations.tick(now)
        self.role.motivations.next_check=0;self.role.motivations.tick(now+31)
        self.assertFalse(self.repo._one('SELECT 1 FROM role_wakes'))
        self.role.attention.finish(self.scope,self.root,{},'sequence-finished')
        rows=self.repo._all("SELECT * FROM role_wakes WHERE kind='idle_probe' AND state='pending'")
        self.assertEqual(len(rows),1)
        self.assertGreaterEqual(rows[0]['due']-time.time(),599)
        self.assertLessEqual(rows[0]['due']-time.time(),3600)
        self.assertEqual(self.life.drive_state(self.scope).drives['exploration'].tension,before)
        self.assertFalse(self.repo._one('SELECT 1 FROM usage_calls'))
        self.assertFalse(self.repo._one('SELECT 1 FROM drive_settlements'))

    async def test_budget_and_explicit_sleep_do_not_get_overridden(self):
        self.high()
        with patch.object(self.role.spending,'background_allowed',return_value=False):
            self.role.motivations.tick(time.time())
        self.assertFalse(self.repo._one('SELECT 1 FROM role_wakes'))
        with self.repo.tx():
            self.repo._exec('INSERT INTO meta VALUES(?,?)',('attention_explicit:'+self.scope.scope_id,'{"mode":"none"}'))
        self.role.motivations.next_check=0;self.role.motivations.tick(time.time())
        self.assertFalse(self.repo._one('SELECT 1 FROM role_wakes'))

    async def test_motivation_wake_requires_concrete_decision_not_new_thought(self):
        candidate=self.high();self.trigger(candidate['id'])
        with self.assertRaises(SessionFailure) as got:
            self.role.rest(self.scope,{},self.root)
        self.assertEqual(got.exception.code,'motivation_decision_required')
        result=self.role.rest(self.scope,dict(motivation_decisions=[dict(ref=candidate['id'],
            base_revision=candidate['revision'],decision='defer',reason='先处理当前交办')]),self.root)
        self.assertTrue(result['resting'])
        self.assertEqual(self.role.motivations.view(self.scope)['candidates'][0]['status'],'deferred')
        self.assertFalse(self.repo._one('SELECT 1 FROM drive_settlements'))

    async def test_act_binds_real_task_and_schedules_execution_without_claiming_completion(self):
        candidate=self.high()
        with self.repo.tx():
            result=self.role.intent(self.scope,dict(goal='查证一个星空知识点',sources=[self.f.event.id],
                motivation=dict(ref=candidate['id'],base_revision=1,decision='act',reason='想弄清楚一个具体问题')),self.root)
        self.assertEqual(result['motivation']['execution'],'pending')
        task=result['intent']
        self.assertTrue(self.repo._one("SELECT 1 FROM role_wakes WHERE intent_ref=? AND kind='task_due' AND state='pending'",(task['ref'],)))
        view=self.role.motivations.view(self.scope)['candidates'][0]
        self.assertEqual(view['execution']['task_status'],'active')
        self.assertEqual(view['execution']['operations'],{})
        self.assertFalse(self.repo._one('SELECT 1 FROM drive_settlements'))

    async def test_waiting_task_cannot_be_used_as_action_and_stale_decision_cannot_repeat(self):
        candidate=self.high();task=self.f.task()
        self.role.tasks.transition(self.scope.scope_id,task['ref'],'paused',cause='test')
        task=self.role.tasks.get(task['ref'],self.scope.scope_id)
        with self.assertRaises(SessionFailure):
            self.role.motivations.decide(self.scope,dict(ref=candidate['id'],base_revision=1,decision='act',reason='尝试绕过等待'),task)
        choice=dict(ref=candidate['id'],base_revision=1,decision='decline',reason='这次先休息')
        self.role.motivations.decide(self.scope,choice)
        with self.assertRaises(SessionFailure):self.role.motivations.decide(self.scope,choice)

    async def test_completion_requires_owned_successful_result_and_does_not_award_satisfaction(self):
        candidate=self.high()
        with self.repo.tx():
            result=self.role.intent(self.scope,dict(goal='核对星空资料',sources=[self.f.event.id],
                motivation=dict(ref=candidate['id'],base_revision=1,decision='act',reason='好奇')),self.root)
        task=result['intent']
        proposal=dict(action='done',ref=task['ref'],base_revision=task['revision'],sources=[self.f.event.id])
        with self.assertRaises(SessionFailure) as got:
            with self.repo.tx():self.role.intent(self.scope,proposal,self.root)
        self.assertEqual(got.exception.code,'motivation_result_required')
        thought,_=self.life.ingest(self.scope,'reflection','inference','我已经全部完成了')
        proposal['result_sources']=[thought.id]
        with self.assertRaises(SessionFailure):
            with self.repo.tx():self.role.intent(self.scope,proposal,self.root)
        with self.repo.tx():
            self.repo._exec("INSERT INTO role_operations(id,session_id,scope_id,intent_ref,tool,request_hash,arguments,status,created) VALUES(?,?,?,?,?,'test','{}','succeeded',?)",
                ('result-op',self.root['session_id'],self.scope.scope_id,task['ref'],'web_fetch',time.time()))
        event,_=self.life.ingest(self.scope,'tool_result','tool_observation','已读取所需资料',payload=dict(operation_id='result-op'))
        proposal['result_sources']=[event.id]
        with self.repo.tx():result=self.role.intent(self.scope,proposal,self.root)
        self.assertEqual(result['intent']['status'],'done')
        view=self.role.motivations.view(self.scope)['candidates'][0]
        self.assertEqual(view['execution']['result']['state'],'result_recorded')
        self.assertFalse(self.repo._one('SELECT 1 FROM drive_settlements'))
        result=self.role.attention.finish(self.scope,self.root,{},'done-in-same-turn')
        self.assertEqual(result['mode'],'adaptive')
        self.assertFalse(self.repo._one("SELECT 1 FROM role_wakes WHERE kind='task_due' AND state='pending'"))

    async def test_real_dispatch_carries_candidate_refs_and_sleep_cancels_pending_drive_wake(self):
        from unittest.mock import AsyncMock
        candidate=self.high();now=time.time()
        self.role.motivations.tick(now)
        events=[]
        async def admit(event):
            events.append(event)
            return dict(state='accepted')
        self.role.ingest=AsyncMock(side_effect=admit)
        self.role.attention.finish(self.scope,self.root,{},'sequence-finished')
        await self.role.attention.dispatch_due(now+3601)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0].payload['motivation_refs'],[candidate['id']])
        self.role.schedule(self.scope,None,0,'pending',kind='motivation')
        self.role.attention.finish(self.scope,self.root,dict(next_attention=dict(mode='none')),'sleep')
        self.assertFalse(self.repo._one("SELECT 1 FROM role_wakes WHERE kind='motivation' AND state='pending'"))

    async def test_chosen_task_cannot_silently_end_without_progress_or_blocker(self):
        candidate=self.high()
        with self.repo.tx():
            result=self.role.intent(self.scope,dict(goal='查清一个问题',sources=[self.f.event.id],
                motivation=dict(ref=candidate['id'],base_revision=1,decision='act',reason='好奇')),self.root)
        task=result['intent']
        event,_=self.life.ingest(self.scope,'runtime_wake','inference','继续行动',payload=dict(wake_plans=[dict(kind='task_due',intent_ref=task['ref'])]))
        self.role.sessions.enqueue(self.root,event)
        self.role.sessions.apply_event(self.root['session_id'],dict(type='step/start',seq=0,time=time.time(),data=dict(turn=1,step=1)))
        with self.repo.tx():self.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=1,handled=1 WHERE session_id=?",(self.root['session_id'],))
        with self.assertRaises(SessionFailure) as got:
            with self.repo.tx():self.role.rest(self.scope,{},self.root)
        self.assertEqual(got.exception.code,'motivation_progress_required')
        with self.repo.tx():self.role.rest(self.scope,dict(activity_blocker='来源服务暂时不可用'),self.root)
        wake=self.repo._one("SELECT due FROM role_wakes WHERE intent_ref=? AND state='pending'",(task['ref'],))
        self.assertGreaterEqual(wake['due']-time.time(),599)
        self.assertIn('来源服务',self.role.motivations.view(self.scope)['candidates'][0]['reason'])

    async def test_expired_or_forgotten_profile_can_be_replaced_without_hidden_revision(self):
        p=self.profile();self.role.remember(self.scope,p,self.root,call_key='old')
        self.repo.redact_events(p['sources'])
        self.root=self.role.sessions.binding(self.scope)
        fresh=self.profile();fresh['text']='称呼新名字'
        result=self.role.remember(self.scope,fresh,self.root,call_key='new')
        self.assertEqual(result['revision'],1)
        self.assertEqual(self.role.people.visible(self.scope,'1')[0]['text'],'称呼新名字')

    async def test_restart_keeps_cooldown_and_reset_invalidates_candidates(self):
        candidate=self.high()
        self.role.motivations.decide(self.scope,dict(ref=candidate['id'],base_revision=1,decision='defer',reason='稍后考虑'))
        replacement=Motivation(self.role)
        self.assertEqual(replacement.scan(self.scope),[])
        self.repo.reset_scope(self.scope)
        self.assertEqual(replacement.view(self.scope)['candidates'],[])
        with self.assertRaises(SessionFailure):replacement.decide(self.scope,dict(ref=candidate['id'],base_revision=2,decision='defer',reason='旧决定'))

    async def test_exact_person_evidence_and_correction_version(self):
        proposal=self.profile()
        result=self.role.remember(self.scope,proposal,self.root,call_key='profile1')
        self.assertEqual(result['revision'],1)
        view=self.role.read(self.scope,dict(kind='person_profile',actor_id='1'),self.root)
        self.assertEqual(view['items'][0]['text'],'称呼小林')
        correction=self.profile();correction.update(text='称呼林老师',base_revision=1)
        self.role.remember(self.scope,correction,self.root,call_key='profile2')
        self.assertEqual([r['text'] for r in self.role.people.visible(self.scope,'1')],['称呼林老师'])
        with self.assertRaises(SessionFailure):self.role.remember(self.scope,proposal,self.root,call_key='profile-stale')

    async def test_group_mention_cannot_be_misattributed_to_mentioned_person(self):
        group=self.life.scope('qq:group:9','')
        self.life.ingest(group,'message','user_report','我是小林。',actor_id='2')
        p=self.profile(group,actor='1');p['actor_id']='2'
        root=self.role.sessions.binding(group)
        with self.assertRaises(SessionFailure) as got:self.role.remember(group,p,root,call_key='misattribute')
        self.assertEqual(got.exception.code,'actor_mismatch')
        p['origin']='interpretation'
        self.role.remember(group,p,root,call_key='interpretation')
        self.assertEqual(self.role.people.visible(group,'2')[0]['origin'],'interpretation')
        self.assertEqual(self.role.people.visible(group,'1'),[])

    async def test_private_profile_is_not_injected_into_group_and_forgetting_hides_it(self):
        p=self.profile();self.role.remember(self.scope,p,self.root,call_key='private')
        group=self.life.scope('qq:group:9','1')
        self.assertEqual(self.role.people.visible(group,'1'),[])
        self.repo.redact_events(p['sources'])
        self.assertEqual(self.role.people.visible(self.scope,'1'),[])

    async def test_inference_cannot_replace_direct_statement_and_has_bounded_lifetime(self):
        p=self.profile();self.role.remember(self.scope,p,self.root,call_key='direct')
        p.update(origin='interpretation',base_revision=1,text='我猜他喜欢另一个称呼')
        with self.assertRaises(SessionFailure):self.role.remember(self.scope,p,self.root,call_key='inference')
        p.update(topic_key='other.topic',base_revision=0,lifetime_days=8)
        with self.assertRaises(SessionFailure):self.role.remember(self.scope,p,self.root,call_key='expired-inference')

    async def test_unified_card_routes_all_scenes_and_hot_reloads_same_session(self):
        self.f.cfg.unified_persona=True
        self.f.cfg.persona='unified.md'
        path=self.f.root/'unified.md';path.write_text('A curious adult character named Fish.',encoding='utf-8')
        files=PersonaFiles(self.f.root,self.f.cfg);files.migrate()
        scopes=[self.scope,self.life.scope('qq:private:2','2'),self.life.scope('qq:group:9','2')]
        for scope in scopes:
            text=self.role.stable_context(scope,{})
            self.assertIn('A curious adult character named Fish.',text)
            self.assertNotIn('公开与私有人格分别保存在两个独立文件',text)
        service=SelfChange(self.life,self.f.cfg,self.f.root)
        await service.apply(self.scope,dict(target='persona',content='An adult character named Fish who loves astronomy.',base_hash=file_hash(path)),'unified-update')
        for scope in scopes:self.assertIn('loves astronomy',self.role.stable_context(scope,{}))
        with self.assertRaises(SessionFailure):await service.apply(scopes[2],dict(target='persona_public',content='changed'),'not-owner')

    async def test_old_statement_cannot_overwrite_new_correction_even_with_current_revision(self):
        original=self.profile();self.role.remember(self.scope,original,self.root,call_key='first')
        corrected=self.profile();corrected.update(text='称呼林老师',base_revision=1)
        self.role.remember(self.scope,corrected,self.root,call_key='second')
        original['base_revision']=2
        with self.assertRaises(SessionFailure) as got:
            self.role.remember(self.scope,original,self.root,call_key='replay-old')
        self.assertEqual(got.exception.code,'stale_profile_evidence')

    async def test_scene_context_uses_actual_speakers_and_separate_profiles(self):
        self.f.cfg.unified_persona=True
        group=self.life.scope('qq:group:9','')
        root=self.role.sessions.binding(group)
        for actor,name in [('1','小林'),('2','阿星')]:
            p=self.profile(group,actor=actor);p['text']='称呼'+name
            self.role.remember(group,p,root,call_key='group-profile-'+actor)
            event=self.repo.get_event(p['sources'][0]);self.role.sessions.enqueue(root,event)
        packet=json.loads(self.role.context(root,group,{}).split('当前状态与材料：\n')[1])
        self.assertEqual(packet['scene']['kind'],'group')
        self.assertTrue(packet['scene']['unified_persona'])
        self.assertEqual(set(packet['scene']['actors']),{'1','2'})
        self.assertEqual(packet['person_profiles']['1'][0]['text'],'称呼小林')
        self.assertEqual(packet['person_profiles']['2'][0]['text'],'称呼阿星')

    async def test_rejected_empty_expression_does_not_commit_drive_decision(self):
        candidate=self.high();self.trigger(candidate['id'])
        with self.assertRaises(SessionFailure):
            await self.role.say(self.scope,dict(text='',motivation_decisions=[dict(ref=candidate['id'],base_revision=1,decision='defer',reason='暂缓')]),self.root,'empty')
        self.assertEqual(self.role.motivations.rows(self.scope)[0]['status'],'pending')

    async def test_idle_timer_waits_for_tool_settlement_and_explicit_continuation(self):
        with self.repo.tx():
            self.repo._exec("INSERT INTO role_operations(id,session_id,scope_id,tool,request_hash,arguments,status,created) VALUES(?,?,?,'research','test','{}','dispatched',?)",
                ('inflight',self.root['session_id'],self.scope.scope_id,time.time()))
        result=self.role.attention.finish(self.scope,self.root,{},'finish-with-work')
        self.assertEqual(result['mode'],'after_sequence')
        self.role.attention.settle_sequences()
        self.assertFalse(self.repo._one('SELECT 1 FROM role_wakes'))
        with self.repo.tx():self.repo._exec("UPDATE role_operations SET status='unknown'")
        self.role.attention.settle_sequences()
        self.assertFalse(self.repo._one('SELECT 1 FROM role_wakes'))
        with self.repo.tx():self.repo._exec("UPDATE role_operations SET status='succeeded'")
        self.role.schedule(self.scope,None,1,'下一步',kind='self_scheduled')
        self.role.attention.settle_sequences()
        self.assertFalse(self.repo._one("SELECT 1 FROM role_wakes WHERE kind='idle_probe'"))
        with self.repo.tx():self.repo._exec("UPDATE role_wakes SET state='delivered'")
        with patch('life.resource_policy.time.time',return_value=10000),patch('life.resource_policy.random.SystemRandom') as rng:
            rng.return_value.randint.return_value=3600
            self.role.attention.settle_sequences()
        self.assertEqual(self.repo._one("SELECT due FROM role_wakes WHERE kind='idle_probe'")['due'],13600)


if __name__=='__main__':unittest.main()
