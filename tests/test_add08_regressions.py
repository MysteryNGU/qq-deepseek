"""R8/R9 and ADD-08 acceptance: disposable databases, no paid calls or QQ."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from life.drives import DriveConfig, DriveEvidence, advance_to, apply_one_evidence, initial_state
from tests.role_fixture import Fixture


class EntryRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)

    def call(self, kind, key, proposal):
        b = self.f.binding
        return dict(kind=kind, call_id=key, proposal=proposal, caller='root',
                    session_id=b['session_id'], root_session_id=b['session_id'], generation=b['generation'])

    async def maintenance_once(self):
        role = self.f.role
        role.running = True
        async def sleep(delay):
            if delay == 5:
                role.running = False
        with patch('life.role_agent.asyncio.sleep', sleep):
            await role.memory_maintenance()

    def configure_index(self, revision='1'):
        class Adapter:
            verified = True
            def cleanup(self): pass
            async def embed(self, texts, **kwargs):
                from life.life_catalog import sha
                return dict(vectors=[[1., 0.] for _ in texts], input_digests=[sha(t) for t in texts])
        self.f.life.memory_index.configure(Adapter(), provider_revision='fake', model_id='fake', model_revision=revision, dimension=2)
        return self.f.life.memory_index

    async def test_R8_mixed_blocked_and_generation_changes(self):
        index = self.configure_index()
        repo = self.f.life.repo
        old = self.f.life.catalog.resolve(self.f.event.id)
        for n in range(101):
            e,_ = self.f.life.ingest_message(self.f.scope, 'newer', occurred_at=self.f.event.occurred_at+n+1)
            r = self.f.life.catalog.resolve(e.id)
            key = index.enqueue_resource(r['resource_id'],r['source_version'])
            with repo.tx():
                repo._exec('UPDATE work_jobs SET status=? WHERE id=?', ('paused' if n%2 else 'unknown',key))
        await self.maintenance_once()
        status = index.status(diagnostic=True)
        self.assertEqual(status['blocked'],101)
        self.assertIsNotNone(status['last_success_at'])
        with repo.tx():
            repo._exec('UPDATE events SET payload=? WHERE id=?',(json.dumps(dict(text='source revision')),self.f.event.id))
        new = self.f.life.catalog.register('event',self.f.event.id)
        self.assertGreater(new['source_version'], old['source_version'])
        await self.maintenance_once()
        # The scan wraps to catch new/updated sources ahead of its saved cursor.
        await self.maintenance_once()
        self.assertIsNotNone(repo._one('SELECT 1 FROM life_semantic_index WHERE resource_id=? AND source_version=?',(new['resource_id'],new['source_version'])))
        self.configure_index('model-2')
        await self.maintenance_once()
        self.assertIsNotNone(repo._one('SELECT 1 FROM life_semantic_index WHERE model_key=?',(index.model_key,)))
        from life.memory_index import MemoryIndex, IndexConfig
        changed = MemoryIndex(index.catalog,index.access,index.jobs,IndexConfig(chunk_tokens=128,overlap_tokens=16))
        changed.configure(index.adapter,provider_revision='fake',model_id='fake',model_revision='model-2',dimension=2)
        self.f.life.memory_index = changed
        await self.maintenance_once()
        self.assertIsNotNone(repo._one('SELECT 1 FROM life_semantic_index WHERE chunker_version=?',(changed.chunker_version,)))

    async def test_R8_denied_and_stale_front_cursor_survives_new_scanner(self):
        index = self.configure_index()
        for n in range(110):
            e,_ = self.f.life.ingest_message(self.f.scope,'denied',occurred_at=self.f.event.occurred_at+1+n)
            self.f.life.life_access.set_rule(e.id,'internal_use','*','deny')
        for n in range(10):
            e,_=self.f.life.ingest(self.f.scope,'reflection','inference','expired',occurred_at=self.f.event.occurred_at-9*86400+n)
            with self.f.life.repo.tx():
                self.f.life.repo._exec('UPDATE life_resources SET expires_at=? WHERE native_id=?',(self.f.life.clock()-1,e.id))
        await self.maintenance_once()
        self.assertEqual(index.status(diagnostic=True)['scan']['excluded_denied'],100)
        from life.memory_index import MemoryIndex
        replacement = MemoryIndex(index.catalog,index.access,index.jobs)
        replacement.configure(index.adapter,provider_revision='fake',model_id='fake',model_revision='1',dimension=2)
        self.f.life.memory_index=replacement
        await self.maintenance_once()
        old=self.f.life.catalog.resolve(self.f.event.id)
        self.assertIsNotNone(self.f.life.repo._one('SELECT 1 FROM life_semantic_index WHERE resource_id=?',(old['resource_id'],)))
        self.assertGreater(replacement.status(diagnostic=True)['scan']['excluded_stale'],0)

    async def test_R8_blocked_window_does_not_starve_old_source(self):
        f = self.f
        index = f.life.memory_index
        class Adapter:
            verified = True
            def cleanup(self): pass
            async def embed(self, texts, **kwargs):
                from life.life_catalog import sha
                return dict(vectors=[[1., 0.] for _ in texts], input_digests=[sha(t) for t in texts])
        index.configure(Adapter(), provider_revision='fake', model_id='fake', model_revision='1', dimension=2)
        for status in ('unknown', 'paused'):
            with self.subTest(status=status):
                # Each newer job is deliberately protected from replay.
                for n in range(100):
                    e, _ = f.life.ingest_message(f.scope, 'blocked', occurred_at=f.event.occurred_at+1+n)
                    r = f.life.catalog.resolve(e.id)
                    key = index.enqueue_resource(r['resource_id'], r['source_version'])
                    with f.life.repo.tx():
                        f.life.repo._exec('UPDATE work_jobs SET status=? WHERE id=?', (status, key))
                await self.maintenance_once()
                old = f.life.catalog.resolve(f.event.id)
                self.assertIsNotNone(f.life.repo._one('SELECT 1 FROM life_semantic_index WHERE resource_id=?', (old['resource_id'],)))
                self.assertEqual(f.life.repo._one("SELECT SUM(attempts) AS n FROM work_jobs WHERE status IN ('unknown','paused')")['n'], 0)

    async def test_R9_invalid_shape_is_correctable_before_call_is_frozen(self):
        f = self.f
        for kind in ('life.appraise', 'life.say'):
            for i, value in enumerate(({'bad': True}, 'bad', 3, [False], [dict(dimension='libido', delta=float('nan'))])):
                with self.subTest(kind=kind, value=value):
                    key = kind+str(i)
                    app = dict(sources=[f.event.id], source_event_ids=[f.event.id], drive_evidence=value)
                    proposal = app if kind=='life.appraise' else dict(text='reply', sources=[f.event.id], appraisal=app)
                    result = await f.role.callback(self.call(kind, key, proposal))
                    self.assertFalse(result['ok'])
                    self.assertIn('drive_evidence', result['message'])
                    self.assertIsNone(f.life.repo._one('SELECT 1 FROM input_resolutions WHERE call_id=?', (key,)))

    async def test_R9_corrected_embedded_appraisal_resumes_and_confirmed_send_is_idempotent(self):
        f=self.f
        f.role.sessions.enqueue(f.binding,f.event)
        with f.life.repo.tx():
            f.life.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=1 WHERE input_id=?",(f.event.id,))
        async def confirmed(action,params):
            f.sent.append(params)
            return dict(retcode=0,data=dict(message_id=42))
        f.expressions.transport=confirmed
        app=dict(sources=[f.event.id],drive_evidence={'bad':True})
        p=dict(text='已经理解',sources=[f.event.id],handled_input_ids=[f.event.id],appraisal=app)
        bad=await f.role.callback(self.call('life.say','correctable',p))
        self.assertEqual(bad['field'],'appraisal.drive_evidence')
        self.assertEqual(bad['input_processing'],'not_committed')
        self.assertEqual(len(f.role.sessions.pending(f.binding['session_id'])),1)
        app['drive_evidence']=[dict(dimension='libido',kind='activation_change',delta=.1,confidence=0)]
        good=await f.role.callback(self.call('life.say','correctable',p))
        self.assertTrue(good['sent_confirmed'])
        self.assertTrue(good['appraisal']['applied'])
        self.assertAlmostEqual(f.life.drive_state(f.scope).drives['libido'].activation,0)
        self.assertEqual(await f.role.callback(self.call('life.say','correctable',p)),good)
        again=await f.role.callback(self.call('life.say','new-id',p))
        self.assertTrue(again['sent_confirmed'])
        self.assertEqual(len(f.sent),1)

    async def test_R9_missing_empty_and_legal_evidence_standalone_and_embedded(self):
        async def confirmed(*args):
            return dict(sent_confirmed=True)
        self.f.role.send=confirmed
        for kind in ('life.appraise','life.say'):
            for i,fields in enumerate(({},dict(drive_evidence=[]),dict(drive_evidence=[
                    dict(dimension='exploration',kind='activation_change',delta=.1,confidence=.5)]))):
                with self.subTest(kind=kind,fields=fields):
                    source,_=self.f.life.ingest_message(self.f.scope,'合法评价独立来源 '+kind+str(i))
                    app=dict(sources=[source.id],**fields)
                    proposal=app if kind=='life.appraise' else dict(text='有效回复',sources=[source.id],appraisal=app)
                    result=await self.f.role.callback(self.call(kind,kind+'-legal-'+str(i),proposal))
                    self.assertTrue(result['ok'],result)
                    if kind=='life.say':
                        self.assertTrue(result['sent_confirmed'])
                        self.assertIn('appraisal',result)

    async def test_R9_other_model_field_types_return_correctable_field_errors(self):
        fields=dict(certainty='high',sources='not-an-array',revision=1.5,negated='false',
            affect_impulse=[],recovery_half_life_seconds={'joy':0},relation_impacts=[False],
            relation_context_changes=[dict(confidence=float('inf'))],affected_need_ids=[3])
        for kind in ('life.appraise','life.say'):
            for field,value in fields.items():
                with self.subTest(kind=kind,field=field):
                    key=kind+'-bad-'+field
                    app=dict(sources=[self.f.event.id],**{field:value}) if field!='sources' else {field:value}
                    p=app if kind=='life.appraise' else dict(text='回复',sources=[self.f.event.id],appraisal=app)
                    result=await self.f.role.callback(self.call(kind,key,p))
                    self.assertFalse(result['ok'],result)
                    self.assertIn(field,result.get('field',''))
                    self.assertIsNone(self.f.life.repo._one('SELECT 1 FROM input_resolutions WHERE call_id=?',(key,)))

    async def test_R9_late_failure_reports_committed_understanding_and_new_call_recovery(self):
        f=self.f
        f.role.sessions.enqueue(f.binding,f.event)
        with f.life.repo.tx():
            f.life.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=1 WHERE input_id=?",(f.event.id,))
        bad=await f.role.callback(self.call('life.say','empty-body',dict(text='',handled_input_ids=[f.event.id])))
        self.assertEqual(bad['input_processing'],'committed')
        self.assertEqual(bad['retry_with'],'new_call_id')
        self.assertFalse(f.sent)
        async def confirmed(*args):
            return dict(sent_confirmed=True)
        f.role.send=confirmed
        good=await f.role.callback(self.call('life.say','fixed-body',dict(text='修正后的表达',sources=[f.event.id])))
        self.assertTrue(good['sent_confirmed'])

    def test_S4_no_second_decay(self):
        cfg = DriveConfig()
        state = initial_state('test', cfg, at=100.)
        state.drives['libido'].stimulation = .4
        state.drives['libido'].stimulation_at = 100.
        state = advance_to(state, 1900., cfg)
        self.assertAlmostEqual(state.drives['libido'].stimulation, .2)
        ev = DriveEvidence(id='zero', root_id='zero', dimension='libido', kind='stimulation_change', delta=0)
        state, _ = apply_one_evidence(state, ev, cfg, now=1900.)
        self.assertAlmostEqual(state.drives['libido'].stimulation, .2)

    async def test_ADD08_protocol_is_exposed(self):
        self.assertTrue(hasattr(self.f.life, 'experience'))


if __name__ == '__main__':
    unittest.main()
