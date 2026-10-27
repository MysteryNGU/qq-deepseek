"""Optional conversation before action and a durable per-intention reply hold."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from life.agent_session import SessionFailure
from life.role_agent import RoleAgent
from life.runtime import LifeRuntime


class WaitingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'life.db'
        self.life = LifeRuntime(self.path)
        self.bridge = SimpleNamespace(_generation=1,session_control=lambda *a,**k:None)
        self.cfg = SimpleNamespace(owner=1,idle_seconds=45)
        self.role = RoleAgent(self.life,self.bridge,self.cfg,self.tmp.name,AsyncMock(return_value=True))
        self.role.replay = AsyncMock()
        self.scope = self.life.scope('qq:private:1','1')
        self.root = self.role.sessions.binding(self.scope)
        self.request = self.input('a task')

    def tearDown(self):
        self.life.close()
        self.tmp.cleanup()

    def input(self,text,kind='user_report',actor='1'):
        event,_ = self.life.ingest(self.scope,'message',kind,text,actor_id=actor)
        self.role.sessions.enqueue(self.root,event)
        with self.life.repo.tx():
            self.life.repo._exec("UPDATE agent_inputs SET state='consumed' WHERE input_id=?",(event.id,))
        return event

    def intent(self,**proposal):
        with self.life.repo.tx():
            return self.role.intent(self.scope,proposal,self.root)['intent']

    def start_task(self):
        with self.life.repo.tx():
            self.role.resolve(self.root,dict(handled_input_ids=[self.request.id]))
        return self.intent(goal='specific work',sources=[self.request.id])

    def wait(self,task):
        return self.intent(action='wait',ref=task['ref'],base_revision=task['revision'],question='Shall we do this together?')

    def native(self,sid=None):
        return self.role.before_tool(self.root,self.scope,dict(session_id=sid or self.root['session_id'],
            call_id='native-'+str(self.life.clock()),tool='read',arguments={}))

    async def say(self,proposal,call_id='question'):
        return await self.role.callback(dict(root_session_id=self.root['session_id'],session_id=self.root['session_id'],
            kind='life.say',call_id=call_id,proposal=proposal))

    async def test_direct_action_and_notice_do_not_require_a_reply(self):
        self.start_task()
        self.assertTrue(self.native()['ok'])
        receipt = await self.say(dict(text='I will check this.',purpose='notice'))
        self.assertTrue(receipt['ok'])
        self.assertEqual(self.role.tasks.waiting(self.scope.scope_id),[])
        self.assertTrue(self.native()['ok'])

    async def test_question_commits_hold_before_send_and_blocks_delegates(self):
        task = self.start_task()
        current = self.role.sessions.require(self.root['session_id'])
        self.role._delegate_started(dict(parentSessionId=current['session_id'],childSessionId='child'))
        self.role._delegate_started(dict(parentSessionId='child',childSessionId='grandchild'))
        self.role.schedule(self.scope,task['ref'],1,'continue old task')
        async def send(*args):
            self.assertEqual(self.role.tasks.get(task['ref'])['status'],'waiting')
            for sid in (self.root['session_id'],'child','grandchild'):
                with self.assertRaises(SessionFailure) as caught:
                    self.native(sid)
                self.assertEqual(caught.exception.code,'reply_pending')
            return True
        self.role.send = send
        receipt = await self.say(dict(text='Would you like me to do this?',
            wait_for_reply=dict(intent_ref=task['ref'],base_revision=task['revision'])))
        self.assertTrue(receipt['ok'])
        waiting = self.role.tasks.get(task['ref'])
        self.assertEqual(waiting['wait_actor_id'],'1')
        self.assertEqual(self.life.repo._one('SELECT state FROM role_wakes WHERE intent_ref=?',(task['ref'],))['state'],'cancelled')
        self.assertTrue(self.life.repo._one("SELECT 1 FROM meta WHERE k LIKE 'role_cancel:%'"))

    async def test_timeout_wake_old_message_and_wrong_actor_cannot_resume(self):
        task = self.wait(self.start_task())
        for event in (self.request,self.input('idle wake','inference'),self.input('yes',actor='2')):
            with self.assertRaises(SessionFailure):
                self.intent(ref=task['ref'],base_revision=task['revision'],action='resume',handled_input_ids=[event.id])
        with self.assertRaises(ValueError):
            self.role.tasks.transition(self.scope.scope_id,task['ref'],'active')
        answer = self.input('yes, do that')
        active = self.intent(ref=task['ref'],base_revision=task['revision'],action='resume',handled_input_ids=[answer.id])
        self.assertEqual(active['status'],'active')
        self.assertIsNone(active['wait_reason'])
        self.assertEqual(active['reply_input_ids'],[answer.id])

    async def test_unrelated_work_and_chat_continue_but_child_goal_does_not_bypass_wait(self):
        task = self.wait(self.start_task())
        self.assertTrue((await self.say(dict(text='We can chat meanwhile.')))['ok'])
        self.role.read(self.scope,dict(kind='intents'),self.root)
        child = self.intent(goal='dependent part',parent_ref=task['ref'])
        with self.assertRaises(SessionFailure):
            self.native()
        self.role.read(self.scope,dict(kind='intents'),self.root)
        self.intent(goal='independent reading')
        self.assertTrue(self.native()['ok'])
        self.assertEqual(self.role.tasks.get(task['ref'])['status'],'waiting')
        self.assertEqual(self.role.tasks.blocker(child['ref'],self.scope.scope_id)['ref'],task['ref'])

    async def test_wait_survives_database_reopen_and_rest_only_schedules_independent_life(self):
        task = self.wait(self.start_task())
        self.life.close()
        self.life = LifeRuntime(self.path)
        self.role = RoleAgent(self.life,self.bridge,self.cfg,self.tmp.name,AsyncMock(return_value=True))
        self.role.replay = AsyncMock()
        self.root = self.role.sessions.binding(self.scope)
        with self.assertRaises(SessionFailure):
            self.native()
        self.role.rest(self.scope,{},self.root)
        self.assertTrue(self.life.repo._one("SELECT 1 FROM role_wakes WHERE state='pending' AND intent_ref IS NULL"))
        answer = self.input('I agree')
        active = self.intent(ref=task['ref'],base_revision=task['revision'],action='resume',handled_input_ids=[answer.id])
        self.assertEqual(active['status'],'active')

    async def test_lost_question_receipt_does_not_duplicate_wait_revision(self):
        task = self.start_task()
        plan = dict(text='Shall we?',wait_for_reply=dict(intent_ref=task['ref'],base_revision=task['revision']))
        self.role.send.side_effect = TimeoutError('ACK unknown')
        with self.assertLogs('life.role_agent',level='ERROR'):
            receipt = await self.say(plan)
        self.assertFalse(receipt['ok'])
        waiting = self.role.tasks.get(task['ref'])
        self.role.send.side_effect = None
        self.assertTrue((await self.say(plan))['ok'])
        self.assertEqual(self.role.tasks.get(task['ref'])['revision'],waiting['revision'])

    async def test_waiting_parent_also_holds_queued_worker_changes(self):
        from life.dsh_changes import process_queued
        from life.self_change import SelfChange
        parent = self.start_task()
        self.role.read(self.scope,dict(kind='intents'),self.root)
        self.intent(goal='dependent upgrade',parent_ref=parent['ref'])
        self.role._self_change = SelfChange(self.life,self.cfg,self.tmp.name)
        await self.role._self_change.apply(self.scope,dict(target='worker',verification_goal='reload tools'),'queued-change')
        self.wait(parent)
        await process_queued(self.role)
        row = self.life.repo._one("SELECT phase FROM self_changes WHERE id='queued-change'")
        self.assertEqual(row['phase'],'cancelled')

    async def test_group_reply_must_come_from_the_person_being_asked(self):
        self.scope = self.life.scope('qq:group:9','')
        self.root = self.role.sessions.binding(self.scope)
        self.request = self.input('discuss a plan',actor='2')
        task = self.wait(self.start_task())
        self.assertEqual(task['wait_actor_id'],'2')
        wrong = self.input('yes',actor='3')
        with self.assertRaises(SessionFailure):
            self.intent(ref=task['ref'],base_revision=task['revision'],action='resume',handled_input_ids=[wrong.id])
        right = self.input('yes',actor='2')
        self.assertEqual(self.intent(ref=task['ref'],base_revision=task['revision'],action='resume',handled_input_ids=[right.id])['status'],'active')


if __name__=='__main__':
    unittest.main()
