"""Request batches and real acknowledgements preserve unread/control fences."""
import unittest

from life.agent_session import AgentSessions,SessionFailure
from tests.role_fixture import Fixture


class InputBatchAckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        self.role,self.repo=self.f.role,self.f.life.repo
        self.sid=self.f.binding['session_id']
        self.seq=0

    def step(self,number):
        self.role.sessions.apply_event(self.sid,dict(seq=self.seq,type='step/start',data=dict(turn=1,step=number)))
        self.seq+=1

    def consume(self,event,step=1):
        self.role.sessions.enqueue(self.f.binding,event)
        with self.repo.tx():
            self.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=1,claimed_step=? WHERE input_id=?",
                (step,event.id))

    def event(self,text):return self.f.life.ingest_message(self.f.scope,text)[0]

    async def send(self,_action,params):
        self.f.sent.append(params)
        return dict(retcode=0,data=dict(message_id=len(self.f.sent)))

    async def call(self,kind,proposal,key='call'):
        return await self.role.callback(dict(kind=kind,proposal=proposal,call_id=key,
            session_id=self.sid,root_session_id=self.sid))

    def test_batch_survives_restart_and_cannot_confirm_later_or_unread_inputs(self):
        self.step(1);self.consume(self.f.event)
        unread=self.event('not in this request');self.role.sessions.enqueue(self.f.binding,unread)
        batch=self.role.sessions.input_batch(self.sid)
        self.assertEqual(batch['input_ids'],[self.f.event.id])
        other=self.event('later consumed input');self.consume(other)
        restored=AgentSessions(self.f.life)
        result=restored.resolve(self.sid,dict(handled_batch_ref=batch['ref']))
        self.assertEqual(set(result['pending']),{unread.id,other.id})
        foreign=restored.binding(self.f.life.scope('qq:group:90','1'))
        with self.assertRaises(SessionFailure):restored.resolve(foreign['session_id'],dict(handled_batch_ref=batch['ref']))
        with self.assertRaises(SessionFailure):restored.resolve(self.sid,dict(handled_input_ids=[unread.id]))

    async def test_batch_resolution_commits_but_new_stop_still_blocks_sending(self):
        self.step(1);self.consume(self.f.event)
        batch=self.role.sessions.input_batch(self.sid)
        stop=self.event('stop this work');self.role.sessions.enqueue(self.f.binding,stop)
        result=await self.call('life.say',dict(text='old answer',handled_batch_ref=batch['ref'],end_turn=True))
        self.assertEqual(result['code'],'input_pending')
        self.assertEqual(result['input_processing'],'committed')
        self.assertEqual([i['input_id'] for i in self.role.sessions.pending(self.sid)],[stop.id])
        self.assertEqual(self.f.sent,[])

    async def test_batch_notice_covers_all_user_inputs_and_internal_wake_does_not_reset_ack(self):
        self.f.cfg.acknowledge_before_work=True
        self.step(1);self.consume(self.f.event);second=self.event('also check the other file');self.consume(second)
        batch=self.role.sessions.input_batch(self.sid)
        self.f.expressions.transport=self.send
        proposal=dict(text='I will check both files.',purpose='notice',end_turn=False,handled_batch_ref=batch['ref'])
        result=await self.call('life.say',proposal)
        self.assertTrue(result['sent_confirmed'],result)
        sources=self.repo._one('SELECT sources FROM deliveries LIMIT 1')['sources']
        self.assertIn(self.f.event.id,sources);self.assertIn(second.id,sources)
        self.role.require_work_acknowledgement(self.f.scope,self.f.binding)
        wake,_=self.f.life.ingest(self.f.scope,'runtime_wake','inference','internal update')
        self.consume(wake,2);self.step(2)
        self.role.sessions.resolve(self.sid,dict(handled_input_ids=[wake.id]))
        self.role.require_work_acknowledgement(self.f.scope,self.f.binding)
        repeat=await self.call('life.say',proposal)
        self.assertEqual(repeat,result)
        self.assertEqual(len(self.f.sent),1)
        new=self.event('a new real request');self.consume(new,2)
        with self.assertRaises(SessionFailure) as error:
            self.role.require_work_acknowledgement(self.f.scope,self.f.binding)
        self.assertEqual(error.exception.code,'acknowledgement_required')

    async def test_unrelated_confirmed_message_does_not_acknowledge_the_request(self):
        self.f.cfg.acknowledge_before_work=True
        self.step(1);self.consume(self.f.event)
        self.f.expressions.transport=self.send
        await self.f.expressions(self.f.scope,'unrelated',dict(text='another topic'),[])
        with self.assertRaises(SessionFailure):self.role.require_work_acknowledgement(self.f.scope,self.f.binding)

    async def test_admission_guides_one_notice_and_restores_shell_without_ending_turn(self):
        self.f.cfg.acknowledge_before_work=True
        self.step(1);self.consume(self.f.event)
        self.role.intent(self.f.scope,dict(action='set_mode',mode='tools'),self.f.binding)
        self.f.expressions.transport=self.send
        admission=self.role.work_admission(self.f.scope,self.f.binding)
        self.assertEqual(admission['next_tool'],'life_say')
        static=await self.call('static_context',{})
        self.assertNotIn('pwsh',static['default_tools'])
        result=await self.call('life.say',dict(admission['proposal_fields'],text='I will check it now.'),'notice')
        self.assertTrue(result['sent_confirmed'],result)
        self.assertFalse(result['turn_complete'])
        self.assertTrue(self.role.work_admission(self.f.scope,self.f.binding)['ready'])
        self.assertIn('pwsh',(await self.call('static_context',{}))['default_tools'])
        self.assertEqual(len(self.f.sent),1)

    def test_internal_input_needs_resolution_but_no_notice(self):
        self.f.cfg.acknowledge_before_work=True
        self.step(1)
        wake,_=self.f.life.ingest(self.f.scope,'runtime_wake','inference','continue task')
        self.consume(wake)
        admission=self.role.work_admission(self.f.scope,self.f.binding)
        self.assertEqual(admission['next_tool'],'life_intent')
        self.role.sessions.resolve(self.sid,admission['proposal_fields'])
        self.assertTrue(self.role.work_admission(self.f.scope,self.f.binding)['ready'])

    def test_unread_stop_cannot_be_acknowledged_by_admission(self):
        self.step(1);self.consume(self.f.event)
        stop=self.event('stop');self.role.sessions.enqueue(self.f.binding,stop)
        admission=self.role.work_admission(self.f.scope,self.f.binding)
        self.assertEqual(admission['next_tool'],'wait_for_input')
        self.assertIsNone(admission['proposal_fields'])
        self.assertIn(stop.id,admission['unread_input_ids'])

    async def test_final_request_batch_projects_fresh_admission_after_consumption(self):
        self.f.cfg.acknowledge_before_work=True
        self.step(1)
        self.role.sessions.enqueue(self.f.binding,self.f.event)
        self.assertEqual(self.role.work_admission(self.f.scope,self.f.binding)['next_tool'],'wait_for_input')
        self.consume(self.f.event)
        result=await self.role.callback(dict(kind='context_validate',session_id=self.sid,root_session_id=self.sid,
            stamp=self.role.context_stamp(self.f.binding,self.f.scope)))
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['input_batch']['work_admission']['next_tool'],'life_say')


if __name__=='__main__':unittest.main()
