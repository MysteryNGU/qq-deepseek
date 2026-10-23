import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from life.agent_session import AgentSessions, SessionFailure
from life.runtime import LifeRuntime


class AgentSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.life = LifeRuntime(Path(self.tmp.name) / 'life.db')
        self.scope = self.life.scope('qq:private:1', '1')
        self.sessions = AgentSessions(self.life)
        self.binding = self.sessions.binding(self.scope)

    def tearDown(self):
        self.life.close()
        self.tmp.cleanup()

    def event(self, text='hello', key='qq:1'):
        return self.life.ingest(self.scope, 'message', 'user_report', text, external_id=key)[0]

    def test_ack_loss_queries_original_input_and_does_not_duplicate(self):
        calls = []
        def send(payload):
            calls.append(payload['input_id'])
            raise TimeoutError('ACK lost')
        self.sessions.bridge = SimpleNamespace(session_input=send,
            input_status=lambda sid, iid: dict(state='accepted', message_id='m1'))
        item = self.sessions.enqueue(self.binding, self.event())
        asyncio.run(self.sessions.deliver(item))
        asyncio.run(self.sessions.deliver(item))
        self.assertEqual(calls, [item['input_id']])
        self.assertEqual(self.sessions.pending(self.binding['session_id'])[0]['state'], 'accepted')

    def test_accepted_input_is_reconciled_after_restart_without_new_identity(self):
        calls = []
        self.sessions.bridge = SimpleNamespace(session_input=lambda payload:dict(state='accepted',message_id='m1'),
            input_status=lambda sid,iid:(calls.append((sid,iid)) or dict(state='consumed',message_id='m1',turn=2,step=3)))
        item = self.sessions.enqueue(self.binding,self.event())
        asyncio.run(self.sessions.deliver(item))
        asyncio.run(self.sessions.deliver(item,reconcile=True))
        self.assertEqual(calls,[(self.binding['session_id'],item['input_id'])])
        self.assertEqual(self.sessions.pending(self.binding['session_id'])[0]['state'],'consumed')

    def test_lost_admitted_history_is_not_recreated_as_a_new_input(self):
        sent = []
        self.sessions.bridge = SimpleNamespace(session_input=lambda payload:(sent.append(payload) or dict(state='accepted',message_id='m1')),
            input_status=lambda sid,iid:dict(state='not_found'))
        item = self.sessions.enqueue(self.binding,self.event())
        asyncio.run(self.sessions.deliver(item))
        with self.assertRaises(SessionFailure) as error:
            asyncio.run(self.sessions.deliver(item,reconcile=True))
        self.assertEqual(error.exception.code,'history_gap')
        self.assertEqual(len(sent),1)

    def test_contiguous_cursor_duplicate_conflict_and_transaction_rollback(self):
        sid = self.binding['session_id']
        first = dict(seq=0, type='turn/start', data={'turn': 1})
        self.assertTrue(self.sessions.apply_event(sid, first))
        self.assertFalse(self.sessions.apply_event(sid, first))
        with self.assertRaises(SessionFailure) as error:
            self.sessions.apply_event(sid, dict(seq=2, type='step/start', data={}))
        self.assertEqual(error.exception.code, 'history_gap')
        self.assertEqual(self.sessions.require(sid)['cursor'], 0)
        with self.assertRaises(SessionFailure):
            self.sessions.apply_event(sid, dict(seq=0, type='turn/start', data={'turn': 2}))

    def test_consumed_does_not_clear_attention_and_later_input_stays_pending(self):
        sid = self.binding['session_id']
        item = self.sessions.enqueue(self.binding, self.event())
        with self.life.repo.tx():
            self.life.repo._exec("UPDATE agent_inputs SET message_id='m1' WHERE input_id=?", (item['input_id'],))
        self.sessions.apply_event(sid, dict(seq=0, type='step/start', data={'turn': 1, 'step': 1}))
        self.sessions.apply_event(sid, dict(seq=1, type='user/message', data={'id': 'm1'}))
        later = self.sessions.enqueue(self.binding, self.event('stop', 'qq:2'))
        result = self.sessions.resolve(sid, {'handled_input_ids': [item['input_id']]})
        self.assertEqual(result['pending'], [later['input_id']])
        with self.assertRaises(SessionFailure):
            self.sessions.resolve(sid, {'handled_input_ids': [later['input_id']]})

    def test_retired_session_cannot_apply_late_callback(self):
        self.sessions.retire(self.scope)
        replacement = self.sessions.binding(self.scope)
        self.assertNotEqual(replacement['session_id'], self.binding['session_id'])
        with self.assertRaises(SessionFailure):
            self.sessions.apply_event(self.binding['session_id'], dict(seq=0, type='turn/start', data={}))


if __name__ == '__main__':
    unittest.main()
