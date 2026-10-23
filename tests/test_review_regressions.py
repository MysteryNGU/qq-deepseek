"""Execution-boundary regressions from the independent 2026-10-23 review."""
import asyncio
import json
import unittest

from audit.review_2026_10_23_probes import Fixture
from life.agent_session import SessionFailure
from life.contracts import Appraisal
from life.role_agent import RoleAgent
from life.self_change import SelfChange


class ReviewRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = Fixture()

    def tearDown(self):
        self.f.close()

    def confirmed_transport(self):
        async def transport(action,params):
            self.f.sent.append(params)
            return dict(retcode=0,data=dict(message_id=len(self.f.sent)))
        self.f.expressions.transport = transport

    def consumed(self,event,turn=1):
        f = self.f
        f.role.sessions.enqueue(f.binding,event)
        with f.life.repo.tx():
            f.life.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=? WHERE input_id=?",(turn,event.id))

    def call(self,kind,call_id,proposal):
        return dict(root_session_id=self.f.binding['session_id'],session_id=self.f.binding['session_id'],
                    caller='role',kind=kind,call_id=call_id,proposal=proposal)

    async def test_partial_understanding_survives_blocked_action_and_control_is_not_reapplied(self):
        f = self.f
        task = f.task()
        second,_ = f.life.ingest_message(f.scope,'A separate message')
        self.consumed(f.event)
        self.consumed(second)
        call = self.call('life.say','partial-control',dict(text='acknowledged',sources=[f.event.id],
            handled_input_ids=[f.event.id],activity_changes=[dict(intent_ref=task['ref'],base_revision=task['revision'],decision='cancel')]))
        result = await f.role.callback(call)
        self.assertEqual(result['code'],'input_pending')
        self.assertEqual([p['input_id'] for p in f.role.sessions.pending(f.binding['session_id'])],[second.id])
        self.assertEqual(f.role.tasks.get(task['ref'],f.scope.scope_id)['status'],'cancelled')
        # A transport retry with the same call must not rerun the old goal revision.
        self.assertEqual((await f.role.callback(call))['code'],'input_pending')
        self.confirmed_transport()
        result = await f.role.callback(self.call('life.say','understand-rest',dict(text='Both understood',
            sources=[second.id],handled_input_ids=[second.id])))
        self.assertTrue(result['sent_confirmed'])
        self.assertFalse(f.role.sessions.pending(f.binding['session_id']))
        self.assertEqual(len(f.sent),1)

    async def test_internal_plaintext_turn_retries_and_real_closure_cancels_recovery(self):
        from life.agent_session import canonical,digest
        from life.role_agent import PROTOCOL_REPAIR
        f = self.f
        self.consumed(f.event,7)
        event = dict(seq=0,type='step/start',data=dict(turn=7,step=0))
        with f.life.repo.tx():
            f.life.repo._exec('INSERT INTO agent_events VALUES(?,?,?,?,?)',
                (f.binding['session_id'],0,digest(event),canonical(event),0))
        f.role._observe_result(f.binding['session_id'],dict(type='turn/end',data=dict(turn=7,reason=dict(kind='completed'))))
        pending = f.life.repo._one("SELECT * FROM role_wakes WHERE reason=? AND state='pending'",(PROTOCOL_REPAIR,))
        self.assertIsNotNone(pending)
        sources = f.life.repo._one('SELECT v FROM meta WHERE k=?',('wake_reply_sources:'+pending['id'],))
        self.assertEqual(json.loads(sources['v']),[f.event.id])
        self.assertFalse(f.sent)  # Internal model prose is never silently forwarded.
        result = await f.role.callback(self.call('life.rest','quiet-completion',dict(handled_input_ids=[f.event.id])))
        self.assertTrue(result['ok'])
        self.assertEqual(f.life.repo._one('SELECT state FROM role_wakes WHERE id=?',(pending['id'],))['state'],'cancelled')
        f.role._observe_result(f.binding['session_id'],dict(type='turn/end',data=dict(turn=7,reason=dict(kind='completed'))))
        self.assertIsNone(f.life.repo._one("SELECT 1 FROM role_wakes WHERE reason=? AND state='pending'",(PROTOCOL_REPAIR,)))

    async def test_recovery_wake_cannot_disguise_original_unknown_delivery_as_a_new_reply(self):
        f = self.f
        proposal = dict(text='Original final answer',sources=[f.event.id])
        with self.assertRaises(TimeoutError):
            await f.role.say(f.scope,proposal,f.binding,'before-recovery')
        recovery,_ = f.life.ingest(f.scope,'runtime_wake','inference','Recovery prompt',
            source_event_ids=[f.event.id],payload=dict(reply_source_ids=[f.event.id]))
        with self.assertRaises(SessionFailure) as error:
            await f.role.say(f.scope,dict(proposal,sources=[recovery.id]),f.binding,'new-call-after-recovery')
        self.assertEqual(error.exception.code,'outcome_unknown')
        self.assertEqual(len(f.sent),1)

    async def test_reply_slot_survives_changed_call_id_restart_and_user_requested_resend(self):
        f = self.f
        proposal = dict(text='same final answer',sources=[f.event.id])
        with self.assertRaises(TimeoutError):
            await f.role.say(f.scope,proposal,f.binding,'call-1')
        restarted = RoleAgent(f.life,f.bridge,f.cfg,f.root,f.role.send)
        with self.assertRaises(SessionFailure) as error:
            await restarted.say(f.scope,proposal,f.binding,'call-2')
        self.assertEqual(error.exception.code,'outcome_unknown')
        self.assertEqual(len(f.sent),1)
        self.confirmed_transport()
        again,_ = f.life.ingest_message(f.scope,'Please send that once again')
        result = await restarted.say(f.scope,dict(text=proposal['text'],sources=[again.id]),f.binding,'call-3')
        self.assertTrue(result['sent_confirmed'])
        self.assertEqual(len(f.sent),2)

    async def test_confirmed_retry_reads_original_and_new_message_is_explicit(self):
        f = self.f
        self.confirmed_transport()
        proposal = dict(text='hello',sources=[f.event.id])
        one = await f.role.say(f.scope,proposal,f.binding,'one')
        two = await f.role.say(f.scope,dict(proposal,handled_input_ids=[],mode='auto'),f.binding,'two')
        self.assertEqual(one['logical_message_id'],two['logical_message_id'])
        self.assertEqual(len(f.sent),1)
        await f.role.say(f.scope,dict(proposal,new_message=True),f.binding,'three')
        self.assertEqual(len(f.sent),2)

    async def test_explicit_independent_message_does_not_resend_an_unknown_reply(self):
        f = self.f
        proposal = dict(text='original answer',sources=[f.event.id])
        with self.assertRaises(TimeoutError):
            await f.role.say(f.scope,proposal,f.binding,'lost')
        self.confirmed_transport()
        with self.assertRaises(SessionFailure):
            await f.role.say(f.scope,dict(proposal,new_message=True),f.binding,'repeat-unknown')
        result = await f.role.say(f.scope,dict(text='The earlier delivery is uncertain; I am checking it.',
            sources=[f.event.id],new_message=True),f.binding,'independent-notification')
        self.assertTrue(result['sent_confirmed'])
        self.assertEqual(len(f.sent),2)

    async def test_child_and_grandchild_cannot_publish_main_can_adopt_and_publish(self):
        f = self.f
        f.task()
        f.role.before_tool(f.binding,f.scope,dict(tool='subagent',call_id='spawn',session_id=f.binding['session_id'],arguments={}))
        f.role._delegate_started(dict(parentSessionId=f.binding['session_id'],childSessionId='child'))
        f.role._delegate_started(dict(parentSessionId='child',childSessionId='grandchild'))
        for sid in ('child','grandchild'):
            result = await f.role.callback(dict(root_session_id=f.binding['session_id'],session_id=sid,
                caller='delegate',kind='before_tool',call_id='publish-'+sid,tool='mcp__bili__bili_reply',
                arguments=dict(text='unadopted',oid='test')))
            self.assertFalse(result['ok'])
        result = f.role.before_tool(f.binding,f.scope,dict(tool='mcp__bili__bili_reply',call_id='adopted',
            session_id=f.binding['session_id'],arguments=dict(text='main verified body',oid='test')))
        self.assertTrue(result['ok'])
        for tool in ('mcp__qq__send_private_msg','mcp__new_plugin__publish'):
            with self.assertRaises(SessionFailure):
                f.role.before_tool(f.binding,f.scope,dict(tool=tool,call_id=tool,session_id='child',arguments={}))
        self.assertFalse(f.sent)

    async def test_transient_failure_reconciles_without_process_exit_or_new_message(self):
        f = self.f
        attempts,statuses = [],[]
        available = False
        def deliver(payload):
            attempts.append(payload['input_id'])
            if not available:
                raise TimeoutError('temporary outage')
            return dict(message_id='accepted-once',state='accepted')
        def status(sid,iid):
            statuses.append(iid)
            if not available:
                raise TimeoutError('temporary outage')
            return dict(state='not_found') if len(attempts)==1 else dict(state='accepted',message_id='accepted-once')
        f.bridge.session_input,f.bridge.input_status = deliver,status
        with self.assertRaises(SessionFailure):
            await f.role.ingest(f.event)
        available = True
        f.role._self_change = SelfChange(f.life,f.cfg,f.root)
        f.role.running = True
        job = asyncio.create_task(f.role.watch_worker())
        try:
            async with asyncio.timeout(8):
                while len(attempts)<2:
                    await asyncio.sleep(.05)
            await asyncio.sleep(.1)
        finally:
            f.role.running = False
            job.cancel()
            await asyncio.gather(job,return_exceptions=True)
        self.assertEqual(attempts,[f.event.id,f.event.id])
        self.assertGreaterEqual(len(statuses),2)
        self.assertEqual(f.role.sessions.pending(f.binding['session_id'])[0]['state'],'accepted')

    async def test_ack_lost_reconciliation_does_not_enqueue_twice(self):
        f = self.f
        calls = []
        def deliver(payload):
            calls.append(payload['input_id'])
            raise TimeoutError('ACK lost after acceptance')
        f.bridge.session_input = deliver
        f.bridge.input_status = lambda *args:dict(state='accepted',message_id='already-accepted')
        await f.role.ingest(f.event)
        item = f.role.sessions.pending(f.binding['session_id'])[0]
        await f.role.sessions.deliver(item,reconcile=True)
        self.assertEqual(calls,[f.event.id])

    async def test_recovery_and_handoff_do_not_repeatedly_cancel_the_same_slow_input(self):
        f = self.f
        calls = []
        f.bridge.session_control = lambda *args,**kwargs:calls.append((args,kwargs))
        await asyncio.gather(*(f.role.request_attention(f.binding,f.event.id) for _ in range(4)))
        await f.role.request_attention(f.binding,f.event.id)
        self.assertEqual(len(calls),1)
        other,_ = f.life.ingest_message(f.scope,'A new interruption')
        await f.role.request_attention(f.binding,other.id)
        self.assertEqual(len(calls),2)
        # A new native worker has its own execution, while duplicate watchers
        # in the same generation still share one attention request.
        f.bridge._generation += 1
        await f.role.request_attention(f.binding,other.id)
        self.assertEqual(len(calls),3)

    async def test_failed_attention_can_retry_without_losing_the_input(self):
        f = self.f
        calls = []
        def control(*args,**kwargs):
            calls.append(args)
            if len(calls)==1:
                raise TimeoutError('temporary RPC failure')
        f.bridge.session_control = control
        with self.assertRaises(TimeoutError):
            await f.role.request_attention(f.binding,f.event.id)
        await f.role.request_attention(f.binding,f.event.id)
        await f.role.request_attention(f.binding,f.event.id)
        self.assertEqual(len(calls),2)

    async def test_definitely_rejected_media_becomes_an_honest_observation_without_false_handling(self):
        from life.services.dsh_bridge import DshRpcError
        f = self.f
        auxiliary,_ = f.life.ingest(f.scope,'perception','tool_observation','image representation',
            source_event_ids=[f.event.id],payload=dict(stimulus_event_id=f.event.id))
        item = f.role.sessions.enqueue(f.binding,auxiliary)
        attempts = []
        def deliver(payload):
            attempts.append(payload['input_id'])
            if payload['input_id']==auxiliary.id:
                raise DshRpcError(-32603,'Image exceeds the configured per-side pixel limit.')
            return dict(state='accepted',message_id='failure-observed')
        f.bridge.session_input = deliver
        f.bridge.input_status = lambda *args:dict(state='not_found')
        await f.role.reconcile_input(item)
        old = f.life.repo._one('SELECT state,handled FROM agent_inputs WHERE input_id=?',(auxiliary.id,))
        self.assertEqual(old['state'],'rejected')
        self.assertEqual(old['handled'],0)
        pending = f.role.sessions.pending(f.binding['session_id'])
        self.assertEqual(len(pending),1)
        failure = f.life.repo.get_event(pending[0]['input_id'])
        self.assertEqual(failure.kind,'perception_failure')
        self.assertTrue(failure.payload['unread'])
        self.assertIsNotNone(f.life.repo.get_event(f.event.id))
        self.assertEqual(len(attempts),2)

    async def test_overlap_media_alias_and_revision_cannot_reconsume_stimuli(self):
        f = self.f
        second,_ = f.life.ingest_message(f.scope,'second stimulus')
        app = Appraisal(id='combined',scope=f.scope,source_event_ids=[f.event.id,second.id],
            certainty=1,proposed_affect_delta={'joy':.15})
        f.life.appraise(f.scope,second,app)
        before = f.life.affect_summary(f.scope)['intensity']['joy']
        alias,_ = f.life.ingest(f.scope,'perception','tool_observation','transcript',
            source_event_ids=[f.event.id],payload=dict(stimulus_event_id=f.event.id))
        for sources in ([f.event.id],[second.id,f.event.id],[alias.id]):
            with self.assertRaises(SessionFailure) as raised:
                f.role.appraise(f.scope,dict(sources=sources,certainty=1,proposed_affect_delta={'joy':.15}),f.binding)
            self.assertEqual(raised.exception.code,'stimulus_overlap')
        self.assertEqual(f.life.affect_summary(f.scope)['intensity']['joy'],before)
        from dataclasses import replace
        f.life.appraise(f.scope,second,replace(app,id='revised',revision=2,supersedes_appraisal_id='combined',
            proposed_affect_delta={'joy':.05}))
        with self.assertRaises(SessionFailure) as raised:
            f.role.appraise(f.scope,dict(sources=[f.event.id],certainty=1,
                proposed_affect_delta={'joy':.15}),f.binding)
        self.assertEqual(raised.exception.code,'stimulus_overlap')

    async def test_confirmed_partial_delivery_and_late_ack_are_experiences_exactly_once(self):
        f = self.f
        async def transport(action,params):
            f.sent.append(params)
            if len(f.sent)==1:
                return dict(retcode=0,data=dict(message_id=10))
            raise TimeoutError('lost ACK')
        f.expressions.transport = transport
        with self.assertRaises(TimeoutError):
            await f.role.say(f.scope,dict(text='first part\nsecond part',sources=[f.event.id]),f.binding,'partial')
        rows = f.life.repo._all('SELECT * FROM deliveries ORDER BY segment')
        self.assertEqual([r['status'] for r in rows],['confirmed','unknown'])
        self.assertEqual(f.life.repo._one("SELECT COUNT(*) AS n FROM events WHERE source_kind='self_output'")['n'],1)
        receipt = dict(retcode=0,data=dict(message_id=11))
        self.assertTrue(f.life.delivery.settle(rows[1]['id'],rows[1]['token'],receipt))
        self.assertFalse(f.life.delivery.settle(rows[1]['id'],rows[1]['token'],receipt))
        self.assertEqual(f.life.repo._one("SELECT COUNT(*) AS n FROM events WHERE source_kind='self_output'")['n'],2)

    async def test_shared_growth_updates_projection_while_private_section_stays_private(self):
        f = self.f
        source = '<!-- persona:public -->\ncurious\n<!-- /persona:public -->\n'\
            '<!-- persona:private -->\nOWNER_SECRET\n<!-- /persona:private -->\n'
        (f.root/'persona.md').write_text(source,encoding='utf-8')
        f.life.persona.activate(f.root/'persona.md')
        public = f.life.scope('qq:group:3','')
        binding = f.role.sessions.binding(public)
        await SelfChange(f.life,f.cfg,f.root).apply(f.scope,dict(target='persona_public',content='New interest: astronomy'),'growth')
        context = f.role.context(binding,public,{})
        self.assertIn('astronomy',context)
        self.assertNotIn('OWNER_SECRET',context)
        self.assertIn('OWNER_SECRET',(f.root/'persona.md').read_text('utf-8'))
        derived = (f.root/'personas/public.md').read_text('utf-8')
        self.assertIn('sha256=',derived)
        self.assertNotIn('OWNER_SECRET',derived)
        with self.assertRaises(SessionFailure):
            await SelfChange(f.life,f.cfg,f.root).apply(f.scope,
                dict(target='persona',content='Accidentally removed audience markers'),'bad-audience-edit')

    async def test_relation_read_distinguishes_defaults_from_actual_experiences(self):
        f = self.f
        initial = f.role.read(f.scope,dict(kind='relationship'))['relationship']
        self.assertEqual(initial['metrics']['trust']['basis'],'policy_initialization')
        self.assertEqual(initial['metrics']['trust']['visible_evidence_count'],0)
        f.role.appraise(f.scope,dict(sources=[f.event.id],relation_protocol=2,subject_actor_id='1',
            based_on_relationship_version=initial['relationship_version'],
            relation_impacts=[dict(metric='trust',direction='increase',magnitude='major',confidence=1.)]),f.binding)
        later = f.role.read(f.scope,dict(kind='relationship'))['relationship']
        self.assertEqual(later['metrics']['trust']['basis'],'policy_and_recorded_experiences')
        self.assertEqual(later['metrics']['trust']['visible_evidence_count'],1)
        self.assertGreater(later['metrics']['trust']['value'],initial['metrics']['trust']['value'])
        packet = json.loads(f.role.context(f.binding,f.scope,{}).split('当前状态与材料：\n')[1])
        self.assertEqual(packet['relationships']['1']['metrics']['trust'],later['metrics']['trust'])

    async def test_old_active_direction_is_not_hidden_by_twenty_completed_tasks(self):
        f = self.f
        f.role.tasks.ensure(f.scope,'ongoing-study','Study astronomy',kind='ongoing',updated_at=1)
        for n in range(25):
            f.role.tasks.ensure(f.scope,'completed-'+str(n),'Finished request',status='done',updated_at=100+n)
        visible = f.role.list_intents(f.scope)
        self.assertEqual(len(visible),20)
        self.assertEqual(visible[0]['ref'],'ongoing-study')
        self.assertEqual(visible[0]['status'],'active')
        other = f.life.scope('qq:group:2','1')
        self.assertFalse(f.role.list_intents(other))

    async def test_complete_expression_has_one_experience_per_confirmed_segment(self):
        from life.role_host import RoleHost
        f = self.f
        self.confirmed_transport()
        host = object.__new__(RoleHost)
        host.cfg,host.life,host.expressions,host.boot_ready = f.cfg,f.life,f.expressions,True
        f.role.send = host.send
        proposal = dict(text='first segment\nsecond segment',sources=[f.event.id])
        await f.role.say(f.scope,proposal,f.binding,'complete')
        await f.role.say(f.scope,proposal,f.binding,'retry-complete')
        self.assertEqual(len(f.sent),2)
        self.assertEqual(f.life.repo._one("SELECT COUNT(*) AS n FROM events WHERE source_kind='self_output'")['n'],2)

    async def test_cancel_during_candidate_evaluation_cannot_cross_publication_gate(self):
        from life.release import NativeWorker,ReleaseController
        from unittest.mock import patch
        import threading
        f = self.f
        (f.root/'worker.py').write_text('def calc(value):\n    return value - 1\n',encoding='utf-8')
        task = f.task()
        entered,release = threading.Event(),threading.Event()
        original = NativeWorker.invoke
        def delayed(worker,*args,**kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('test gate')
            return original(worker,*args,**kwargs)
        proposal = dict(target='code',patch=dict(file='worker.py',old='value - 1',new='value + 1'),
            probes=[dict(module='worker',function='calc',arguments={'value':0},expected=1)])
        with patch.object(NativeWorker,'invoke',delayed):
            job = asyncio.create_task(SelfChange(f.life,f.cfg,f.root).apply(f.scope,proposal,'cancel-during-check'))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,5))
                f.role.tasks.transition(f.scope.scope_id,task['ref'],'cancelled',cause='user_control')
            finally:
                release.set()
            result = await job
        controller = ReleaseController(f.root,f.root/'data/life-control',NativeWorker())
        self.assertEqual(controller.queued_deployments(),[])
        self.assertFalse(result['applied'])
        self.assertIn('value - 1',(f.root/'worker.py').read_text('utf-8'))

    async def test_queued_publication_cancel_revision_forget_and_restart(self):
        from life.release import NativeWorker,ReleaseController
        f = self.f
        (f.root/'worker.py').write_text('def calc(value):\n    return value - 1\n',encoding='utf-8')
        task = f.task()
        proposal = dict(target='code',patch=dict(file='worker.py',old='value - 1',new='value + 1'),
            probes=[dict(module='worker',function='calc',arguments={'value':0},expected=1)])
        changes = SelfChange(f.life,f.cfg,f.root)
        receipt = await changes.apply(f.scope,proposal,'cancel-after-queue')
        self.assertEqual(receipt['phase'],'queued_for_supervisor')
        controller = ReleaseController(f.root,f.root/'data/life-control',NativeWorker())
        f.role.tasks.transition(f.scope.scope_id,task['ref'],'waiting',cause='user_reply')
        self.assertEqual(controller.queued_deployments(),[])
        self.assertEqual(controller.deployment(receipt['candidate_id'])['phase'],'queued')
        f.role.tasks.transition(f.scope.scope_id,task['ref'],'cancelled',cause='user_control')
        restarted = ReleaseController(f.root,f.root/'data/life-control',NativeWorker())
        self.assertEqual(restarted.queued_deployments(),[])
        self.assertEqual(restarted.deployment(receipt['candidate_id'])['phase'],'cancelled')
        with self.assertRaises(PermissionError):
            restarted.install_source(receipt['candidate_id'])
        self.assertIn('value - 1',(f.root/'worker.py').read_text('utf-8'))

    async def test_queued_publication_is_invalidated_by_forget_without_touching_source(self):
        from life.release import NativeWorker,ReleaseController
        f = self.f
        (f.root/'worker.py').write_text('def calc(value):\n    return value - 1\n',encoding='utf-8')
        f.task()
        receipt = await SelfChange(f.life,f.cfg,f.root).apply(f.scope,dict(target='code',
            patch=dict(file='worker.py',old='value - 1',new='value + 1'),
            probes=[dict(module='worker',function='calc',arguments={'value':0},expected=1)]),'forget-queued')
        f.life.repo.bump_forget_epoch(f.scope.scope_id)
        controller = ReleaseController(f.root,f.root/'data/life-control',NativeWorker())
        self.assertEqual(controller.queued_deployments(),[])
        self.assertEqual(controller.deployment(receipt['candidate_id'])['error'],'source_forgotten')
        self.assertIn('value - 1',(f.root/'worker.py').read_text('utf-8'))

    async def test_queued_publication_rechecks_the_goal_revision(self):
        from life.release import NativeWorker,ReleaseController
        f = self.f
        (f.root/'worker.py').write_text('def calc(value):\n    return value - 1\n',encoding='utf-8')
        task = f.task()
        result = await SelfChange(f.life,f.cfg,f.root).apply(f.scope,dict(target='code',
            patch=dict(file='worker.py',old='value - 1',new='value + 1'),
            probes=[dict(module='worker',function='calc',arguments={'value':0},expected=1)]),'revised-goal')
        f.role.tasks.ensure(f.scope,task['ref'],'Changed requested behavior',revision=task['revision']+1)
        controller = ReleaseController(f.root,f.root/'data/life-control',NativeWorker())
        self.assertEqual(controller.queued_deployments(),[])
        self.assertEqual(controller.deployment(result['candidate_id'])['error'],'goal_cancelled_or_revised')

    async def test_native_code_submission_requires_a_cancellable_goal(self):
        f = self.f
        receipt = await f.role.callback(dict(root_session_id=f.binding['session_id'],
            session_id=f.binding['session_id'],caller='role',kind='life.change_self',call_id='unowned-code',
            proposal=dict(target='code',patch=dict(file='worker.py',old='before',new='after'),probes=[])))
        self.assertFalse(receipt['ok'])
        self.assertEqual(receipt['code'],'invalid_input')

    async def test_cancel_after_queue_fetch_is_rechecked_at_source_replacement(self):
        from life.release import NativeWorker,ReleaseController
        f = self.f
        (f.root/'worker.py').write_text('def calc(value):\n    return value - 1\n',encoding='utf-8')
        task = f.task()
        receipt = await SelfChange(f.life,f.cfg,f.root).apply(f.scope,dict(target='code',
            patch=dict(file='worker.py',old='value - 1',new='value + 1'),
            probes=[dict(module='worker',function='calc',arguments={'value':0},expected=1)]),'last-instant-cancel')
        controller = ReleaseController(f.root,f.root/'data/life-control',NativeWorker())
        self.assertEqual(controller.queued_deployments(),[receipt['candidate_id']])
        f.role.tasks.transition(f.scope.scope_id,task['ref'],'cancelled',cause='user_control')
        with self.assertRaisesRegex(PermissionError,'publication_cancelled'):
            controller.install_source(receipt['candidate_id'])
        self.assertIn('value - 1',(f.root/'worker.py').read_text('utf-8'))
