"""Execution ownership and failure recovery with fake work and durable receipts."""
import json
import unittest

from tests.role_fixture import Fixture
from life.agent_session import SessionFailure,canonical
from life.contracts import Scope
from life.initiative import Initiative,request_signature
from life.text import stable_id


class InitiativeTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.cfg.continuity_enabled = True
        self.f.role.continuity_protocol = 1
        self.i = self.f.role.initiative
        self.task = self.f.task()
        self.root = self.f.role.sessions.require(self.f.binding['session_id'])

    def call(self,call_id='read',tool='read_file',arguments=None,root=None,scope=None):
        binding = root or self.root
        return self.f.role.before_tool(binding,scope or self.f.scope,dict(tool=tool,call_id=call_id,
            session_id=binding['session_id'],arguments=arguments or dict(path='synthetic.txt')))

    def result(self,call_id,*,error=False,seq=1,code='ENOENT',tool=None):
        op = self.f.life.repo._one('SELECT * FROM role_operations WHERE id=?',
            (stable_id('roleop',self.root['session_id'],call_id),))
        with self.f.life.repo.tx():
            status = self.i.observe_tool(op,dict(seq=seq,data=dict(message=dict(isError=error,
                content=[dict(type='text',text=canonical(dict(ok=False,code=code)) if error else 'actual result')]))))
            self.f.life.repo._exec('UPDATE role_operations SET status=? WHERE id=?',(status,op['id']))

    def suspend(self):
        for k in range(3):
            self.call(str(k))
            self.result(str(k),error=True,seq=k)
        return self.i.guards(self.f.scope)[0]

    def test_R01_one_global_slot_across_scopes_and_chat_still_readable(self):
        self.assertTrue(self.call()['ok'])
        other = Scope(self.f.scope.agent_id,'qq:private:3','1')
        binding = self.f.role.sessions.binding(other)
        denied = self.call('other',root=binding,scope=other)
        self.assertEqual(denied['code'],'resource_wait')
        self.assertEqual(self.f.life.repo._one('SELECT COUNT(*) AS n FROM role_operations')['n'],1)
        self.assertEqual(self.f.life.repo._one("SELECT state FROM role_wakes WHERE scope_id=?",(other.scope_id,))['state'],'waiting_resource')
        self.assertIn('items',self.f.role.read(other,dict(kind='intents')))
        slot = self.i.get(self.i.slot_key())
        self.assertEqual(len(slot['members']),1)

    def test_R03_same_run_cannot_start_two_tools(self):
        self.call('one')
        self.assertEqual(self.call('two')['code'],'resource_wait')
        self.result('one')
        self.assertTrue(self.call('two')['ok'])

    def test_G01_guard_survives_new_call_ids_and_restart(self):
        guard = self.suspend()
        self.assertEqual((guard['count'],guard['phase']),(3,'suspended'))
        self.f.role.initiative = self.i = Initiative(self.f.role)
        with self.assertRaises(SessionFailure) as failure:
            self.call('new-id')
        self.assertEqual(failure.exception.code,'retry_suspended')
        self.assertTrue(self.call('other-target',arguments=dict(path='unrelated.txt'))['ok'])

    def test_G02_new_greeting_and_checkpoint_do_not_clear_guard(self):
        guard = self.suspend()
        event,_ = self.f.life.ingest_message(self.f.scope,'你好')
        with self.f.life.repo.tx():
            self.i.new_input(event)
            self.f.role.intent(self.f.scope,dict(ref=self.task['ref'],base_revision=self.task['revision'],
                checkpoint='换个办法吧',sources=[event.id]),self.root)
        self.root = self.f.role.sessions.require(self.root['session_id'])
        self.assertEqual(self.i.get(guard['ref'])['count'],3)
        with self.assertRaises(SessionFailure):
            self.call('again')

    def test_G02_unknown_write_blocks_immediately_without_replay(self):
        self.call('write',tool='write_file')
        self.result('write',error=True,code='TIMEOUT')
        guard = self.i.guards(self.f.scope)[0]
        self.assertEqual(guard['phase'],'outcome_unknown')
        with self.assertRaises(SessionFailure) as failure:
            self.call('write-again',tool='write_file')
        self.assertEqual(failure.exception.code,'outcome_unknown')
        self.assertTrue(self.i.get(self.i.slot_key())['members'])

    def test_G03_probe_is_exactly_once_and_failure_suspends_immediately(self):
        guard = self.suspend()
        task = self.f.role.tasks.get(self.task['ref'],self.f.scope.scope_id)
        proposal = dict(guard_ref=guard['ref'],base_version=guard['version'],change_note='尝试已确认存在的另一输入',
            changed_source_refs=[],candidate=dict(tool='read_file',arguments=dict(path='other.txt')))
        with self.f.life.repo.tx():
            self.i.request_probe(self.f.scope,task,proposal)
        self.call('probe',arguments=dict(path='other.txt'))
        self.result('probe',error=True,seq=9)
        updated = self.i.get(guard['ref'])
        self.assertEqual(updated['phase'],'suspended')
        proposal.update(base_version=updated['version'])
        proposal['candidate']['arguments']['path']='third.txt'
        with self.assertRaises(SessionFailure):
            with self.f.life.repo.tx():
                self.i.request_probe(self.f.scope,task,proposal)

    def test_G03_random_suffix_and_timestamps_are_not_a_new_method(self):
        self.assertEqual(request_signature('read_file',dict(path='out_aaaaaaaaaaaa.wav',timestamp=1)),
                         request_signature('read_file',dict(path='out_bbbbbbbbbbbb.wav',timestamp=2)))

    def test_R02_delegate_admission_retains_lane_until_terminal(self):
        self.call('spawn',tool='subagent')
        with self.f.life.repo.tx():
            self.f.role._register_delegate('child',self.root['session_id'],self.root,'spawn')
        self.assertEqual(self.call('root-extra')['code'],'resource_wait')
        child = self.f.role.sessions.require('child')
        self.assertTrue(self.call('child-read',root=child)['ok'])
        op_id = stable_id('roleop','child','child-read')
        with self.f.life.repo.tx():
            self.i.finish_member(op_id)
        self.assertTrue(self.i.get(self.i.slot_key())['members'])
        with self.f.life.repo.tx():
            self.i.delegate_finished('child',dict(status='completed'))
        self.assertFalse(self.i.get(self.i.slot_key())['members'])

    def test_delegate_without_parent_call_id_does_not_guess(self):
        self.call('spawn',tool='subagent')
        with self.assertRaises(SessionFailure):
            with self.f.life.repo.tx():
                self.f.role._register_delegate('child',self.root['session_id'],self.root)
        self.assertFalse(self.f.life.repo._one("SELECT 1 FROM agent_sessions WHERE session_id='child'"))

    def test_delegate_creation_and_actual_result_have_separate_evidence(self):
        self.call('spawn',tool='subagent')
        with self.f.life.repo.tx():
            self.f.role._register_delegate('child',self.root['session_id'],self.root,'spawn','actual-run')
        self.result('spawn')
        activity=self.f.role.continuity.activities(self.f.scope)[0]
        self.assertEqual(activity['phase'],'running')
        self.f.role._delegate_finished(dict(parentSessionId=self.root['session_id'],childSessionId='child',
            runId='actual-run',status='completed',lastAssistantMessage=[dict(type='text',text='Actual bounded result')]))
        activity=self.f.role.continuity.activities(self.f.scope)[0]
        self.assertEqual(activity['phase'],'result_available')
        self.assertFalse(activity['goal_verified'])
        result=self.f.life.repo.get_event(activity['result_refs'][0])
        self.assertEqual(result.payload['child_session_id'],'child')

    def test_R04_reconcile_keeps_unverified_owner_and_expired_gpu_lease(self):
        self.call()
        with self.f.life.repo.tx():
            self.i.reconcile()
        self.assertEqual(self.i.get(self.i.slot_key())['phase'],'outcome_unknown')
        jobs = self.f.life.jobs
        jobs.enqueue(self.f.scope,'test',{},key='a')
        first = jobs.claim('a')
        self.assertTrue(jobs.acquire(first,'gpu:0'))
        with self.f.life.repo.tx():
            self.f.life.repo._exec('UPDATE resource_leases SET until=0')
        jobs.enqueue(self.f.scope,'test',{},key='b')
        self.assertFalse(jobs.acquire(jobs.claim('b'),'gpu:0'))

    def test_R05_waiters_wake_one_at_a_time_and_decline_advances(self):
        self.call()
        second = Scope(self.f.scope.agent_id,'qq:private:3','1')
        third = Scope(self.f.scope.agent_id,'qq:private:4','1')
        for scope in (second,third):
            self.call(scope.conversation_id,root=self.f.role.sessions.binding(scope),scope=scope)
        self.result('read')
        offer = self.i.get('initiative:resource_offer')
        self.assertEqual(offer['scope_id'],second.scope_id)
        self.assertEqual(self.f.life.repo._one("SELECT COUNT(*) AS n FROM role_wakes WHERE state='waiting_resource'")['n'],1)
        with self.f.life.repo.tx():
            self.f.role.attention.finish_resource_offer(second.scope_id)
            self.i.notify_one_waiter()
        self.assertEqual(self.i.get('initiative:resource_offer')['scope_id'],third.scope_id)

    def test_G04_protocol_recovery_suspends_but_real_input_gets_one_opportunity(self):
        for k in range(3):
            with self.f.life.repo.tx():
                allowed = self.i.protocol_result(self.f.scope,self.root['session_id'],'receipt-'+str(k))
        self.assertFalse(allowed)
        self.assertIsNone(self.f.role.schedule(self.f.scope,None,1,'restarted recovery',kind='recovery'))
        wake,_ = self.f.life.ingest(self.f.scope,'runtime_wake','inference','system generated')
        with self.f.life.repo.tx():
            self.i.new_input(wake)
        with self.f.life.repo.tx():
            self.assertFalse(self.i.protocol_result(self.f.scope,self.root['session_id'],'receipt-4'))
        event,_ = self.f.life.ingest_message(self.f.scope,'new actual input')
        with self.f.life.repo.tx():
            self.i.new_input(event)
        key = 'initiative:guard:'+self.f.scope.scope_id+':protocol:'+self.root['session_id']
        self.assertEqual(self.i.get(key)['phase'],'active')
        with self.f.life.repo.tx():
            self.assertFalse(self.i.protocol_result(self.f.scope,self.root['session_id'],'receipt-5'))

    def test_real_file_change_reopens_the_affected_guard_only(self):
        self.suspend()
        (self.f.root/'synthetic.txt').write_text('Actual new file evidence',encoding='utf-8')
        self.assertTrue(self.call('file-now-exists')['ok'])

    def test_legacy_bound_activity_does_not_become_interactive_after_new_chat(self):
        with self.f.life.repo.tx():
            self.f.life.repo._exec('DELETE FROM meta WHERE k=?',('initiative:intent:'+self.task['ref'],))
            event,_=self.f.life.ingest_message(self.f.scope,'你好')
            self.f.role.sessions.enqueue(self.root,event)
            self.f.life.repo._exec("UPDATE agent_inputs SET state='consumed',handled=1")
        self.assertEqual(self.i.attribution(self.f.scope,self.root)['classification'],'autonomous')
        with self.f.life.repo.tx():
            self.f.role.intent(self.f.scope,dict(ref=self.task['ref'],base_revision=self.task['revision'],origin='user',
                sources=[event.id],checkpoint='new wording'),self.root)
        self.root=self.f.role.sessions.require(self.root['session_id'])
        self.assertEqual(self.i.attribution(self.f.scope,self.root)['classification'],'autonomous')

    def test_rest_and_disabled_feature_do_not_release_live_work(self):
        self.call()
        with self.f.life.repo.tx():
            self.f.role.rest(self.f.scope,dict(next_attention=dict(mode='none')),self.root)
        self.assertTrue(self.i.get(self.i.slot_key())['members'])
        self.f.cfg.continuity_enabled=False
        with self.assertRaises(SessionFailure) as failure:
            self.call('cannot-expand')
        self.assertEqual(failure.exception.code,'retry_suspended')
        self.assertTrue(self.i.get(self.i.slot_key())['members'])

    def test_resumed_child_uses_actual_new_activation_and_ignores_late_old_end(self):
        self.call('spawn',tool='subagent')
        with self.f.life.repo.tx():
            self.f.role._register_delegate('child',self.root['session_id'],self.root,'spawn','activation-one')
            self.i.delegate_finished('child',dict(status='completed',runId='activation-one'))
        self.call('resume',tool='send_message',arguments=dict(agent_id='child',message='Continue this actual task'))
        with self.f.life.repo.tx():
            self.f.role._register_delegate('child',self.root['session_id'],self.root,'resume','activation-two')
            self.i.delegate_finished('child',dict(status='completed',runId='activation-one'))
        self.assertIn('delegate:child',self.i.get(self.i.slot_key())['members'])
        with self.f.life.repo.tx():
            self.i.delegate_finished('child',dict(status='completed',runId='activation-two'))
        self.assertFalse(self.i.get(self.i.slot_key())['members'])

    def test_async_acceptance_is_not_a_terminal_and_reconcile_cannot_infer_it(self):
        self.call('async',tool='plugin_async')
        op=self.f.life.repo._one('SELECT * FROM role_operations WHERE tool=\'plugin_async\'')
        with self.f.life.repo.tx():
            status=self.i.observe_tool(op,dict(seq=9,data=dict(message=dict(isError=False,
                content=[dict(type='text',text=canonical(dict(status='accepted',job_id='external-owned-id')))]))))
            self.f.life.repo._exec('UPDATE role_operations SET status=? WHERE id=?',(status,op['id']))
            self.i.reconcile()
        self.assertEqual(status,'unknown')
        self.assertEqual(self.i.get(self.i.slot_key())['phase'],'outcome_unknown')

    def test_forget_erases_guard_bodies_but_preserves_physical_ownership(self):
        note=None
        with self.f.life.repo.tx():
            note=self.f.role.remember(self.f.scope,dict(kind='inference',text='synthetic continuity note',sources=[self.f.event.id],
                continuity=dict(type='thought',action='create')),self.root,call_key='note')
        self.call()
        self.f.life.forget(self.f.scope,memory_ids=[note['memory_id']])
        slot=self.i.get(self.i.slot_key())
        self.assertTrue(slot['members'])
        self.assertEqual(slot['phase'],'outcome_unknown')
        self.assertFalse(self.i.guards(self.f.scope))
        self.assertNotIn('arguments',self.i.get('initiative:operation:'+next(iter(slot['members']))))


if __name__=='__main__':
    unittest.main()
