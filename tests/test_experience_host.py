"""Real role callback/context/wake flow using only an isolated fake transport."""
import json
import unittest

from life.agent_session import SessionFailure
from tests.role_fixture import Fixture


class ExperienceHostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        self.role,self.life=self.f.role,self.f.life
        self.role.experience_protocol=1
        self.at=1000.
        self.life.clock=self.life.catalog.clock=self.life.jobs.clock=lambda:self.at
        self.number=0

    def source(self,text):
        event,_=self.life.ingest_message(self.f.scope,text)
        self.event=event
        self.role.sessions.enqueue(self.f.binding,event)
        with self.life.repo.tx():
            self.life.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=1 WHERE input_id=?",(event.id,))
        return event

    def call(self,kind,key,p):
        sid=self.f.binding['session_id']
        return dict(kind=kind,call_id=key,proposal=p,caller='role',session_id=sid,root_session_id=sid)

    async def simulation(self,action,**fields):
        self.number+=1
        state=self.life.experience.read(self.f.scope)
        p=dict(experience_protocol=1,experience_domain='adult_intimate',base_revision=state['state_revision'],
            action=action,sources=[self.event.id],subject_actor_id='1',handled_input_ids=[self.event.id],**fields)
        result=await self.role.callback(self.call('life.simulation',str(self.number),p))
        self.assertTrue(result['ok'],result)
        return result

    async def enter(self):
        self.source('明确成年亲密情境')
        return await self.simulation('enter',adult_participants=True,context_confirmed=True,willingness='interested')

    async def peak(self):
        await self.enter()
        for delta in (.25,.25,.05):
            self.source('独立的新语言输入')
            result=await self.simulation('appraise_input',direction='increase',effects={'arousal':delta},confidence=1,reason='本条语义评价')
        return result

    async def test_callback_receipt_context_and_one_persistent_cognitive_opportunity(self):
        result=await self.peak()
        self.assertEqual(len(result['transition_refs']),1)
        transition=result['transition_refs'][0]
        context=self.role.context(self.f.binding,self.f.scope,{})
        self.assertIn(transition,context)
        self.assertIn('adult_intimate',context)
        self.role.experience_tick();self.role.experience_tick()
        self.assertEqual(self.life.repo._one("SELECT COUNT(*) AS n FROM role_wakes WHERE kind='experience'")['n'],1)
        ack=await self.simulation('ack',transition_refs=[transition])
        self.assertEqual(ack['acknowledged'],[transition])
        self.assertEqual(self.life.experience.read(self.f.scope)['transitions'][0]['ack_state'],'consumed')
        self.assertFalse(self.f.sent)

    async def test_input_stop_cancels_pending_wake_and_late_tagged_expression(self):
        result=await self.peak()
        self.source('明确开启持续方案')
        await self.simulation('start',profile_confirmed=True,input_profile=dict(method='明确方案',intensity=.5,valence=.5,rhythm='continuous'))
        run=self.life.experience.read(self.f.scope)['run']
        self.role.experience_tick()
        self.source('关闭持续输入')
        late=dict(text='旧版本尚未派发的表达',sources=[self.event.id],handled_input_ids=[self.event.id],
            experience_run_ref=run['simulation_run_id'],experience_control_revision=run['control_revision'])
        receipt=await self.role.callback(self.call('life.say','late',late))
        self.assertEqual(receipt['code'],'cancel_requested')
        self.assertFalse(self.f.sent)
        self.assertFalse(self.life.repo._one("SELECT 1 FROM role_wakes WHERE kind='experience' AND state='pending'"))

    async def test_old_protocol_is_explicitly_unready_and_cannot_use_legacy_drive_field(self):
        self.source('普通闲聊')
        self.role.experience_protocol=None
        p=dict(experience_protocol=1,experience_domain='adult_intimate',action='enter',base_revision=0,
            sources=[self.event.id],subject_actor_id='1',adult_participants=True,context_confirmed=True,willingness='interested')
        result=await self.role.callback(self.call('life.simulation','old-worker',p))
        self.assertEqual(result['code'],'protocol_mismatch')
        read=await self.role.callback(self.call('life.read','unready',dict(kind='simulation')))
        self.assertFalse(read['available'])
        app=dict(sources=[self.event.id],drive_evidence=[dict(dimension='exploration',kind='stimulation_change',delta=.2)])
        result=await self.role.callback(self.call('life.appraise','retired-field',app))
        self.assertEqual(result['code'],'experience_protocol_required')

    async def test_frozen_expression_and_prepared_delivery_keep_stop_fence_after_reconstruction(self):
        from life.expression import Expressions
        await self.enter()
        self.source('明确开启持续方案')
        await self.simulation('start',profile_confirmed=True,input_profile=dict(method='明确方案',intensity=.5,valence=.5,rhythm='continuous'))
        run=self.life.experience.read(self.f.scope)['run']
        proposal=dict(text='尚未发送的旧表达',experience_run_ref=run['simulation_run_id'],
            experience_control_revision=run['control_revision'])
        async def interrupted(*args):
            raise RuntimeError('isolated crash before dispatch')
        self.f.expressions.sleep=interrupted
        with self.assertRaises(RuntimeError):
            await self.f.expressions(self.f.scope,'frozen',proposal,[self.event.id])
        row=self.life.repo._one("SELECT * FROM expressions WHERE id='frozen'")
        plan=json.loads(row['plan'])
        self.assertEqual(plan['experience_run_ref'],run['simulation_run_id'])
        pending=self.life.repo._one("SELECT * FROM deliveries WHERE turn_id='frozen'")
        self.assertEqual(pending['status'],'pending')
        # Stop through the semantic control path, without a new chat revision,
        # so only the persisted experience fence can prevent this old send.
        await self.simulation('stop')
        rebuilt=Expressions(self.life,self.f.media,self.f.voices,self.f.transport)
        with self.assertRaises(SessionFailure) as failure:
            rebuilt.require_current(self.f.scope,row)
        self.assertEqual(failure.exception.code,'cancel_requested')
        self.assertIsNone(self.life.delivery.claim(pending['id']))
        self.assertFalse(self.f.sent)

    async def test_internal_memory_callback_has_no_simulation_authority(self):
        worker=self.role.memory_worker
        worker.protocol=1
        self.at=10000.
        self.f.bridge.session_input=lambda p:dict(state='accepted',message_id='fake')
        await worker.tick()
        row=self.life.repo._one("SELECT k FROM meta WHERE k LIKE 'memory_internal:%'")
        sid=row['k'].split(':',1)[1]
        result=await self.role.callback(dict(kind='life.simulation',call_id='denied',caller='role',
            session_id=sid,root_session_id=sid,proposal=dict(action='start')))
        self.assertEqual(result['code'],'scope_mismatch')
        self.assertFalse(self.f.sent)

    async def test_tick_uses_no_model_calls_and_channel_failure_preserves_opening_intent(self):
        await self.enter()
        self.source('明确开启')
        await self.simulation('start',profile_confirmed=True,input_profile=dict(method='明确方案',intensity=1,valence=.5,rhythm='continuous'))
        self.role.experience_channel_ready=lambda:False
        self.role.experience_tick()
        state=self.life.experience.read(self.f.scope)
        self.assertTrue(state['run']['desired_active'])
        self.assertEqual(state['run']['execution_state'],'paused')
        self.at+=600
        self.role.experience_tick()
        self.assertEqual(self.life.experience.read(self.f.scope)['arousal'],0)
        self.role.experience_channel_ready=lambda:True
        self.role.experience_tick()
        self.at+=30;self.role.experience_tick()
        self.assertGreater(self.life.experience.read(self.f.scope)['arousal'],0)
        self.assertFalse(self.f.sent)


if __name__=='__main__':
    unittest.main()
