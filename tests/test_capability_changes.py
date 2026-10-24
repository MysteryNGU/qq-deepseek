"""Install/restart facts stay attached to the original question and actual scope."""
import json
import unittest

from tests.role_fixture import Fixture
from life.self_change import SelfChange
from life.dsh_changes import process_queued
from life.text import stable_id


class CapabilityChangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        self.role=self.f.role
        self.role._self_change=SelfChange(self.f.life,self.f.cfg,self.f.root)

    async def test_A07_restart_snapshot_uses_recovered_original_agent_and_never_verifies_goal(self):
        task=self.f.task()
        self.f.bridge.restart_worker=lambda:None
        self.f.bridge.hello=lambda:dict(workerGeneration=2)
        scopes=[]
        def tools(*,scope):
            scopes.append(scope);return [dict(name='ordinary_installed_tool')]
        self.f.bridge.tools=tools
        async def recover():
            return None
        self.role.recover=recover
        await self.role._self_change.apply(self.f.scope,dict(target='worker',verification_goal='Read the original synthetic palette'),'restart')
        await process_queued(self.role)
        self.assertEqual(scopes,[self.f.binding['session_id']])
        row=self.f.life.repo._one("SELECT receipt FROM self_changes WHERE id='restart'")
        receipt=json.loads(row['receipt'])
        self.assertTrue(receipt['applied'])
        self.assertFalse(receipt['behavior_validated'])
        self.assertFalse(receipt['current_agent_snapshot']['verified_use'])
        self.assertEqual(receipt['current_agent_snapshot']['original_intent_ref'],task['ref'])
        self.assertEqual(self.role.tasks.get(task['ref'],self.f.scope.scope_id)['status'],'active')
        self.assertTrue(self.f.life.repo._one('SELECT 1 FROM role_wakes WHERE intent_ref=?',(task['ref'],)))

    async def test_A07_missing_environment_is_recorded_without_a_success_or_new_goal(self):
        task=self.f.task()
        def unavailable():
            raise FileNotFoundError('synthetic environment missing')
        self.f.bridge.restart_worker=unavailable
        async def recover():
            return None
        self.role.recover=recover
        await self.role._self_change.apply(self.f.scope,dict(target='worker',verification_goal='Original question'),'missing-env')
        await process_queued(self.role)
        receipt=json.loads(self.f.life.repo._one("SELECT receipt FROM self_changes WHERE id='missing-env'")['receipt'])
        self.assertFalse(receipt['applied'])
        self.assertFalse(receipt['behavior_validated'])
        self.assertIn('environment missing',receipt['error'])
        self.assertEqual(self.role.tasks.get(task['ref'],self.f.scope.scope_id)['status'],'active')

    def test_A07_install_visible_failed_call_and_real_success_remain_distinct_from_goal(self):
        for name,value,error in [('missing_environment',dict(ok=False,code='environment_missing'),True),
                ('installed_requires_restart',dict(installed=True,restartRequired=True),False),
                ('visible_but_failed',dict(ok=False,code='credential_missing'),True),
                ('verified_palette_read',dict(colors=['navy','coral'],observation='actual fixed sample'),False)]:
            f=Fixture()
            try:
                task=f.task();binding=f.role.sessions.require(f.binding['session_id'])
                f.role.before_tool(binding,f.scope,dict(tool='ordinary_palette',call_id=name,
                    session_id=binding['session_id'],arguments={}))
                f.role._observe_result(binding['session_id'],dict(type='tool/result',seq=1,data=dict(
                    message=dict(toolCallId=name,isError=error,content=[dict(type='text',text=json.dumps(value))]))))
                op=f.life.repo._one('SELECT * FROM role_operations WHERE id=?',(stable_id('roleop',binding['session_id'],name),))
                self.assertEqual(op['status'],'unknown' if error else 'succeeded')
                self.assertFalse(f.role.continuity.activities(f.scope)[0]['goal_verified'])
                self.assertEqual(f.role.tasks.get(task['ref'],f.scope.scope_id)['status'],'active')
                self.assertTrue(f.life.repo._one("SELECT 1 FROM events WHERE kind='tool_result'"))
            finally:
                f.close()
