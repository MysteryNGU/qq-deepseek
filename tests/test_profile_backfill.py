import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from life.role_agent import RoleAgent
from life.runtime import LifeRuntime


class ProfileBackfillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.life=LifeRuntime(Path(self.tmp.name)/'life.db');self.addCleanup(self.life.close)
        cfg=SimpleNamespace(owner=1,profile_review_enabled=True,profile_backfill=dict(enabled=True))
        self.role=RoleAgent(self.life,SimpleNamespace(_generation=1),cfg,Path(self.tmp.name),AsyncMock())
        self.role.sessions.deliver=AsyncMock(return_value=dict(state='accepted'));self.role.replay=AsyncMock()
        self.scope=self.life.scope('qq:group:10')

    def message(self,text,actor='2'):
        event,_=self.life.ingest(self.scope,'message','user_report',text,actor_id=actor);self.role.people.observe(event);return event

    async def batch(self):
        await self.role.profile_worker.tick()
        row=self.life.repo._one("SELECT s.* FROM agent_sessions s JOIN meta m ON m.k='profile_backfill:'||s.session_id WHERE s.status='current'")
        self.root=dict(row);return self.role.profile_worker.record(self.root['session_id'])

    async def call(self,tool,key,**p):
        return await self.role.callback(dict(kind='life.'+tool,root_session_id=self.root['session_id'],session_id=self.root['session_id'],call_id=key,proposal=p))

    async def test_evidence_backed_profile_and_no_new_information_cover_whole_batch(self):
        first=self.message('I prefer to be called Lin.');second=self.message('Hello.')
        await self.batch();self.assertTrue((await self.call('read','read',kind='person_batch'))['ok'])
        result=await self.call('remember','commit',kind='person_batch_result',profiles=[dict(actor_id='2',field='address',topic_key='address.name',origin='explicit_statement',text='Call me Lin.',sources=[first.id],base_revision=0)],reviews=[dict(source_ref=second.id,reason='Greeting without a lasting preference')])
        self.assertTrue(result['ok'],result);self.assertEqual(result['reviewed_sources'],2)
        self.assertEqual(self.role.people.visible(self.scope,'2')[0]['origin'],'explicit_statement')
        replay=await self.call('remember','commit',kind='person_batch_result',profiles=[dict(actor_id='2',field='address',topic_key='address.name',origin='explicit_statement',text='Call me Lin.',sources=[first.id],base_revision=0)],reviews=[dict(source_ref=second.id,reason='Greeting without a lasting preference')])
        self.assertEqual(result,replay)
        self.role.send.assert_not_called()

    async def test_incomplete_coverage_rolls_back_every_profile(self):
        first=self.message('Call me Lin.');self.message('Hello.')
        await self.batch();await self.call('read','read',kind='person_batch')
        result=await self.call('remember','commit',kind='person_batch_result',profiles=[dict(actor_id='2',field='address',topic_key='address.name',origin='explicit_statement',text='Lin',sources=[first.id],base_revision=0)])
        self.assertEqual(result['code'],'incomplete_coverage');self.assertEqual(self.role.people.visible(self.scope,'2'),[])
        self.assertEqual(self.role.profile_worker.status()['pending_messages'],2)

    async def test_batch_pages_do_not_mark_unseen_sources_as_read(self):
        events=[self.message('普通问候 '+str(i)) for i in range(8)]
        await self.batch()
        page=await self.call('read','page0',kind='person_batch')
        self.assertEqual(len(page['items']),2)
        reviews=[dict(source_ref=e.id,reason='普通问候，没有长期偏好信息') for e in events]
        early=await self.call('remember','early',kind='person_batch_result',reviews=reviews)
        self.assertEqual(early['code'],'incomplete_coverage')
        for offset in (2,4,6):
            self.assertEqual(page['next_read'],dict(kind='person_batch',offset=offset))
            page=await self.call('read','page'+str(offset),**page['next_read'])
        self.assertIsNone(page['next_read'])
        result=await self.call('remember','done',kind='person_batch_result',reviews=reviews)
        self.assertEqual(result['reviewed_sources'],8)

    async def test_cannot_send_or_mix_other_people_or_skip_long_source(self):
        first=self.message('a'*1500);other=self.message('Hi','3');meta=await self.batch()
        self.assertEqual(list(meta['sources']),[first.id])
        self.assertEqual((await self.call('say','send',text='unwanted'))['code'],'scope_mismatch')
        await self.call('read','read',kind='person_batch')
        early=await self.call('remember','early',kind='person_batch_result',reviews=[dict(source_ref=first.id,reason='No lasting information')])
        self.assertEqual(early['code'],'incomplete_coverage')
        await self.call('read','page',kind='person_source',source_ref=first.id,offset=400)
        mixed=await self.call('remember','mixed',kind='person_batch_result',reviews=[dict(source_ref=other.id,reason='Wrong person')])
        self.assertFalse(mixed['ok'])
        good=await self.call('remember','good',kind='person_batch_result',reviews=[dict(source_ref=first.id,reason='Uninformative repeated text')])
        self.assertTrue(good['ok'],good)

    async def test_source_reset_fences_internal_submission(self):
        event=self.message('A fact');await self.batch();await self.call('read','read',kind='person_batch')
        self.life.tombstones.apply(self.life.tombstones.append(self.scope,[],[event.id]),self.scope,[],[event.id])
        result=await self.call('remember','after-reset',kind='person_batch_result',reviews=[dict(source_ref=event.id,reason='No new information')])
        self.assertFalse(result['ok']);self.assertEqual(self.role.people.visible(self.scope,'2'),[])

    async def test_pause_is_not_completion_and_does_not_reissue_paid_batch(self):
        event=self.message('Something unclear');await self.batch()
        result=await self.call('rest','pause',reason='Need clarification')
        self.assertTrue(result['ok']);self.assertEqual(self.role.profile_worker.status()['pending_messages'],1)
        self.role.memory_worker._retire=AsyncMock(return_value=True)
        self.role.profile_worker.next_check=0;await self.role.profile_worker.tick()
        self.assertEqual(self.role.sessions.deliver.await_count,1)


if __name__=='__main__':unittest.main()
