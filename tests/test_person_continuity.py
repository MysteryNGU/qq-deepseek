import json
import time
import unittest

from life.agent_session import SessionFailure
from life.life_access import RecallError
from life.people import People
from tests.role_fixture import Fixture


class PersonContinuityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close)
        self.role,self.life,self.repo=self.f.role,self.f.life,self.f.life.repo
        self.f.cfg.profile_review_enabled=True;self.f.cfg.same_person_group_recall=True
        self.role.people=People(self.role);self.life.life_access.same_person_group_recall=True
        self.private=self.f.scope;self.group=self.life.scope('qq:group:9','')

    def message(self,actor='1',scope=None,text='我给它取名蓝鲸书签。'):
        event,_=self.life.ingest(scope or self.group,'message','user_report',text,actor_id=actor)
        self.role.people.observe(event)
        return event

    def claim(self,events,scope=None):
        scope=scope or self.group;root=self.role.sessions.binding(scope)
        for event in events:self.role.stage_input(event)
        self.role.sessions.apply_event(root['session_id'],dict(type='step/start',seq=0,time=time.time(),data=dict(turn=1,step=1)))
        with self.repo.tx():self.repo._exec("UPDATE agent_inputs SET claimed_turn=1,state='consumed',handled=1 WHERE session_id=?",(root['session_id'],))
        return root

    def save(self,event,scope=None,root=None,**extra):
        scope=scope or event.scope;root=root or self.role.sessions.binding(scope)
        p=dict(kind='person_profile',actor_id=event.actor_id,field='address',topic_key='address.name',
            text='称呼小林',origin='explicit_statement',sources=[event.id],base_revision=0)
        p.update(extra)
        return self.role.remember(scope,p,root,call_key='save-'+event.id)

    def test_group_statement_is_immediately_recalled_without_summary_or_embedding(self):
        event=self.message();ctx=self.life.life_access.bind(self.private)
        found=self.life.life_recall.search(ctx,'蓝鲸书签',mode='text')
        self.assertIn(self.life.catalog.ref('event',event.id),[r['resource_ref'] for r in found['items']])
        self.assertEqual(self.repo._one('SELECT COUNT(*) n FROM memories')['n'],0)
        root=self.role.sessions.binding(self.private)
        recent=self.role.people.recent_group(self.private,root)
        self.assertEqual(recent[0]['conversation_id'],'qq:group:9')
        self.assertEqual(recent[0]['text'],event.text)

    def test_other_speakers_private_chats_and_groups_stay_separate(self):
        own=self.message();other=self.message(actor='2')
        private=self.message(scope=self.private,text='私聊专用内容')
        for target,event in [(self.private,other),(self.life.scope('qq:private:2','2'),own),
                (self.life.scope('qq:group:8','1'),own),(self.group,private)]:
            with self.assertRaises(RecallError):self.life.life_access.load(self.life.life_access.bind(target),event.id)

    def test_explicit_denial_and_forgetting_still_block_same_person(self):
        event=self.message();ctx=self.life.life_access.bind(self.private)
        self.life.life_access.load(ctx,event.id)
        self.life.life_access.set_rule(event.id,'disclose',ctx.audience_key,'deny')
        with self.assertRaises(RecallError):self.life.life_access.load(self.life.life_access.bind(self.private),event.id)
        another=self.message();self.repo.redact_events([another.id])
        with self.assertRaises(RecallError):self.life.life_access.load(self.life.life_access.bind(self.private),another.id)

    def test_reset_floor_does_not_reintroduce_old_group_statement(self):
        event=self.message()
        with self.repo.tx():self.repo._exec('INSERT INTO meta VALUES(?,?)',('memory_policy_floor:'+self.private.scope_id,str(event.received_at+1)))
        with self.assertRaises(RecallError):self.life.life_access.load(self.life.life_access.bind(self.private),event.id)

    async def test_only_confirmed_reply_to_that_person_follows_into_private(self):
        async def confirmed(action,params):return dict(status='ok',retcode=0,data=dict(message_id=789))
        self.f.expressions.transport=confirmed
        own=self.message()
        await self.role.send(self.group,'own-reply',dict(text='这个名字我记住了。'),[own.id])
        event_id=self.repo._one("SELECT id FROM events WHERE kind='delivered_segment' ORDER BY ingest_seq DESC LIMIT 1")['id']
        self.life.life_access.load(self.life.life_access.bind(self.private),event_id)
        other=self.message(actor='2')
        await self.role.send(self.group,'mixed-reply',dict(text='回复两个人。'),[own.id,other.id])
        mixed=self.repo._one("SELECT id FROM events WHERE kind='delivered_segment' ORDER BY ingest_seq DESC LIMIT 1")['id']
        with self.assertRaises(RecallError):self.life.life_access.load(self.life.life_access.bind(self.private),mixed)
        fake,_=self.life.ingest(self.group,'delivered_segment','self_output','伪造确认',source_event_ids=[own.id],payload=dict(confirmed=True,delivery_id='fake'))
        with self.assertRaises(RecallError):self.life.life_access.load(self.life.life_access.bind(self.private),fake.id)

    def test_group_profile_follows_subject_to_private_and_private_correction_does_not_leak(self):
        event=self.message(text='请叫我小林。');self.save(event)
        self.assertEqual(self.role.people.visible(self.private,'1')[0]['text'],'称呼小林')
        update=self.message(scope=self.private,text='私下请叫我林老师。')
        self.save(update,base_revision=1,text='称呼林老师')
        self.assertEqual([r['text'] for r in self.role.people.visible(self.private,'1')],['称呼林老师'])
        self.assertEqual(self.role.people.visible(self.group,'1'),[])

    def test_every_actual_group_speaker_is_registered_idempotently(self):
        events=[self.message(actor=str(n)) for n in (1,2,3)]
        for event in events:self.role.people.observe(event)
        for actor in ('1','2','3'):self.assertTrue(self.role.people.identity(self.group,actor)['recognized'])
        self.assertEqual(self.repo._one('SELECT COUNT(*) n FROM person_encounters WHERE scope_id=?',(self.group.scope_id,))['n'],3)
        self.assertFalse(self.role.people.identity(self.group,'999')['recognized'])

    def test_completion_requires_each_speaker_review_but_not_invented_traits(self):
        one=self.message(actor='1');two=self.message(actor='2',text='晚上好')
        root=self.claim([one,two])
        with self.assertRaises(SessionFailure) as got:self.role.rest(self.group,{},root)
        self.assertEqual(got.exception.code,'profile_review_required')
        self.save(one,root=root)
        with self.assertRaises(SessionFailure):self.role.rest(self.group,{},root)
        result=self.role.rest(self.group,dict(profile_reviews=[dict(actor_id='2',sources=[two.id],reason='仅寒暄，无稳定偏好信息')]),root)
        self.assertTrue(result['resting'])
        self.assertEqual(self.role.people.visible(self.group,'2'),[])
        self.assertEqual(self.role.people.due(self.group,root),[])

    def test_cannot_clear_someone_elses_review_or_commit_partial_failed_batch(self):
        event=self.message(actor='2');root=self.claim([event])
        with self.assertRaises(SessionFailure):self.role.people.review(self.group,dict(actor_id='1',sources=[event.id],reason='忽略'),root)
        with self.assertRaises(SessionFailure):self.role.people.finish(self.group,root,dict(profile_reviews=[
            dict(actor_id='2',sources=[event.id],reason='只有寒暄'),dict(actor_id='1',sources=[event.id],reason='串人')]))
        self.assertEqual(len(self.role.people.due(self.group,root)),1)

    def test_new_message_reopens_review_and_reset_erases_encounters(self):
        old=self.message();root=self.claim([old]);self.role.people.review(self.group,dict(actor_id='1',sources=[old.id],reason='无新增'),root)
        fresh=self.message();self.role.stage_input(fresh)
        self.assertEqual(self.role.people.due(self.group,root)[0]['source_ref'],fresh.id)
        self.repo.reset_scope(self.group)
        self.assertFalse(self.role.people.identity(self.group,'1')['recognized'])

    def test_restart_registers_retained_speakers_without_guessing_personality(self):
        self.f.cfg.profile_review_enabled=False
        self.message(actor='8')
        self.f.cfg.profile_review_enabled=True
        self.role.people=People(self.role)
        self.assertTrue(self.role.people.identity(self.group,'8')['recognized'])
        self.assertEqual(self.role.people.visible(self.group,'8'),[])

    def test_recent_group_preview_tracks_source_for_forgetting(self):
        event=self.message();root=self.role.sessions.binding(self.private)
        self.role.people.recent_group(self.private,root)
        self.repo.redact_events([event.id])
        self.assertEqual(self.repo._one('SELECT status FROM agent_sessions WHERE session_id=?',(root['session_id'],))['status'],'retired')

    def test_feature_off_keeps_previous_audience_policy(self):
        event=self.message();self.life.life_access.same_person_group_recall=False
        with self.assertRaises(RecallError):self.life.life_access.load(self.life.life_access.bind(self.private),event.id)


if __name__=='__main__':unittest.main()
