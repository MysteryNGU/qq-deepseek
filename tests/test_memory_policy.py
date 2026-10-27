"""Persistent learning cannot accumulate standing rules or revive stale conclusions."""
import json
import time
import unittest
from unittest.mock import patch

from tests.role_fixture import Fixture
from life.agent_session import SessionFailure, canonical


class MemoryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.repo = self.f.life.repo
        self.scope = self.f.scope
        self.role = self.f.role
        self.root = self.f.binding

    def remember(self, text, **fields):
        fields.setdefault('applicability','ongoing')
        p = dict(kind='preference', text=text, sources=[self.f.event.id], **fields)
        with self.repo.tx():
            return self.role.remember(self.scope,p,self.root)

    def test_default_is_on_demand_and_inference_cannot_become_standing(self):
        receipt = self.remember('以后称呼用您',topic_key='address.form')
        row = self.repo._one('SELECT * FROM memory_annotations WHERE id=?',(receipt['memory_id'],))
        self.assertEqual(row['always_load'],0)
        guessed,_ = self.f.life.ingest(self.scope,'thought','inference','我猜他喜欢这样的称呼',source_event_ids=[self.f.event.id])
        with self.assertRaisesRegex(ValueError,'direct_user'):
            with self.repo.tx():
                self.role.remember(self.scope,dict(kind='reminder',text='以后都这样称呼',sources=[guessed.id],always_load=True),self.root)
        self.assertEqual(self.repo._one('SELECT COUNT(*) AS n FROM memory_annotations')['n'],1)

    def test_sixty_corrections_update_one_topic_and_preserve_other_preferences(self):
        with self.repo.tx():
            self.repo._exec("UPDATE events SET payload=? WHERE id=?",(canonical(dict(text='称呼用您')),self.f.event.id))
        self.remember('称呼用您',topic_key='address.form',always_load=True)
        for i in range(60):
            event,_ = self.f.life.ingest_message(self.scope,'新语速 '+str(i))
            with self.repo.tx():
                self.role.remember(self.scope,dict(kind='preference',text='声线语速更新为 '+str(i),
                    topic_key='voice.speed',sources=[event.id]),self.root)
        active = self.f.life.recollection.annotations(self.scope,'声线语速',None)
        self.assertEqual(len(active),2)
        self.assertTrue(any('59' in r['text'] for r in active))
        self.assertFalse(any('更新为 0' in r['text'] for r in active))
        self.assertTrue(any('称呼用您' == r['text'] for r in active))

    def test_repeated_self_note_cannot_extend_expiration(self):
        event,_ = self.f.life.ingest(self.scope,'thought','inference','临时判断')
        with patch('life.recollection.now_ts',return_value=100):
            key = self.f.life.recollection.annotate(self.scope,'reminder','等待一次核验',[event.id])
        expiry = self.repo._one('SELECT expires FROM memory_annotations WHERE id=?',(key,))['expires']
        with patch('life.recollection.now_ts',return_value=1000):
            same = self.f.life.recollection.annotate(self.scope,'reminder','等待一次核验',[event.id])
        self.assertEqual(key,same)
        self.assertEqual(self.repo._one('SELECT expires FROM memory_annotations WHERE id=?',(key,))['expires'],expiry)

    @patch('life.recollection.STANDING_TOKEN_BUDGET',10000)
    def test_standing_capacity_rejects_growth_without_losing_old_values(self):
        with self.repo.tx():
            self.repo._exec('UPDATE events SET payload=? WHERE id=?',(canonical(dict(text='短偏好')),self.f.event.id))
        for i in range(12):
            self.remember('短偏好 '+str(i),topic_key='preference.'+str(i),always_load=True)
        with self.assertRaisesRegex(ValueError,'standing_reminders_full'):
            self.remember('第十三个常驻偏好',topic_key='preference.extra',always_load=True)
        newer,_=self.f.life.ingest_message(self.scope,'更新偏好零')
        with self.repo.tx():
            self.role.remember(self.scope,dict(kind='preference',text='更新偏好零',sources=[newer.id],
                topic_key='preference.0',always_load=True,applicability='ongoing'),self.root)
        rows = self.f.life.recollection.annotations(self.scope,'',None,mandatory=True)
        self.assertEqual(len(rows),12)
        self.assertTrue(any(r['text']=='更新偏好零' for r in rows))

    def test_expired_interpretation_is_excluded_from_both_retrieval_routes(self):
        self.f.cfg.continuity_enabled=True
        self.role.continuity_protocol=1
        with self.repo.tx():
            receipt=self.role.remember(self.scope,dict(kind='inference',text='等待水彩画资源',sources=[self.f.event.id],
                continuity=dict(type='thought')),self.root)
            self.repo.update_memory(receipt['memory_id'],valid_until=time.time()-1)
        self.assertFalse(self.role.continuity.notes(self.scope))
        self.assertNotIn('等待水彩画资源',self.f.life.context_block(self.scope,'水彩画资源',1200))
        detail=self.role.continuity.notes(self.scope,ref=receipt['memory_id'])
        self.assertFalse(detail[0]['current'])

    def test_task_revision_invalidates_its_temporary_conclusion(self):
        self.f.cfg.continuity_enabled=True
        self.role.continuity_protocol=1
        task=self.f.task()
        with self.repo.tx():
            receipt=self.role.remember(self.scope,dict(kind='inference',text='水彩画资料还在等待',
                sources=[self.f.event.id],intent_ref=task['ref'],continuity=dict(type='thought')),self.root)
        self.assertTrue(self.role.continuity.notes(self.scope))
        with self.repo.tx():
            self.role.intent(self.scope,dict(ref=task['ref'],base_revision=task['revision'],checkpoint='已经得到资料'),self.root)
        self.assertFalse(self.role.continuity.notes(self.scope))
        self.assertNotIn('水彩画资料还在等待',self.f.life.context_block(self.scope,'水彩画资料',1200))
        self.assertIsNotNone(self.repo.get_memory(receipt['memory_id']))

    def test_own_thought_cannot_create_its_own_retrieval_query(self):
        self.f.cfg.continuity_enabled=True
        self.role.continuity_protocol=1
        with self.repo.tx():
            self.role.remember(self.scope,dict(kind='inference',text='ZEBRA_UNRELATED_TOPIC',sources=[self.f.event.id],
                continuity=dict(type='thought')),self.root)
        with patch.object(self.f.life.life_recall,'context',wraps=self.f.life.life_recall.context) as retrieve:
            context,memory=self.role.continuity.context(self.scope,self.root,{})
        self.assertEqual(retrieve.call_args.args[1],[])
        self.assertNotIn('ZEBRA_UNRELATED_TOPIC',memory)
        # Lifecycle review is deliberately independent of retrieval. It may
        # expose an old interpretation without turning it into a recall query.
        self.assertEqual(context['items'][0]['authority'],'interpretation')
        self.assertTrue(context['review_due'])

    def test_repeating_an_interpretation_does_not_renew_lifetime(self):
        with self.repo.tx():
            first=self.f.life.memory.remember(self.scope,'interpretation','水彩画可能需要留白',[self.f.event.id],source_kind='inference')
            self.repo.update_memory(first.id,valid_until=time.time()+100)
            second=self.f.life.memory.remember(self.scope,'interpretation','水彩画可能需要留白',[self.f.event.id],source_kind='inference')
        self.assertEqual(first.id,second.id)
        self.assertLess(second.valid_until,time.time()+101)

    def test_same_task_topic_cannot_be_recreated_or_revive_a_cancelled_task(self):
        with self.repo.tx():
            first=self.role.intent(self.scope,dict(goal='收集表情',topic_key='stickers',sources=[self.f.event.id]),self.root)['intent']
            self.role.intent(self.scope,dict(action='cancel',ref=first['ref'],base_revision=first['revision']),self.root)
        with self.assertRaises(SessionFailure) as caught:
            with self.repo.tx():
                self.role.intent(self.scope,dict(goal='换个说法重新收表情',topic_key='stickers',sources=[self.f.event.id]),self.root)
        self.assertEqual(caught.exception.code,'intent_exists')
        self.assertEqual(self.role.tasks.get(first['ref'])['status'],'cancelled')

    def test_rephrasing_an_expired_thought_requires_new_evidence(self):
        old,_=self.f.life.ingest(self.scope,'thought','inference','旧的临时判断',occurred_at=time.time()-8*86400)
        with self.assertRaisesRegex(ValueError,'new_evidence'):
            with self.repo.tx():
                self.f.life.memory.remember(self.scope,'interpretation','给旧判断换个新说法',[old.id],source_kind='inference')
        self.assertEqual(self.repo._one('SELECT COUNT(*) AS n FROM memories')['n'],0)

    def test_cleared_preferences_cannot_be_relearned_from_old_messages(self):
        floor=time.time()
        with self.repo.tx():
            self.repo._exec('UPDATE events SET occurred_at=?,received_at=? WHERE id=?',(floor-1,floor-1,self.f.event.id))
            self.repo._exec('INSERT INTO meta VALUES(?,?)',('memory_policy_floor:'+self.scope.scope_id,str(floor)))
        with self.assertRaisesRegex(ValueError,'new_user_evidence'):
            self.remember('把旧约定换一种说法重新记住',topic_key='address.form')
        fresh,_=self.f.life.ingest_message(self.scope,'新的明确约定')
        with self.repo.tx():
            result=self.role.remember(self.scope,dict(kind='preference',text='明确的新约定',topic_key='address.form',sources=[fresh.id]),self.root)
        self.assertTrue(result['saved'])

    def old_session(self):
        for seq in range(64):
            self.role.sessions.apply_event(self.root['session_id'],dict(seq=seq,type='turn/end',data=dict(turn=seq)))

    def test_idle_session_renews_without_copying_old_context_or_losing_tasks(self):
        task=self.f.task()
        self.old_session()
        new=self.role.sessions.renew_idle(self.scope)
        self.assertNotEqual(new['session_id'],self.root['session_id'])
        self.assertEqual(new['intent_ref'],task['ref'])
        self.assertEqual(self.repo._one('SELECT COUNT(*) AS n FROM agent_events WHERE session_id=?',(new['session_id'],))['n'],0)
        self.assertEqual(self.role.tasks.get(task['ref'])['status'],'active')

    def test_pending_input_or_live_execution_prevents_session_renewal(self):
        self.old_session()
        self.role.sessions.enqueue(self.root,self.f.event)
        self.assertEqual(self.role.sessions.renew_idle(self.scope)['session_id'],self.root['session_id'])
        with self.repo.tx():
            self.repo._exec("UPDATE agent_inputs SET handled=1,state='settled'")
            self.f.life.jobs.enqueue(self.scope,'local_media',{},key='live-generation')
        self.assertEqual(self.role.sessions.renew_idle(self.scope)['session_id'],self.root['session_id'])


if __name__=='__main__':
    unittest.main()
