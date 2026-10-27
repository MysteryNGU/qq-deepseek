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
        self.assertTrue(self.call(tool='pwsh')['ok'])
        other = Scope(self.f.scope.agent_id,'qq:private:3','1')
        binding = self.f.role.sessions.binding(other)
        denied = self.call('other',tool='pwsh',root=binding,scope=other)
        self.assertEqual(denied['code'],'resource_wait')
        self.assertEqual(self.f.life.repo._one('SELECT COUNT(*) AS n FROM role_operations')['n'],1)
        self.assertEqual(self.f.life.repo._one("SELECT state FROM role_wakes WHERE scope_id=?",(other.scope_id,))['state'],'waiting_resource')
        self.assertIn('items',self.f.role.read(other,dict(kind='intents')))
        slot = self.i.get(self.i.slot_key())
        self.assertEqual(len(slot['members']),1)

    def test_R03_same_run_cannot_start_two_tools(self):
        self.call('one',tool='pwsh')
        self.assertEqual(self.call('two',tool='pwsh')['code'],'resource_wait')
        self.result('one')
        self.assertTrue(self.call('two',tool='pwsh')['ok'])

    def test_G01_guard_survives_new_call_ids_and_restart(self):
        guard = self.suspend()
        self.assertEqual((guard['count'],guard['phase']),(3,'suspended'))
        self.f.role.initiative = self.i = Initiative(self.f.role)
        with self.assertRaises(SessionFailure) as failure:
            self.call('new-id')
        self.assertEqual(failure.exception.code,'retry_suspended')
        self.assertTrue(self.call('other-target',arguments=dict(path='unrelated.txt'))['ok'])

    def test_saved_read_failure_clears_a_phantom_async_hold_without_replaying(self):
        self.call('old-read')
        op = self.f.life.repo._one('SELECT * FROM role_operations WHERE id=?',
            (stable_id('roleop',self.root['session_id'],'old-read'),))
        data = dict(message=dict(toolCallId='old-read',isError=True,
            content=[dict(type='text',text=canonical(dict(ok=False,job_id='observed-only',code='ENOENT')))]))
        with self.f.life.repo.tx():
            self.f.life.repo._exec("UPDATE role_operations SET status='unknown',result=? WHERE id=?",(canonical(data),op['id']))
            metadata = self.i.get('initiative:operation:'+op['id'])
            self.i.put('initiative:operation:'+op['id'],dict(metadata,awaiting_async=True))
            self.i.record_result(op['id'],'old-bad-classification',unknown=True)
        self.assertEqual(self.i.guards(self.f.scope)[0]['phase'],'outcome_unknown')
        self.assertEqual(self.i.reconcile_read_results(),1)
        self.assertFalse(self.i.get('initiative:operation:'+op['id'])['awaiting_async'])
        self.assertEqual(self.i.guards(self.f.scope)[0]['phase'],'active')
        self.assertTrue(self.call('next-read')['ok'])

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
        self.assertEqual(self.call('root-extra',tool='pwsh')['code'],'resource_wait')
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
        self.call(tool='pwsh')
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
        self.call(tool='pwsh')
        second = Scope(self.f.scope.agent_id,'qq:private:3','1')
        third = Scope(self.f.scope.agent_id,'qq:private:4','1')
        for scope in (second,third):
            self.call(scope.conversation_id,tool='pwsh',root=self.f.role.sessions.binding(scope),scope=scope)
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
        self.call(tool='pwsh')
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
        self.call(tool='pwsh')
        self.f.life.forget(self.f.scope,memory_ids=[note['memory_id']])
        slot=self.i.get(self.i.slot_key())
        self.assertTrue(slot['members'])
        self.assertEqual(slot['phase'],'outcome_unknown')
        self.assertFalse(self.i.guards(self.f.scope))
        self.assertNotIn('arguments',self.i.get('initiative:operation:'+next(iter(slot['members']))))

    def aborted(self,call_id='scan',code='ABORTED',*,reconcile=False):
        op=self.f.life.repo._one('SELECT * FROM role_operations WHERE id=?',
            (stable_id('roleop',self.root['session_id'],call_id),))
        data=dict(error=dict(name='AbortError',code=code),message=dict(toolCallId=call_id,isError=True,
            content=[dict(type='text',text='Error: tool call aborted')]))
        with self.f.life.repo.tx():
            self.f.life.repo._exec("UPDATE role_operations SET status='unknown',result=? WHERE id=?",(canonical(data),op['id']))
            if reconcile:
                self.i.record_result(op['id'],'old-result',unknown=True)
                self.i.finish_member(op['id'],terminal=False)
                self.i=Initiative(self.f.role)
                self.f.role.initiative=self.i
                self.i.reconcile()
            else:
                status=self.i.observe_tool(op,dict(seq=50,data=data))
                self.f.life.repo._exec('UPDATE role_operations SET status=? WHERE id=?',(status,op['id']))
        return self.f.life.repo._one('SELECT * FROM role_operations WHERE id=?',(op['id'],))

    def test_unknown_execution_allows_readonly_diagnostics_without_releasing_owner(self):
        self.call('scan',tool='pwsh',arguments=dict(command='Get-ChildItem -Recurse'))
        self.result('scan',error=True,code='TIMEOUT')
        before=self.i.get(self.i.slot_key())
        for tool in ('glob','read','grep'):
            self.assertTrue(self.call(tool,tool=tool)['ok'])
        self.assertEqual(self.i.get(self.i.slot_key()),before)
        self.assertEqual(self.call('new-work',tool='pwsh',arguments=dict(command='different'))['code'],'resource_wait')

    def test_aborted_shell_releases_execution_but_keeps_effect_unknown(self):
        args=dict(command='Get-ChildItem -Recurse')
        self.call('scan',tool='pwsh',arguments=args)
        op=self.aborted()
        self.assertEqual(op['status'],'unknown')
        self.assertFalse(self.i.get(self.i.slot_key())['members'])
        activity=self.f.role.continuity.activities(self.f.scope,ref=op['id'])[0]
        self.assertEqual((activity['phase'],activity['execution_state']),('outcome_unknown','settled'))
        with self.assertRaises(SessionFailure) as failure:
            self.call('replay',tool='pwsh',arguments=args)
        self.assertEqual(failure.exception.code,'outcome_unknown')
        self.assertTrue(self.call('different',tool='pwsh',arguments=dict(command='Get-Item bot_console.py'))['ok'])

    def test_before_dispatch_abort_has_no_effect_and_can_be_reselected(self):
        self.call('scan',tool='write_file')
        op=self.aborted(code='ABORTED_BEFORE_DISPATCH')
        self.assertEqual(op['status'],'cancelled')
        self.assertFalse(self.i.get(self.i.slot_key())['members'])
        self.assertTrue(self.call('retry',tool='write_file')['ok'])

    def test_restart_reconciles_saved_abort_without_replaying_command(self):
        for code in ('ABORTED','ABORTED_BEFORE_DISPATCH'):
            with self.subTest(code=code):
                self.call(code,tool='pwsh',arguments=dict(command=code))
                op=self.aborted(code,code,reconcile=True)
                self.assertFalse(self.i.get(self.i.slot_key())['members'])
                self.assertEqual(op['status'],'unknown' if code=='ABORTED' else 'cancelled')

    def test_unregistered_plugin_abort_does_not_claim_background_work_stopped(self):
        self.call('scan',tool='plugin_async')
        self.aborted()
        self.assertTrue(self.i.get(self.i.slot_key())['members'])

    def test_verified_execution_exit_frees_lane_without_claiming_success(self):
        self.call('old',tool='pwsh',arguments=dict(command='synthetic side effect'))
        op=stable_id('roleop',self.root['session_id'],'old')
        with self.f.life.repo.tx():
            self.f.life.repo._exec("UPDATE role_operations SET status='unknown' WHERE id=?",(op,))
            self.i.record_result(op,'lost-receipt',unknown=True)
            metadata=self.i.get('initiative:operation:'+op)
            self.i.put('initiative:operation:'+op,dict(metadata,execution_state='settled',
                execution_receipt_ref='verified-worker-tree-exit'))
            self.i.reconcile()
        self.assertFalse(self.i.get(self.i.slot_key())['members'])
        self.assertEqual(self.f.life.repo._one('SELECT status FROM role_operations WHERE id=?',(op,))['status'],'unknown')
        with self.assertRaises(SessionFailure):
            self.call('repeat',tool='pwsh',arguments=dict(command='synthetic side effect'))

    def test_error_text_is_not_runtime_cancellation_evidence(self):
        self.call('scan',tool='pwsh')
        self.result('scan',error=True,code='ABORTED')
        with self.f.life.repo.tx():
            self.i.reconcile()
        self.assertTrue(self.i.get(self.i.slot_key())['members'])

    def file_refusal(self,call_id,code='FS_NOT_OBSERVED',name='FsError'):
        return dict(error=dict(name=name,code=code),message=dict(toolCallId=call_id,isError=True,
            content=[dict(type='text',text='synthetic file refusal')]))

    def test_native_file_preflight_refusals_release_lane_with_and_without_continuity(self):
        for enabled in (True,False):
            for tool in ('write','edit'):
                for code in ('FS_NOT_OBSERVED','FS_STALE_VERSION'):
                    with self.subTest(enabled=enabled,tool=tool,code=code):
                        self.f.cfg.continuity_enabled=True
                        call_id=tool+code+str(enabled)
                        self.call(call_id,tool=tool,arguments=dict(file_path=call_id+'.txt'))
                        self.f.cfg.continuity_enabled=enabled
                        op_id=stable_id('roleop',self.root['session_id'],call_id)
                        op=self.f.life.repo._one('SELECT * FROM role_operations WHERE id=?',(op_id,))
                        self.assertEqual(self.i.untracked_tool_status(op,self.file_refusal(call_id,code)),'failed')
                        self.f.role._observe_result(self.root['session_id'],dict(type='tool/result',seq=51,
                            data=self.file_refusal(call_id,code)))
                        op=self.f.life.repo._one('SELECT * FROM role_operations WHERE id=?',(op_id,))
                        self.assertEqual(op['status'],'failed')
                        self.assertFalse((self.i.get(self.i.slot_key()) or {}).get('members'))
                        if enabled:
                            self.assertEqual(self.i.get('initiative:operation:'+op_id)['execution_state'],'settled')

    def test_saved_file_refusal_repairs_slot_and_guard_once_without_replay(self):
        self.call('old-write',tool='write')
        op_id=stable_id('roleop',self.root['session_id'],'old-write')
        with self.f.life.repo.tx():
            self.f.life.repo._exec("UPDATE role_operations SET status='unknown',result=? WHERE id=?",
                (canonical(self.file_refusal('old-write')),op_id))
            self.i.record_result(op_id,'old-unknown-file-result',unknown=True)
            self.i.finish_member(op_id,terminal=False)
            self.i.reconcile()
        self.assertEqual(self.f.life.repo._one('SELECT status FROM role_operations WHERE id=?',(op_id,))['status'],'failed')
        self.assertFalse(self.i.get(self.i.slot_key())['members'])
        self.assertEqual(self.i.guards(self.f.scope)[0]['phase'],'active')
        snapshot=self.i.get('initiative:operation:'+op_id)
        self.assertEqual(self.i.reconcile_read_results(),0)
        self.assertEqual(self.i.get('initiative:operation:'+op_id),snapshot)
        self.assertTrue(self.call('different-work',tool='pwsh')['ok'])

    def test_file_refusal_requires_native_tool_error_type_and_matching_call(self):
        cases=[('plugin_async','FsError','refusal'),('write','Error','refusal'),('edit','FsError','another-call')]
        for n,(tool,name,receipt_call) in enumerate(cases):
            with self.subTest(tool=tool,name=name,receipt_call=receipt_call):
                # Each unresolved owner remains protected, so use a fresh isolated fixture.
                f=Fixture()
                try:
                    f.cfg.continuity_enabled=True
                    f.role.continuity_protocol=1
                    f.task()
                    binding=f.role.sessions.require(f.binding['session_id'])
                    f.role.before_tool(binding,f.scope,dict(tool=tool,call_id='refusal',
                        session_id=binding['session_id'],arguments=dict(file_path='x.txt')))
                    op=f.life.repo._one('SELECT * FROM role_operations')
                    data=self.file_refusal(receipt_call,name=name)
                    with f.life.repo.tx():
                        status=f.role.initiative.observe_tool(op,dict(seq=n,data=data))
                        f.life.repo._exec("UPDATE role_operations SET status=?,result=? WHERE id=?",(status,canonical(data),op['id']))
                        f.role.initiative.reconcile()
                    self.assertEqual(status,'unknown')
                    self.assertTrue(f.role.initiative.get(f.role.initiative.slot_key())['members'])
                finally:
                    f.close()

    def test_readonly_exception_does_not_bypass_public_scope_or_failure_guard(self):
        other=Scope(self.f.scope.agent_id,'qq:private:3','3')
        self.assertEqual(self.call('public',root=self.f.role.sessions.binding(other),scope=other)['code'],'scope_mismatch')
        self.suspend()
        with self.assertRaises(SessionFailure) as failure:
            self.call('again')
        self.assertEqual(failure.exception.code,'retry_suspended')


if __name__=='__main__':
    unittest.main()
