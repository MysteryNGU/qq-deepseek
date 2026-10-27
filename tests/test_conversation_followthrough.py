"""Regression coverage for real deliveries and bounded thought maintenance."""
import json
import time
import unittest
from unittest.mock import patch

from tests.role_fixture import Fixture
from life.agent_session import SessionFailure


class FollowThroughTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.cfg.continuity_enabled = True
        self.f.role.continuity_protocol = 1
        self.repo = self.f.life.repo

    def note(self,text):
        with self.repo.tx():
            return self.f.role.continuity.write_note(self.f.scope,
                dict(text=text,sources=[self.f.event.id],continuity=dict(type='thought',action='create')),text)

    def input(self,event):
        self.f.role.sessions.enqueue(self.f.binding,event)
        with self.repo.tx():
            self.repo._exec("UPDATE agent_inputs SET state='consumed',handled=1 WHERE input_id=?",(event.id,))

    async def transport(self,action,params):
        self.f.sent.append(params)
        return dict(retcode=0,data=dict(message_id=len(self.f.sent)))

    def test_idle_context_contains_unrelated_old_thought_without_query(self):
        note = self.note('Explore watercolour composition')
        packet,_ = self.f.role.continuity.context(self.f.scope,self.f.binding,{})
        self.assertEqual(packet['query_count'],0)
        self.assertIn(note['note_ref'],[n['ref'] for n in packet['review_due']])
        self.assertIn(note['note_ref'],[n['ref'] for n in packet['items']])

    def test_review_retains_multiple_notes_without_renewal_and_rotates(self):
        a,b = self.note('Watercolour composition'),self.note('Study cloud shapes')
        old = self.repo.get_memory(a['note_ref'])
        with self.repo.tx():
            self.f.role.rest(self.f.scope,dict(continuity_refs=[a['note_ref']]),self.f.binding)
        self.assertEqual([n['ref'] for n in self.f.role.continuity.review_due(self.f.scope)],[b['note_ref']])
        new = self.repo.get_memory(a['note_ref'])
        self.assertEqual((old.version,old.valid_until),(new.version,new.valid_until))
        self.assertEqual(len(self.f.role.continuity.notes(self.f.scope)),2)

    def test_idle_rest_requires_review_but_accepts_retention(self):
        note = self.note('Study clouds')
        wake,_ = self.f.life.ingest(self.f.scope,'runtime_wake','inference','idle',payload=dict(wake_kind='idle_probe'))
        self.input(wake)
        with self.repo.tx(),self.assertRaisesRegex(SessionFailure,'待复核'):
            self.f.role.rest(self.f.scope,{},self.f.binding)
        with self.repo.tx():
            result=self.f.role.rest(self.f.scope,dict(continuity_refs=[note['note_ref']]),self.f.binding)
        self.assertTrue(result['resting'])

    def test_expiry_maintenance_needs_no_new_note_and_preserves_evidence(self):
        note = self.note('Temporary thought')
        old=self.repo.get_memory(note['note_ref'])
        self.assertEqual(self.f.life.memory.archive_expired_interpretations('default',now=old.valid_until+1),1)
        new=self.repo.get_memory(old.id)
        self.assertEqual(new.status,'archived')
        self.assertEqual(new.summary,old.summary)
        self.assertTrue(self.repo.get_event(self.f.event.id))

    def test_full_store_allows_replacing_a_topic_and_returns_actionable_capacity(self):
        first=self.note('First topic')
        for i in range(63):
            self.note('Distinct note '+str(i))
        with self.repo.tx(),self.assertRaises(SessionFailure) as caught:
            self.note('A new topic beyond capacity')
        self.assertEqual(caught.exception.code,'interpretations_full')
        with self.repo.tx():
            revised=self.f.role.continuity.write_note(self.f.scope,
                dict(text='Updated first topic',sources=[self.f.event.id],
                    continuity=dict(type='thought',action='replace',base_ref=first['note_ref'],base_version=1)), 'replace-full')
        self.assertEqual(self.repo.get_memory(first['note_ref']).status,'superseded')
        self.assertEqual(self.repo.get_memory(revised['note_ref']).summary,'Updated first topic')
        self.assertEqual(self.repo._one("SELECT count(*) n FROM memories WHERE status='active'")['n'],64)

    async def test_work_requires_real_ack_then_proceeds_and_new_input_resets(self):
        self.f.cfg.acknowledge_before_work=True
        self.input(self.f.event)
        guard=lambda:self.f.role.require_work_acknowledgement(self.f.scope,self.f.binding)
        with self.assertRaisesRegex(SessionFailure,'简短应答'):
            guard()
        self.f.expressions.transport=self.transport
        await self.f.expressions(self.f.scope,'ack',dict(text='I will check that.'),[self.f.event.id])
        guard()
        new,_=self.f.life.ingest_message(self.f.scope,'Another request')
        self.f.life.delivery.new_input(self.f.scope,'next',[new.id])
        self.input(new)
        with self.assertRaises(SessionFailure):
            guard()

    async def test_unknown_ack_does_not_allow_execution(self):
        self.f.cfg.acknowledge_before_work=True
        self.input(self.f.event)
        with self.assertRaises(TimeoutError):
            await self.f.expressions(self.f.scope,'ack',dict(text='I will check.'),[self.f.event.id])
        with self.assertRaises(SessionFailure):
            self.f.role.require_work_acknowledgement(self.f.scope,self.f.binding)

    async def interrupted(self,explicit=False):
        new=None
        async def send(action,params):
            nonlocal new
            result=await self.transport(action,params)
            if len(self.f.sent)==2:
                new,_=self.f.life.ingest_message(self.f.scope,'New incoming image')
                self.f.life.delivery.new_input(self.f.scope,'interrupt',[new.id])
            return result
        self.f.expressions.transport=send
        parts=lambda texts:[dict(kind='text',text=t,mode='single') for t in texts]
        with self.assertRaises(SessionFailure):
            await self.f.expressions(self.f.scope,'original',dict(parts=parts(['First.','Second.','Old final.'])),[self.f.event.id])
        await self.f.expressions(self.f.scope,'resumed',dict(parts=parts(['First.','Second.','New response.']),new_message=explicit),[new.id])
        return [p['message'][0]['data']['text'] for p in self.f.sent]

    async def test_interrupted_confirmed_prefix_is_not_resent(self):
        self.assertEqual(await self.interrupted(),['First.','Second.','New response.'])

    async def test_explicit_resend_preserves_repetition(self):
        self.assertEqual(await self.interrupted(True),['First.','Second.','First.','Second.','New response.'])

    async def test_completed_reply_can_be_repeated_on_new_request(self):
        self.f.expressions.transport=self.transport
        await self.f.expressions(self.f.scope,'one',dict(text='Hello.'),[self.f.event.id])
        new,_=self.f.life.ingest_message(self.f.scope,'Repeat it')
        self.f.life.delivery.new_input(self.f.scope,'next',[new.id])
        await self.f.expressions(self.f.scope,'two',dict(text='Hello.'),[new.id])
        self.assertEqual(len(self.f.sent),2)
