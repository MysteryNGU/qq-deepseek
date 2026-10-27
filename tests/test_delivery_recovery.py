import json
import unittest
from unittest.mock import AsyncMock

from tests.role_fixture import Fixture
from life.agent_session import SessionFailure
from life.delivery import DeliveryNotSent
from life.role_agent import RoleAgent


class DeliveryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = Fixture()
        self.f.role.sessions.deliver = AsyncMock(return_value={})

    def tearDown(self):
        self.f.close()

    def handled(self):
        with self.f.life.repo.tx():
            self.f.life.repo._exec("UPDATE agent_inputs SET handled=1,state='consumed'")

    def notice(self):
        row = self.f.life.repo._one("SELECT id FROM events WHERE kind='delivery_recovery'")
        return self.f.life.repo.get_event(row['id'])

    async def test_partial_reply_recovery_preserves_confirmed_bubble_and_role_chooses_remainder(self):
        f = self.f
        async def transport(action, params):
            if f.sent:
                raise DeliveryNotSent('offline before write')
            f.sent.append(params)
            return dict(retcode=0,data=dict(message_id=1))
        f.expressions.transport = transport
        with self.assertRaises(SessionFailure) as error:
            await f.role.say(f.scope,dict(text='first\nsecond',sources=[f.event.id]),f.binding,'first')
        self.assertEqual(error.exception.code,'channel_offline')
        self.assertEqual([r['status'] for r in f.life.repo._all('SELECT status FROM deliveries ORDER BY segment')],
                         ['confirmed','deferred'])
        self.assertEqual(await f.role.review_deliveries(),1)
        event = self.notice()
        self.assertIn('"status":"confirmed"',event.text)
        self.assertIn('"status":"deferred"',event.text)
        self.assertEqual(len(f.sent),1)  # Reconnect does not itself send any content.
        self.assertEqual(await f.role.review_deliveries(),0)
        restarted = RoleAgent(f.life,f.bridge,f.cfg,f.root,f.role.send)
        self.assertEqual(await restarted.review_deliveries(),0)
        self.handled()
        async def connected(action,params):
            f.sent.append(params)
            return dict(retcode=0,data=dict(message_id=len(f.sent)))
        f.expressions.transport = connected
        result = await f.role.say(f.scope,dict(text='second',sources=[event.id]),f.binding,'continue')
        self.assertTrue(result['sent_confirmed'])
        self.assertEqual([p['message'][0]['data']['text'] for p in f.sent],['first','second'])

    async def test_unknown_send_stays_unknown_and_notice_cannot_bypass_duplicate_guard(self):
        f = self.f
        proposal = dict(text='original',sources=[f.event.id])
        with self.assertRaises(TimeoutError):
            await f.role.say(f.scope,proposal,f.binding,'lost-receipt')
        self.assertEqual(await f.role.review_deliveries(),1)
        event = self.notice()
        self.handled()
        with self.assertRaises(SessionFailure) as error:
            await f.role.say(f.scope,dict(proposal,sources=[event.id]),f.binding,'retry')
        self.assertEqual(error.exception.code,'outcome_unknown')
        self.assertEqual(len(f.sent),1)
        self.assertEqual(f.life.repo._one('SELECT status FROM deliveries')['status'],'unknown')

    async def test_known_unsent_body_can_be_reconsidered_after_new_context(self):
        f = self.f
        async def offline(*args):
            raise DeliveryNotSent('offline')
        f.expressions.transport = offline
        with self.assertRaises(SessionFailure):
            await f.role.say(f.scope,dict(text='answer',sources=[f.event.id]),f.binding,'offline')
        # A startup/new chat input must not erase the fact that a generated
        # response was never sent; the role still gets to reconsider it.
        newer,_ = f.life.ingest_message(f.scope,'What happened to that reply?')
        f.role.sessions.enqueue(f.binding,newer)
        self.assertEqual(f.life.repo._one('SELECT status FROM deliveries')['status'],'deferred')
        await f.role.review_deliveries()
        event = self.notice()
        self.handled()
        async def connected(action,params):
            f.sent.append(params)
            return dict(retcode=0,data=dict(message_id=2))
        f.expressions.transport = connected
        result = await f.role.say(f.scope,dict(text='answer',sources=[event.id]),f.binding,'fresh-context')
        self.assertTrue(result['sent_confirmed'])
        self.assertEqual(len(f.sent),1)

    async def test_notification_survives_cognition_failure_without_duplicate_enqueue(self):
        f = self.f
        with self.assertRaises(TimeoutError):
            await f.role.say(f.scope,dict(text='answer',sources=[f.event.id]),f.binding,'lost')
        f.role.sessions.deliver.side_effect = ConnectionError('DSH offline')
        with self.assertRaises(ConnectionError):
            await f.role.review_deliveries()
        self.assertEqual(len(f.role.sessions.pending(f.binding['session_id'])),1)
        self.assertEqual(await f.role.review_deliveries(),0)
        self.assertEqual(f.life.repo._one('SELECT COUNT(*) n FROM delivery_reviews')['n'],1)

    async def test_recovery_does_not_revive_forgotten_sources_or_cross_conversations(self):
        f = self.f
        with self.assertRaises(TimeoutError):
            await f.role.say(f.scope,dict(text='private',sources=[f.event.id]),f.binding,'lost')
        other = f.life.scope('qq:private:2','2')
        logical = f.life.repo._one('SELECT turn_id FROM deliveries')['turn_id']
        self.assertEqual(f.role.read(other,dict(kind='deliveries',logical_message_id=logical))['items'],[])
        with f.life.repo.tx():
            f.life.repo._exec('UPDATE events SET redacted_at=1 WHERE id=?',(f.event.id,))
        self.assertEqual(await f.role.review_deliveries(),0)

    async def test_startup_recovers_prepared_but_never_dispatched_expression(self):
        f = self.f
        from life.delivery import DeliveryContext
        ctx = DeliveryContext(f.life.delivery,f.scope,'interrupted',0,(f.event.id,),0,0)
        identity = f.life.delivery.prepare(ctx,'send_private_msg',dict(user_id=1,message='draft'))
        f.life.delivery.recover()
        self.assertEqual(f.life.repo._one('SELECT status FROM deliveries WHERE id=?',(identity,))['status'],'deferred')
        self.assertEqual(await f.role.review_deliveries(),1)
        self.assertFalse(f.sent)

    async def test_transport_only_proves_not_sent_before_socket_write(self):
        from life.role_host import RoleHost
        host = object.__new__(RoleHost)
        host.ws = None
        with self.assertRaises(DeliveryNotSent):
            await host._call_raw('send_private_msg',dict(user_id=1,message='test'))
        host._rpc = {}
        host.ws = type('Socket',(),{'send':AsyncMock(side_effect=ConnectionError('write interrupted'))})()
        with self.assertRaises(ConnectionError) as error:
            await host._call_raw('send_private_msg',dict(user_id=1,message='test'))
        self.assertNotIsInstance(error.exception,DeliveryNotSent)
        self.assertEqual(host._rpc,{})


if __name__=='__main__':
    unittest.main()
