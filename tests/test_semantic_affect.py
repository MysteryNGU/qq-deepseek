import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from life.contracts import Appraisal
from life.runtime import LifeRuntime
from life.agent_session import SessionFailure


class SemanticAffectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 100000.
        self.life = LifeRuntime(Path(self.tmp.name)/'life.db',clock=lambda:self.now)
        self.a = self.life.scope('qq:private:1','1')
        self.b = self.life.scope('qq:group:3','2')

    def tearDown(self):
        self.life.close()
        self.tmp.cleanup()

    def stimulate(self,scope,delta,kind='user_report',app_id=None,**kwargs):
        event,_ = self.life.ingest(scope,'message',kind,'semantic evidence')
        app = Appraisal(id=app_id or event.id+'-app',scope=scope,source_event_ids=[event.id],
                        proposed_affect_delta=delta,certainty=1,**kwargs)
        self.life.appraise(scope,event,app)
        return event,app

    def test_no_heuristic_claim_and_one_stimulus(self):
        event,_ = self.life.ingest_message(self.a,'bad words do not decide meaning')
        self.assertIsNone(self.life.appraise(self.a,event))
        self.assertFalse(self.life.affect_summary(self.a))
        app = Appraisal(id='semantic',scope=self.a,source_event_ids=[event.id],
                        proposed_affect_delta={'joy':.6},certainty=1,negated=True,quoted_or_hypothetical=True)
        self.life.appraise(self.a,event,app)
        first = self.life.affect_summary(self.a)['intensity']['joy']
        self.assertGreater(first,.75)
        self.assertEqual(self.life.appraise(self.a,event,app).receipt['status'],'duplicate')
        self.assertEqual(first,self.life.affect_summary(self.a)['intensity']['joy'])

    def test_shared_state_private_causes_and_real_actor_relations(self):
        self.stimulate(self.a,{'hurt':.4},relation_protocol=2,subject_actor_id='1',
            based_on_relationship_version=self.life.relationship(self.a).version,
            relation_impacts=[dict(metric='trust',direction='decrease',magnitude='major',confidence=1.)])
        self.assertEqual(self.life.affect_summary(self.a)['intensity'],self.life.affect_summary(self.b)['intensity'])
        self.assertEqual(self.life.affect_summary(self.b)['causes'],[])
        self.assertLess(self.life.relationship(self.a).trust,self.life.relationship(self.b).trust)
        self.life.advance_time(self.a)
        self.now += 100
        once = self.life.advance_time(self.b,online_seconds=100).to_dict()
        twice = self.life.advance_time(self.a,online_seconds=100).to_dict()
        self.assertEqual(once['drives'],twice['drives'])

    def test_revision_keeps_later_conversation_and_time_changes(self):
        event,first = self.stimulate(self.a,{'hurt':.5})
        self.now += 60
        self.stimulate(self.b,{'joy':.3})
        self.now += 60
        self.life.advance_time(self.a,online_seconds=60)
        before = self.life.affect_summary(self.b)
        revised = replace(first,id='revision',proposed_affect_delta={'hurt':.05},revision=2,
                          supersedes_appraisal_id=first.id)
        self.life.appraise(self.a,event,revised)
        after = self.life.affect_summary(self.b)
        self.assertAlmostEqual(after['intensity']['joy'],before['intensity']['joy'],places=5)
        self.assertLess(after['intensity']['hurt'],before['intensity']['hurt']-.3)
        self.assertGreater(after['version'],before['version'])
        self.assertTrue(any(c['emotion']=='joy' for c in after['causes']))

    def test_self_imagination_cannot_punish_person(self):
        initial = self.life.relationship(self.a).trust
        with self.assertRaises(SessionFailure) as raised:
            self.stimulate(self.a,{'hurt':.3},kind='inference',relation_protocol=2,subject_actor_id='1',
                based_on_relationship_version=self.life.relationship(self.a).version,
                relation_impacts=[dict(metric='trust',direction='decrease',magnitude='major',confidence=1.)])
        self.assertEqual(raised.exception.code,'actor_mismatch')
        self.assertEqual(initial,self.life.relationship(self.a).trust)
        self.assertFalse(self.life.affect_summary(self.a))
        self.stimulate(self.a,{'hurt':.3},kind='inference')
        self.assertGreater(self.life.affect_summary(self.a)['intensity']['hurt'],.35)

    def test_rate_change_applies_after_old_interval_only(self):
        self.stimulate(self.a,{'hurt':.6},recovery_half_life_seconds={'hurt':100})
        self.now += 100
        self.stimulate(self.a,{},recovery_half_life_seconds={'hurt':10})
        at_change = self.life.affect_summary(self.a)['intensity']['hurt']
        self.assertAlmostEqual(at_change,.4,places=5)
        for dt in (1,3,5,8):
            self.life.affect_summary(self.b,self.now+dt)
        self.now += 10
        self.assertAlmostEqual(self.life.affect_summary(self.a)['intensity']['hurt'],.25,places=5)

    def test_parameter_change_and_revision_preserve_piecewise_time(self):
        event,first = self.stimulate(self.a,{'hurt':.6})
        self.now += 100
        new = replace(self.life.config.affect,half_life=dict(self.life.config.affect.half_life,hurt=10))
        self.life.configure_dynamics(self.a,new,self.life.config.drives)
        at_change = self.life.affect_summary(self.a)['intensity']['hurt']
        self.now += 10
        before = self.life.affect_summary(self.a)['intensity']['hurt']
        self.assertAlmostEqual(before,.1+(at_change-.1)/2,places=5)
        revised = replace(first,id='same-meaning-new-version',revision=2,supersedes_appraisal_id=first.id)
        self.life.appraise(self.a,event,revised)
        self.assertAlmostEqual(self.life.affect_summary(self.a)['intensity']['hurt'],before,places=5)


if __name__ == '__main__':
    unittest.main()
