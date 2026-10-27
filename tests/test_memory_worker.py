"""Internal DSH host integration with isolated data and fake admission only."""
import json
import unittest

from life.memory_worker import OrganizeConfig
from tests.role_fixture import Fixture


class InternalMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture()
        self.addCleanup(self.f.close)
        self.role,self.life,self.repo=self.f.role,self.f.life,self.f.life.repo
        self.worker=self.role.memory_worker
        self.worker.config=OrganizeConfig(batch_events=5,interval_seconds=3600)
        self.worker.protocol=1
        self.admitted=[]
        self.f.bridge.session_input=self.admit
        self.f.bridge.input_status=lambda sid,iid:dict(state='not_found')
        self.f.bridge.session_control=lambda *args,**kwargs:dict(state='stopped')

    def admit(self,payload):
        self.admitted.append(payload)
        return dict(state='accepted',message_id='fake-message-'+payload['input_id'])

    def binding(self):
        row=self.repo._one(
            "SELECT * FROM agent_sessions WHERE scope_id LIKE ? AND status='current'",
            (self.life.agent_id+'|__memory_organize__:%',))
        return dict(row)

    async def call(self,root,kind,proposal=None,key='call-1',**extra):
        params=dict(session_id=root['session_id'],root_session_id=root['session_id'],
                    caller='role',kind=kind,call_id=key,proposal=proposal or {},**extra)
        return await self.role.callback(params)

    async def test_real_host_dispatch_read_commit_retire_and_no_user_message(self):
        await self.worker.tick()
        self.assertEqual(len(self.admitted),1)
        root=self.binding()
        static=await self.call(root,'static_context')
        self.assertEqual(static['processing_mode'],'internal')
        self.assertNotIn('Review fixture question',static['text'])
        page=await self.call(root,'life.read',dict(kind='memory_batch'),key='read-1')
        self.assertFalse(page['has_more'])
        self.assertIn('Review fixture question',json.dumps(page))
        proposal=dict(kind='organized_memory',coverage_digest=page['coverage_digest'],base_revision=0,
            claims=[dict(text='收到了一次提问',kind='reported',evidence_refs=[i['resource_ref'] for i in page['items']])])
        saved=await self.call(root,'life.remember',proposal,key='save-1')
        self.assertTrue(saved['ok'])
        self.assertTrue(saved['turn_complete'])
        self.assertEqual(await self.call(root,'life.remember',proposal,key='save-1'),saved)
        await self.worker.tick()
        self.assertEqual(self.repo._one('SELECT status FROM agent_sessions WHERE session_id=?',(root['session_id'],))['status'],'retired')
        expected=self.repo.get_event(self.f.event.id).ingest_seq
        self.assertEqual(self.repo._one('SELECT v FROM meta WHERE k=?',('memory_organize_cursor:'+self.life.agent_id,))['v'],str(expected))
        self.assertEqual(self.f.sent,[])
        self.assertEqual(self.repo._one('SELECT receipt FROM life_calls WHERE session_id=? LIMIT 1',(root['session_id'],))['receipt'],'{}')

    async def test_internal_cannot_send_search_or_change_settings(self):
        await self.worker.tick()
        root=self.binding()
        for kind in ('life.say','life.media','life.consult','life.change_self','method_get'):
            result=await self.call(root,kind,dict(text='do not send'),key=kind)
            self.assertFalse(result['ok'])
            self.assertEqual(result['code'],'scope_mismatch')
        for name in ('web_search','bash','life_say','life_media'):
            result=await self.call(root,'before_tool',key=name,tool=name)
            self.assertFalse(result['ok'])
        self.assertEqual(self.f.sent,[])

    async def test_host_commits_model_classified_ordinary_fragment_for_other_audience(self):
        event,_=self.life.ingest_message(self.f.scope,'推荐电影小森林。')
        await self.worker.tick()
        root=self.binding()
        page=await self.call(root,'life.read',dict(kind='memory_batch'),key='read-shared')
        resource=self.life.catalog.resolve(event.id)
        body=self.life.catalog.body(resource)
        start=len(body.split('\n',1)[0].encode('utf-8'))+1
        proposal=dict(kind='organized_memory',coverage_digest=page['coverage_digest'],base_revision=0,
            claims=[dict(text='收到作品推荐',kind='reported',evidence_refs=[i['resource_ref'] for i in page['items']])],
            ordinary_fragments=[dict(source_ref=resource['resource_id'],classification='ordinary',
                category='recommendation',uncertain=False,spans=[[start,len(body.encode('utf-8'))]])])
        result=await self.call(root,'life.remember',proposal,key='save-shared')
        self.assertTrue(result['ok'],result)
        self.assertEqual(len(result['share_refs']),1)
        ctx=self.life.life_access.bind(self.life.scope('qq:private:2','2'))
        _,shared=self.life.life_access.load(ctx,result['share_refs'][0])
        self.assertIn('推荐电影小森林。',shared)
        self.assertEqual(self.f.sent,[])

    async def test_sources_deleted_after_read_cannot_return_cached_text(self):
        await self.worker.tick()
        root=self.binding()
        first=await self.call(root,'life.read',dict(kind='memory_batch'),key='read')
        self.assertTrue(first['ok'])
        self.repo.redact_events([self.f.event.id])
        repeated=await self.call(root,'life.read',dict(kind='memory_batch'),key='read')
        self.assertFalse(repeated['ok'])
        self.assertNotIn('Review fixture question',json.dumps(repeated))
        await self.worker.tick()
        self.assertEqual(self.repo._one('SELECT status FROM agent_sessions WHERE session_id=?',(root['session_id'],))['status'],'retired')

    async def test_missing_handshake_never_admits_private_sources(self):
        self.worker.protocol=None
        await self.worker.tick()
        self.assertFalse(self.admitted)
        self.assertEqual(self.worker.status()['state'],'protocol_missing')

    async def test_one_non_chat_experience_is_reviewed_and_housekeeping_is_excluded(self):
        self.repo.redact_events([self.f.event.id])
        event,_=self.life.ingest(self.f.scope,'media_generated','tool_observation','完成了一次图像生成')
        self.life.ingest(self.f.scope,'state_time','inference','时间推进')
        await self.worker.tick()
        root=self.binding()
        metadata=self.worker.record(root['session_id'])
        self.assertEqual(set(metadata['sources']),{self.life.catalog.resolve(event.id)['resource_id']})
        self.assertEqual(len(self.admitted),1)

    async def test_empty_hour_does_not_call_model_and_restart_keeps_due_time(self):
        self.repo.redact_events([self.f.event.id])
        await self.worker.tick()
        self.assertFalse(self.admitted)
        self.life.ingest(self.f.scope,'delivered_segment','self_output','刚发出一段内容')
        from life.memory_worker import InternalMemory
        restarted=InternalMemory(self.role)
        restarted.protocol=1
        await restarted.tick()
        self.assertFalse(self.admitted)
        with self.repo.tx():
            self.repo._exec('UPDATE meta SET v=? WHERE k=?',('0','memory_review_checked:'+self.life.agent_id))
        await restarted.tick()
        self.assertEqual(len(self.admitted),1)

    async def test_large_hour_drains_batches_without_waiting_another_hour(self):
        self.worker.config=OrganizeConfig(batch_events=1)
        self.life.ingest(self.f.scope,'delivered_expression','self_output','回复已发送')
        await self.worker.tick()
        first=self.binding()
        page=await self.call(first,'life.read',dict(kind='memory_batch'),key='batch-one-read')
        proposal=dict(kind='organized_memory',coverage_digest=page['coverage_digest'],base_revision=0,
            claims=[dict(text='本批已核对',kind='reported',evidence_refs=[i['resource_ref'] for i in page['items']])])
        self.assertTrue((await self.call(first,'life.remember',proposal,key='batch-one-save'))['ok'])
        await self.worker.tick()
        self.assertEqual(len(self.admitted),2)
        self.assertNotEqual(first['session_id'],self.binding()['session_id'])

    async def test_cannot_commit_without_reading_or_silently_rest(self):
        await self.worker.tick()
        root=self.binding()
        metadata=self.worker.record(root['session_id'])
        job=self.repo._one('SELECT payload FROM work_jobs WHERE id=?',(metadata['job_id'],))
        payload=json.loads(job['payload'])
        result=await self.call(root,'life.remember',dict(kind='organized_memory',coverage_digest=payload['coverage_digest'],
            claims=[dict(text='未读不能总结',kind='reported',evidence_refs=list(payload['sources']))]))
        self.assertEqual(result['code'],'incomplete_coverage')
        result=await self.call(root,'life.rest')
        self.assertEqual(result['code'],'incomplete_coverage')
        paused=await self.call(root,'life.rest',dict(reason='材料需要稍后核对'),key='pause')
        self.assertTrue(paused['turn_complete'])
        self.assertEqual(self.repo._one('SELECT status FROM work_jobs WHERE id=?',(metadata['job_id'],))['status'],'paused')

    async def test_admission_unknown_keeps_same_input_and_no_second_session(self):
        def unavailable(*args,**kwargs):
            raise TimeoutError('fake lost acknowledgement')
        self.f.bridge.session_input=unavailable
        self.f.bridge.input_status=unavailable
        await self.worker.tick()
        root=self.binding()
        self.assertEqual(self.worker.record(root['session_id'])['state'],'admission_unknown')
        self.f.bridge.input_status=lambda sid,iid:dict(state='accepted',message_id='same-input')
        await self.worker.tick()
        self.assertEqual(self.worker.record(root['session_id'])['state'],'accepted')
        self.assertFalse(self.admitted)
        self.assertEqual(self.repo._one('SELECT COUNT(*) AS n FROM agent_inputs WHERE session_id=?',(root['session_id'],))['n'],1)

    async def test_expired_lease_is_quarantined_instead_of_replayed(self):
        await self.worker.tick()
        root=self.binding()
        metadata=self.worker.record(root['session_id'])
        with self.repo.tx():
            self.repo._exec('UPDATE work_jobs SET lease_until=0 WHERE id=?',(metadata['job_id'],))
        await self.worker.tick()
        self.assertEqual(self.repo._one('SELECT status FROM work_jobs WHERE id=?',(metadata['job_id'],))['status'],'quarantined')
        self.assertEqual(len(self.admitted),1)

    async def test_changed_policy_stops_internal_work_before_replaying_it(self):
        await self.worker.tick()
        root=self.binding()
        metadata=self.worker.record(root['session_id'])
        self.life.life_access.set_rule(self.f.event.id,'inspect','*','allow')
        await self.worker.tick()
        job=self.repo._one('SELECT status,error FROM work_jobs WHERE id=?',(metadata['job_id'],))
        self.assertEqual(job['status'],'paused')
        self.assertEqual(job['error'],'internal_sources_changed')
        self.assertEqual(self.repo._one('SELECT status FROM agent_sessions WHERE session_id=?',(root['session_id'],))['status'],'retired')
        self.assertEqual(len(self.admitted),1)


if __name__=='__main__':
    unittest.main()
