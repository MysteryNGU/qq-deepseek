"""Resource policy acceptance with fixed time, real persistence and bounded readers."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch

from life.agent_session import SessionFailure,canonical
from life.resource_policy import ResourcePolicy,read_observation,estimate_tokens
from life.role_agent import RoleAgent
from life.runtime import LifeRuntime,LifeConfig


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        (self.path/'persona.md').write_text('Synthetic test role.',encoding='utf-8')
        (self.path/'skills').mkdir()
        self.life = LifeRuntime(self.path/'life.db',config=LifeConfig(relationship_owner='1'))
        self.cfg = SimpleNamespace(owner=1,persona='persona.md',resource_policy={})
        self.bridge = SimpleNamespace(_generation=1,model='test')
        self.role = RoleAgent(self.life,self.bridge,self.cfg,self.path,AsyncMock(return_value=True))
        self.scope = self.life.scope('qq:private:1','1')
        self.root = self.role.sessions.binding(self.scope)
        self.turn = 0

    def tearDown(self):
        self.life.close();self.tmp.cleanup()

    def input(self,kind='idle_probe',scope=None):
        scope = scope or self.scope
        event,_ = self.life.ingest(scope,'runtime_wake' if kind!='user_input' else 'message',
            'user_report' if kind=='user_input' else 'inference','A new input',payload=dict(wake_kind=kind))
        root = self.role.sessions.binding(scope)
        self.role.sessions.enqueue(root,event)
        self.turn += 1
        got = self.life.repo._one('SELECT MAX(seq) AS seq FROM agent_events WHERE session_id=?',(root['session_id'],))
        seq = (got['seq'] if got['seq'] is not None else -1)+1
        self.role.sessions.apply_event(root['session_id'],dict(type='step/start',seq=seq,time=1000,data=dict(turn=self.turn,step=1)))
        with self.life.repo.tx():
            self.life.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=?,handled=1 WHERE session_id=? AND input_id=?",
                (self.turn,root['session_id'],event.id))
        return event,root

    def note(self,body):
        return self.life.ingest(self.scope,'tool_result','tool_observation',body,payload=dict(text=body))[0]

    def call(self,key='call',revision=2,**kwargs):
        result = dict(call_key=key,revision=revision,session_id=self.root['session_id'],root_session_id=self.root['session_id'],
            provider='test',model='test',started_at=time.time(),status='completed',usage=dict(inputTokens=100,cacheReadTokens=200,outputTokens=30,reasoningTokens=20))
        result.update(kwargs)
        self.role.usage.ingest(result,self.root)
        return result

    async def tick(self):
        self.role.running = True
        async def done(_):self.role.running=False
        with patch('life.role_agent.asyncio.sleep',side_effect=done):
            await self.role.wake_loop()

    async def test_T01_local_tick_without_due_work_does_not_call_a_model(self):
        self.bridge.session_events = AsyncMock()
        await self.tick()
        self.bridge.session_events.assert_not_called()
        self.assertFalse(self.life.repo._all('SELECT * FROM usage_calls'))

    def test_T02_T04_adaptive_idle_has_one_identity_and_two_unchanged_per_tier(self):
        delays = []
        for i in range(9):
            self.input()
            result = self.role.rest(self.scope,dict(reflection_outcome='unchanged',reason='new words '+str(i)),self.root)
            delays.append(result['attention']['after_seconds'])
        self.assertEqual(delays,[900,1800,1800,3600,3600,7200,7200,7200,7200])
        self.assertEqual(self.life.repo._one("SELECT COUNT(*) AS n FROM role_wakes WHERE state='pending' AND kind='idle_probe'")['n'],1)

    def test_T03_T07_explicit_short_schedule_survives_default_final_expression(self):
        self.input()
        self.role.rest(self.scope,dict(next_attention=dict(mode='after',after_seconds=45)),self.root)
        result = self.role.attention.finish(self.scope,self.root,{},'later-final',expressed=True)
        self.assertEqual(result['mode'],'existing_explicit')
        rows = self.life.repo._all("SELECT * FROM role_wakes WHERE state='pending'")
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['kind'],'self_scheduled')
        self.assertAlmostEqual(rows[0]['due']-time.time(),45,delta=1)

    async def test_T05_real_input_cancels_default_idle_without_waiting(self):
        self.role.schedule(self.scope,None,7200,'idle',kind='idle_probe')
        self.bridge.session_input = lambda **kwargs: dict(state='accepted',message_id='real-message')
        self.role.sessions.deliver = AsyncMock(return_value=dict(state='accepted'))
        event,_ = self.life.ingest_message(self.scope,'Please stop and listen')
        await self.role.ingest(event)
        self.role.sessions.deliver.assert_awaited_once()
        self.assertFalse(self.life.repo._one("SELECT 1 FROM role_wakes WHERE kind='idle_probe' AND state='pending'"))
        for task in self.role._workers:task.cancel()
        await asyncio.gather(*self.role._workers,return_exceptions=True)

    def test_T06_T11_idle_survives_restart_without_catchup_storm(self):
        self.input()
        self.role.rest(self.scope,dict(reflection_outcome='unchanged'),self.root)
        row = dict(self.life.repo._one('SELECT * FROM role_wakes'))
        self.life.close()
        self.life = LifeRuntime(self.path/'life.db',config=LifeConfig(relationship_owner='1'))
        self.role = RoleAgent(self.life,self.bridge,self.cfg,self.path,AsyncMock())
        later = dict(self.life.repo._one('SELECT * FROM role_wakes'))
        self.assertEqual(row,later)
        self.assertLessEqual(self.role.resources.idle_delay(100000),7200)

    async def test_T08_wait_blocker_cannot_be_released_by_idle(self):
        task = self.role.tasks.ensure(self.scope,'waiting','Wait for an answer')
        self.role.tasks.transition(self.scope.scope_id,task['ref'],'waiting',cause='ask_user')
        self.role.schedule(self.scope,task['ref'],0,'continue',kind='task_due')
        self.input()
        self.role.rest(self.scope,{},self.root)
        await self.tick()
        self.assertEqual(self.role.tasks.get(task['ref'],self.scope.scope_id)['status'],'waiting')

    async def test_T10_dispatch_ack_cannot_close_a_new_plan_revision(self):
        key = self.role.schedule(self.scope,None,0,'original',kind='self_scheduled')
        async def deliver(event):
            self.role.schedule(self.scope,None,50,'replacement',kind='self_scheduled')
            return dict(state='accepted')
        self.role.ingest = AsyncMock(side_effect=deliver)
        await self.tick()
        plan = self.life.repo._one('SELECT * FROM role_wakes WHERE id=?',(key,))
        self.assertEqual(plan['state'],'pending')
        self.assertEqual(plan['plan_revision'],2)
        self.assertIsNone(plan['input_id'])

    async def test_T09_busy_role_receives_one_batch_for_two_actual_results(self):
        self.input('user_input')
        self.role.schedule(self.scope,None,0,'first result',kind='tool_result',semantic_key='result:a')
        self.role.schedule(self.scope,None,0,'second result',kind='delegate_result',semantic_key='result:b')
        self.role.ingest = AsyncMock(return_value=dict(state='accepted'))
        await self.tick()
        self.role.ingest.assert_awaited_once()
        event = self.role.ingest.call_args.args[0]
        self.assertEqual(len(event.payload['wake_plans']),2)
        self.assertEqual(event.payload['wake_kind'],'mixed')
        self.assertEqual({r['input_id'] for r in self.life.repo._all('SELECT input_id FROM role_wakes')},{event.id})

    async def test_T10_unknown_dispatch_retries_identical_persisted_input(self):
        self.role.schedule(self.scope,None,0,'scheduled',kind='self_scheduled')
        self.role.ingest = AsyncMock(side_effect=[RuntimeError('ACK lost'),dict(state='accepted')])
        with self.assertRaises(RuntimeError):await self.role.attention.dispatch_due(time.time())
        first = self.role.ingest.call_args.args[0]
        self.assertEqual(self.life.repo._one('SELECT state FROM role_wakes')['state'],'dispatching')
        await self.role.attention.dispatch_due(time.time())
        second = self.role.ingest.call_args.args[0]
        self.assertEqual(first.id,second.id)
        self.assertEqual(self.life.repo._one('SELECT state FROM role_wakes')['state'],'delivered')

    def test_T12_state_follows_elapsed_time_not_attention_frequency(self):
        a = LifeRuntime(self.path/'a.db',clock=lambda:1000.)
        b = LifeRuntime(self.path/'b.db',clock=lambda:1000.)
        try:
            x = a.scope('qq:private:1','1');y=b.scope('qq:private:1','1')
            a.advance_time(x,1000,online_seconds=0);b.advance_time(y,1000,online_seconds=0)
            for moment in (1100,1200,1300):a.advance_time(x,moment,online_seconds=100)
            b.advance_time(y,1300,online_seconds=300)
            left,right = a.drive_state(x).to_dict()['drives'],b.drive_state(y).to_dict()['drives']
            for dim in left:
                for key,value in left[dim].items():
                    if isinstance(value,(int,float)):
                        self.assertAlmostEqual(value,right[dim][key],places=6)
                    else:self.assertEqual(value,right[dim][key])
        finally:a.close();b.close()

    def test_R14_old_release_image_cannot_open_a_v2_database(self):
        from supervise import validate_data_compatibility
        source = self.path/'old-code';(source/'life').mkdir(parents=True)
        (source/'bot.py').write_text('# old')
        (source/'life/runtime.py').write_text('# no supported-format declaration')
        self.life.relationship(self.scope)
        with self.assertRaisesRegex(RuntimeError,'relationship_schema_incompatible'):
            validate_data_compatibility(source,database=self.life.repo.path)
        (source/'life/runtime.py').write_text('SUPPORTED_RELATIONSHIP_SCHEMA = 2\n')
        validate_data_compatibility(source,database=self.life.repo.path)

    def test_T13_T16_middle_error_and_exact_full_pagination(self):
        body = '\n'.join('line '+str(i)+(' EPERM' if i==300 else '') for i in range(600))
        event = self.note(body)
        found = read_observation(self.role,self.scope,dict(event_id=event.id,mode='search',query='EPERM'),self.role.resources)
        self.assertIn('EPERM',canonical(found))
        self.assertEqual(found['lines'][3]['line'],301)
        proposal = dict(event_id=event.id,mode='lines',limit=100)
        recovered = []
        for _ in range(100):
            page = read_observation(self.role,self.scope,proposal,self.role.resources)
            recovered.extend(item['text'] for item in page['lines'])
            if not page['has_more']:break
            proposal['cursor'] = page['next_cursor']
        self.assertEqual('\n'.join(recovered),body)
        adapted = read_observation(self.role,self.scope,dict(event_id=event.id,include_full=True),self.role.resources)
        self.assertTrue(adapted['include_full_adapted'])
        self.assertTrue(adapted['has_more'])

    def test_T14_chinese_emoji_and_single_giant_json_have_bounded_valid_envelopes(self):
        event = self.note(json.dumps({'log':'中🙂'*10000},ensure_ascii=False))
        page = read_observation(self.role,self.scope,dict(event_id=event.id,mode='lines'),self.role.resources)
        text = canonical(page)
        self.assertIsInstance(json.loads(text),dict)
        self.assertLessEqual(estimate_tokens(text),self.role.resources.text_result_max_tokens)
        self.assertLessEqual(len(text.encode()),self.role.resources.text_result_max_utf8_bytes)
        self.assertTrue(page['has_more'])
        self.assertGreater(json.loads(page['next_cursor'])['offset_chars'],0)
        middle = self.note(json.dumps(dict(log='前'*10000+'MARKER=real-middle'+'后'*10000),ensure_ascii=False))
        found = read_observation(self.role,self.scope,dict(event_id=middle.id,mode='search',query='MARKER'),self.role.resources)
        self.assertIn('MARKER=real-middle',canonical(found))
        self.assertGreater(found['lines'][0]['offset_chars'],0)

    def test_T17_T29_forgotten_and_other_audience_results_are_not_readable(self):
        event = self.note('private diagnostics')
        group = self.life.scope('qq:group:3','1')
        with self.assertRaises(SessionFailure):
            read_observation(self.role,group,dict(event_id=event.id),self.role.resources)
        self.life.repo.redact_events([event.id])
        with self.assertRaises(SessionFailure) as caught:
            read_observation(self.role,self.scope,dict(event_id=event.id),self.role.resources)
        self.assertEqual(caught.exception.code,'source_removed')

    def test_T21_T24_T25_cumulative_usage_replaces_not_adds_and_reasoning_is_not_double_counted(self):
        self.call(revision=1,status='started',usage=None)
        self.call(revision=2)
        self.call(revision=2)
        summary = self.role.usage.read(self.scope,{})
        self.assertEqual(summary['calls'],1)
        self.assertEqual(summary['known_totals']['reported_tokens'],330)
        self.assertEqual(summary['known_totals']['reasoning'],20)
        self.assertEqual(summary['unknown_or_partial_calls'],0)
        self.assertIsNone(summary['price_estimate'])

    def test_T22_T23_T26_failed_calls_summary_and_descendant_are_separate_with_gaps(self):
        self.call('failed',status='failed',usage=None)
        self.call('retry')
        self.call('summary',purpose='compaction')
        with self.life.repo.tx():
            self.role._register_delegate('child',self.root['session_id'],self.root)
        self.call('child-call',session_id='child')
        result = self.role.usage.read(self.scope,{})
        self.assertEqual(result['calls'],4)
        self.assertEqual(result['unknown_or_partial_calls'],1)
        self.assertEqual(result['purposes']['compaction'],1)
        self.assertEqual(result['purposes']['delegate'],1)
        self.assertEqual(result['known_totals']['reported_tokens'],990)
        self.assertIn('external_plugins_calling_providers_outside_DSH',result['excludes'])

    def test_T24_inconsistent_total_is_preserved_and_flagged(self):
        self.call(usage=dict(inputTokens=1,cacheReadTokens=2,outputTokens=3,totalTokens=999))
        result = self.role.usage.read(self.scope,{})
        self.assertEqual(result['known_totals']['provider_total'],999)
        self.assertEqual(result['total_mismatches'],1)

    def test_T25_accounting_survives_reopening_without_count_growth(self):
        self.call()
        before = self.role.usage.read(self.scope,{})
        self.life.close()
        self.life = LifeRuntime(self.path/'life.db')
        self.role = RoleAgent(self.life,self.bridge,self.cfg,self.path,AsyncMock())
        after = self.role.usage.read(self.scope,{})
        self.assertEqual(after['calls'],before['calls'])
        self.assertEqual(after['known_totals'],before['known_totals'])

    async def test_T26_usage_projection_failure_does_not_silence_cognition(self):
        self.role.sessions.replay = AsyncMock()
        def unavailable(*args,**kwargs):raise OSError('transient accounting read failure')
        self.bridge.resource_events = unavailable
        await self.role.replay(self.root['session_id'])
        self.assertTrue(self.role.usage.read(self.scope,{})['projection_gap'])
        self.assertIn('当前状态与材料',self.role.context(self.root,self.scope,{}))

    def test_T27_T28_compact_snapshot_keeps_control_relationship_semantics_and_versions(self):
        self.input('user_input')
        text = self.role.context(self.root,self.scope,{})
        stable,dynamic = text.split('\n\n当前状态与材料：\n')
        packet = json.loads(dynamic)
        self.assertIn('relation_protocol',stable)
        self.assertNotIn('relationship_semantics',packet)
        self.assertIn('relationship_version',packet['relationships']['1'])
        self.assertIn('meaning',packet['relationships']['1']['metrics']['bond'])
        self.assertIn('pending_inputs',packet)
        self.assertIn('scope',packet)
        self.assertNotIn('sampled_at',packet)

    def test_T27_required_reminders_survive_tiny_target_and_more_than_two_hundred_items(self):
        event,_=self.life.ingest_message(self.scope,'Keep these explicit reminders.')
        for i in range(205):
            self.life.recollection.annotate(self.scope,'reminder','MUST_REMEMBER_'+str(i)+' '+('retain this instruction '*5),[event.id],always_load=True)
        self.role.resources=ResourcePolicy.parse(dict(dynamic_context_target_tokens=128))
        packet=json.loads(self.role.context(self.root,self.scope,{}).split('当前状态与材料：\n')[1])
        self.assertEqual(len(packet['mandatory_reminders']),205)
        self.assertIn('MUST_REMEMBER_0 ',canonical(packet['mandatory_reminders']))
        self.assertTrue(packet['mandatory_overflow'])
        other=self.life.scope('qq:group:3','1')
        self.assertFalse(self.life.recollection.annotations(other,'',None,mandatory=True))

    def test_T29_usage_is_scoped_before_aggregation(self):
        self.call()
        other = self.life.scope('qq:private:2','2')
        self.assertEqual(self.role.usage.read(other,{})['calls'],0)
        with self.assertRaises(SessionFailure):self.role.usage.read(other,dict(global_=True,**{'global':True}))

    async def test_T30_stale_wake_generation_and_epoch_cannot_dispatch(self):
        key = self.role.schedule(self.scope,None,0,'old',kind='idle_probe')
        with self.life.repo.tx():
            self.life.repo._exec('UPDATE role_wakes SET generation=99 WHERE id=?',(key,))
        self.role.ingest = AsyncMock()
        await self.tick()
        self.role.ingest.assert_not_awaited()
        self.assertEqual(self.life.repo._one('SELECT state FROM role_wakes')['state'],'cancelled')

    def test_invalid_policy_and_conflicting_attention_are_rejected(self):
        for raw in ({'idle_base_seconds':True},{'idle_base_seconds':float('inf')},{'idle_base_seconds':8000}, {'unknown':1}):
            with self.assertRaises(ValueError):ResourcePolicy.parse(raw)
        with self.assertRaises(SessionFailure):
            self.role.rest(self.scope,dict(next_attention=dict(mode='adaptive'),wake_after_seconds=45),self.root)

    def test_unchanged_finish_is_idempotent_and_new_thought_resets_backoff(self):
        self.input()
        first = self.role.rest(self.scope,dict(reflection_outcome='unchanged'),self.root)
        second = self.role.rest(self.scope,dict(reflection_outcome='unchanged'),self.root)
        self.assertTrue(second['attention']['duplicate'])
        self.input()
        third = self.role.rest(self.scope,dict(reflection_outcome='new_thought',thought='A private imagined view'),self.root)
        self.assertEqual(third['attention']['unchanged_streak'],0)
