"""Task pagination, duplicate retirement and model-controlled activity endings."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from life.agent_session import SessionFailure,canonical
from life.resource_policy import estimate_tokens
from life.role_agent import RoleAgent
from life.runtime import LifeRuntime
from life.text import stable_id


class TaskActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.life = LifeRuntime(self.path/'life.db')
        self.cfg = SimpleNamespace(owner=1)
        self.bridge = SimpleNamespace(_generation=1)
        self.sender = AsyncMock(return_value=True)
        self.role = RoleAgent(self.life,self.bridge,self.cfg,self.path,self.sender)
        self.role.replay = AsyncMock()
        self.scope = self.life.scope('qq:private:1','1')
        self.root = self.role.sessions.binding(self.scope)
        self.source,_ = self.life.ingest_message(self.scope,'Please check my tasks.')
        self.role.sessions.enqueue(self.root,self.source)
        self.role.sessions.apply_event(self.root['session_id'],dict(type='step/start',seq=0,time=1000,data=dict(turn=1,step=1)))
        with self.life.repo.tx():
            self.life.repo._exec("UPDATE agent_inputs SET state='consumed',handled=1,claimed_turn=1 WHERE input_id=?",(self.source.id,))

    def tearDown(self):
        self.life.close()
        self.tmp.cleanup()

    async def call(self,tool,key,**proposal):
        return await self.role.callback(dict(kind='life.'+tool,root_session_id=self.root['session_id'],
            session_id=self.root['session_id'],call_id=key,proposal=proposal))

    def task(self,ref,**links):
        return self.role.tasks.ensure(self.scope,ref,'Task '+ref,revision=1,kind='ongoing',
            checkpoint='Still pending',**links)

    async def test_messages_allow_reading_and_another_message_before_explicit_rest(self):
        first = await self.call('say','first',text='I found something.',sources=[self.source.id])
        self.assertTrue(first['sent_confirmed'])
        self.assertFalse(first['turn_complete'])
        self.assertFalse(self.life.repo._one('SELECT 1 FROM role_wakes'))
        self.assertFalse(self.life.repo._one("SELECT 1 FROM meta WHERE k LIKE 'role_closed_turn:%'"))
        self.assertTrue((await self.call('read','reading',kind='intents'))['ok'])
        second = await self.call('say','second',text='Here is another thought.',sources=[self.source.id])
        self.assertFalse(second['turn_complete'])
        self.assertEqual(self.sender.await_count,2)
        resting = await self.call('rest','stop',reflection_outcome='action')
        self.assertTrue(resting['turn_complete'])
        self.assertEqual(self.life.repo._one("SELECT v FROM meta WHERE k=?",('role_closed_turn:'+self.root['session_id'],))['v'],'1')
        self.assertEqual(self.life.repo._one("SELECT COUNT(*) AS n FROM role_wakes WHERE state='pending'")['n'],1)

    async def test_last_expression_can_end_without_extra_rest_call(self):
        result = await self.call('say','last',text='That is all for now.',end_turn=True,sources=[self.source.id])
        self.assertTrue(result['turn_complete'])
        self.assertTrue(self.life.repo._one('SELECT 1 FROM role_wakes'))

    async def test_invalid_end_flags_have_no_outgoing_effect(self):
        for proposal in [dict(end_turn='false'),dict(end_turn=True,purpose='notice')]:
            result = await self.call('say','invalid'+str(proposal),text='Do not send.',sources=[self.source.id],**proposal)
            self.assertFalse(result['ok'])
        self.sender.assert_not_awaited()

    async def test_lost_delivery_does_not_report_turn_complete_or_retry_under_new_id(self):
        self.sender.return_value = dict(sent_confirmed=False)
        result = await self.call('say','unconfirmed',text='Pending delivery.',end_turn=True,sources=[self.source.id])
        self.assertFalse(result['turn_complete'])
        self.assertFalse(self.life.repo._one('SELECT 1 FROM role_wakes'))

    async def test_incomplete_listing_blocks_answer_until_all_task_pages_are_read(self):
        for i in range(5):self.task('t'+str(i))
        page = await self.call('read','page1',kind='intents',limit=2)
        self.assertEqual(page['total'],5)
        self.assertTrue(page['has_more'])
        blocked = await self.call('say','premature',text='Only two tasks.',sources=[self.source.id])
        self.assertEqual(blocked['code'],'incomplete_task_read')
        rest = await self.call('rest','premature-rest')
        self.assertEqual(rest['code'],'incomplete_task_read')
        refs = [t['ref'] for t in page['items']]
        while page['has_more']:
            page = await self.call('read','page'+str(len(refs)),**page['next_read'])
            refs.extend(t['ref'] for t in page['items'])
        self.assertEqual(refs,['t0','t1','t2','t3','t4'])
        self.assertTrue((await self.call('say','complete',text='Five tasks.',end_turn=True,sources=[self.source.id]))['ok'])

    def test_long_unicode_rows_are_bounded_and_full_details_remain_available(self):
        for i in range(25):
            self.role.tasks.ensure(self.scope,'large'+str(i),'任务🙂'*700,revision=1,checkpoint='进度'*600)
        refs = []
        p = dict(kind='intents')
        while True:
            page = self.role.read(self.scope,p,self.root)
            body = canonical(dict(ok=True,**page))
            self.assertLessEqual(estimate_tokens(body),self.role.resources.text_preview_target_tokens)
            self.assertLessEqual(len(body.encode()),self.role.resources.text_result_max_utf8_bytes)
            refs.extend(t['ref'] for t in page['items'])
            for t in page['items']:
                self.assertTrue(t['details_truncated'])
                detail = self.role.read(self.scope,t['next_read'],self.root)
                self.assertEqual(detail['task']['goal'],'任务🙂'*700)
            if not page['has_more']:break
            p = page['next_read']
        self.assertEqual(len(set(refs)),25)

    def test_pagination_rejects_changes_and_cross_conversation_cursors(self):
        for i in range(3):self.task('t'+str(i))
        page = self.role.read(self.scope,dict(kind='intents',limit=1),self.root)
        other = self.life.scope('qq:private:2','2')
        with self.assertRaises(SessionFailure):self.role.read(other,page['next_read'])
        self.role.tasks.ensure(self.scope,'t1','New goal',revision=2)
        with self.assertRaises(SessionFailure) as caught:self.role.read(self.scope,page['next_read'],self.root)
        self.assertEqual(caught.exception.code,'stale_context')

    def test_old_valid_tasks_stay_pending_and_completed_tasks_require_all_filter(self):
        task = self.task('old',updated_at=1)
        done = self.task('done')
        self.role.tasks.transition(self.scope.scope_id,done['ref'],'done')
        page = self.role.read(self.scope,dict(kind='intents'),self.root)
        self.assertEqual([t['ref'] for t in page['items']],[task['ref']])
        self.assertEqual(page['items'][0]['execution_status'],'pending')
        self.assertEqual(self.role.read(self.scope,dict(kind='intents',status='all'),self.root)['total'],2)

    async def test_new_user_message_releases_an_obsolete_listing_obligation(self):
        for i in range(3):self.task('t'+str(i))
        self.role.read(self.scope,dict(kind='intents',limit=1),self.root)
        event,_ = self.life.ingest_message(self.scope,'Never mind that list.')
        self.role.stage_input(event)
        self.role.task_reading.require_complete(self.root['session_id'])

    def test_merge_retires_duplicate_preserves_progress_and_cancels_its_wake(self):
        source = self.task('old')
        target = self.task('new')
        self.role.schedule(self.scope,source['ref'],30,'continue',kind='task_due')
        with self.life.repo.tx():
            result = self.role.intent(self.scope,dict(action='merge',ref='old',base_revision=1,
                target_ref='new',target_base_revision=1),self.root)
        self.assertEqual(result['merged']['status'],'cancelled')
        self.assertEqual(result['merged']['merged_into'],'new')
        self.assertEqual(result['intent']['checkpoint'],target['checkpoint'])
        self.assertEqual(result['intent']['merged_from'],['old'])
        self.assertEqual(self.life.repo._one("SELECT state FROM role_wakes WHERE intent_ref='old'")['state'],'cancelled')
        page = self.role.read(self.scope,dict(kind='intents'),self.root)
        self.assertEqual([t['ref'] for t in page['items']],['new'])
        self.assertEqual(self.role.tasks.get('old')['goal'],source['goal'])

    def test_merge_rejects_stale_other_scope_and_live_operations(self):
        self.task('old');self.task('new')
        proposal = dict(action='merge',ref='old',base_revision=1,target_ref='new',target_base_revision=2)
        with self.assertRaises(SessionFailure):self.role.intent(self.scope,proposal,self.root)
        proposal['target_base_revision']=1
        with self.life.repo.tx():
            self.life.repo._exec("INSERT INTO role_operations(id,session_id,scope_id,intent_ref,tool,request_hash,arguments,status,created) "
                "VALUES('running',?,?,?,'read','hash','{}','dispatched',1)",(self.root['session_id'],self.scope.scope_id,'old'))
        with self.assertRaises(SessionFailure):self.role.intent(self.scope,proposal,self.root)
        other = self.life.scope('qq:private:2','2')
        with self.assertRaises(SessionFailure):self.role.intent(other,proposal,self.role.sessions.binding(other))

    async def test_new_tasks_require_current_catalog_review_and_goal_dedup_ignores_topic_changes(self):
        first = await self.call('intent','new1',goal='Collect stickers',topic_key='stickers',sources=[self.source.id])
        self.assertTrue(first['ok'])
        duplicate = await self.call('intent','dup',goal='Collect stickers!',topic_key='renamed',sources=[self.source.id])
        self.assertEqual(duplicate['code'],'intent_exists')
        other = await self.call('intent','new2',goal='Study astronomy',sources=[self.source.id])
        self.assertEqual(other['code'],'task_review_required')
        await self.call('read','review',kind='intents')
        self.assertTrue((await self.call('intent','new2-reviewed',goal='Study astronomy',sources=[self.source.id]))['ok'])
        stale = await self.call('intent','new3',goal='Study painting',sources=[self.source.id])
        self.assertEqual(stale['code'],'task_review_required')

    async def test_merged_task_cannot_be_reopened_even_by_fresh_user_or_service_transition(self):
        self.task('old');self.task('new')
        merged = await self.call('intent','merge',action='merge',ref='old',base_revision=1,target_ref='new',target_base_revision=1)
        self.assertTrue(merged['ok'])
        for action in ['save','resume','done']:
            result = await self.call('intent','revive-'+action,action=action,ref='old',base_revision=2,checkpoint='Again')
            self.assertEqual(result['code'],'task_merged')
        with self.assertRaises(ValueError):self.role.tasks.transition(self.scope.scope_id,'old','active',user_resume=True)
        with self.assertRaises(ValueError):self.role.tasks.ensure(self.scope,'old',None,status='active')

    async def test_closed_task_requires_a_genuinely_later_user_input_to_resume(self):
        task = self.task('closed')
        cancelled = await self.call('intent','cancel',action='cancel',ref=task['ref'],base_revision=1)
        revision = cancelled['intent']['revision']
        saved = await self.call('intent','resave',ref=task['ref'],base_revision=revision,checkpoint='New wording')
        self.assertEqual(saved['code'],'task_closed')
        old = await self.call('intent','old-resume',action='resume',ref=task['ref'],base_revision=revision,
            handled_input_ids=[self.source.id])
        self.assertEqual(old['code'],'cancel_requested')
        event,_ = self.life.ingest_message(self.scope,'Resume the closed task.')
        self.role.sessions.enqueue(self.root,event)
        with self.life.repo.tx():
            self.life.repo._exec("UPDATE agent_inputs SET state='consumed' WHERE input_id=?",(event.id,))
        fresh = await self.call('intent','fresh-resume',action='resume',ref=task['ref'],base_revision=revision,
            handled_input_ids=[event.id])
        self.assertTrue(fresh['ok'])
        self.assertEqual(fresh['intent']['status'],'active')

    async def test_unsettled_work_prevents_false_completion(self):
        task = self.task('working')
        with self.life.repo.tx():
            self.life.repo._exec("INSERT INTO role_operations(id,session_id,scope_id,intent_ref,tool,request_hash,arguments,status,created) "
                "VALUES('live',?,?,?,'write_file','hash','{}','unknown',1)",(self.root['session_id'],self.scope.scope_id,task['ref']))
        done = await self.call('intent','done',action='done',ref=task['ref'],base_revision=1,end_direction=True)
        self.assertEqual(done['code'],'task_work_unsettled')
        self.assertEqual(self.role.tasks.get(task['ref'])['status'],'active')

    async def test_invalid_actions_and_unknown_references_do_not_create_records(self):
        for proposal in [dict(action='surprise',goal='Unexpected'),dict(action='resume',ref='missing'),
                         dict(goal='',origin='user')]:
            self.assertFalse((await self.call('intent','bad-'+str(proposal),**proposal))['ok'])
        self.assertEqual(self.role.task_reading.catalog(self.scope),[])

    async def test_self_wake_cannot_claim_a_new_user_assignment(self):
        wake,_ = self.life.ingest(self.scope,'runtime_wake','inference','An idle opportunity')
        result = await self.call('intent','fake-origin',goal='An idea',origin='user',sources=[wake.id])
        self.assertFalse(result['ok'])
        self.assertEqual(self.role.task_reading.catalog(self.scope),[])
        valid = await self.call('intent','real-origin',goal='A direct request',origin='user',sources=[self.source.id])
        self.assertTrue(valid['ok'])
        task = valid['intent']
        updated = await self.call('intent','retain-origin',ref=task['ref'],base_revision=task['revision'],origin='self',sources=[wake.id])
        self.assertEqual(updated['intent']['origin'],'user')
        self.assertEqual(updated['intent']['origin_source_event_ids'],[self.source.id])
        self.assertEqual(updated['intent']['origin_evidence'],'recorded_at_creation')

    def test_definite_read_errors_are_failed_with_initiative_off_and_reconcile_is_idempotent(self):
        ids = []
        for tool,call in [('read','read-failure'),('write_file','write-unknown')]:
            oid = stable_id('roleop',self.root['session_id'],call)
            data = dict(message=dict(toolCallId=call,isError=True,content=[dict(type='text',text='Error: unavailable')]))
            with self.life.repo.tx():
                self.life.repo._exec("INSERT INTO role_operations(id,session_id,scope_id,tool,request_hash,arguments,status,result,created) "
                    "VALUES(?,?,?,?,'hash','{}','unknown',?,1)",(oid,self.root['session_id'],self.scope.scope_id,tool,canonical(data)))
            ids.append(oid)
        self.assertEqual(self.role.initiative.reconcile_read_results(),1)
        self.assertEqual(self.role.initiative.reconcile_read_results(),0)
        self.assertEqual(self.life.repo._one('SELECT status FROM role_operations WHERE id=?',(ids[0],))['status'],'failed')
        self.assertEqual(self.life.repo._one('SELECT status FROM role_operations WHERE id=?',(ids[1],))['status'],'unknown')

    def test_new_failed_read_receipt_is_settled_even_without_continuity(self):
        call = 'new-read-error'
        op = stable_id('roleop',self.root['session_id'],call)
        with self.life.repo.tx():
            self.life.repo._exec("INSERT INTO role_operations(id,session_id,scope_id,tool,request_hash,arguments,status,created) "
                "VALUES(?,?,?,'read','hash','{}','dispatched',1)",(op,self.root['session_id'],self.scope.scope_id))
        self.role._observe_result(self.root['session_id'],dict(type='tool/result',seq=1,
            data=dict(message=dict(toolCallId=call,isError=True,content=[dict(type='text',text='Error: not found')]))))
        self.assertEqual(self.life.repo._one('SELECT status FROM role_operations WHERE id=?',(op,))['status'],'failed')

    def test_read_receipts_containing_job_ids_do_not_create_phantom_background_work(self):
        op = dict(id='read-operation',session_id=self.root['session_id'],tool='read')
        with self.life.repo.tx():
            status = self.role.initiative.observe_tool(op,dict(seq=10,data=dict(message=dict(isError=False,
                content=[dict(type='text',text=canonical(dict(status='done',job_id='observed-job',text='Result')))]))))
        self.assertEqual(status,'succeeded')
        self.assertFalse(self.role.initiative.get('initiative:operation:'+op['id']))


if __name__=='__main__':
    unittest.main()
