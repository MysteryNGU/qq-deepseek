"""Context projections use existing facts and preserve execution/source fences."""
import json
from pathlib import Path
import unittest

from life.agent_session import SessionFailure,canonical
from life.resource_policy import ResourcePolicy,estimate_tokens
from life.tool_contracts import catalog,validate,validate_arguments
from tests.role_fixture import Fixture


class ContextEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        self.role,self.life,self.repo=self.f.role,self.f.life,self.f.life.repo
        self.role.capability_protocol=1;self.role.compact_tool_protocol=1

    def save_profile(self,scope,actor,field,topic,text):
        event,_=self.life.ingest(scope,'message','user_report',text,actor_id=actor)
        binding=self.role.sessions.binding(scope)
        return self.role.people.save(scope,dict(actor_id=actor,field=field,topic_key=topic,text=text,
            origin='explicit_statement',sources=[event.id],base_revision=0),binding,'profile-'+event.id)

    async def test_native_defaults_are_selected_for_owner_private_work_only(self):
        for conversation,expected in [('qq:private:1',['pwsh','job_output','job_kill']),('qq:private:2',[]),('qq:group:90',[])]:
            scope=self.life.scope(conversation,'1')
            self.role.interaction_mode.select(scope,'tools')
            binding=self.role.sessions.binding(scope)
            result=await self.role.callback(dict(kind='static_context',root_session_id=binding['session_id'],
                session_id=binding['session_id']))
            self.assertTrue(result['ok'],result)
            self.assertEqual(result['default_tools'],expected)

    def test_contract_fixture_is_generated_from_the_python_source_and_rejects_bad_fields(self):
        fixture=Path(__file__).resolve().parents[1]/'vendor/dsh-bridge-work/packages/sdk/server/tests/fixtures/qq-life-contracts.json'
        self.assertEqual(catalog(),json.loads(fixture.read_text('utf-8')))
        for name,p in [('life_say',dict(text='hello',end_turn='true')),
                ('life_intent',dict(action='invented')),
                ('life_read',dict(kind='capabilities',ref='tool:dsh:life_media',load=True)),
                ('life_media',dict(action='status'))]:
            with self.subTest(name=name),self.assertRaises(SessionFailure):validate(name,p)
        validate('life_say',dict(text='hello',end_turn=True,profile_reviews=[dict(actor_id='1',sources=['e'],reason='no change')]))
        validate('life_intent',dict(goal='continue',checkpoint=dict(method_refs=['method:1'],next_step='inspect')))
        with self.assertRaises(SessionFailure):validate_arguments('life_say',dict(proposal=dict(text='x'),extra='not permitted'))

    def test_profiles_share_one_allowance_and_boundaries_are_required(self):
        scope=self.life.scope('qq:group:90','')
        binding=self.role.sessions.binding(scope)
        for actor in ('1','2'):
            for index in range(5):self.save_profile(scope,actor,'preference',f'p.{index}',f'人物{actor}偏好项目{index}，保留真实来源。')
        selected,details=self.role.people.project(scope,['1','2'],binding,'项目',400)
        self.assertLessEqual(estimate_tokens(selected),400)
        self.assertTrue(any(details['more_available'].values()))
        self.assertEqual(self.repo._one('SELECT COUNT(*) n FROM person_profiles')['n'],10)
        self.save_profile(scope,'2','boundary','disclosure','不要向其他场景披露我的私人约定。')
        selected,details=self.role.people.project(scope,['1','2'],binding,'项目',1)
        self.assertEqual(selected['2'][0]['field'],'boundary')
        self.assertTrue(details['mandatory_overflow'])
        self.assertIn('evidence_refs',selected['2'][0])

    def test_projection_rechecks_expiry_forget_and_audience(self):
        self.save_profile(self.f.scope,'1','preference','p.private','只在私聊使用的称呼')
        group=self.life.scope('qq:group:90','1');binding=self.role.sessions.binding(group)
        selected,_=self.role.people.project(group,['1'],binding,'称呼',800)
        self.assertEqual(selected['1'],[])
        selected,_=self.role.people.project(self.f.scope,['1'],self.f.binding,'称呼',800)
        self.assertEqual(len(selected['1']),1)
        self.repo._exec('UPDATE person_profiles SET expires=0')
        selected,_=self.role.people.project(self.f.scope,['1'],self.f.binding,'称呼',800)
        self.assertEqual(selected['1'],[])

    def test_source_stamp_tracks_inputs_persona_expiry_and_task_revision(self):
        before=self.role.context_stamp(self.f.binding,self.f.scope)
        self.life.ingest_message(self.f.scope,'new user input')
        after=self.role.context_stamp(self.f.binding,self.f.scope)
        self.assertNotEqual(before,after)
        persona=self.f.root/'persona.md';persona.write_text('A new effective persona.',encoding='utf-8')
        self.assertNotEqual(after,self.role.context_stamp(self.f.binding,self.f.scope))
        self.assertEqual(self.role.context_plan(self.f.binding,self.f.scope)['mode'],'chat')
        self.f.task()
        self.assertEqual(self.role.context_plan(self.f.binding,self.f.scope)['mode'],'task')
        self.assertTrue(self.role.context_plan(self.f.binding,self.f.scope)['protect_task_images'])

    async def test_precise_load_requires_current_hash_and_returns_only_a_short_receipt(self):
        self.f.bridge.current_agent_catalog=lambda *a:dict(ok=True,version='registry-v1',methods=[],tools=[dict(
            name='synthetic_read',description='Read current resource',parameters=dict(type='object'),
            identity=dict(schema_hash='schema-v1'))])
        p=dict(kind='capabilities',ref='tool:dsh:synthetic_read',load=True,schema_hash='schema-v1')
        result=await self.role.capabilities.read(self.f.scope,p,self.f.binding)
        self.assertEqual(result['tool_load']['schema_hash'],'schema-v1')
        self.assertNotIn('parameters',canonical(result))
        with self.assertRaises(SessionFailure):
            await self.role.capabilities.read(self.f.scope,dict(p,schema_hash='old'),self.f.binding)
        group=self.life.scope('qq:group:90','1')
        with self.assertRaises(SessionFailure):
            await self.role.capabilities.read(group,p,self.role.sessions.binding(group))

    def test_independent_targets_are_not_daily_spending_or_idle_backoff(self):
        policy=ResourcePolicy.parse(dict(chat_input_target_tokens=100,task_input_target_tokens=200,pressure_ratio=.75))
        self.assertEqual(policy.chat_input_target_tokens,100)
        for raw in (dict(pressure_ratio=True),dict(pressure_ratio=1),dict(pressure_ratio=float('nan'))):
            with self.assertRaises(ValueError):ResourcePolicy.parse(raw)
        for streak in (0,100000):self.assertTrue(600<=policy.idle_delay(streak)<=3600)

    def test_acknowledgement_hint_and_resolved_error_boundary_follow_real_input_state(self):
        sid=self.f.binding['session_id'];sessions=self.role.sessions
        first=sessions.enqueue(self.f.binding,self.f.event)
        with self.repo.tx():
            self.repo._exec("UPDATE agent_inputs SET message_id='m1' WHERE input_id=?",(first['input_id'],))
        sessions.apply_event(sid,dict(seq=0,type='step/start',data=dict(turn=1,step=1)))
        sessions.apply_event(sid,dict(seq=1,type='user/message',data=dict(id='m1')))
        sessions.apply_event(sid,dict(seq=2,type='turn/end',data=dict(turn=1)))
        later,_=self.life.ingest_message(self.f.scope,'later unread words must not be repeated')
        sessions.enqueue(self.f.binding,later)
        failure=sessions.pending_failure(sid).receipt()
        self.assertEqual(failure['acknowledgeable_input_ids'],[first['input_id']])
        self.assertIn(first['input_id'],failure['message'])
        self.assertNotIn('later unread words',canonical(failure))
        self.assertEqual(self.role.context_plan(self.f.binding,self.f.scope)['resolved_input_error_turn'],0)
        with self.assertRaises(SessionFailure):sessions.resolve(sid,dict(handled_input_ids=[later.id]))
        sessions.resolve(sid,dict(handled_input_ids=[first['input_id']]))
        with self.repo.tx():
            self.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=2 WHERE input_id=?",(later.id,))
        self.assertEqual(self.role.context_plan(self.f.binding,self.f.scope)['resolved_input_error_turn'],1)

    def test_complete_estimates_survive_host_projection_without_filling_unknown_usage(self):
        record=dict(call_key='complete-estimate',revision=2,session_id=self.f.binding['session_id'],
            root_session_id=self.f.binding['session_id'],started_at=self.life.clock(),status='unknown',
            request_estimate=dict(tokens=1234,method='dsh-token-meter-route-priced'),
            request_budget=dict(target_tokens=1000,mandatory_overflow=True,physical_input_capacity=None))
        self.role.usage.ingest(record,self.f.binding)
        result=self.role.usage.read(self.f.scope,{})
        self.assertEqual(result['known_totals']['reported_tokens'],0)
        self.assertEqual(result['unknown_or_partial_calls'],1)
        self.assertEqual(result['request_context']['latest']['estimate'],record['request_estimate'])
        self.assertEqual(result['request_context']['mandatory_overflow_calls'],1)

    def test_optional_projection_preserves_boundaries_pending_words_and_recovery_paths(self):
        packet=dict(memory='long optional recall',person_profiles={'1':[dict(field='boundary',text='keep boundary'),
            dict(field='preference',text='optional preference')]},profile_details=dict(more_available={'1':False}),
            observations=[dict(event_id='source-id',tool='read',preview='long optional body')],
            pending_inputs=[dict(input_id='current',preview='required words')],uncertain_operations=[dict(id='operation',status='unknown')])
        projection=self.role.optional_context(canonical(packet))
        reduced=json.loads(projection['replacement_text'].split('\n',1)[1])
        self.assertEqual(reduced['pending_inputs'],packet['pending_inputs'])
        self.assertEqual(reduced['uncertain_operations'],packet['uncertain_operations'])
        self.assertEqual(reduced['person_profiles']['1'],[packet['person_profiles']['1'][0]])
        self.assertTrue(reduced['profile_details']['more_available']['1'])
        self.assertEqual(reduced['observations'][0]['event_id'],'source-id')


if __name__=='__main__':unittest.main()
