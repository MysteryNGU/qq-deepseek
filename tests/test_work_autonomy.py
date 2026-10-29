import json
import time
import unittest
from unittest.mock import patch

from life.agent_session import SessionFailure
from life.tool_contracts import catalog, validate
from tests.role_fixture import Fixture


class WorkAutonomyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        self.role,self.life,self.repo=self.f.role,self.f.life,self.f.life.repo
        self.scope,self.root=self.f.scope,self.f.binding
        self.role.compact_tool_protocol=1
        self.seq=0

    def task_wake(self):
        task=self.f.task()
        event,_=self.life.ingest(self.scope,'runtime_wake','inference','继续任务',
            payload=dict(wake_kind='task_due',wake_plans=[dict(kind='task_due',intent_ref=task['ref'])]))
        self.root,item=self.role.stage_input(event)
        self.role.sessions.apply_event(self.root['session_id'],dict(type='step/start',seq=self.seq,time=time.time(),data=dict(turn=1,step=1)))
        self.seq+=1
        with self.repo.tx():self.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=1,handled=1 WHERE session_id=?",(self.root['session_id'],))
        return task,event

    async def call(self,kind,key,**p):
        return await self.role.callback(dict(kind='life.'+kind,call_id=key,proposal=p,
            root_session_id=self.root['session_id'],session_id=self.root['session_id']))

    async def test_wake_routes_to_task_and_requires_execution_then_can_finish(self):
        task,event=self.task_wake()
        self.assertEqual(self.root['intent_ref'],task['ref'])
        self.assertEqual(self.role.interaction_mode.state(self.scope)['mode'],'tools')
        stopped=await self.call('rest','empty')
        self.assertEqual(stopped.get('code'),'task_progress_required',stopped)
        op=self.role.before_tool(self.root,self.scope,dict(tool='read',call_id='step',session_id=self.root['session_id'],arguments={}))
        with self.repo.tx():self.repo._exec("UPDATE role_operations SET status='succeeded',result='{}' WHERE id=?",(op['operation_id'],))
        result,_=self.life.ingest(self.scope,'tool_result','tool_observation','verified output',payload=dict(operation_id=op['operation_id']))
        done=await self.call('intent','done',action='done',ref=task['ref'],base_revision=task['revision'],
            sources=[event.id],result_sources=[result.id])
        self.assertTrue(done['ok'],done)
        rest=await self.call('rest','finish')
        self.assertTrue(rest['ok'],rest)
        self.assertEqual(self.role.interaction_mode.state(self.scope)['mode'],'chat')

    async def test_blocker_is_accepted_by_real_contract_and_schedules_followup(self):
        task,_=self.task_wake()
        result=await self.call('rest','blocked',activity_blocker='所需服务连接失败，稍后复查')
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.role.tasks.get(task['ref'])['wake_blocker'],'所需服务连接失败，稍后复查')
        self.assertTrue(self.repo._one("SELECT 1 FROM role_wakes WHERE intent_ref=? AND state='pending'",(task['ref'],)))
        self.assertEqual(self.role.interaction_mode.state(self.scope)['mode'],'tools')

    def test_work_build_never_calls_social_projectors_and_delegate_has_no_persona(self):
        self.role.interaction_mode.select(self.scope,'tools')
        with patch.object(self.role.people,'project',side_effect=AssertionError('profile injected')), \
             patch.object(self.life,'drive_view',side_effect=AssertionError('drive injected')), \
             patch.object(self.role,'relationship_view',side_effect=AssertionError('relationship injected')):
            full=self.role.context(self.root,self.scope,{})
        static,dynamic=full.split('\n\n当前状态与材料：\n',1)
        self.assertNotIn('You are a curious character.',static)
        packet=json.loads(dynamic)
        for key in ('affect','drives','relationships','person_profiles','motivations','experience','contact_policy','continuity','persona_hash'):
            self.assertNotIn(key,packet)
        self.assertIn('mode:chat',static)
        delegate=self.role.context(self.root,self.scope,dict(caller='delegate'))
        self.assertNotIn('You are a curious character.',delegate)
        self.assertNotIn('当前状态与材料',delegate)

    def test_work_contracts_remove_relationship_fields_without_mutating_chat(self):
        work=catalog(work=True);chat=catalog()
        self.assertNotEqual(work['version'],chat['version'])
        encoded=json.dumps(work,ensure_ascii=False)
        for field in ('relationship_version','relation_impacts','affect_impulse','contact_basis','profile_reviews'):
            self.assertNotIn(field,encoded)
        self.assertIn('relation_impacts',json.dumps(chat))
        validate('life_intent',dict(goal='research',execution_mode='continuous'))
        validate('life_intent',dict(action='done',ref='task',base_revision=1,result_sources=['real-result']))
        validate('life_rest',dict(activity_blocker='service unavailable'))
        validate('life_say',dict(text='需要稍后重试',end_turn=True,activity_blocker='service unavailable'))

    def test_persona_edit_does_not_invalidate_work_request_but_remains_visible_in_chat(self):
        self.role.interaction_mode.select(self.scope,'tools')
        before=self.role.context_stamp(self.root,self.scope)
        (self.f.root/'persona.md').write_text('NEW_PERSONA_MARKER',encoding='utf-8')
        self.assertEqual(before,self.role.context_stamp(self.root,self.scope))
        self.assertNotIn('NEW_PERSONA_MARKER',self.role.stable_context(self.scope,{}))
        self.role.interaction_mode.select(self.scope,'chat')
        self.assertIn('NEW_PERSONA_MARKER',self.role.stable_context(self.scope,{}))

    def test_task_wake_cannot_override_manual_chat_or_resume_paused_task(self):
        task=self.f.task()
        self.role.interaction_mode.user_select(self.scope,'chat')
        event,_=self.life.ingest(self.scope,'runtime_wake','inference','old wake',
            payload=dict(wake_plans=[dict(kind='task_due',intent_ref=task['ref'])]))
        binding,_=self.role.stage_input(event)
        self.assertEqual(self.role.interaction_mode.state(self.scope)['policy'],'chat')
        self.assertEqual(self.role.tasks.get(task['ref'])['status'],'paused')
        self.assertIsNone(binding['intent_ref'])

    def test_profile_review_is_deferred_until_chat_without_inventing_a_review(self):
        self.role.cfg.profile_review_enabled=True
        self.role.people.observe(self.f.event)
        self.role.sessions.enqueue(self.root,self.f.event)
        with self.repo.tx():self.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=1,handled=1 WHERE session_id=?",(self.root['session_id'],))
        self.role.sessions.apply_event(self.root['session_id'],dict(type='step/start',seq=0,time=time.time(),data=dict(turn=1,step=1)))
        self.role.interaction_mode.select(self.scope,'tools')
        with self.repo.tx():self.role.people.finish(self.scope,self.root,{})
        row=self.repo._one('SELECT state,reason FROM person_encounters WHERE event_id=?',(self.f.event.id,))
        self.assertEqual((row['state'],row['reason']),('pending','deferred_tool_mode'))
        self.role.interaction_mode.select(self.scope,'chat')
        self.role.sessions.apply_event(self.root['session_id'],dict(type='step/start',seq=1,time=time.time(),data=dict(turn=2,step=1)))
        self.assertEqual(self.role.people.due(self.scope,self.root)[0]['source_ref'],self.f.event.id)

    def test_old_unknown_write_keeps_uncertainty_but_does_not_freeze_idle(self):
        with self.repo.tx():
            for name in ('pwsh','read'):
                self.repo._exec("INSERT INTO role_operations(id,session_id,scope_id,tool,request_hash,arguments,status,created) VALUES(?,?,?,?,?,'{}','unknown',1)",
                    (name,self.root['session_id'],self.scope.scope_id,name,name))
        self.role.attention.reconcile_stale_operations(time.time())
        self.assertEqual(self.repo._one("SELECT status FROM role_operations WHERE id='pwsh'")['status'],'unknown')
        self.assertEqual(self.repo._one("SELECT status FROM role_operations WHERE id='read'")['status'],'failed')
        result=self.role.attention.finish(self.scope,self.root,{},'done')
        self.assertEqual(result['mode'],'adaptive')
        self.assertTrue(self.repo._one("SELECT 1 FROM role_wakes WHERE kind='idle_probe' AND state='pending'"))

    async def test_queued_media_is_real_waiting_work_not_a_no_progress_failure(self):
        task,_=self.task_wake()
        with self.repo.tx():
            self.repo._exec("INSERT INTO work_jobs(id,scope_id,kind,payload,status,due,root_id,forget_epoch,created,updated) "
                "VALUES('image',?,'local_media','{}','pending',?,?,?,?,?)",(self.scope.scope_id,time.time(),task['ref'],
                    self.repo.forget_epoch(self.scope.scope_id),time.time(),time.time()))
        result=await self.call('rest','queued')
        self.assertTrue(result['ok'],result)
        self.assertEqual(self.role.interaction_mode.state(self.scope)['mode'],'tools')


if __name__=='__main__':unittest.main()
