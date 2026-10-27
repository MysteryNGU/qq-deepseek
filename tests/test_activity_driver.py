import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from life.role_agent import RoleAgent
from life.runtime import LifeRuntime


class ActivityDriverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.life=LifeRuntime(Path(self.tmp.name)/'life.db');self.addCleanup(self.life.close)
        self.role=RoleAgent(self.life,SimpleNamespace(_generation=1),SimpleNamespace(owner=1),Path(self.tmp.name),AsyncMock(return_value=True))
        self.scope=self.life.scope('qq:private:1','1');self.root=self.role.sessions.binding(self.scope)
        self.source,_=self.life.ingest_message(self.scope,'Continue describing the garden until I say stop.')
        self.role.sessions.enqueue(self.root,self.source)
        with self.life.repo.tx():self.life.repo._exec("UPDATE agent_inputs SET state='consumed',handled=1 WHERE input_id=?",(self.source.id,))
        self.seq=-1
        self.role.replay=AsyncMock()

    def cycle(self,turn,end=False):
        self.seq+=1
        self.role.sessions.apply_event(self.root['session_id'],dict(type='turn/end' if end else 'turn/start',seq=self.seq,time=1000,data=dict(turn=turn)))
        if not end:
            self.seq+=1
            self.role.sessions.apply_event(self.root['session_id'],dict(type='step/start',seq=self.seq,time=1000,data=dict(turn=turn,step=1)))

    async def call(self,tool,key,**p):
        return await self.role.callback(dict(kind='life.'+tool,root_session_id=self.root['session_id'],session_id=self.root['session_id'],call_id=key,proposal=p))

    async def create(self):
        self.cycle(1)
        result=await self.call('intent','create',goal='Describe the garden',kind='ongoing',origin='user',sources=[self.source.id])
        self.assertTrue(result['ok'],result);return result['intent']

    async def test_three_new_delivered_segments_across_turns_and_rest(self):
        task=await self.create()
        for turn,text in enumerate(['The garden gate is blue.','A row of roses follows the path.','The path ends beside a pond.'],1):
            if turn>1:self.cycle(turn)
            self.assertTrue((await self.call('say','say'+str(turn),text=text,sources=[self.source.id]))['sent_confirmed'])
            self.assertTrue((await self.call('rest','rest'+str(turn)))['ok'])
            self.role.activities.settle()
            self.assertIsNotNone(self.role.tasks.get(task['ref'])['activity']['pending'])
            self.cycle(turn,True);self.role.activities.settle()
            wake=self.life.repo._one("SELECT * FROM role_wakes WHERE intent_ref=? AND state='pending'",(task['ref'],))
            self.assertEqual(wake['kind'],'task_due')
            with self.life.repo.tx():self.life.repo._exec("UPDATE role_wakes SET state='delivered' WHERE id=?",(wake['id'],))
        state=self.role.tasks.get(task['ref'])['activity']
        self.assertEqual(state['delivery_progress'],3);self.assertEqual(state['stalled'],0)
        self.assertEqual(self.role.send.await_count,3)

    async def test_unknown_operation_holds_pending_and_restart_preserves_it(self):
        task=await self.create();await self.call('rest','rest')
        self.cycle(1,True)
        with self.life.repo.tx():self.life.repo._exec("INSERT INTO role_operations(id,session_id,scope_id,intent_ref,tool,request_hash,arguments,status,created) VALUES('op',?,?,?,'write','hash','{}','unknown',1)",(self.root['session_id'],self.scope.scope_id,task['ref']))
        self.role.activities.settle();self.assertIsNotNone(self.role.tasks.get(task['ref'])['activity']['pending'])
        from life.activity_driver import ActivityDriver
        driver=ActivityDriver(self.role)
        with self.life.repo.tx():self.life.repo._exec("UPDATE role_operations SET status='succeeded' WHERE id='op'")
        driver.settle();self.assertIsNone(self.role.tasks.get(task['ref'])['activity']['pending'])
        driver.settle();self.assertEqual(self.life.repo._one("SELECT COUNT(*) AS n FROM role_wakes WHERE intent_ref=? AND state='pending'",(task['ref'],))['n'],1)

    async def test_cancel_fences_pending_and_old_wake(self):
        task=await self.create();await self.call('rest','rest');self.cycle(1,True)
        with self.life.repo.tx():self.role.tasks.transition(self.scope.scope_id,task['ref'],'cancelled')
        self.role.activities.settle()
        self.assertIsNone(self.role.tasks.get(task['ref'])['activity']['pending'])
        self.assertFalse(self.life.repo._one("SELECT 1 FROM role_wakes WHERE intent_ref=? AND state='pending'",(task['ref'],)))

    async def test_no_progress_does_not_spin_forever(self):
        task=await self.create()
        for turn in range(1,4):
            if turn>1:self.cycle(turn)
            result=await self.call('rest','rest'+str(turn));self.assertTrue(result['ok'],result)
            self.cycle(turn,True);self.role.activities.settle()
        saved=self.role.tasks.get(task['ref'])
        self.assertEqual(saved['status'],'paused');self.assertEqual(saved['activity']['hold'],'activity_no_progress')

    async def test_repeated_text_and_unconfirmed_send_are_not_new_progress(self):
        task=await self.create()
        root=self.role.sessions.require(self.root['session_id'])
        with self.life.repo.tx():
            self.role.activities.delivered(self.scope,root,'same')
            self.role.activities.delivered(self.scope,root,'same')
        self.role.send.return_value=dict(sent_confirmed=False)
        await self.call('say','lost',text='not confirmed',sources=[self.source.id])
        self.assertEqual(self.role.tasks.get(task['ref'])['activity']['delivery_progress'],1)

    async def test_interest_only_and_once_tasks_do_not_auto_continue(self):
        result=await self.call('intent','once',goal='A single reply',origin='user',sources=[self.source.id])
        self.assertEqual(result['intent']['execution_mode'],'manual')
        await self.call('rest','rest')
        self.assertFalse(self.role.tasks.get(result['intent']['ref'])['activity'].get('pending'))

    async def test_waiting_for_reply_is_not_a_timer_or_consent(self):
        task=await self.create()
        result=await self.call('intent','wait',action='wait',ref=task['ref'],base_revision=task['revision'],question='Continue tomorrow?',actor_id='1',sources=[self.source.id])
        self.assertTrue(result['ok'],result)
        await self.call('rest','rest');self.cycle(1,True);self.role.activities.settle()
        self.assertEqual(self.role.tasks.get(task['ref'])['status'],'waiting')
        self.assertFalse(self.life.repo._one("SELECT 1 FROM role_wakes WHERE intent_ref=? AND state='pending'",(task['ref'],)))

    async def test_explicit_same_person_group_to_private_transfer_keeps_identity(self):
        self.life.life_access.same_person_group_recall=True
        group=self.life.scope('qq:group:10')
        request,_=self.life.ingest(group,'message','user_report','Describe the garden',actor_id='1')
        self.role.tasks.ensure(group,'group-task','Describe the garden',revision=1,kind='ongoing',origin='user',
            execution_mode='continuous',origin_source_event_ids=[request.id],source_event_ids=[request.id],checkpoint='Gate described')
        self.assertEqual(self.role.activities.related(self.scope)[0]['ref'],'group-task')
        result=await self.call('intent','move',action='transfer',ref='group-task',base_revision=1,sources=[self.source.id])
        self.assertTrue(result['ok'],result);self.assertEqual(result['intent']['checkpoint'],'Gate described')
        self.assertEqual(self.role.tasks.get('group-task')['scope_id'],self.scope.scope_id)
        self.assertIsNone(self.role.tasks.get('group-task',group.scope_id))

    async def test_other_person_or_mixed_evidence_cannot_be_transferred(self):
        self.life.life_access.same_person_group_recall=True;group=self.life.scope('qq:group:10')
        source,_=self.life.ingest(group,'message','user_report','A task for someone else',actor_id='2')
        self.role.tasks.ensure(group,'other-task','Other task',revision=1,kind='ongoing',origin='user',origin_source_event_ids=[source.id],source_event_ids=[source.id])
        self.assertEqual(self.role.activities.related(self.scope),[])
        result=await self.call('intent','move-other',action='transfer',ref='other-task',base_revision=1,sources=[self.source.id])
        self.assertEqual(result['code'],'scope_mismatch')


if __name__=='__main__':unittest.main()
