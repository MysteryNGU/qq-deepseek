import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from life.agent_session import SessionFailure
from life.role_agent import RoleAgent
from life.runtime import LifeRuntime


class ControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.life = LifeRuntime(self.root/'life.db')
        self.scope = self.life.scope('qq:private:1','1')
        self.cancelled = []
        bridge = SimpleNamespace(_generation=1,session_control=lambda *args,**kwargs:self.cancelled.append(kwargs))
        self.role = RoleAgent(self.life,bridge,SimpleNamespace(owner=1,life_agent='default'),self.root,AsyncMock())
        self.binding = self.role.sessions.binding(self.scope)

    def tearDown(self):
        self.life.close()
        self.tmp.cleanup()

    def consumed(self,text='question',kind='user_report'):
        event,_ = self.life.ingest(self.scope,'message',kind,text)
        item = self.role.sessions.enqueue(self.binding,event)
        with self.life.repo.tx():
            self.life.repo._exec("UPDATE agent_inputs SET state='consumed' WHERE input_id=?",(item['input_id'],))
        return event

    def tool(self,name='read',call='1',sid=None):
        return dict(tool=name,call_id=call,session_id=sid or self.binding['session_id'],arguments={})

    def resolve(self,root,proposal):
        with self.life.repo.tx():
            return self.role.resolve(root,proposal)

    def intent(self,scope,proposal,root):
        with self.life.repo.tx():
            return self.role.intent(scope,proposal,root)

    async def test_input_hold_blocks_dispatch_until_model_resolves_it(self):
        event = self.consumed()
        self.assertFalse(self.role.before_tool(self.binding,self.scope,self.tool())['ok'])
        self.resolve(self.binding,dict(handled_input_ids=[event.id]))
        self.assertTrue(self.role.before_tool(self.binding,self.scope,self.tool())['ok'])
        self.consumed('stop')
        self.assertFalse(self.role.before_tool(self.binding,self.scope,self.tool(call='later'))['ok'])

    async def test_scheduling_a_wake_does_not_leave_input_storage_in_an_open_transaction(self):
        self.role.schedule(self.scope,None,20,'think later')
        event = self.consumed('a new message after scheduling')
        self.assertEqual(self.life.repo.get_event(event.id).text,'a new message after scheduling')

    async def test_retired_worker_error_notification_cannot_recreate_a_wake(self):
        self.role.sessions.retire(self.scope)
        self.role._observe_result(self.binding['session_id'],dict(type='turn/end',data=dict(reason=dict(kind='error'))))
        self.assertEqual(self.life.repo._one('SELECT COUNT(*) AS n FROM role_wakes')['n'],0)

    async def test_cancel_includes_child_with_no_tool_and_its_descendant(self):
        event = self.consumed()
        self.resolve(self.binding,dict(handled_input_ids=[event.id]))
        intent = self.intent(self.scope,dict(goal='long research',sources=[event.id]),self.binding)['intent']
        current = self.role.sessions.require(self.binding['session_id'])
        self.role.before_tool(current,self.scope,self.tool('subagent'))
        self.role._delegate_started(dict(parentSessionId=current['session_id'],childSessionId='child'))
        self.role._delegate_started(dict(parentSessionId='child',childSessionId='grandchild'))
        stop = self.consumed('stop that research')
        self.resolve(current,dict(handled_input_ids=[stop.id],activity_changes=[dict(intent_ref=intent['ref'],
            base_revision=intent['revision'],decision='cancel')]))
        await self.role.cancel_intent(current,intent['ref'])
        self.assertEqual(set(self.cancelled[0]['target_session_ids']),{'child','grandchild'})
        with self.assertRaises(SessionFailure):
            self.role.before_tool(current,self.scope,self.tool(sid='grandchild'))
        self.assertIsNone(self.role.sessions.require(current['session_id'])['intent_ref'])

    async def test_self_pause_can_resume_but_user_pause_requires_new_human_input(self):
        event = self.consumed()
        self.resolve(self.binding,dict(handled_input_ids=[event.id]))
        task = self.intent(self.scope,dict(goal='self interest',sources=[event.id]),self.binding)['intent']
        paused = self.intent(self.scope,dict(ref=task['ref'],base_revision=task['revision'],action='pause'),self.binding)['intent']
        resumed = self.intent(self.scope,dict(ref=task['ref'],base_revision=paused['revision'],action='resume'),self.binding)['intent']
        current = self.role.sessions.require(self.binding['session_id'])
        stop = self.consumed('pause')
        self.resolve(current,dict(handled_input_ids=[stop.id],activity_changes=[dict(intent_ref=task['ref'],
            base_revision=resumed['revision'],decision='pause')]))
        paused = self.role.tasks.get(task['ref'])
        wake = self.consumed('wake up',kind='inference')
        for ids in ([event.id],[wake.id]):
            with self.assertRaises(SessionFailure):
                self.intent(self.scope,dict(ref=task['ref'],base_revision=paused['revision'],action='resume',handled_input_ids=ids),current)
        human = self.consumed('continue that')
        resumed = self.intent(self.scope,dict(ref=task['ref'],base_revision=paused['revision'],action='resume',handled_input_ids=[human.id]),current)
        self.assertEqual(resumed['intent']['status'],'active')

    async def test_foreign_reference_and_stale_worker_rejected(self):
        event = self.consumed()
        other = self.life.scope('qq:private:2','2')
        self.role.tasks.ensure(other,'foreign','private goal')
        with self.assertRaises(SessionFailure):
            self.intent(self.scope,dict(ref='foreign',sources=[event.id]),self.binding)
        self.assertEqual(self.role.tasks.get('foreign')['scope_id'],other.scope_id)
        with self.assertRaises(SessionFailure):
            self.role._bind_caller(dict(root_session_id=self.binding['session_id'],session_id=self.binding['session_id'],_bridge_generation=0))


if __name__=='__main__':
    unittest.main()
