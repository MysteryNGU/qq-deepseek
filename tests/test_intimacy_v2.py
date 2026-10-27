"""Protocol-2 consent, boundaries and private numerical regressions; isolated DBs."""
import json
from pathlib import Path
import tempfile
import unittest

from life.agent_session import SessionFailure
from life.experience import ExperienceConfig, ExperienceSimulation
from life.runtime import LifeRuntime


class IntimacyV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.at=1000.
        self.path=Path(self.tmp.name)/'life.db'
        self.life=LifeRuntime(self.path,clock=lambda:self.at)
        self.addCleanup(lambda:self.life.close())
        self.scope=self.life.scope('qq:private:1','1')
        self.number=0
        self.source('本次成年亲密情境，愿意开启')

    def source(self,text,**kwargs):
        self.event,_=self.life.ingest_message(self.scope,text,**kwargs)
        return self.event

    def state(self):
        return self.life.experience.read(self.scope)

    def proposal(self,action,**kwargs):
        return dict(experience_protocol=2,experience_domain='adult_intimate',action=action,
            base_revision=self.state()['state_revision'],sources=[self.event.id],subject_actor_id='1',**kwargs)

    def submit(self,action,**kwargs):
        self.number+=1
        return self.life.experience.submit(self.scope,self.proposal(action,**kwargs),'v2:'+str(self.number))

    def enter(self):
        return self.submit('enter',adult_participants=True,context_confirmed=True,willingness='interested')

    def impulse(self,delta,**kwargs):
        return self.submit('appraise_input',direction='increase' if delta>0 else 'decrease' if delta<0 else 'unchanged',
            effects={'arousal':delta} if delta else {},confidence=1,reason='本条实际语义',**kwargs)

    def request(self):
        self.enter()
        for i,delta in enumerate((.25,.25,.1)):
            self.source('独立实际输入'+str(i));self.impulse(delta)
        return self.state()['permission_request']['request_id']

    def delivered(self,request_id):
        service=self.life.experience
        service.bind_request_expression(self.scope,request_id,'question:'+request_id)
        service.request_delivered(self.scope,request_id,'question:'+request_id)

    def response(self,request_id,text='可以',decision='allow'):
        event=self.source(text)
        return self.submit('permission.respond',request_id=request_id,decision=decision,evidence_ref=event.id)

    def idle_binding(self):
        self.enter()
        self.source('结束亲密场景')
        self.at+=120
        self.life.experience.advance()
        self.assertEqual(self.state()['phase'],'idle')

    def test_threshold_only_requests_and_does_not_self_answer(self):
        request=self.request()
        self.assertEqual(self.state()['peak_count'],0)
        self.assertEqual(self.state()['phase'],'awaiting_permission')
        self.at+=60;self.life.experience.advance()
        self.assertEqual(self.state()['permission_request']['request_id'],request)
        self.assertEqual(len(self.state()['transitions']),1)
        with self.assertRaises(SessionFailure):self.response(request)

    def test_confirmed_single_permission_atomically_escalates(self):
        request=self.request();self.delivered(request)
        self.source('可以')
        p=self.proposal('permission.respond',request_id=request,decision='allow',evidence_ref=self.event.id)
        result=self.life.experience.submit(self.scope,p,'allow')
        state=self.state()
        self.assertEqual(state['peak_count'],1)
        self.assertEqual(state['phase'],'building')
        self.assertAlmostEqual(state['arousal'],.24)
        self.assertAlmostEqual(state['sensitivity'],1.06)
        self.assertAlmostEqual(state['current_threshold'],.565)
        self.assertAlmostEqual(state['round_interval_seconds'],108)
        self.assertEqual(self.life.experience.submit(self.scope,p,'allow'),dict(result,duplicate=True))
        with self.assertRaises(SessionFailure):self.response(request)
        self.assertEqual(self.state()['peak_count'],1)

    def test_silence_times_out_once_and_never_reasks(self):
        request=self.request();self.delivered(request)
        self.at+=90;self.life.experience.advance()
        self.assertIsNone(self.state()['permission_request'])
        self.assertEqual(self.state()['denied_count'],1)
        self.at+=3600;self.life.experience.advance()
        self.assertEqual(self.state()['denied_count'],1)
        self.assertEqual(self.state()['peak_count'],0)
        self.source('普通插话');self.impulse(.25)
        self.assertIsNone(self.state()['permission_request'])
        with self.assertRaises(SessionFailure):self.response(request)

    def test_pending_floor_does_not_override_stop(self):
        self.request()
        self.at+=3600;self.life.experience.advance()
        self.assertAlmostEqual(self.state()['arousal'],.35)
        self.source('停止')
        self.assertEqual(self.state()['phase'],'idle')
        self.assertEqual(self.state()['arousal'],0)
        self.assertTrue(self.state()['stop_latched'])
        self.at+=3600;self.life.experience.advance()
        self.assertEqual(self.state()['arousal'],0)

    def test_stop_same_instant_as_allow_wins_even_with_refreshed_revision(self):
        request=self.request();self.delivered(request)
        self.source('可以')
        p=self.proposal('permission.respond',request_id=request,decision='allow',evidence_ref=self.event.id)
        self.source('停止')
        p['base_revision']=self.state()['state_revision']
        with self.assertRaises(SessionFailure):self.life.experience.submit(self.scope,p,'late')
        self.assertEqual(self.state()['peak_count'],0)

    def test_quoted_inferred_negative_and_self_answers_cannot_allow(self):
        request=self.request();self.delivered(request)
        for text in ('他应该会同意','“可以”','如果可以就继续','不同意','普通插话'):
            with self.subTest(text=text),self.assertRaises(SessionFailure):self.response(request,text)
        self.source('可以',role='assistant')
        with self.assertRaises(SessionFailure):
            self.submit('permission.respond',request_id=request,decision='allow',evidence_ref=self.event.id)
        self.assertEqual(self.state()['peak_count'],0)

    def test_uncertain_does_not_allow_and_exact_expiry_is_late(self):
        request=self.request();self.delivered(request)
        self.response(request,'可能吧','uncertain')
        self.assertEqual(self.state()['phase'],'awaiting_permission')
        self.at+=90
        with self.assertRaises(SessionFailure):self.response(request)
        self.life.experience.advance()
        self.assertEqual(self.state()['peak_count'],0)

    def test_short_answer_after_another_delivered_question_is_ambiguous(self):
        request=self.request();self.delivered(request)
        self.life.ingest(self.scope,'delivered_segment','self_output','另一个问题？',role='assistant')
        with self.assertRaises(SessionFailure):self.response(request,'好')
        self.assertEqual(self.state()['peak_count'],0)
        self.response(request,'同意本次请求')
        self.assertEqual(self.state()['peak_count'],1)

    def test_denial_requires_new_intent_then_rearm_then_new_input(self):
        request=self.request();self.delivered(request)
        self.response(request,'不可以','deny')
        self.assertEqual(self.state()['denied_count'],1)
        self.at+=900;self.life.experience.advance()
        self.source('现在愿意继续');self.impulse(0,willingness='interested')
        self.assertIsNone(self.state()['permission_request'])
        self.source('新的实际推进');self.impulse(.15)
        self.assertIsNotNone(self.state()['permission_request'])
        request2=self.state()['permission_request']['request_id'];self.delivered(request2)
        before=self.state()['arousal']
        self.response(request2)
        self.assertAlmostEqual(self.state()['arousal'],before*.4**1.25)
        self.assertEqual(self.state()['denied_count'],0)

    def test_settling_monotone_until_deadline_then_resets(self):
        self.enter();self.impulse(.2)
        self.source('结束亲密场景')
        state=self.state();deadline=state['settle_anchor']['deadline']
        self.assertEqual(state['phase'],'settling')
        self.at+=30;self.life.experience.advance()
        self.assertGreater(self.state()['arousal'],0)
        self.assertGreater(self.state()['momentary_satiety'],state['momentary_satiety'])
        self.at=deadline;self.life.experience.advance()
        self.assertEqual(self.state()['phase'],'idle')
        self.assertEqual(self.state()['momentary_satiety'],0)
        self.assertEqual(self.state()['sensitivity'],1)

    def test_private_cannot_bypass_shared_permission(self):
        self.request()
        with self.assertRaises(SessionFailure):self.submit('private.begin')
        self.assertEqual(self.state()['phase'],'awaiting_permission')

    def test_private_relief_growth_idempotency_and_no_shared_records(self):
        self.idle_binding()
        prior=len(self.state()['transitions'])
        result=self.submit('private.begin')
        p=self.proposal('private.end',run_id=result['run_id'],outcome='completed')
        self.life.experience.submit(self.scope,p,'private-complete')
        local=self.life.experience.private.read_local(self.scope)
        debt=local['relief_debt']
        self.assertGreater(debt,0)
        self.assertAlmostEqual(local['rate_gain'],.05)
        self.assertAlmostEqual(local['sensitivity_gain'],.02)
        self.life.experience.submit(self.scope,p,'private-complete')
        self.assertAlmostEqual(self.life.experience.private.read_local(self.scope)['rate_gain'],.05)
        self.assertAlmostEqual(self.life.experience.private.read_local(self.scope)['sensitivity_gain'],.02)
        self.at+=1800
        self.assertAlmostEqual(self.life.experience.private.read_local(self.scope)['relief_debt'],debt/2)
        self.assertEqual(len(self.state()['transitions']),prior)
        shared=json.dumps(self.state())
        self.assertNotIn('relief_debt',shared)
        self.assertNotIn('rate_gain',shared)
        self.assertNotIn(result['run_id'],shared)
        self.assertFalse(self.life.experience.read(self.life.scope('qq:private:2','2'))['available'])

    def test_stop_aborts_private_without_awarding_gain(self):
        self.idle_binding();run=self.submit('private.begin')['run_id']
        self.source('停止')
        with self.assertRaises(SessionFailure):self.submit('private.end',run_id=run,outcome='completed')
        self.assertEqual(self.life.experience.private.read_local(self.scope)['rate_gain'],0)
        self.assertFalse(self.life.experience.private.running(self.scope))
        with self.assertRaises(SessionFailure):self.submit('private.begin')

    def test_private_abort_gains_nothing_and_rate_caps(self):
        self.idle_binding()
        run=self.submit('private.begin')['run_id']
        self.submit('private.end',run_id=run,outcome='aborted')
        self.assertEqual(self.life.experience.private.read_local(self.scope)['rate_gain'],0)
        self.assertEqual(self.life.experience.private.read_local(self.scope)['sensitivity_gain'],0)
        for _ in range(23):
            run=self.submit('private.begin')['run_id'];self.submit('private.end',run_id=run,outcome='completed')
        self.assertEqual(self.life.experience.private.read_local(self.scope)['rate_gain'],1)
        self.assertAlmostEqual(self.life.experience.private.read_local(self.scope)['sensitivity_gain'],.46)

    def test_private_sensitivity_survives_episodes_settlement_stop_and_restart(self):
        self.idle_binding()
        run=self.submit('private.begin')['run_id']
        self.submit('private.end',run_id=run,outcome='completed')
        self.assertAlmostEqual(self.state()['sensitivity'],1.02)
        self.source('新的明确成年开启');self.enter()
        self.source('新的正向输入')
        self.assertAlmostEqual(self.impulse(.1)['actual_delta']['arousal'],.102)
        self.source('结束亲密场景');self.at+=120;self.life.experience.advance()
        self.assertAlmostEqual(self.state()['sensitivity'],1.02)
        self.source('停止')
        self.assertAlmostEqual(self.state()['sensitivity'],1.02)
        self.assertEqual(self.state()['arousal'],0)
        self.life.close();self.at+=86400
        self.life=LifeRuntime(self.path,clock=lambda:self.at)
        self.assertAlmostEqual(self.state()['sensitivity'],1.02)
        self.source('重新明确开启');self.enter()
        self.source('重新开启后的输入')
        self.assertAlmostEqual(self.impulse(.1)['actual_delta']['arousal'],.102)

    def test_private_sensitivity_applies_to_authorized_continuous_input(self):
        self.idle_binding()
        run=self.submit('private.begin')['run_id'];self.submit('private.end',run_id=run,outcome='completed')
        self.source('新的明确成年开启');self.enter()
        self.source('明确的单次输入方案')
        self.submit('start',profile_confirmed=True,input_profile=dict(method='确认方式',intensity=1.,valence=.5,rhythm='continuous'))
        self.at+=1;self.life.experience.advance()
        from life.experience_math import arousal_step
        self.assertAlmostEqual(self.state()['arousal'],arousal_step(0.,1.,.003*.5*1.02,1800.))

    def test_private_sensitivity_caps_and_is_erased_only_by_explicit_binding_reset(self):
        self.idle_binding()
        for _ in range(30):
            run=self.submit('private.begin')['run_id'];self.submit('private.end',run_id=run,outcome='completed')
        self.assertAlmostEqual(self.state()['sensitivity'],1.5)
        self.assertAlmostEqual(self.life.experience.private.read_local(self.scope)['sensitivity_gain'],.5)
        self.at+=30*86400
        self.assertAlmostEqual(self.state()['sensitivity'],1.5)
        self.life.reset_scope(self.scope)
        self.assertAlmostEqual(self.state()['sensitivity'],1.)

    def test_lowered_config_caps_immediately_limit_effective_rate_and_sensitivity(self):
        self.idle_binding()
        for _ in range(2):
            run=self.submit('private.begin')['run_id'];self.submit('private.end',run_id=run,outcome='completed')
        before=self.life.experience.private.read_local(self.scope)
        self.life.experience=ExperienceSimulation(self.life,dict(rate_gain_cap=.05,private_sensitivity_gain_cap=.01))
        self.assertAlmostEqual(self.state()['sensitivity'],1.01)
        self.at+=3600
        state=self.life.experience.private.read_local(self.scope)
        import math
        self.assertAlmostEqual(state['tension'],1.-(1.-before['tension'])*math.exp(-.05*1.05))
        self.assertAlmostEqual(state['rate_gain'],.1)
        self.assertAlmostEqual(state['sensitivity_gain'],.04)

    def test_legacy_private_snapshot_does_not_invent_sensitivity_from_rate(self):
        self.idle_binding()
        with self.life.repo.tx():
            state=self.life.experience.private._load(self.scope)
            state.pop('sensitivity_gain');state['rate_gain']=.8
            self.life.experience.private._save(self.scope,state)
        self.assertEqual(self.life.experience.private.read_local(self.scope)['sensitivity_gain'],0.)
        self.assertEqual(self.state()['sensitivity'],1.)
        run=self.submit('private.begin')['run_id'];self.submit('private.end',run_id=run,outcome='completed')
        self.assertAlmostEqual(self.state()['sensitivity'],1.02)

    def test_sensitivity_gain_is_not_reapplied_by_math_revision(self):
        self.idle_binding()
        run=self.submit('private.begin')['run_id'];self.submit('private.end',run_id=run,outcome='completed')
        self.source('新的成年开启');self.enter()
        self.source('当次实际输入');old=self.impulse(.2)
        fixed=self.submit('appraise_input',direction='increase',effects={'arousal':.1},confidence=1.,
            reason='修订本条实际语义',supersedes_call_key=old['call_key'])
        self.assertAlmostEqual(fixed['after']['arousal'],.102)
        self.assertAlmostEqual(fixed['after']['sensitivity'],1.02)
        self.assertAlmostEqual(self.life.experience.private.read_local(self.scope)['sensitivity_gain'],.02)

    def test_restart_cancels_pending_permission_without_peak_or_denial(self):
        request=self.request();self.delivered(request)
        self.life.close();self.at+=100
        self.life=LifeRuntime(self.path,clock=lambda:self.at)
        self.assertIsNone(self.state()['permission_request'])
        self.assertEqual(self.state()['peak_count'],0)
        self.assertEqual(self.state()['denied_count'],0)
        with self.assertRaises(SessionFailure):self.response(request)

    def test_restart_aborts_private_without_gain_and_preserves_completed_gain(self):
        self.idle_binding();run=self.submit('private.begin')['run_id']
        self.submit('private.end',run_id=run,outcome='completed')
        self.submit('private.begin')
        self.life.close();self.at+=3600
        self.life=LifeRuntime(self.path,clock=lambda:self.at)
        self.assertFalse(self.life.experience.private.running(self.scope))
        self.assertAlmostEqual(self.life.experience.private.read_local(self.scope)['rate_gain'],.05)

    def test_invalid_config_falls_back_as_a_group_and_logs_reason(self):
        for raw in ({'rate_gain_cap':float('inf')},{'peak_keep_ratio':1},{'threshold':.2},
                    {'round_interval_k':True},{'private_enabled':'yes'},
                    {'private_sensitivity_gain_per_run':True},{'private_sensitivity_gain_cap':.51}):
            with self.subTest(raw=raw):
                instance=ExperienceSimulation(self.life,raw)
                self.assertEqual(instance.config,ExperienceConfig())
                self.assertIsNotNone(instance.config_error)

    def test_corrupt_state_fails_closed_and_recovery_preserves_fault_record(self):
        self.enter()
        with self.life.repo.tx():
            self.life.repo._exec("UPDATE experience_state SET payload='not json'")
        self.life.experience.recover()
        self.assertEqual(self.state()['phase'],'idle')
        self.assertTrue(self.state()['stop_latched'])

    def test_old_protocol_cannot_fall_back_to_auto_peak(self):
        p=self.proposal('enter',adult_participants=True,context_confirmed=True,willingness='interested')
        p['experience_protocol']=1
        with self.assertRaises(SessionFailure):self.life.experience.submit(self.scope,p,'old')

    def test_schema_one_snapshot_is_quarantined_without_importing_consent(self):
        self.enter()
        with self.life.repo.tx():
            old=self.life.experience._load()[1]
            old.update(rule_version='adult-intimate-1',arousal=.9,phase='peak')
            self.life.repo._exec('UPDATE experience_state SET payload=?',(json.dumps(old),))
            self.life.repo._exec("UPDATE meta SET v='1' WHERE k=?",('experience_schema:'+self.life.agent_id,))
        self.life.close();self.life=LifeRuntime(self.path,clock=lambda:self.at)
        self.assertEqual(self.state()['arousal'],0)
        self.assertEqual(self.state()['phase'],'idle')
        self.assertTrue(self.state()['stop_latched'])
        self.assertEqual(self.life.repo._one('SELECT v FROM meta WHERE k=?',('experience_schema:'+self.life.agent_id,))['v'],'2')
        self.assertEqual(self.life.repo._one('SELECT COUNT(*) AS n FROM experience_faults')['n'],1)

    def test_shared_and_private_state_erased_with_binding(self):
        self.idle_binding();run=self.submit('private.begin')['run_id']
        self.submit('private.end',run_id=run,outcome='completed')
        self.life.reset_scope(self.scope)
        for table in ('experience_private_state','experience_private_journal','experience_state','experience_transitions'):
            self.assertEqual(self.life.repo._one('SELECT COUNT(*) AS n FROM '+table)['n'],0)

    def test_r5_retains_desire_and_does_not_start_shared_scene(self):
        self.idle_binding();self.at+=60000
        candidate=self.life.experience.private.candidate(self.scope)
        self.assertTrue(candidate['desire_present']);self.assertTrue(candidate['eligible'])
        self.assertEqual(self.state()['mode'],'none')
        self.submit('initiation.select',candidate_id=candidate['candidate_id'],decision='act')
        self.life.experience.private.invitation_delivered(self.scope,candidate['candidate_id'])
        self.at+=60000
        candidate=self.life.experience.private.candidate(self.scope)
        self.assertTrue(candidate['desire_present']);self.assertFalse(candidate['eligible'])
        self.assertEqual(self.state()['mode'],'none')

    def test_many_successful_rounds_reach_caps_without_auto_peak(self):
        self.enter()
        for round_number in range(20):
            self.at+=125;self.life.experience.advance()
            for _ in range(5):
                if self.state()['permission_request']:break
                self.source('新的当次正向输入');self.impulse(.25)
            request=self.state()['permission_request']['request_id']
            self.assertEqual(self.state()['peak_count'],round_number)
            self.delivered(request);self.response(request)
        state=self.state()
        self.assertEqual(state['peak_count'],20)
        self.assertEqual(state['sensitivity'],1.5)
        self.assertEqual(state['current_threshold'],.75)
        self.assertEqual(state['round_interval_seconds'],30)

    def test_continuous_authorization_is_not_inferred_from_a_single_profile(self):
        self.enter()
        self.source('本次采用已确认方式')
        fields=dict(profile_confirmed=True,input_profile=dict(method='确认方式',intensity=1,valence=.5,rhythm='continuous'))
        with self.assertRaises(SessionFailure):self.submit('start',authorization_mode='until_cancel',**fields)
        self.submit('start',**fields)
        self.at+=100;self.life.experience.advance()
        self.assertLess(self.state()['arousal'],.01)
        self.assertEqual(self.state()['peak_count'],0)

    def test_failed_hot_configuration_retains_last_verified_group(self):
        self.life.experience=ExperienceSimulation(self.life,dict(sensitivity_gain=.04))
        self.enter()
        replacement=ExperienceSimulation(self.life,dict(sensitivity_gain=float('nan')))
        self.assertEqual(replacement.config.sensitivity_gain,.04)

    def test_private_projection_ignores_other_audience_activation(self):
        self.idle_binding()
        before=self.life.experience.private.read_local(self.scope)['effective_pressure']
        from life.drives import DriveEvidence
        other=self.life.scope('qq:private:2','2')
        event,_=self.life.ingest_message(other,'另一个受众的真实输入')
        evidence=DriveEvidence(id='other-activation',root_id=event.id,dimension='libido',
            kind='activation_change',delta=.2,confidence=1.,sources=[event.id])
        self.life.apply_drive_evidence(other,[evidence],now=self.at)
        self.assertAlmostEqual(self.life.experience.private.read_local(self.scope)['effective_pressure'],before)

    def test_ordinary_drive_replay_does_not_reapply_binding_effects(self):
        self.idle_binding()
        before=self.life.experience.private.read_local(self.scope)
        from life.drives import DriveEvidence
        evidence=DriveEvidence(id='replay',root_id=self.event.id,dimension='libido',
            kind='activation_change',delta=.2,confidence=1.,sources=[self.event.id])
        self.life._replaying=True
        try:self.life.experience.private.apply_evidence(self.scope,evidence,self.at)
        finally:self.life._replaying=False
        after=self.life.experience.private.read_local(self.scope)
        self.assertEqual(after['activation'],before['activation'])
        self.assertEqual(after['rate_gain'],before['rate_gain'])


if __name__=='__main__':
    unittest.main()
