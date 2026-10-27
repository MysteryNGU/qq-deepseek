"""Synthetic, isolated integration tests for design T01/T03/T05/T19/T21-23."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import struct
import tempfile
import unittest

from life.contracts import Scope
from life.life_access import RecallError
from life.life_catalog import encode, sha
from life.memory_index import IndexConfig, chunks, vector_blob
from life.runtime import LifeRuntime
from life.text import terms


class Embedding:
    def __init__(self):
        self.calls = []
        self.before_return = None
        self.invalid = False

    async def embed(self, texts, **options):
        self.calls.append(options)
        if self.before_return:
            self.before_return()
        vectors = [[1., 0., 0.] if any(w in t for w in ('电影', '影片')) else [0., 1., 0.] for t in texts]
        if self.invalid:
            vectors[0] = [float('nan'), 0., 0.]
        return dict(vectors=vectors, input_digests=[sha(t) for t in texts],
                    actual_usage=None, receipt='fake-receipt')


class LifeMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.life = LifeRuntime(Path(self.temp.name)/'life.db', agent_id='fish')
        self.a = self.life.scope('qq:private:1', '1')
        self.b = self.life.scope('qq:private:2', '2')
        self.access, self.catalog = self.life.life_access, self.life.catalog
        self.internal = lambda: self.access.bind(self.a, mode='internal')
        self.recall = self.life.life_recall

    def tearDown(self):
        self.life.close()
        self.temp.cleanup()

    def event(self, scope=None, text='甲推荐电影X', external_id=None):
        event, _ = self.life.ingest_message(scope or self.a, text, external_id=external_id)
        return event, self.catalog.resolve(event.id)

    def model(self):
        adapter = Embedding()
        self.life.memory_index.configure(adapter, provider_revision='fake-v1', model_id='synthetic',
                                          model_revision='1', dimension=3)
        return adapter

    def test_index_retirement_requires_verified_complete_replacement_and_grace(self):
        event,resource=self.event();self.model();self.build(resource)
        index=self.life.memory_index;old_key=index.model_key
        adapter=Embedding();adapter.verified=True
        index.configure(adapter,provider_revision='next',model_id='next',model_revision='2',dimension=3)
        self.assertEqual(index.retire_replaced_generations(),0)
        self.build(resource)
        self.assertEqual(index.retire_replaced_generations(),0)
        with self.life.repo.tx():self.life.repo._exec('UPDATE life_semantic_index SET updated_at=0 WHERE model_key=?',(index.model_key,))
        adapter.verified=False;self.assertEqual(index.retire_replaced_generations(),0)
        adapter.verified=True;self.assertGreater(index.retire_replaced_generations(),0)
        self.assertFalse(self.life.repo._one('SELECT 1 FROM life_semantic_index WHERE model_key=?',(old_key,)))
        self.assertIsNotNone(self.life.repo.get_event(event.id));self.assertEqual(index.retire_replaced_generations(),0)

    def build(self, resource):
        index = self.life.memory_index
        key = index.enqueue_resource(resource['resource_id'], resource['source_version'])
        lease = self.life.jobs.claim(key)
        asyncio.run(index.build(lease))
        return key, lease

    def test_raw_events_precede_model_and_external_id_is_only_dedup(self):
        event, resource = self.event(external_id='platform-1')
        duplicate, _ = self.event(external_id='platform-1')
        other, _ = self.event(external_id='platform-2')
        self.assertEqual(event.id, duplicate.id)
        self.assertNotEqual(event.id, other.id)
        result = self.recall.search(self.internal(), '电影')
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(result['coverage']['semantic'], 'unavailable')
        self.assertTrue(self.catalog.live(resource))

    def test_internal_unifies_but_audience_does_not_read_archive(self):
        event, resource = self.event()
        internal = self.access.bind(self.b, mode='internal')
        self.assertEqual(len(self.recall.search(internal, '电影')['items']), 1)
        ctx = self.access.bind(self.b)
        self.assertEqual(self.recall.search(ctx, '电影')['items'], [])
        with self.assertRaisesRegex(RecallError, 'not_available'):
            self.recall.read(ctx, event.id)
        with self.assertRaises(RecallError):
            self.recall.search(replace(internal, _seal=object()), '电影')

    def test_cross_bot_and_cycles_rejected(self):
        one, first = self.event()
        two, second = self.event(text='另一个经历')
        with self.life.repo.tx():
            self.catalog.add_edge(first['resource_id'], second['resource_id'], second['source_version'])
            with self.assertRaisesRegex(RecallError, 'source_cycle'):
                self.catalog.add_edge(second['resource_id'], first['resource_id'], first['source_version'])
        other = LifeRuntime(Path(self.temp.name)/'other.db', agent_id='other')
        try:
            ctx = other.life_access.bind(other.scope('qq:private:1'))
            with self.assertRaises(RecallError):
                self.recall.read(ctx, one.id)
        finally:
            other.close()

    def test_cjk_punctuation_does_not_manufacture_terms(self):
        self.assertNotIn('甲乙', terms('甲，乙'))
        self.assertIn('电影', terms('推荐电影。今天'))

    def test_vector_semantics_and_model_generations(self):
        _, resource = self.event(text='这部电影有意思')
        self.model()
        self.build(resource)
        ctx = self.internal()
        result = asyncio.run(self.recall.search_async(ctx, '影片', mode='semantic'))
        self.assertEqual(result['items'][0]['resource_ref'], resource['resource_id'])
        self.assertEqual(result['coverage']['semantic'], 'ready')
        self.life.memory_index.configure(Embedding(), provider_revision='fake-v2', model_id='new', model_revision='2', dimension=3)
        result = asyncio.run(self.recall.search_async(ctx, '影片', mode='semantic'))
        self.assertEqual(result['items'], [])
        self.assertEqual(result['coverage']['semantic'], 'partial')

    def test_bad_vector_and_delete_during_embedding_never_commit(self):
        _, resource = self.event()
        adapter = self.model()
        adapter.invalid = True
        with self.assertRaisesRegex(RecallError, 'invalid_embedding'):
            self.build(resource)
        self.assertEqual(self.life.repo._one('SELECT COUNT(*) AS n FROM life_semantic_index')['n'], 0)
        adapter.invalid = False
        _, fresh = self.event(text='电影的新经历')
        adapter.before_return = lambda: self.life.repo.redact_events([fresh['native_id']])
        with self.assertRaises(RecallError):
            self.build(fresh)
        self.assertIsNone(self.life.repo._one('SELECT 1 FROM life_semantic_index WHERE resource_id=?', (fresh['resource_id'],)))

    def test_vectors_reject_zero_nan_inf_and_wrong_dimension(self):
        for vector in ([0, 0, 0], [float('inf'), 0, 0], [float('nan'), 1, 0], [1, 2]):
            with self.assertRaises(RecallError):
                vector_blob(vector, 3)
        values = struct.unpack('<fff', vector_blob([1, 2, 3], 3))
        self.assertAlmostEqual(sum(v*v for v in values), 1, places=6)

    def test_utf8_chunk_boundaries_and_source_reconstruction(self):
        text = '电影。今天没有去电影院！\n'*30
        config = IndexConfig(chunk_tokens=32, overlap_tokens=4)
        raw = text.encode('utf-8')
        parts = list(chunks(text, config))
        for start, end, body in parts:
            self.assertEqual(raw[start:end].decode('utf-8'), body)
        self.assertEqual(parts[0][0], 0)
        self.assertEqual(parts[-1][1], len(raw))

    def test_associations_keep_mentions_and_participants_distinct(self):
        _, resource = self.event(text='甲提到乙推荐电影X')
        # Actual speaker is 1; mentioned actor 2 is not a participant.
        body = self.catalog.body(resource).encode('utf-8')
        start = body.index('乙'.encode('utf-8'))
        self.life.memory_index.add_mention(resource, 'actor:qq:2', 'actor', 'mentions', 'stated', (start, start+3))
        with self.assertRaises(RecallError):
            self.life.memory_index.add_mention(resource, 'actor:qq:2', 'actor', 'participant', 'observed')
        found = self.recall.search(self.internal(), '', mode='related', entity_refs=['actor:qq:2'])
        self.assertEqual(found['items'][0]['resource_ref'], resource['resource_id'])
        audience = self.access.bind(self.b)
        self.assertEqual(self.recall.search(audience, '', mode='related', entity_refs=['actor:qq:2'])['items'], [])

    def test_read_cursor_utf8_scope_and_mutation_fences(self):
        event, resource = self.event(text='长文没有完成任务。'*200)
        ctx = self.internal()
        page = self.recall.read(ctx, event.id, max_tokens=800, max_bytes=1800)
        self.assertTrue(page['has_more'])
        raw = self.catalog.body(resource).encode('utf-8')
        self.assertEqual(page['text'].encode('utf-8'), raw[:page['byte_end']])
        self.assertLessEqual(len(encode(page).encode('utf-8')),1800)
        second = self.recall.read(ctx, event.id, cursor=page['next_cursor'], max_tokens=800, max_bytes=1800)
        self.assertEqual(second['byte_start'], page['byte_end'])
        with self.assertRaises(RecallError):
            self.recall.read(self.access.bind(self.b), event.id, cursor=page['next_cursor'])
        self.life.repo.redact_events([event.id])
        with self.assertRaises(RecallError):
            self.recall.read(ctx, event.id, cursor=page['next_cursor'])

    def test_recall_paging_excludes_new_events_and_rejects_tampering(self):
        self.event(text='电影甲'); self.event(text='电影乙')
        ctx = self.internal()
        page = self.recall.search(ctx, '电影', limit=1)
        self.event(text='电影丙')
        second = self.recall.search(ctx, '电影', limit=1, cursor=page['next_cursor'])
        self.assertNotEqual(page['items'][0]['resource_ref'], second['items'][0]['resource_ref'])
        self.assertFalse(second['has_more'])
        with self.assertRaisesRegex(RecallError, 'stale_cursor'):
            self.recall.search(ctx, '电影', cursor=page['next_cursor'][:-4]+'AAAA')

    def test_summary_requires_all_pages_and_source_versions(self):
        _, first = self.event(); _, second = self.event(self.b, '后来实际读到电影介绍')
        organizer = self.life.memory_organize
        ctx = self.internal()
        packet = organizer.prepare(ctx, [first['resource_id'], second['resource_id']], topic_ref='topic-1')
        lease = self.life.jobs.claim(packet['job_ref'])
        proposal = dict(coverage_digest=packet['coverage_digest'], claims=[
            dict(text='甲推荐作品', kind='reported', evidence_refs=[first['resource_id']]),
            dict(text='后来读到资料', kind='observed', evidence_refs=[second['resource_id']])])
        with self.assertRaisesRegex(RecallError, 'incomplete_coverage'):
            organizer.commit(ctx, lease, proposal, 'call-1')
        organizer.read_batch(ctx, lease, limit=1)
        with self.assertRaises(RecallError):
            organizer.commit(ctx, lease, proposal, 'call-1')
        organizer.read_batch(ctx, lease, after=sorted(packet['sources'])[0])
        result = organizer.commit(ctx, lease, proposal, 'call-1')
        self.assertEqual(organizer.commit(ctx, lease, proposal, 'call-1'), result)
        self.assertTrue(result['complete_for_range'])
        with self.assertRaises(RecallError):
            self.recall.read(self.access.bind(self.a), result['resource_ref'])
        self.life.repo.redact_events([first['native_id']])
        self.assertEqual(self.life.repo.get_memory(result['memory_id']).summary, '')
        self.assertIsNone(self.life.repo._one('SELECT 1 FROM meta WHERE k=?', ('memory_summary:'+result['memory_id'],)))
        with self.assertRaises(RecallError):
            self.recall.read(self.internal(), result['resource_ref'])

    def test_ordinary_span_shares_without_exposing_secret_or_archive(self):
        event, resource = self.event(text='推荐电影X。我的秘密不能告诉别人。')
        body = self.catalog.body(resource).encode('utf-8')
        start = body.index('推荐'.encode('utf-8'))
        end = start+len('推荐电影X。'.encode('utf-8'))
        shared = self.life.memory_organize.prepare_share(self.internal(), resource['resource_id'], [(start, end)],
            evaluator=lambda **kw: 'ordinary' if kw['selected'] == ('推荐电影X。',) else 'restricted')
        ctx = self.access.bind(self.b)
        packet = self.recall.search(ctx, '电影')
        self.assertEqual(len(packet['items']), 1)
        self.assertNotIn('秘密', encode(packet))
        self.assertNotIn(resource['resource_id'], encode(packet))
        self.assertEqual(packet['items'][0]['resource_ref'], shared)
        with self.assertRaises(RecallError):
            self.recall.read(ctx, event.id)
        self.life.repo.redact_events([event.id])
        self.assertEqual(self.recall.search(self.access.bind(self.b), '电影')['items'], [])

    def test_deny_revokes_old_binding_and_old_cursor(self):
        event, resource = self.event()
        memory=self.life.memory.remember_episode(self.a,'电影推荐的旧概括',[event.id])
        self.life.recollection.annotate(self.a,'reminder','电影推荐的旧叮嘱',[event.id])
        ctx = self.internal()
        self.access.set_rule(resource['resource_id'], 'internal_use', '*', 'deny')
        with self.assertRaises(RecallError):
            self.recall.read(ctx, event.id)
        self.assertEqual(self.recall.search(self.internal(), '电影')['items'], [])
        self.assertNotIn('旧概括',self.life.context_block(self.a,'电影',2400))
        self.assertNotIn('旧叮嘱',self.life.context_block(self.a,'电影',2400))
        self.assertEqual(self.life.memory.explain(memory.id,self.a)['error'],'access_denied')

    def test_delete_erases_vectors_and_restart_cannot_rebuild(self):
        event, resource = self.event()
        self.model(); self.build(resource)
        self.life.repo.redact_events([event.id])
        self.assertEqual(self.life.repo._one('SELECT COUNT(*) AS n FROM life_semantic_index')['n'], 0)
        self.life.close()
        self.life = LifeRuntime(Path(self.temp.name)/'life.db', agent_id='fish')
        ctx = self.life.life_access.bind(self.a, mode='internal')
        self.life.catalog.backfill(limit=100)
        self.assertEqual(self.life.life_recall.search(ctx, '电影')['items'], [])

    def test_long_source_returns_reference_instead_of_permanent_empty_page(self):
        _,resource=self.event(text='尚未完成，不可声称完成。'*1000)
        result=self.recall.search(self.internal(),'完成',budget=1200)
        self.assertEqual(result['items'][0]['read_ref'],resource['resource_id'])
        self.assertFalse(result['items'][0]['preview_complete'])
        self.assertIsNone(result['items'][0]['preview'])

    def test_summary_long_source_requires_all_utf8_pages(self):
        _,resource=self.event(text='没有完成这件事。'*1500)
        ctx=self.internal()
        organizer=self.life.memory_organize
        packet=organizer.prepare(ctx,[resource['resource_id']])
        lease=self.life.jobs.claim(packet['job_ref'])
        proposal=dict(coverage_digest=packet['coverage_digest'],claims=[dict(text='事情尚未完成',kind='reported',evidence_refs=[resource['resource_id']])])
        page=organizer.read_batch(ctx,lease,max_tokens=2000)
        self.assertTrue(page['has_more'])
        with self.assertRaisesRegex(RecallError,'incomplete_coverage'):
            organizer.commit(ctx,lease,proposal,'long-summary')
        count=1
        while page['has_more']:
            page=organizer.read_batch(ctx,lease,cursor=page['next_cursor'],max_tokens=2000)
            count+=1
            self.assertLess(count,100)
        self.assertTrue(organizer.commit(ctx,lease,proposal,'long-summary')['complete_for_range'])

    def test_shared_fragment_keeps_actual_occurrence_time(self):
        _,resource=self.event()
        body=self.catalog.body(resource).encode('utf-8')
        start=body.index('甲推荐'.encode('utf-8'))
        shared=self.life.memory_organize.prepare_share(self.internal(),resource['resource_id'],[(start,len(body))],evaluator=lambda **kw:'ordinary')
        packet=self.recall.search(self.access.bind(self.b),'电影')
        self.assertEqual(packet['items'][0]['occurred_at'],resource['occurred_at'])

    def test_cross_bot_source_cannot_enter_deletion_graph(self):
        event,_=self.event()
        with self.assertRaisesRegex(ValueError,'cross_agent_source'):
            self.life.ingest(Scope('other','qq:private:9'),'observation','tool_observation','非法来源',source_event_ids=[event.id])

    def test_deny_on_source_revokes_previously_shared_span(self):
        _,resource=self.event()
        body=self.catalog.body(resource).encode('utf-8')
        start=body.index('甲推荐'.encode('utf-8'))
        self.life.memory_organize.prepare_share(self.internal(),resource['resource_id'],[(start,len(body))],evaluator=lambda **kw:'ordinary')
        ctx=self.access.bind(self.b)
        self.assertTrue(self.recall.search(ctx,'电影')['items'])
        self.access.set_rule(resource['resource_id'],'disclose',ctx.audience_key,'deny')
        self.assertFalse(self.recall.search(self.access.bind(self.b),'电影')['items'])

    def test_source_correction_invalidates_legacy_reader_descendants_too(self):
        event,_=self.event()
        parent=self.life.memory.remember(self.a,'fact','电影是X',[event.id])
        child=self.life.memory.remember(self.a,'episode','基于旧事实的概括',[event.id],derived_from=[parent.id])
        self.life.repo.update_memory(parent.id,summary='纠正：实际是电影Y',version=parent.version+1)
        self.assertEqual(self.life.repo.get_memory(child.id).status,'superseded')
        self.assertNotIn('基于旧事实的概括',self.life.context_block(self.a,'旧事实',2400))


if __name__ == '__main__':
    unittest.main()
