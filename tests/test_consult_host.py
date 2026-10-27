"""Persistent consultation input adoption and host authority checks."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import tempfile
import unittest

from scripts.codex_consult import atomic_json
from life.agent_session import SessionFailure
from life.role_agent import RoleAgent
from life.runtime import LifeRuntime


class ConsultHostTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ('INIT.md','AGENTS.md'):
            (self.root/name).write_text('synthetic init',encoding='utf-8')
        config = self.root/'consult.json'
        import sys
        atomic_json(config,dict(enabled=True,project=str(self.root),data_dir=str(self.root/'consult-data'),
            codex=sys.executable,python=sys.executable))
        self.life = LifeRuntime(self.root/'life.db')
        self.cfg = SimpleNamespace(owner=1,consult_config=str(config),life_agent='default')
        self.role = RoleAgent(self.life,SimpleNamespace(_generation=1),self.cfg,self.root,AsyncMock())
        self.role.consult_protocol = 1
        self.role.replay = AsyncMock()
        self.scope = self.life.scope('qq:private:1','1')
        self.binding = self.role.sessions.binding(self.scope)

    def tearDown(self):
        self.life.close()
        self.temp.cleanup()

    async def ask(self, **proposal):
        return await self.role.callback(dict(kind='life.consult',root_session_id=self.binding['session_id'],
            session_id=self.binding['session_id'],call_id='ask-1',proposal=dict(message='a synthetic question',**proposal)))

    async def test_short_chat_has_no_intent_and_control_queries_ignore_pending_input(self):
        first = await self.ask()
        self.assertTrue(first['launch'],first)
        self.assertEqual(self.life.repo._one("SELECT COUNT(*) AS n FROM meta WHERE k LIKE 'action_task:%'")['n'],0)
        message,_ = self.life.ingest_message(self.scope,'new real message')
        self.role.stage_input(message)
        result = await self.role.callback(dict(kind='life.consult',root_session_id=self.binding['session_id'],
            session_id=self.binding['session_id'],call_id='status-1',proposal=dict(action='status',request_id=first['request_id'])))
        self.assertEqual(result['status'],'queued',result)
        blocked = await self.role.callback(dict(kind='consult_bind',root_session_id=self.binding['session_id'],
            session_id=self.binding['session_id'],call_id='ask-1',request_id=first['request_id'],job_id='job-1'))
        self.assertEqual(blocked['code'],'input_pending',blocked)

    async def test_result_adoption_crash_window_and_new_generation_are_deduplicated(self):
        first = await self.ask()
        bridge = self.role.consult.bridge
        bridge.commit(first['request_id'],'completed',answer='synthetic answer',evidence=dict(process_stopped=True))
        real_adopted = bridge.adopted
        def crash(*args):raise OSError('fixture crash before adopted')
        bridge.adopted = crash
        with self.assertRaises(OSError):
            await self.role.consult.adopt()
        bridge.adopted = real_adopted
        await self.role.consult.adopt()
        await self.role.consult.adopt()
        self.assertEqual(self.life.repo._one("SELECT COUNT(*) AS n FROM events WHERE kind='consult_result'")['n'],1)
        self.assertEqual(self.life.repo._one("SELECT COUNT(*) AS n FROM agent_inputs WHERE input_id LIKE 'evt%'")['n'],1)
        operation = self.life.repo._one("SELECT * FROM role_operations WHERE tool='life_consult'")
        self.assertEqual(operation['status'],'succeeded')
        self.assertEqual(bridge.status(first['request_id'])['delivery'],'adopted')

    async def test_group_cannot_read_private_request_and_reset_does_not_reinject(self):
        first = await self.ask()
        group = self.life.scope('qq:group:2','1')
        binding = self.role.sessions.binding(group)
        denied = await self.role.callback(dict(kind='life.consult',root_session_id=binding['session_id'],
            session_id=binding['session_id'],call_id='public-1',proposal=dict(action='read',request_id=first['request_id'])))
        self.assertEqual(denied['code'],'scope_not_supported')
        self.role.consult.bridge.commit(first['request_id'],'completed',answer='private result',evidence=dict(process_stopped=True))
        record = self.role.consult.bridge.records()[0]
        with self.role.consult.bridge.state() as state:
            state['requests'][record['request_id']]['forget_epoch'] = -1
        await self.role.consult.adopt()
        self.assertEqual(self.life.repo._one("SELECT COUNT(*) AS n FROM events WHERE kind='consult_result'")['n'],0)

    async def test_restart_rebinds_an_unadopted_result_to_the_new_generation_once(self):
        first = await self.ask()
        bridge = self.role.consult.bridge
        bridge.commit(first['request_id'],'completed',answer='answer before restart',evidence=dict(process_stopped=True))
        original = bridge.adopted
        bridge.adopted = lambda *args: (_ for _ in ()).throw(OSError('crash after inbox commit'))
        with self.assertRaises(OSError):await self.role.consult.adopt()
        bridge.adopted = original
        self.role.sessions.retire(self.scope)
        restored = RoleAgent(self.life,SimpleNamespace(_generation=2),self.cfg,self.root,AsyncMock())
        current = restored.sessions.binding(self.scope)
        self.assertGreater(current['generation'],self.binding['generation'])
        await restored.consult.adopt()
        await restored.consult.adopt()
        self.assertEqual(self.life.repo._one("SELECT COUNT(*) AS n FROM events WHERE kind='consult_result'")['n'],1)
        self.assertEqual(self.life.repo._one('SELECT COUNT(*) AS n FROM agent_inputs WHERE session_id=?',(current['session_id'],))['n'],1)
        self.assertEqual(restored.consult.bridge.status(first['request_id'])['delivery'],'adopted')

    async def test_late_stop_releases_consult_lane_but_keeps_unknown_answer_protected(self):
        self.cfg.continuity_enabled=True
        self.role.continuity_protocol=1
        first=await self.ask()
        bridge=self.role.consult.bridge
        operation=self.life.repo._one("SELECT * FROM role_operations WHERE tool='life_consult'")
        initiative=self.role.initiative
        self.assertIn(operation['id'],initiative.get(initiative.slot_key())['members'])
        bridge.commit(first['request_id'],'unknown',reason='interrupted_without_final_evidence')
        await self.role.consult.adopt()
        self.assertIn(operation['id'],initiative.get(initiative.slot_key())['members'])
        bridge.settle(first['request_id'],stopped=True,exit_code=1)
        await self.role.consult.adopt()
        self.assertFalse(initiative.get(initiative.slot_key())['members'])
        self.assertEqual(self.life.repo._one('SELECT status FROM role_operations WHERE id=?',(operation['id'],))['status'],'unknown')
        metadata=initiative.get('initiative:operation:'+operation['id'])
        self.assertEqual(metadata['execution_state'],'settled')
        self.assertEqual(initiative.get(metadata['guard_ref'])['phase'],'outcome_unknown')
        count=self.life.repo._one("SELECT COUNT(*) AS n FROM events WHERE kind='consult_result'")['n']
        await self.role.consult.adopt()
        self.assertEqual(self.life.repo._one("SELECT COUNT(*) AS n FROM events WHERE kind='consult_result'")['n'],count)

    async def test_old_adopted_stop_receipt_repairs_lane_even_when_continuity_disabled(self):
        self.cfg.continuity_enabled=True
        self.role.continuity_protocol=1
        first=await self.ask()
        bridge=self.role.consult.bridge
        bridge.commit(first['request_id'],'unknown',reason='incomplete_result',evidence=dict(process_stopped=True))
        record=bridge.records()[0]
        bridge.adopted(first['request_id'],record['version'])
        self.cfg.continuity_enabled=False
        await self.role.consult.adopt()
        self.assertFalse(self.role.initiative.get(self.role.initiative.slot_key())['members'])
        self.assertEqual(self.life.repo._one("SELECT COUNT(*) AS n FROM events WHERE kind='consult_result'")['n'],0)

    async def test_cancelled_consult_clears_unknown_guard_only_after_stop_proof(self):
        self.cfg.continuity_enabled=True
        self.role.continuity_protocol=1
        first=await self.ask()
        bridge=self.role.consult.bridge
        bridge.commit(first['request_id'],'unknown',reason='interrupted_without_final_evidence')
        await self.role.consult.adopt()
        bridge.cancel(first['request_id'])
        bridge.settle(first['request_id'],stopped=True)
        await self.role.consult.adopt()
        initiative=self.role.initiative
        self.assertFalse(initiative.get(initiative.slot_key())['members'])
        operation=self.life.repo._one("SELECT * FROM role_operations WHERE tool='life_consult'")
        self.assertEqual(operation['status'],'cancelled')
        metadata=initiative.get('initiative:operation:'+operation['id'])
        self.assertEqual(initiative.get(metadata['guard_ref'])['phase'],'active')


if __name__ == '__main__':unittest.main()
