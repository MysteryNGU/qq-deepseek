"""Internal connection needs may motivate contact without invented external facts."""
import time
import unittest

from life.agent_session import SessionFailure
from life.tool_contracts import validate
from tests.role_fixture import Fixture


class MissingYouTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.role, self.life, self.repo = self.f.role, self.f.life, self.f.life.repo
        self.scope, self.root = self.f.scope, self.f.binding
        self.f.expressions.transport = self.transport

    async def transport(self, action, params):
        self.f.sent.append(params)
        return dict(retcode=0, data=dict(message_id=len(self.f.sent)))

    def drive(self, dimension='relatedness', tension=.8, recovery=0):
        state = self.life.drive_state(self.scope)
        drive = state.drives[dimension]
        drive.tension, drive.recovery_until = tension, recovery
        drive.activation = 0
        drive.learned_offset = 0
        drive.phase = 'accumulating'
        self.repo.save_state(self.life._drive_aggregate(self.scope), state.to_dict())
        self.role.motivations.scan(self.scope)
        return [r for r in self.role.motivations.rows(self.scope) if r['dimension'] == dimension]

    def wake(self, refs):
        event, _ = self.life.ingest(self.scope, 'runtime_wake', 'inference', '空闲思考',
            payload=dict(wake_kind='idle_probe', wake_plans=[dict(kind='idle_probe')], motivation_refs=refs))
        self.role.sessions.enqueue(self.root, event)
        self.role.sessions.apply_event(self.root['session_id'], dict(type='step/start', seq=0,
            time=time.time(), data=dict(turn=1, step=1)))
        with self.repo.tx():
            self.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=1,handled=1 WHERE session_id=?",
                           (self.root['session_id'],))
        return event

    def proposal(self, wake):
        basis = next(b for b in self.role.contact_policy.state(self.scope)['missing_you'] if b['dimension'] == 'relatedness')
        return dict(text='有点想你，过来陪你待一会儿。', sources=[wake.id], contact_basis=basis, end_turn=True)

    async def test_desire_alone_sends_and_settles_only_expression(self):
        candidate = self.drive()[0]
        wake = self.wake([candidate['id']])
        proposal = self.proposal(wake)
        before = self.life.drive_state(self.scope).drives['relatedness'].tension
        validate('life_say', proposal)
        receipt = await self.role.say(self.scope, proposal, self.root, 'missing-you')
        self.assertTrue(receipt['sent_confirmed'])
        row = self.repo._one('SELECT status,revision FROM motivation_decisions WHERE id=?', (candidate['id'],))
        self.assertEqual(row['status'], 'expressed')
        self.assertEqual(self.life.drive_state(self.scope).drives['relatedness'].tension, before)
        self.assertFalse(self.repo._one('SELECT 1 FROM drive_settlements'))
        self.assertEqual(self.role.contact_policy.state(self.scope)['unanswered'], 1)
        replay = await self.role.say(self.scope, proposal, self.root, 'replay')
        self.assertTrue(replay['sent_confirmed'])
        self.assertEqual(len(self.f.sent), 1)
        self.assertEqual(self.repo._one('SELECT revision FROM motivation_decisions WHERE id=?', (candidate['id'],))['revision'], row['revision'])

    async def test_desire_does_not_override_backoff(self):
        candidate = self.drive()[0]
        wake = self.wake([candidate['id']])
        proposal = self.proposal(wake)
        await self.role.say(self.scope, proposal, self.root, 'first')
        with self.assertRaisesRegex(SessionFailure, '自发联系暂缓'):
            await self.role.say(self.scope, dict(proposal, text='再找你聊聊。', new_message=True), self.root, 'second')

    async def test_low_need_recovery_and_old_cycle_cannot_invent_desire(self):
        self.drive(tension=.1)
        self.assertFalse(self.role.contact_policy.missing_you(self.scope))
        candidate = self.drive()[0]
        wake = self.wake([candidate['id']])
        proposal = self.proposal(wake)
        proposal['contact_basis']['cycle_id'] += 1
        with self.assertRaisesRegex(SessionFailure, '亲近意愿已变化'):
            await self.role.say(self.scope, proposal, self.root, 'stale')
        self.drive(recovery=time.time()+3600)
        self.assertFalse(self.role.contact_policy.missing_you(self.scope))
        self.assertEqual(self.f.sent, [])

    async def test_need_rechecked_at_send_and_unknown_is_not_expression_completed(self):
        candidate = self.drive()[0]
        wake = self.wake([candidate['id']])
        proposal = self.proposal(wake)
        self.drive(tension=.1)
        with self.assertRaisesRegex(SessionFailure, '亲近意愿已变化'):
            await self.role.say(self.scope, proposal, self.root, 'cooled')
        self.drive()
        self.f.expressions.transport = self.f.transport
        try:
            await self.role.say(self.scope, proposal, self.root, 'unknown')
        except (SessionFailure, TimeoutError):
            pass
        row = self.repo._one('SELECT status FROM motivation_decisions WHERE id=?', (candidate['id'],))
        self.assertNotEqual(row['status'], 'expressed')
        self.assertEqual(self.role.contact_policy.state(self.scope)['unanswered'], 0)

    async def test_other_motives_still_require_a_decision(self):
        candidate = self.drive()[0]
        other = self.drive('exploration')[0]
        wake = self.wake([candidate['id'], other['id']])
        proposal = self.proposal(wake)
        with self.assertRaisesRegex(SessionFailure, '本轮动机尚无行动'):
            await self.role.say(self.scope, proposal, self.root, 'unfinished')
        proposal['motivation_decisions'] = [dict(ref=other['id'], base_revision=other['revision'],
            decision='defer', reason='先表达想念，稍后探索')]
        validate('life_say', proposal)
        result = await self.role.say(self.scope, proposal, self.root, 'decided')
        self.assertTrue(result['sent_confirmed'])

    async def test_desire_is_for_owner_and_schemas_cover_no_topic_contact(self):
        self.drive()
        self.assertTrue(self.role.contact_policy.missing_you(self.scope))
        self.assertFalse(self.role.contact_policy.missing_you(self.life.scope('qq:private:2', '2')))
        self.assertFalse(self.role.contact_policy.missing_you(self.life.scope('qq:group:1', '1')))
        with self.assertRaises(SessionFailure):
            validate('life_say', dict(text='想你', contact_basis=dict(kind='missing_you')))
        with self.assertRaises(SessionFailure):
            validate('life_say', dict(text='想你', contact_basis=dict(kind='missing_you', dimension='libido', cycle_id=0)))
        validate('life_rest', dict(motivation_decisions=[dict(ref='id', base_revision=1, decision='defer', reason='稍后')]))
