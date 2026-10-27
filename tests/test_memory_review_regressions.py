"""R1-R7 counterexamples, using only disposable data and fake services."""
import asyncio
from datetime import datetime
import json
import unittest
from unittest.mock import patch

from life.embedding import EmbeddingConfig, HttpEmbedding
from life.life_access import RecallError
from life.agent_session import SessionFailure
from life.spending import ZONE,Spending,SpendingPolicy
from tests.role_fixture import Fixture


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.f=Fixture()
        self.addCleanup(self.f.close)
        self.life,self.repo=self.f.life,self.f.life.repo

    def organize(self,events,claims,key='review-counterexample'):
        ctx=self.life.life_access.bind(self.f.scope,mode='internal')
        prepared=self.life.memory_organize.prepare(ctx,[e.id for e in events],topic_ref=key)
        lease=self.life.jobs.claim(prepared['job_ref'])
        page=self.life.memory_organize.read_batch(ctx,lease)
        self.assertFalse(page['has_more'])
        proposal=dict(coverage_digest=page['coverage_digest'],claims=[dict(text=text,kind=kind,
            evidence_refs=[self.life.catalog.resolve(e.id)['resource_id'] for e in refs]) for text,kind,refs in claims])
        return ctx,self.life.memory_organize.commit(ctx,lease,proposal,key)

    def test_R1_policy_cleanup_does_not_forget_original_history(self):
        event=self.repo.get_event(self.f.event.id)
        with self.repo.tx():
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',
                ('memory_policy_floor:'+self.f.scope.scope_id,str(event.received_at+1)))
        ctx=self.life.life_access.bind(self.f.scope)
        self.assertIn(event.text,self.life.life_recall.read(ctx,event.id)['text'])
        self.assertTrue(self.life.life_recall.search(ctx,'fixture')['items'])

    def test_R2_revoked_thought_stays_historical_after_organizing(self):
        event,_=self.life.ingest(self.f.scope,'reflection','inference','旧推断反例')
        with self.repo.tx():
            self.repo._exec('INSERT INTO learning_source_barriers VALUES(?,?,?,?)',
                (self.f.scope.scope_id,event.id,'withdrawn',self.life.clock()))
        ctx,saved=self.organize([event],[('复活的推断反例','interpreted',[event])])
        result=self.life.life_recall.search(ctx,'复活的推断反例')
        self.assertNotIn(saved['resource_ref'],[item['resource_ref'] for item in result['items']])
        self.assertIn('复活的推断反例',self.life.life_recall.read(ctx,saved['resource_ref'])['text'])

    def test_R1_old_interpretation_is_readable_but_not_automatically_adopted(self):
        note=self.life.memory.remember(self.f.scope,'interpretation','旧结论不能复活',[self.f.event.id],source_kind='inference')
        with self.repo.tx():
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('memory_policy_floor:'+self.f.scope.scope_id,str(note.recorded_at+1)))
        ctx=self.life.life_access.bind(self.f.scope)
        self.assertNotIn('旧结论不能复活',json.dumps(self.life.life_recall.search(ctx,'旧结论'),ensure_ascii=False))
        page=self.life.life_recall.read(ctx,note.id)
        self.assertIn('旧结论不能复活',page['text'])
        self.assertFalse(page['learning']['eligible'])
        self.life.life_access.set_rule(self.f.event.id,'disclose','*','deny')
        with self.assertRaises(RecallError):
            self.life.life_recall.read(self.life.life_access.bind(self.f.scope),note.id)
        self.repo.redact_events([self.f.event.id])
        with self.assertRaises(RecallError):
            self.life.life_recall.read(self.life.life_access.bind(self.f.scope),self.f.event.id)

    def test_R2_mixed_cross_scope_summary_filters_only_invalid_claims(self):
        old,_=self.life.ingest(self.f.scope,'reflection','inference','旧来源')
        other=self.life.scope('qq:private:2','2')
        good,_=self.life.ingest_message(other,'海鸥新材料')
        with self.repo.tx():
            self.repo._exec('INSERT INTO learning_source_barriers VALUES(?,?,?,?)',(self.f.scope.scope_id,old.id,'revoked',self.life.clock()))
        ctx,saved=self.organize([old,good],[('不能采用的鲸鱼推断','interpreted',[old]),('海鸥可靠材料','reported',[good])])
        result=json.dumps(self.life.life_recall.search(ctx,'海鸥'),ensure_ascii=False)
        self.assertIn('海鸥可靠材料',result)
        self.assertNotIn('不能采用的鲸鱼推断',result)
        original=self.life.life_recall.read(ctx,saved['resource_ref'])
        self.assertIn('不能采用的鲸鱼推断',original['text'])
        item=next(i for i in self.life.life_recall.search(ctx,'海鸥')['items'] if i['resource_ref']==saved['resource_ref'])
        self.assertEqual(item['content_hash'],original['content_hash'])
        self.assertFalse(item['preview_complete'])
        self.assertEqual(item['preview_scope'],'current_claims')
        self.assertEqual(item['source_kind'],'inference')
        with self.repo.tx():
            self.repo._exec('INSERT INTO learning_source_barriers VALUES(?,?,?,?)',(other.scope_id,good.id,'revoked',self.life.clock()))
        self.assertNotIn(saved['resource_ref'],[i['resource_ref'] for i in self.life.life_recall.search(ctx,'海鸥')['items']])

    def test_R2_expired_origin_and_repeated_summaries_never_renew_it(self):
        now=self.life.catalog.clock()
        old,_=self.life.ingest(self.f.scope,'reflection','inference','过期旧想法',occurred_at=now-8*86400)
        ctx,expired=self.organize([old],[('过期结论','reported',[old])])
        self.assertFalse(self.life.catalog.live(self.life.catalog.resolve(expired['resource_ref'])))
        fresh,_=self.life.ingest(self.f.scope,'thought','inference','新想法')
        _,first=self.organize([fresh],[('第一版想法','interpreted',[fresh])],key='first')
        with patch.object(self.life.catalog,'clock',return_value=now+2*86400):
            _,second=self.organize([fresh],[('第二版想法','interpreted',[fresh])],key='second')
        def deadline(saved):
            return json.loads(self.repo._one('SELECT v FROM meta WHERE k=?',('memory_summary:'+saved['memory_id'],))['v'])['claims'][0]['learning']['valid_until']
        self.assertEqual(deadline(first),deadline(second))
        with patch.object(self.life.catalog,'clock',return_value=now+8*86400):
            self.assertFalse(self.life.catalog.live(self.life.catalog.resolve(second['resource_ref'])))
            self.assertIn('第二版想法',self.life.life_recall.read(ctx,second['resource_ref'])['text'])

    def test_R2_original_short_lifetime_and_task_revision_are_retained(self):
        task=self.f.task()
        origin,_=self.life.ingest(self.f.scope,'thought','inference','临时结论')
        note=self.life.memory.remember(self.f.scope,'interpretation','临时结论',[origin.id],source_kind='inference',
            valid_until=self.life.clock()+60,topic_tags=['intent:'+task['ref'],'intent-revision:'+str(task['revision'])])
        ctx,saved=self.organize([origin],[('仍需当前任务','interpreted',[origin])])
        self.assertTrue(self.life.catalog.live(self.life.catalog.resolve(saved['resource_ref'])))
        raw=json.loads(self.repo._one('SELECT v FROM meta WHERE k=?',('action_task:'+task['ref'],))['v'])
        raw['revision']+=1
        with self.repo.tx():
            self.repo._exec('UPDATE meta SET v=? WHERE k=?',(json.dumps(raw),'action_task:'+task['ref']))
        self.assertFalse(self.life.catalog.live(self.life.catalog.resolve(saved['resource_ref'])))
        self.assertIn('仍需当前任务',self.life.life_recall.read(ctx,saved['resource_ref'])['text'])
        self.assertEqual(json.loads(self.repo._one('SELECT v FROM meta WHERE k=?',('memory_summary:'+saved['memory_id'],))['v'])['claims'][0]['learning']['valid_until'],note.valid_until)

    def test_R2_media_version_changes_learning_but_not_history(self):
        self.life.jobs.enqueue(self.f.scope,'local_media',dict(spec=dict(model_version='model-1',
            _adapter=dict(provider_id='fake',adapter_version='1',digest='one'))),key='media-evidence')
        event,_=self.life.ingest(self.f.scope,'media_result','tool_observation','实验材料',payload=dict(job_id='media-evidence'))
        self.life.catalog.provider_current=lambda tags:True
        ctx,saved=self.organize([event],[('该版本的构图结论','interpreted',[event])])
        self.life.catalog.provider_current=lambda tags:False
        self.assertFalse(self.life.catalog.live(self.life.catalog.resolve(saved['resource_ref'])))
        self.assertIn('该版本的构图结论',self.life.life_recall.read(ctx,saved['resource_ref'])['text'])

    def test_R3_denied_first_event_does_not_starve_following_event(self):
        worker=self.f.role.memory_worker
        worker.protocol=1
        self.life.life_access.set_rule(self.f.event.id,'internal_use','*','deny')
        good,_=self.life.ingest(self.f.scope,'observation','tool_observation','合法后续经历')
        admitted=[]
        self.f.bridge.session_input=lambda p:admitted.append(p) or dict(state='accepted',message_id='one')
        self.f.bridge.session_control=lambda *a,**kw:dict(state='stopped')
        asyncio.run(worker.tick())
        self.assertEqual(len(admitted),1)
        job=self.repo._one("SELECT payload FROM work_jobs WHERE kind='memory_organize'")
        sources=json.loads(job['payload'])['sources']
        self.assertEqual(set(sources),{self.life.catalog.resolve(good.id)['resource_id']})
        self.assertEqual(json.loads(job['payload'])['coverage_excluded'][0]['classification'],'purpose_denied')
        jobrow=self.repo._one("SELECT * FROM work_jobs WHERE kind='memory_organize'")
        metadata=json.loads(self.repo._one("SELECT v FROM meta WHERE k LIKE 'memory_internal:%'")['v'])
        lease=worker._lease(metadata)
        ctx=self.life.life_access.bind(self.f.scope,mode='internal')
        page=self.life.memory_organize.read_batch(ctx,lease)
        result=self.life.memory_organize.commit(ctx,lease,dict(coverage_digest=page['coverage_digest'],
            claims=[dict(text='合法后续经历已整理',kind='observed',evidence_refs=list(sources))]),'partial-window')
        self.assertFalse(result['complete_for_range'])
        self.assertEqual(page['excluded_count'],1)

    def test_R3_denied_ancestor_is_excluded_without_loading_the_body(self):
        worker=self.f.role.memory_worker;worker.protocol=1
        self.life.ingest(self.f.scope,'observation','tool_observation','派生内容',source_event_ids=[self.f.event.id])
        self.life.life_access.set_rule(self.f.event.id,'internal_use','*','deny')
        with patch.object(self.life.catalog,'body',side_effect=AssertionError('denied body read')):
            asyncio.run(worker.tick())
        reasons=[json.loads(r['v'])['classification'] for r in self.repo._all("SELECT v FROM meta WHERE k LIKE 'memory_review_exclusion:%'")]
        self.assertEqual(reasons,['purpose_denied','purpose_denied'])

    def test_R3_all_denied_or_deleted_sources_advance_across_restart_without_reading(self):
        from life.memory_worker import InternalMemory
        worker=self.f.role.memory_worker;worker.protocol=1
        self.life.life_access.set_rule(self.f.event.id,'internal_use','*','deny')
        deleted,_=self.life.ingest(self.f.scope,'observation','tool_observation','将被删除')
        self.repo.redact_events([deleted.id])
        with patch.object(self.life.catalog,'body',side_effect=AssertionError('forbidden source body read')):
            asyncio.run(worker.tick())
        after=int(self.repo._one('SELECT v FROM meta WHERE k=?',('memory_organize_cursor:'+self.life.agent_id,))['v'])
        self.assertEqual(after,self.repo.get_event(deleted.id).ingest_seq)
        restarted=InternalMemory(self.f.role);restarted.protocol=1
        asyncio.run(restarted.tick())
        self.assertFalse(restarted.status()['window_active'])
        rows=self.repo._all("SELECT v FROM meta WHERE k LIKE 'memory_review_exclusion:%'")
        self.assertEqual({json.loads(r['v'])['classification'] for r in rows},{'purpose_denied','deleted'})

    def test_R3_unprojectable_batch_is_visible_as_repair_backlog_and_does_not_stall(self):
        worker=self.f.role.memory_worker;worker.protocol=1
        with patch.object(self.life.catalog,'live',return_value=False):
            asyncio.run(worker.tick())
        status=worker.status()
        self.assertEqual(status['state'],'attention_required')
        self.assertEqual(status['repair_backlog'],1)
        self.assertGreater(int(self.repo._one('SELECT v FROM meta WHERE k=?',('memory_organize_cursor:'+self.life.agent_id,))['v']),0)
        asyncio.run(worker.tick())
        self.assertFalse(worker.status()['window_active'])

    def test_R3_deny_during_read_waits_for_stop_then_rescans_legal_sources(self):
        self.stopped_source_rescan(delete=False)

    def test_R3_delete_during_read_waits_for_stop_then_rescans_legal_sources(self):
        self.stopped_source_rescan(delete=True)

    def stopped_source_rescan(self,*,delete):
        worker=self.f.role.memory_worker;worker.protocol=1
        good,_=self.life.ingest(self.f.scope,'observation','tool_observation','仍合法的经历')
        admitted=[]
        self.f.bridge.session_input=lambda p:admitted.append(p) or dict(state='accepted',message_id='one')
        self.f.bridge.session_control=lambda *a,**kw:dict(state='cancel_requested')
        asyncio.run(worker.tick())
        root=dict(self.repo._one("SELECT * FROM agent_sessions WHERE scope_id LIKE ? AND status='current'",
            (self.life.agent_id+'|__memory_organize__:%',)))
        metadata=worker.record(root['session_id'])
        lease=worker._lease(metadata)
        self.life.memory_organize.read_batch(worker._context(root,metadata),lease)
        if delete:
            self.repo.redact_events([self.f.event.id])
        else:
            self.life.life_access.set_rule(self.f.event.id,'internal_use','*','deny')
        asyncio.run(worker.tick())
        self.assertEqual(len(admitted),1)
        self.assertEqual(worker.status()['state'],'attention_required')
        self.assertEqual(worker.status()['reason'],'reader_stop_pending')
        self.f.bridge.session_control=lambda *a,**kw:dict(state='stopped')
        asyncio.run(worker.tick())
        self.assertEqual(len(admitted),2)
        latest=self.repo._one("SELECT payload FROM work_jobs WHERE kind='memory_organize' AND status='running'")
        self.assertEqual(set(json.loads(latest['payload'])['sources']),{self.life.catalog.resolve(good.id)['resource_id']})

    def share(self,text,selected):
        event,_=self.life.ingest_message(self.f.scope,text)
        resource=self.life.catalog.resolve(event.id)
        body=self.life.catalog.body(resource).encode('utf-8')
        start=body.index(selected.encode('utf-8'))
        proposal=dict(source_ref=resource['resource_id'],classification='ordinary',category='recommendation',
            uncertain=False,spans=[[start,start+len(selected.encode('utf-8'))]])
        ctx=self.life.life_access.bind(self.f.scope,mode='internal')
        return self.f.role.memory_worker.sharing.prepare(ctx,proposal,{resource['resource_id']:resource['source_version']})

    def test_R4_postposed_confidentiality_blocks_ordinary_proposal(self):
        with self.assertRaises(RecallError):
            self.share('推荐电影小森林。这件事先别告诉任何人。','推荐电影小森林。')

    def test_R4_confidentiality_negation_quotations_and_independent_matters(self):
        selected='推荐电影小森林。'
        for text in ['请保密。'+selected,selected+'别告诉任何人。',selected+'刚才说的先保密。',
                     '不'+selected,'他说“这件事保密”。'+selected]:
            with self.subTest(text=text),self.assertRaises(RecallError):
                self.share(text,selected)
        for text in [selected,selected+'我的密码是 secret。',selected+'我的秘密不能告诉别人。',
                     selected+'电影台词是“这件事别告诉任何人”。','电影台词是“别告诉别人”。'+selected,
                     selected+'下面的内容要保密：我的密码是 secret。']:
            with self.subTest(text=text):
                self.assertTrue(self.share(text,selected))

    def test_R5_shared_first_person_quote_has_safe_attribution(self):
        ref=self.share('我推荐电影小森林。','我推荐电影小森林。')
        ctx=self.life.life_access.bind(self.life.scope('qq:private:2','2'))
        item=next(i for i in self.life.life_recall.search(ctx,'电影')['items'] if i['resource_ref']==ref)
        self.assertEqual(item['attribution']['experience'],'reported_by_contact')
        self.assertTrue(item['attribution']['speaker'])
        self.assertEqual(item['actor_refs'],[])
        self.assertEqual(item['evidence_refs'],[])
        self.assertEqual(item['attribution']['quote_voice'],'original_speaker')
        self.assertEqual(item['attribution']['bot_experience'],'heard')
        self.assertNotIn('actor:qq:1',json.dumps(item))
        page=self.life.life_recall.read(ctx,ref)
        self.assertEqual(page['attribution'],item['attribution'])
        self.assertIn('attribution',page['text'])

    def test_R5_first_person_story_is_heard_not_personally_experienced(self):
        ref=self.share('我昨天去了海边。','我昨天去了海边。')
        ctx=self.life.life_access.bind(self.life.scope('qq:private:2','2'))
        page=self.life.life_recall.read(ctx,ref)
        self.assertEqual(page['attribution']['experience'],'reported_by_contact')
        self.assertEqual(page['attribution']['bot_experience'],'heard')
        self.assertNotIn('actor',page['attribution'])

    def test_R6_evicted_success_gets_a_new_paid_generation_not_unknown(self):
        config=EmbeddingConfig.parse(dict(enabled=True,endpoint='http://localhost/embeddings',model_id='fake',
            model_revision='1',provider_revision='1',dimension=2,allow_external_text=True,
            price_version='fake',input_nano_per_token=1))
        calls=[]
        def transport(texts):
            calls.append(texts)
            return dict(data=[dict(index=0,embedding=[1.,0.])],usage=dict(prompt_tokens=2))
        adapter=HttpEmbedding(self.life,self.f.role.spending,config,transport=transport)
        async def run():
            return await adapter.embed(['查询'],model_key='m',purpose='memory_query',call_key='same',source_refs=[])
        asyncio.run(run())
        now=self.life.clock()
        with patch.object(self.life,'clock',return_value=now+3601):
            adapter.cleanup()
            result=asyncio.run(run())
        self.assertTrue(result['vectors'])
        self.assertEqual(len(calls),2)
        self.assertEqual(self.repo._one('SELECT COUNT(*) AS n FROM spending_calls')['n'],2)

    def embedding(self,transport):
        config=EmbeddingConfig.parse(dict(enabled=True,endpoint='http://localhost/embeddings',model_id='fake',
            model_revision='1',provider_revision='1',dimension=2,allow_external_text=True,price_version='fake',input_nano_per_token=1))
        return HttpEmbedding(self.life,self.f.role.spending,config,transport=transport)

    def embed(self,adapter,key):
        return asyncio.run(adapter.embed(['查询'],model_key='m',purpose='memory_query',call_key=key,source_refs=[]))

    def test_R6_capacity_eviction_restart_and_same_generation_single_charge(self):
        calls=[]
        def transport(texts):
            calls.append(texts)
            return dict(data=[dict(index=0,embedding=[1.,0.])],usage=dict(prompt_tokens=2))
        adapter=self.embedding(transport)
        for i in range(129):
            self.embed(adapter,'query-'+str(i))
        adapter.cleanup()
        cached=json.loads(self.repo._one('SELECT v FROM meta WHERE k=?',('memory_embedding:'+self.life.agent_id+':query-0',))['v'])
        self.assertEqual(cached['state'],'evicted')
        restarted=self.embedding(transport)
        second=self.embed(restarted,'query-0')
        self.assertEqual(second,self.embed(restarted,'query-0'))
        self.assertEqual(len(calls),130)
        self.assertEqual(second['receipt']['generation'],1)
        self.assertEqual(self.repo._one('SELECT COUNT(*) AS n FROM spending_calls')['n'],130)

    def test_R6_unknown_expiry_and_restart_never_resubmit(self):
        calls=[]
        def transport(texts):
            calls.append(texts)
            raise TimeoutError('fake uncertainty')
        adapter=self.embedding(transport)
        self.assertEqual(self.embed(adapter,'uncertain')['state'],'unknown')
        with patch.object(self.life,'clock',return_value=self.life.clock()+7200):
            adapter.cleanup()
            self.assertEqual(self.embed(self.embedding(transport),'uncertain')['state'],'unknown')
        self.assertEqual(len(calls),1)
        self.assertEqual(self.repo._one('SELECT state FROM spending_calls')['state'],'reserved')

    def test_R6_evicted_query_requires_new_budget_admission(self):
        calls=[]
        def transport(texts):
            calls.append(texts)
            return dict(data=[dict(index=0,embedding=[1.,0.])],usage=dict(prompt_tokens=2))
        adapter=self.embedding(transport)
        self.embed(adapter,'budgeted')
        self.f.role.spending.policy=SpendingPolicy(enabled=True,daily_target_fen=0,daily_limit_fen=0)
        with patch.object(self.life,'clock',return_value=self.life.clock()+3601):
            adapter.cleanup()
            with self.assertRaisesRegex(RecallError,'embedding_budget_unavailable'):
                self.embed(adapter,'budgeted')
        self.assertEqual(len(calls),1)
        self.assertEqual(self.repo._one('SELECT COUNT(*) AS n FROM spending_calls')['n'],1)

    def test_R7_external_settlement_uses_completion_day(self):
        start=datetime(2026,10,25,23,59,59,tzinfo=ZONE).timestamp()
        spending=self.f.role.spending
        with patch('life.spending.time.time',return_value=start):
            spending.reserve_external('midnight',purpose='memory_query',maximum_nano=100,price_version='fake',source_refs=[])
        with patch('life.spending.time.time',return_value=start+2):
            spending.settle_external('midnight',50)
        self.assertEqual(spending.snapshot(start+2)['charged_nano'],50)

    def test_R7_missing_usage_keeps_reserve_and_actual_completion_through_reopen(self):
        start=datetime(2026,10,25,23,59,59,tzinfo=ZONE).timestamp()
        spending=self.f.role.spending
        with patch('life.spending.time.time',return_value=start):
            spending.reserve_external('unknown-bill',purpose='memory_query',maximum_nano=100,price_version='fake-price',source_refs=[])
        with patch('life.spending.time.time',return_value=start+2):
            spending.settle_external('unknown-bill',None)
        reopened=Spending(self.f.role)
        self.assertEqual(reopened.snapshot(start+86402)['reserved_nano'],100)
        with patch('life.spending.time.time',return_value=start+86402):
            reopened.settle_external('unknown-bill',50)
            reopened.settle_external('unknown-bill',0)
        row=self.repo._one('SELECT * FROM spending_calls WHERE call_key=?',('unknown-bill',))
        record=json.loads(row['payload'])
        self.assertEqual(row['day'],'2026-10-26')
        self.assertEqual(record['started_at'],start)
        self.assertEqual(record['finished_at'],start+2)
        self.assertEqual(record['price_version'],'fake-price')
        self.assertEqual(reopened.snapshot(start+2)['charged_nano'],50)
        self.assertEqual(reopened.snapshot(start+86402)['charged_nano'],0)

    def test_R7_cross_midnight_cost_still_blocks_next_day_hard_limit(self):
        start=datetime(2026,10,25,23,59,59,tzinfo=ZONE).timestamp()
        spending=self.f.role.spending
        spending.policy=SpendingPolicy(enabled=True,daily_target_fen=1,daily_limit_fen=1)
        with patch('life.spending.time.time',return_value=start):
            spending.reserve_external('full-budget',purpose='memory_query',maximum_nano=10_000_000,price_version='fake',source_refs=[])
        with patch('life.spending.time.time',return_value=start+2):
            spending.settle_external('full-budget',10_000_000)
            with self.assertRaisesRegex(SessionFailure,'预算不足'):
                spending.reserve_external('cannot-spend',purpose='memory_query',maximum_nano=1,price_version='fake',source_refs=[])


if __name__=='__main__':
    unittest.main()
