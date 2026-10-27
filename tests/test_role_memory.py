import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from life.agent_session import SessionFailure
from life.role_agent import RoleAgent
from life.runtime import LifeRuntime


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.life = LifeRuntime(Path(self.tmp.name)/'life.db')
        self.scope = self.life.scope('qq:private:1','1')
        self.other = self.life.scope('qq:group:2','1')
        self.role = RoleAgent(self.life,SimpleNamespace(),SimpleNamespace(owner=1),self.tmp.name,None)
        self.root = self.role.sessions.binding(self.scope)

    def tearDown(self):
        self.life.close()
        self.tmp.cleanup()

    def save(self,proposal):
        with self.life.repo.tx():
            return self.role.remember(self.scope,proposal,self.root)

    def event(self,text,source='user_report',scope=None):
        return self.life.ingest(scope or self.scope,'message',source,text)[0]

    def test_language_correction_supersedes_reminder_and_is_retrieved(self):
        first = self.event('以后用简短语气')
        old = self.save(dict(kind='preference',text='回复要简短',sources=[first.id]))
        later = self.event('解释复杂事情时多讲清楚，不必简短')
        self.save(dict(kind='preference',text='复杂事情讲清楚，可适当展开',sources=[later.id],
                       supersedes=[old['memory_id']],cues=['复杂','解释'],skill_refs=['explain.md']))
        block = self.life.context_block(self.scope,'解释复杂问题',2000)
        self.assertIn(later.text,block)
        self.assertNotIn('复杂事情讲清楚，可适当展开',block)
        self.assertNotIn('回复要简短',block)
        self.assertIn('explain.md',block)
        self.assertNotIn(later.text,self.life.context_block(self.other,'解释复杂问题',2000))

    def test_imagination_cannot_become_external_fact(self):
        imagined = self.event('想象了一座房子','inference')
        with self.assertRaises(SessionFailure):
            self.save(dict(kind='fact',text='有一座房子',subject_id='self',predicate='home',value='house',sources=[imagined.id]))
        receipt = self.save(dict(kind='imagination',text='我想象自己住在海边',sources=[imagined.id]))
        self.assertEqual(self.life.repo.memories_by_ids([receipt['memory_id']])[0].kind,'interpretation')

    def test_foreign_fact_candidate_cannot_supersede_legal_fact(self):
        first = self.event('我住在上海')
        old = self.save(dict(kind='fact',text='用户住在上海',subject_id='1',predicate='city',value='上海',sources=[first.id]))
        foreign = self.event('wrong place',scope=self.other)
        with self.assertRaises(SessionFailure):
            self.save(dict(kind='fact',text='别处',subject_id='1',predicate='city',value='elsewhere',sources=[foreign.id],correction=True))
        self.assertEqual(self.life.repo.memories_by_ids([old['memory_id']])[0].status,'active')


if __name__=='__main__':
    unittest.main()
