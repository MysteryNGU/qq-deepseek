"""Evidence, permission and real delivery regression tests for companion changes."""
import time
import unittest

from life.agent_session import SessionFailure
from life.contact_policy import ContactPolicy, closing_text
from life.life_access import RecallError
from life.stable_facts import StableFacts
from life.tool_contracts import validate
from tests.role_fixture import Fixture


class CompanionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.role, self.life, self.repo = self.f.role, self.f.life, self.f.life.repo
        self.scope, self.root = self.f.scope, self.f.binding
        self.f.expressions.transport = self.transport

    async def transport(self, action, params):
        self.f.sent.append(params)
        return dict(retcode=0, data=dict(message_id=len(self.f.sent)))

    def event(self, text, **kwargs):
        return self.life.ingest(self.scope, 'message', 'user_report', text, actor_id='1', **kwargs)[0]

    def fact(self, event, **changes):
        return dict(actor_id='1', topic_key='work.employer', quote=event.text,
                    evidence_refs=[event.id], **changes)

    def save(self, *proposals):
        ctx = self.life.life_access.bind(self.scope, mode='internal')
        with self.repo.tx():
            return StableFacts(self.life.catalog, self.life.life_access).commit(ctx, list(proposals),
                {ref for p in proposals for ref in p['evidence_refs']})

    def wake(self, kind='idle_probe'):
        event, _ = self.life.ingest(self.scope, 'runtime_wake', 'inference', '醒来',
            payload=dict(wake_kind=kind, wake_plans=[dict(kind=kind)]))
        self.role.sessions.enqueue(self.root, event)
        with self.repo.tx():
            self.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=NULL,handled=1 WHERE session_id=?",
                           (self.root['session_id'],))
        return event

    async def test_fact_revision_merge_and_old_evidence(self):
        old = self.event('我在甲公司工作。')
        one = self.save(self.fact(old))[0]
        duplicate = self.event(old.text)
        two = self.save(self.fact(duplicate))[0]
        self.assertEqual(one['resource_ref'], two['resource_ref'])
        new = self.event('我已经换到乙公司工作。')
        three = self.save(self.fact(new, operation='change'))[0]
        self.assertTrue(three['supersedes'])
        result = self.save(self.fact(old, operation='correct'))[0]
        self.assertEqual(result['reason'], 'older_than_current')
        ctx = self.life.life_access.bind(self.scope)
        found = self.life.life_recall.search(ctx, '公司', intent='facts', diagnostics=True, budget=6000)
        self.assertEqual(found['items'][0]['resource_ref'], three['resource_ref'])
        self.assertEqual(found['items'][0]['fact']['actor_id'], '1')
        self.assertEqual(found['items'][0]['fact']['previous_ref'], one['resource_ref'])
        self.assertIn('text', found['items'][0]['match_routes'])
        self.assertNotIn(one['resource_ref'], [i['resource_ref'] for i in found['items']])
        self.assertIn(old.text, self.life.life_recall.read(ctx, one['resource_ref'])['text'])

    async def test_fact_conflict_retains_uncertainty(self):
        old = self.save(self.fact(self.event('我在甲公司工作。')))[0]
        conflict = self.save(self.fact(self.event('我在乙公司工作。')))[0]
        self.assertEqual(conflict['status'], 'disputed')
        self.assertEqual(self.life.catalog.resolve(old['resource_ref'])['status'], 'disputed')

    async def test_contact_pauses_after_three_and_reset_isolated(self):
        event = self.event('最近在读一本书。')
        self.wake()
        proposal = dict(text='想起那本书。', sources=[event.id], new_message=True,
                        contact_basis=dict(kind='followup', sources=[event.id]))
        for i in range(3):
            await self.role.say(self.scope, dict(proposal, text='想起那本书。' + str(i)), self.root, 'contact-' + str(i))
            state = self.role.contact_policy.state(self.scope)
            self.assertEqual(state['unanswered'], i + 1)
            self.assertFalse(state['available'])
            # Move only this fake receipt into the past to test the next interval.
            with self.repo.tx():
                self.repo._exec('UPDATE deliveries SET updated=? WHERE scope_id=?', (time.time()-15000, self.scope.scope_id))
        self.assertTrue(self.role.contact_policy.state(self.scope)['paused'])
        with self.assertRaisesRegex(SessionFailure, '自发联系暂缓'):
            await self.role.say(self.scope, dict(proposal, text='第四次'), self.root, 'fourth')
        other = self.life.scope('qq:private:2', '2')
        self.assertEqual(self.role.contact_policy.state(other)['unanswered'], 0)
        self.repo.reset_scope(self.scope)
        self.assertEqual(self.role.contact_policy.state(self.scope)['unanswered'], 0)

    async def test_fact_quotes_actor_atomicity_and_scope(self):
        event = self.event('我不在甲公司工作。')
        good = self.fact(event)
        bad = dict(good, quote='我在甲公司工作。')
        with self.assertRaisesRegex(RecallError, 'fact_evidence_mismatch'):
            self.save(good, bad)
        self.assertEqual(self.repo.find_facts(self.scope, '1', 'work.employer'), [])
        with self.assertRaises(RecallError):
            self.save(dict(good, actor_id='2'))
        saved = self.save(good)[0]
        other = self.life.scope('qq:group:2', '1')
        result = self.life.life_recall.search(self.life.life_access.bind(other), '公司', diagnostics=True)
        self.assertEqual(result['items'], [])
        self.assertEqual(result['diagnostics']['empty_reason'], 'no_visible_match')
        self.assertNotIn(saved['resource_ref'], str(result))
        self.repo.redact_events([event.id])
        with self.assertRaises(RecallError):
            self.life.life_recall.read(self.life.life_access.bind(self.scope), saved['resource_ref'])

    async def test_organizer_extracts_facts_in_same_commit_and_replays(self):
        event = self.event('我在甲公司工作。')
        ctx = self.life.life_access.bind(self.scope, mode='internal')
        organize = self.life.memory_organize
        prepared = organize.prepare(ctx, [event.id])
        lease = self.life.jobs.claim(prepared['job_ref'])
        organize.read_batch(ctx, lease)
        resource = self.life.catalog.resolve(event.id)['resource_id']
        proposal = dict(coverage_digest=prepared['coverage_digest'],
            claims=[dict(text=event.text, kind='reported', evidence_refs=[resource])],
            stable_facts=[dict(self.fact(event), evidence_refs=[resource]),
                          dict(self.fact(event), evidence_refs=[resource], quote='没有说过的内容')])
        saved = organize.commit(ctx, lease, proposal, 'test-fact-summary')
        self.assertTrue(saved['stable_facts'][0]['saved'])
        self.assertEqual(saved['stable_facts'][1]['reason'], 'fact_evidence_mismatch')
        self.assertFalse(saved['stable_facts'][1]['saved'])
        self.assertEqual(saved, organize.commit(ctx, lease, proposal, 'test-fact-summary'))

    async def test_recall_diagnostic_pagination_and_budget(self):
        for i in range(4):
            self.event('电影讨论' + str(i))
        ctx = self.life.life_access.bind(self.scope)
        first = self.life.life_recall.search(ctx, '电影', intent='recent', diagnostics=True, limit=1)
        self.assertTrue(first['has_more'])
        second = self.life.life_recall.search(ctx, '电影', intent='recent', diagnostics=True, limit=1, cursor=first['next_cursor'])
        self.assertLessEqual(second['items'][0]['occurred_at'], first['items'][0]['occurred_at'])
        self.assertIn('text', second['items'][0]['match_routes'])
        with self.assertRaises(RecallError):
            self.life.life_recall.search(ctx, '电影', intent='facts', diagnostics=True, cursor=first['next_cursor'])

    async def test_real_send_requires_contact_basis_and_backs_off(self):
        event = self.event('明天要去看展览。')
        self.wake()
        proposal = dict(text='想起你提过的展览。', sources=[event.id])
        with self.assertRaisesRegex(SessionFailure, '依据'):
            await self.role.say(self.scope, proposal, self.root, 'contact-no-basis')
        proposal['contact_basis'] = dict(kind='followup', sources=[event.id])
        sent = await self.role.say(self.scope, proposal, self.root, 'contact-1')
        self.assertTrue(sent['sent_confirmed'])
        state = self.role.contact_policy.state(self.scope)
        self.assertEqual(state['unanswered'], 1)
        self.assertFalse(state['available'])
        self.assertEqual(ContactPolicy(self.role).state(self.scope)['unanswered'], 1)
        with self.assertRaisesRegex(SessionFailure, '自发联系暂缓'):
            await self.role.say(self.scope, dict(proposal, text='还想再问一句。', new_message=True), self.root, 'contact-2')
        # A duplicate confirmed RPC must replay, not send again or count again.
        replay = await self.role.say(self.scope, proposal, self.root, 'contact-replay')
        self.assertTrue(replay['sent_confirmed'])
        self.assertEqual(self.role.contact_policy.state(self.scope)['unanswered'], 1)
        self.event('展览挺不错，我刚回家。')
        self.assertEqual(self.role.contact_policy.state(self.scope)['unanswered'], 0)

    async def test_unknown_send_is_not_unanswered_and_source_must_be_real(self):
        wake = self.wake()
        proposal = dict(text='打个招呼', sources=[wake.id], contact_basis=dict(kind='followup', sources=[wake.id]))
        with self.assertRaisesRegex(SessionFailure, '念头'):
            await self.role.say(self.scope, proposal, self.root, 'imagined-contact')
        event = self.event('我在看一部电影。')
        proposal.update(sources=[event.id], contact_basis=dict(kind='followup', sources=[event.id]))
        self.f.expressions.transport = self.f.transport
        try:
            await self.role.say(self.scope, proposal, self.root, 'unknown-contact')
        except (SessionFailure, TimeoutError):
            pass
        self.assertEqual(self.role.contact_policy.state(self.scope)['unanswered'], 0)

    async def test_closing_is_silent_but_questions_and_media_are_not(self):
        event = self.event('晚安')
        sent = await self.role.say(self.scope, dict(text='晚安', sources=[event.id]), self.root, 'night-1')
        self.assertTrue(sent['sent_confirmed'])
        reply = self.event('晚安！')
        with self.assertRaisesRegex(SessionFailure, '已经送达收尾'):
            await self.role.say(self.scope, dict(text='晚安啦', sources=[reply.id]), self.root, 'night-2')
        question = self.event('晚安之前能帮我看下这个问题吗？')
        sent = await self.role.say(self.scope, dict(text='可以，把问题发给我。', sources=[question.id]), self.root, 'night-question')
        self.assertTrue(sent['sent_confirmed'])
        self.assertFalse(closing_text('晚安，明天几点叫你？'))
        self.assertFalse(closing_text('晚安前我还有事情想说'))

    async def test_explicit_task_wake_and_user_input_are_exempt(self):
        event = self.event('提醒我交报告。')
        self.wake('task_due')
        self.assertFalse(self.role.contact_policy.voluntary(self.scope, self.root))
        sent = await self.role.say(self.scope, dict(text='该交报告了。', sources=[event.id]), self.root, 'reminder')
        self.assertTrue(sent['sent_confirmed'])
        self.assertEqual(self.role.contact_policy.state(self.scope)['unanswered'], 0)

    async def test_mixed_voluntary_wakes_do_not_bypass_policy(self):
        event, _ = self.life.ingest(self.scope, 'runtime_wake', 'inference', '混合自主唤醒',
            payload=dict(wake_kind='mixed', wake_plans=[dict(kind='idle_probe'), dict(kind='self_scheduled')]))
        self.role.sessions.enqueue(self.root, event)
        with self.repo.tx():
            self.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=NULL,handled=1 WHERE session_id=?",
                           (self.root['session_id'],))
        self.assertTrue(self.role.contact_policy.voluntary(self.scope, self.root))

    async def test_schema_allows_new_fields_and_rejects_unknown_intent(self):
        validate('life_read', dict(kind='recall', query='公司', intent='facts', diagnostics=True))
        validate('life_read', dict(kind='contact_policy'))
        validate('life_say', dict(text='你好', contact_basis=dict(kind='followup', sources=['evt'])))
        validate('life_remember', dict(kind='organized_memory', stable_facts=[dict(actor_id='1',
            topic_key='work.employer', quote='原话', evidence_refs=['evt'], operation='observe')]))
        with self.assertRaises(SessionFailure):
            validate('life_read', dict(kind='recall', intent='hidden'))
