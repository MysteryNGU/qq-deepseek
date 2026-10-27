"""No network/model calls: admission, idempotency, unknown and deletion fences."""
import asyncio
from dataclasses import replace
import json
import unittest

from life.embedding import EmbeddingConfig, HttpEmbedding
from life.life_access import RecallError
from life.spending import SpendingPolicy
from tests.role_fixture import Fixture


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.role, self.life = self.f.role, self.f.life
        self.config = EmbeddingConfig.parse(dict(enabled=True,endpoint='http://127.0.0.1:9000/embeddings',
            model_id='fake',model_revision='1',provider_revision='local-v1',dimension=3,
            allow_external_text=True,price_version='test-price-v1',input_nano_per_token=2))
        self.calls = 0

    def tearDown(self):
        self.f.close()

    def transport(self, texts):
        self.calls += 1
        return dict(data=[dict(index=i,embedding=[1.,2.,3.]) for i in range(len(texts))],usage=dict(prompt_tokens=5,total_tokens=5))

    def run_call(self, adapter, key='call-1', refs=()):
        return asyncio.run(adapter.embed(['电影'], model_key='model-1', purpose='memory_index', call_key=key, source_refs=list(refs)))

    def test_valid_config_requires_scope_price_and_real_model_identity(self):
        self.assertFalse(EmbeddingConfig.parse().enabled)
        for change in (dict(allow_external_text=False),dict(input_nano_per_token=None),dict(dimension=0),
                       dict(endpoint='http://external.example/embeddings'),dict(model_revision='')):
            with self.assertRaises(ValueError):
                EmbeddingConfig.parse(dict(self.config.__dict__,**change))

    def test_repeated_call_reuses_result_and_single_bill(self):
        adapter = HttpEmbedding(self.life,self.role.spending,self.config,transport=self.transport)
        first = self.run_call(adapter)
        second = self.run_call(adapter)
        self.assertEqual(first,second)
        self.assertEqual(self.calls,1)
        row = self.life.repo._one('SELECT * FROM spending_calls WHERE call_key=?',('call-1',))
        self.assertEqual(row['state'],'settled')
        self.assertEqual(row['charged'],10)
        self.assertIsNone(first.get('price_estimate'))

    def test_unknown_is_not_retried_and_reservation_survives(self):
        def failure(texts):
            self.calls += 1
            raise TimeoutError('secret input must never be recorded')
        adapter = HttpEmbedding(self.life,self.role.spending,self.config,transport=failure)
        self.assertEqual(self.run_call(adapter)['state'],'unknown')
        self.assertEqual(self.run_call(adapter)['state'],'unknown')
        self.assertEqual(self.calls,1)
        row = self.life.repo._one('SELECT * FROM spending_calls WHERE call_key=?',('call-1',))
        self.assertEqual(row['state'],'reserved')
        self.assertGreater(row['reserved'],0)
        value = self.life.repo._one("SELECT v FROM meta WHERE k LIKE 'memory_embedding:%'")['v']
        self.assertNotIn('secret input',value)

    def test_hard_zero_limit_blocks_io_but_preserves_raw_event(self):
        self.role.spending.policy = SpendingPolicy(enabled=True,daily_target_fen=0,daily_limit_fen=0)
        adapter = HttpEmbedding(self.life,self.role.spending,self.config,transport=self.transport)
        with self.assertRaisesRegex(RecallError,'embedding_budget_unavailable'):
            self.run_call(adapter)
        self.assertEqual(self.calls,0)
        self.assertIsNotNone(self.life.repo.get_event(self.f.event.id))
        self.assertIsNone(self.life.repo._one('SELECT 1 FROM spending_calls'))

    def test_unknown_usage_remains_reserved(self):
        def missing(texts):
            data = self.transport(texts)
            data.pop('usage')
            return data
        adapter = HttpEmbedding(self.life,self.role.spending,self.config,transport=missing)
        result = self.run_call(adapter)
        self.assertIsNone(result['actual_usage'])
        self.assertEqual(self.life.repo._one('SELECT state FROM spending_calls')['state'],'reserved')

    def test_forget_during_http_adoption_keeps_cost_but_erases_vector_cache(self):
        ref = self.life.catalog.resolve(self.f.event.id)['resource_id']
        def forget(texts):
            self.life.repo.redact_events([self.f.event.id])
            return self.transport(texts)
        adapter = HttpEmbedding(self.life,self.role.spending,self.config,transport=forget)
        with self.assertRaisesRegex(RecallError,'stale_source'):
            self.run_call(adapter,refs=[ref])
        self.assertEqual(self.life.repo._one('SELECT state FROM spending_calls')['state'],'settled')
        for row in self.life.repo._all("SELECT v FROM meta WHERE k LIKE 'memory_embedding:%'"):
            self.assertNotIn('result',json.loads(row['v']))

    def test_invalid_provider_order_is_never_indexed(self):
        def invalid(texts):
            return dict(data=[dict(index=1,embedding=[1,2,3])],usage=dict(prompt_tokens=5))
        adapter = HttpEmbedding(self.life,self.role.spending,self.config,transport=invalid)
        with self.assertRaisesRegex(RecallError,'invalid_embedding'):
            self.run_call(adapter)


if __name__=='__main__':
    unittest.main()
