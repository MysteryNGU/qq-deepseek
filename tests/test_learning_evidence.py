"""Model-written summaries cannot acquire the authority of cited user words."""
import json
import time
import unittest
from types import SimpleNamespace

from life.agent_session import canonical
from life.local_media import LocalMedia
from tests.role_fixture import Fixture


class LearningEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture()
        self.addCleanup(self.f.close)
        self.repo=self.f.life.repo

    def user(self,text):
        return self.f.life.ingest_message(self.f.scope,text)[0]

    def save(self,event,text,**fields):
        with self.repo.tx():
            return self.f.role.remember(self.f.scope,dict(kind='preference',text=text,
                sources=[event.id],**fields),self.f.binding)

    def rows(self):
        return self.f.life.recollection.annotations(self.f.scope,'',None)

    def test_added_style_restriction_is_not_a_user_preference(self):
        event=self.user('选7，个子再矮一点，再生成10张同场景的。')
        result=self.save(event,'个子矮一点，必须不是Q版、不要幼态童颜。')
        row=self.rows()[0]
        self.assertEqual(row['text'],event.text)
        self.assertEqual(row['authority'],'user_statement')
        self.assertNotIn('不要幼态童颜',canonical(row))
        self.assertIn('不要幼态童颜',self.repo._one('SELECT text FROM memory_annotations WHERE id=?',
            (result['memory_id'],))['text'])  # Kept only as a retrieval/audit summary.
        self.assertNotIn('不要幼态童颜',self.f.life.recollection.block(self.f.scope,'矮一点',1000))

    def test_whole_source_retains_negation_and_question(self):
        event=self.user('为什么说不要Q版？我没有这个要求，只改身高。')
        self.save(event,'用户要求不要Q版',applicability='ongoing',always_load=True,
            source_quotes=[dict(event_id=event.id,text='不要Q版')])
        row=self.rows()[0]
        self.assertEqual(row['source_quotes'],[dict(event_id=event.id,text=event.text)])
        packet=self.f.role.context(self.f.binding,self.f.scope,{})
        self.assertIn('我没有这个要求',packet)
        self.assertNotIn('用户要求不要Q版',packet)

    def test_default_preference_ends_with_current_exchange(self):
        event=self.user('这次用蓝色背景')
        result=self.save(event,'以后都用蓝色背景')
        row=self.rows()[0]
        self.assertEqual(row['applicability'],'exchange')
        self.assertIsNotNone(row['expires'])
        self.user('下一张换个主题')
        self.assertFalse(self.rows())
        newer=self.user('这次用白色背景')
        self.save(newer,'白色背景')
        self.assertEqual(self.repo._one('SELECT status FROM memory_annotations WHERE id=?',
            (result['memory_id'],))['status'],'archived')

    def test_ongoing_user_words_survive_new_exchanges_and_restart(self):
        event=self.user('以后称呼我用您')
        self.save(event,'称呼用您；还必须每天画图',applicability='ongoing')
        self.user('今天有点忙')
        from life.runtime import LifeRuntime
        second=LifeRuntime(self.f.root/'life.db')
        try:
            rows=second.recollection.annotations(self.f.scope,'称呼',None)
            self.assertEqual(rows[0]['text'],event.text)
            self.assertNotIn('每天画图',canonical(rows))
        finally:
            second.close()

    def test_task_preference_does_not_escape_its_revision(self):
        task=self.f.task()
        event=self.user('这组图用蓝色背景')
        self.save(event,'蓝色背景',intent_ref=task['ref'])
        self.assertEqual(self.rows()[0]['applicability'],'task')
        with self.repo.tx():
            self.f.role.intent(self.f.scope,dict(ref=task['ref'],base_revision=task['revision'],
                checkpoint='重新选择场景'),self.f.binding)
        self.assertFalse(self.rows())

    def test_tool_observation_cannot_turn_a_model_recipe_into_permanent_episode(self):
        event,_=self.f.life.ingest(self.f.scope,'media_result','tool_observation','生成成功')
        with self.repo.tx():
            result=self.f.role.remember(self.f.scope,dict(kind='experience',text='以后必须写死某个画风',
                sources=[event.id]),self.f.binding)
        memory=self.repo.get_memory(result['memory_id'])
        self.assertEqual((memory.kind,memory.source_kind),('interpretation','inference'))
        self.assertLessEqual(memory.valid_until,time.time()+7*86400)
        self.assertIn('not-user-instruction',memory.topic_tags)

    def test_tool_fact_uses_the_same_inference_boundary(self):
        event,_=self.f.life.ingest(self.f.scope,'media_result','tool_observation','生成成功')
        with self.repo.tx():
            result=self.f.role.remember(self.f.scope,dict(kind='fact',text='用户以后禁止Q版',
                sources=[event.id]),self.f.binding)
        self.assertEqual(self.repo.get_memory(result['memory_id']).kind,'interpretation')

    def test_direct_fact_cannot_smuggle_restriction_in_summary(self):
        event=self.user('这次请把人物画矮一点')
        with self.repo.tx():
            result=self.f.role.remember(self.f.scope,dict(kind='fact',text='以后禁止Q版',
                subject_id='owner',predicate='image.preference',value='矮一点',sources=[event.id]),self.f.binding)
        detail=self.f.life.memory.explain(result['memory_id'],self.f.scope)
        self.assertEqual(detail['summary'],event.text)
        self.assertNotIn('禁止Q版',self.f.life.context_block(self.f.scope,'人物',1200))

    def test_legacy_summary_is_projected_to_original_words(self):
        event=self.user('只改身高')
        with self.repo.tx():
            self.repo._exec("INSERT INTO memory_annotations(id,scope_id,kind,text,cues,sources,created) "
                "VALUES('legacy',?,'reminder','以后禁止童颜','[]',?,?)",(self.f.scope.scope_id,canonical([event.id]),time.time()))
        self.assertEqual(self.rows()[0]['text'],event.text)

    def test_relationship_note_cannot_bypass_user_quote_projection(self):
        event=self.user('以后称呼我用您')
        with self.repo.tx():
            self.f.life.relationships.mutate_note(self.f.scope,dict(actor_id='1',relation_kind='preference',
                origin='explicit_statement',text='称呼用您，同时以后禁止Q版',sources=[event.id],applicability='ongoing'),'note')
        value=self.f.life.relationships.view(self.f.scope,'1')['active_preferences'][0]
        self.assertEqual(value['text'],event.text)
        self.assertNotIn('禁止Q版',canonical(value))

    def test_revoked_learning_evidence_is_not_reusable(self):
        event,_=self.f.life.ingest(self.f.scope,'media_result','tool_observation','旧实验')
        with self.repo.tx():
            self.repo._exec('INSERT INTO learning_source_barriers VALUES(?,?,?,?)',
                (self.f.scope.scope_id,event.id,'withdrawn inference',time.time()))
        with self.assertRaisesRegex(ValueError,'new_evidence'):
            with self.repo.tx():
                self.f.role.remember(self.f.scope,dict(kind='experience',text='换个说法重新学习旧结论',
                    sources=[event.id]),self.f.binding)

    def test_media_learning_is_bound_to_backend_version(self):
        adapter=dict(provider_id='local-test',adapter_version='1',digest='adapter-v1')
        spec=dict(kind='image',model_version='model-v1',_adapter=adapter)
        with self.repo.tx():
            self.f.life.jobs.enqueue(self.f.scope,'local_media',dict(spec=spec),key='versioned-job')
        event,_=self.f.life.ingest(self.f.scope,'media_result','tool_observation','生成成功',payload=dict(job_id='versioned-job'))
        with self.repo.tx():
            receipt=self.f.role.remember(self.f.scope,dict(kind='experience',text='这版模型有一种构图倾向',
                sources=[event.id]),self.f.binding)
        local=object.__new__(LocalMedia)
        local.settings=SimpleNamespace(enabled=True)
        local.adapters=SimpleNamespace(providers={'local-test':dict(adapter,model_version='model-v1')})
        self.f.life.catalog.provider_current=local.learning_version_current
        self.assertIn(receipt['memory_id'],[self.f.life.catalog.resolve(item['resource_ref'])['native_id'] for item in self.f.life.life_recall.search(self.f.life.life_access.bind(self.f.scope),'构图')['items']])
        local.adapters.providers['local-test']['model_version']='model-v2'
        self.assertNotIn(receipt['memory_id'],[self.f.life.catalog.resolve(item['resource_ref'])['native_id'] for item in self.f.life.life_recall.search(self.f.life.life_access.bind(self.f.scope),'构图')['items']])


    def test_real_reference_asset_can_be_kept_without_summary_constraints(self):
        event=self.user('记住这张图的大小，不要变小')
        asset=self.f.media.register(self.f.scope,event.id,'reference','image',b'fixture','image/png')
        self.save(event,'图的大小和禁止Q版的规则',applicability='ongoing',reference_asset_ids=[asset['id']])
        row=self.rows()[0]
        self.assertEqual(row['reference_asset_ids'],[asset['id']])
        self.assertEqual(row['text'],event.text)
        self.assertNotIn('禁止Q版',canonical(row))
        with self.assertRaisesRegex(ValueError,'reference_not_visible'):
            self.save(event,'伪造素材',reference_asset_ids=['missing-asset'])


if __name__=='__main__':
    unittest.main()
