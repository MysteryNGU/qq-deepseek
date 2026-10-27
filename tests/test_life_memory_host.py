"""Verify the running host's two context paths and ordinary read tools."""
from unittest.mock import patch
import unittest

from life.agent_session import SessionFailure,canonical
from tests.role_fixture import Fixture


class MemoryHostTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture()
        self.life,self.role,self.scope=self.f.life,self.f.role,self.f.scope

    def tearDown(self):
        self.f.close()

    def share(self):
        other=self.life.scope('qq:private:2','2')
        event,_=self.life.ingest_message(other,'推荐电影X。我的秘密不能转述。')
        resource=self.life.catalog.resolve(event.id)
        body=self.life.catalog.body(resource).encode('utf-8')
        start=body.index('推荐'.encode('utf-8'))
        ctx=self.life.life_access.bind(other,mode='internal')
        shared=self.life.memory_organize.prepare_share(ctx,resource['resource_id'],
            [(start,start+len('推荐电影X。'.encode('utf-8')))],evaluator=lambda **kw:'ordinary')
        return event,shared

    def test_normal_context_recalls_share_and_never_its_private_ancestor(self):
        event,shared=self.share()
        text=self.life.context_block(self.scope,'电影X',2400)
        self.assertIn('推荐电影X',text)
        self.assertNotIn('秘密',text)
        self.assertNotIn(event.id,text)
        self.assertIn(shared,text)

    def test_continuity_context_uses_same_shared_material_and_budget(self):
        event,shared=self.share()
        self.f.cfg.continuity_enabled=True
        self.role.continuity_protocol=1
        latest,_=self.life.ingest_message(self.scope,'聊聊电影X')
        self.role.stage_input(latest)
        with patch.object(self.life.life_recall,'context',wraps=self.life.life_recall.context) as extend:
            context,text=self.role.continuity.context(self.scope,self.f.binding,{})
        self.assertTrue(extend.called)
        self.assertIn('推荐电影X',text)
        self.assertNotIn('秘密',text+canonical(context))
        self.assertLessEqual(context['optional_estimated_tokens'],self.role.resources.memory_context_target_tokens)

    def test_tools_do_not_accept_internal_mode_or_arbitrary_bot(self):
        event,shared=self.share()
        result=self.role.read(self.scope,dict(kind='recall',query='电影',mode='text',agent_id='other'))
        self.assertIn(shared,canonical(result))
        self.assertNotIn('秘密',canonical(result))
        with self.assertRaises(SessionFailure):
            self.role.read(self.scope,dict(kind='life_source',ref=event.id,mode='internal'))


if __name__=='__main__':
    unittest.main()
