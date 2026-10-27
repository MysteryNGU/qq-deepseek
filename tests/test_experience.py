"""ADD-08 numeric, persistence and source contracts, with a controlled clock."""
import json
from pathlib import Path
import tempfile
import unittest

from life.agent_session import SessionFailure
from life.experience import METRICS
from life.runtime import LifeConfig, LifeRuntime


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.at = 1000.
        self.path = Path(self.tmp.name)/'life.db'
        self.life = LifeRuntime(self.path, clock=lambda:self.at)
        self.addCleanup(lambda:self.life.close())
        self.scope = self.life.scope('qq:private:1', '1')
        self.counter = 0
        self.source('明确的成年亲密场景')

    def source(self, text):
        self.event, _ = self.life.ingest_message(self.scope, text)
        return self.event

    def state(self):
        return self.life.experience.read(self.scope)

    def proposal(self, action, **kwargs):
        return dict(experience_protocol=2, experience_domain='adult_intimate', action=action,
            base_revision=self.state()['state_revision'], sources=[self.event.id], subject_actor_id='1', **kwargs)

    def submit(self, action, **kwargs):
        self.counter += 1
        return self.life.experience.submit(self.scope, self.proposal(action, **kwargs), 'call:'+str(self.counter))

    def enter(self):
        return self.submit('enter', adult_participants=True, context_confirmed=True, willingness='interested')

    def impulse(self, delta=.1, **kwargs):
        return self.submit('appraise_input', direction='increase' if delta>0 else 'decrease' if delta<0 else 'unchanged',
            effects={'arousal':delta} if delta else {}, reason='此条语言的具体语义', **kwargs)

    def start(self, intensity=1.):
        self.source('一直按已确认方案持续，直到我说停')
        return self.submit('start', profile_confirmed=True,authorization_mode='until_cancel',
            input_profile=dict(method='已确认方案', intensity=intensity, valence=.6, rhythm='continuous'))

    def test_impulse_without_run_positive_negative_unchanged_and_coverage(self):
        self.enter()
        for daily in self.life.drive_view(self.scope).values():
            self.assertFalse({'stimulation','scene_climaxes','climax_threshold'} & set(daily))
        before = self.life.drive_state(self.scope).to_dict()
        positive = self.impulse(.2, confidence=1)
        self.assertAlmostEqual(positive['actual_delta']['arousal'], .2)
        self.assertIsNone(self.state()['run'])
        self.source('当前语言降低体验强度')
        self.assertEqual(self.state()['input_coverage'][0]['status'], 'pending')
        negative = self.impulse(-.1, confidence=1)
        self.assertAlmostEqual(negative['actual_delta']['arousal'], -.1)
        self.source('这条只是一般插话')
        neutral = self.impulse(0)
        self.assertFalse(neutral['applied'])
        self.assertEqual(self.state()['input_coverage'][0]['status'], 'unchanged')
        self.assertEqual(before, self.life.drive_state(self.scope).to_dict())

    def test_zero_missing_low_and_full_confidence(self):
        self.enter()
        for conf, expected in ((0,0), (.2,.02), (1,.1), (None,.05)):
            self.source('新的语义证据')
            receipt = self.impulse(.1, **({} if conf is None else dict(confidence=conf)))
            self.assertAlmostEqual(receipt['actual_delta']['arousal'], expected)
            if conf==0:
                self.assertFalse(receipt['applied'])
                self.assertEqual(receipt['unchanged_reason'], 'zero_confidence')

    def test_non_finite_and_wrong_domain_rejected(self):
        self.enter()
        for field, value in (('confidence', float('nan')), ('confidence', float('inf')), ('confidence', False),
                             ('effects', {'arousal':float('nan')}), ('experience_domain','exploration')):
            p = self.proposal('appraise_input', direction='increase', effects={'arousal':.1}, reason='实际语义')
            p[field] = value
            with self.assertRaises(SessionFailure):
                self.life.experience.submit(self.scope,p,'invalid:'+field+str(value))
        self.assertEqual(self.state()['arousal'], 0)

    def test_ordinary_work_exploration_and_self_output_cannot_enter_or_impulse(self):
        for domain in ('ordinary_chat', 'exploration', 'work'):
            p=self.proposal('enter',adult_participants=True,context_confirmed=True,willingness='interested')
            p['experience_domain']=domain
            with self.assertRaises(SessionFailure):
                self.life.experience.submit(self.scope,p,domain)
        self.enter()
        self.event,_=self.life.ingest_message(self.scope,'自己的表达',role='assistant')
        with self.assertRaises(SessionFailure):
            self.impulse(.1)
        self.assertEqual(self.state()['arousal'],0)

    def test_explicit_context_participant_audience_and_willingness_required(self):
        for change in (dict(adult_participants=False),dict(context_confirmed=False),dict(willingness='not_now'),dict(subject_actor_id='2')):
            p=self.proposal('enter',adult_participants=True,context_confirmed=True,willingness='interested')
            p.update(change)
            with self.assertRaises(SessionFailure):
                self.life.experience.submit(self.scope,p,'enter:'+str(change))
        self.enter()
        other=self.life.scope('qq:private:2','2')
        self.assertFalse(self.life.experience.read(other)['available'])
        self.source('现在不想继续')
        self.submit('appraise_input',direction='unchanged',effects={},willingness='not_now',reason='本次明确没有意愿')
        self.source('新的正向推进')
        with self.assertRaises(SessionFailure):
            self.impulse(.1)

    def test_threshold_exact_crossing_receipt_unique_and_role_read(self):
        self.enter()
        self.impulse(.25, confidence=1)
        self.source('第二条')
        self.impulse(.25, confidence=1)
        self.source('阈值前最后一条')
        p = self.proposal('appraise_input', direction='increase', effects={'arousal':.05}, confidence=1, reason='到达阈值')
        r = self.life.experience.submit(self.scope, p, 'threshold')
        self.assertEqual(len(r['transition_refs']), 1)
        self.assertEqual(self.state()['peak_count'], 0)
        self.assertEqual(self.state()['phase'], 'awaiting_permission')
        self.assertEqual(self.life.experience.submit(self.scope,p,'threshold')['transition_refs'], r['transition_refs'])
        self.assertEqual(len(self.state()['transitions']), 1)
        self.assertEqual(self.state()['transitions'][0]['logical_at'], self.at)
        request=self.state()['permission_request']['request_id']
        self.life.experience.bind_request_expression(self.scope,request,'question')
        self.life.experience.request_delivered(self.scope,request,'question')
        self.source('可以')
        self.submit('permission.respond',request_id=request,decision='allow',evidence_ref=self.event.id)
        self.assertEqual(self.state()['peak_count'],1)
        self.source('恢复期再提交')
        self.impulse(.25, confidence=1)
        self.assertEqual(self.state()['peak_count'], 1)

    def test_continuous_90_minutes_no_idle_stop_or_need_settlement(self):
        self.enter(); self.start()
        daily = self.life.repo.load_state(self.life.state_key('drive', self.scope))
        self.at += 5400
        self.life.experience.advance(self.at)
        s = self.state()
        self.assertTrue(s['run']['desired_active'])
        self.assertEqual(s['run']['execution_state'], 'running')
        self.assertEqual(s['peak_count'], 0)
        self.assertGreater(s['adaptation'], 0)
        self.assertGreater(s['scene_load'], 0)
        self.assertEqual(daily, self.life.repo.load_state(self.life.state_key('drive', self.scope)))
        self.assertLess(self.life.repo._one('SELECT COUNT(*) AS n FROM experience_journal')['n'], 10)

    def test_adjust_and_intermittent_profile_apply_from_actual_boundary(self):
        self.enter(); self.start()
        self.at+=120
        self.life.experience.advance()
        before=self.state()['arousal']
        self.source('调整为较弱的间歇方式')
        run=self.state()['run']['simulation_run_id']
        self.submit('adjust',run_ref=run,profile_confirmed=True,input_profile=dict(method='明确调整方案',intensity=.2,valence=-.3,rhythm='intermittent',on_seconds=5,off_seconds=15))
        self.assertAlmostEqual(self.state()['arousal'],before)
        self.assertEqual(self.state()['run']['input_revision'],2)
        self.at+=120
        self.life.experience.advance()
        self.assertLess(self.state()['arousal'],before+.1)
        rows=[json.loads(r['payload']) for r in self.life.repo._all("SELECT payload FROM experience_journal WHERE kind='interval'")]
        self.assertTrue(any(r['input_revision']==1 and r['to']<=1120 for r in rows))
        self.assertTrue(any(r['input_revision']==2 and r['from']>=1120 for r in rows))

    def test_source_revocation_blocks_all_advance_entry_points_even_without_elapsed_time(self):
        self.enter();self.start()
        origin=self.event.id
        self.life.life_access.set_rule(origin,'internal_use','*','deny')
        run=self.state()['run']
        with self.assertRaises(SessionFailure):
            self.life.experience.guard_expression(self.scope,dict(experience_run_ref=run['simulation_run_id'],
                experience_control_revision=run['control_revision']))
        with self.assertRaises(SessionFailure):
            self.impulse(.1,confidence=1)
        self.life.experience.advance()
        stopped=self.state()
        self.assertEqual(stopped['run']['execution_state'],'paused')
        self.assertTrue(stopped['run']['desired_active'])
        self.assertEqual(stopped['run']['blocked_reason'],'source_or_scope_invalid')
        self.at+=120
        self.life.experience.advance()
        self.assertEqual(self.state()['arousal'],0)
        self.assertEqual(self.state()['peak_count'],0)

    def test_language_hold_transition_retains_event_impulse_origin(self):
        from life.experience import ExperienceSimulation
        self.life.experience=ExperienceSimulation(self.life,dict(threshold=.5,release_threshold=.3,min_hold_seconds=5))
        self.enter()
        for delta in (.25,.25,.05):
            self.source('达到并保持语言阈值')
            self.impulse(delta,confidence=1)
        self.assertEqual(self.state()['peak_count'],0)
        self.at+=5
        self.life.experience.advance()
        transition=self.state()['transitions'][0]
        self.assertEqual(transition['logical_at'],self.at)
        self.assertEqual(transition['effect_origin'],'event_impulse')
        self.assertIsNone(transition['run_id'])

    def test_invalid_keep_ratio_falls_back_and_pending_never_auto_peaks(self):
        from life.experience import ExperienceSimulation
        self.life.experience=ExperienceSimulation(self.life,dict(peak_keep_ratio=1))
        self.enter();self.impulse(.25,confidence=1)
        self.source('第二条');self.impulse(.25,confidence=1)
        self.source('跨越');r=self.impulse(.25,confidence=1)
        self.assertEqual(len(r['transition_refs']),1)
        self.assertEqual(self.state()['peak_count'],0)
        self.assertEqual(self.life.experience.config.peak_keep_ratio,.4)
        self.source('降低归零');self.impulse(-.25,confidence=1)
        self.source('继续降低');self.impulse(-.25,confidence=1)
        self.source('归零');self.impulse(-.25,confidence=1)
        self.assertEqual(self.state()['arousal'],.35)
        self.source('停止')
        self.at+=180
        self.life.experience.advance()
        self.assertEqual(self.state()['phase'],'idle')
        self.assertEqual(self.state()['scene_peak_count'],0)
        self.assertEqual(self.state()['peak_count'],0)

    def test_stop_resets_scene_and_requires_new_explicit_opening(self):
        self.enter(); self.start()
        self.at += 100
        self.source('关闭持续输入')
        s = self.state()
        self.assertFalse(s['run']['desired_active'])
        self.assertEqual(s['experience_context'], 'closed')
        self.assertEqual(s['arousal'],0)
        self.source('关闭持续输入后新的单次语言')
        with self.assertRaises(SessionFailure):self.impulse(.1,confidence=1)
        self.source('新的明确成年亲密开启')
        self.enter()
        self.source('新的单次语言')
        self.impulse(.1, confidence=1)
        late = self.proposal('appraise_input', direction='increase', effects={'arousal':.1}, reason='迟到的旧结果')
        self.source('退出亲密场景')
        self.assertEqual(self.state()['experience_context'], 'settling')
        with self.assertRaises(SessionFailure):
            self.life.experience.submit(self.scope, late, 'late')
        late['base_revision'] = self.state()['state_revision']
        with self.assertRaises(SessionFailure):
            self.life.experience.submit(self.scope, late, 'late-refreshed')

    def test_pause_resume_does_not_fill_paused_interval(self):
        self.enter(); self.start()
        run = self.state()['run']['simulation_run_id']
        self.source('暂停当前方案')
        self.submit('pause', run_ref=run, reason='用户明确暂停')
        self.at += 300
        self.life.experience.advance()
        self.assertEqual(self.state()['arousal'], 0)
        self.source('现在恢复原方案')
        self.submit('resume', run_ref=run, willingness='interested')
        self.at += 60
        self.life.experience.advance()
        self.assertGreater(self.state()['arousal'], 0)

    def test_out_of_order_inputs_require_earlier_semantics_without_backdating_effects(self):
        self.enter()
        self.at+=1;first=self.source('先发生的语言')
        self.at+=1;second=self.source('后发生的语言')
        with self.assertRaises(SessionFailure) as failure:
            self.impulse(.2,confidence=1)
        self.assertEqual(failure.exception.code,'input_pending')
        self.assertEqual(self.state()['arousal'],0)
        self.event=first;self.impulse(.1,confidence=1)
        self.event=second;self.impulse(.2,confidence=1)
        self.assertAlmostEqual(self.state()['arousal'],.1*2**(-1/1800)+.2)

    def test_pending_barrier_omitted_semantics_not_marked_success(self):
        self.enter(); self.start()
        self.at += 10
        self.source('新的含糊控制输入')
        fence = self.state()['integrated_until']
        self.at += 60
        self.life.experience.advance()
        self.assertEqual(self.state()['integrated_until'], fence)
        self.assertEqual(self.state()['input_coverage'][0]['status'], 'pending')
        self.impulse(0)
        # Time spent awaiting interpretation does not become new simulation.
        self.assertLessEqual(self.state()['integrated_until'], self.at)

    def test_old_result_and_old_start_cannot_cross_stop_revision(self):
        self.enter(); self.start()
        self.at+=30
        self.source('待处理的原话')
        old=self.proposal('appraise_input',direction='increase',effects={'arousal':.1},reason='迟到结果')
        oldstart=self.proposal('start',profile_confirmed=True,input_profile=dict(method='旧方式',intensity=1,valence=.5,rhythm='continuous'))
        oldcontrol=self.state()['run']['control_revision']
        self.source('关闭持续输入')
        for key,p in (('old-effect',old),('old-start',oldstart)):
            p['base_revision']=self.state()['state_revision']
            with self.assertRaises(SessionFailure):
                self.life.experience.submit(self.scope,p,key)
        with self.assertRaises(SessionFailure):
            self.life.experience.advance(expected_control_revision=oldcontrol)
        self.assertFalse(self.state()['run']['desired_active'])

    def test_repeated_advances_and_reads_do_not_double_decay(self):
        self.enter(); self.impulse(.4/2, confidence=1)
        self.source('第二个独立语义冲量'); self.impulse(.2, confidence=1)
        self.at += 1800
        self.life.experience.advance()
        self.assertAlmostEqual(self.state()['arousal'], .2, places=10)
        for _ in range(4):
            self.life.experience.advance(); self.state()
        self.source('无影响'); self.impulse(0)
        self.assertAlmostEqual(self.state()['arousal'], .2, places=10)

    def test_revision_replays_at_original_time_and_never_resends_actions(self):
        self.enter()
        r = self.impulse(.2, confidence=1)
        self.at += 1800
        self.life.experience.advance()
        p = self.proposal('appraise_input', direction='increase', effects={'arousal':.1}, confidence=1,
            reason='修正原始语义', supersedes_call_key=r['call_key'])
        fixed = self.life.experience.submit(self.scope,p,'revision')
        self.assertTrue(fixed['revised'])
        self.assertFalse(fixed['external_actions_replayed'])
        self.assertAlmostEqual(self.state()['arousal'], .05, places=10)
        self.assertEqual(self.life.experience.submit(self.scope,p,'revision'),dict(fixed,duplicate=True))

    def test_revision_cannot_erase_permission_control_history(self):
        self.enter();a=self.impulse(.25,confidence=1)
        self.source('第二条');self.impulse(.25,confidence=1)
        self.source('跨越');original=self.proposal('appraise_input',direction='increase',effects={'arousal':.1},confidence=1,reason='原解释')
        peak=self.life.experience.submit(self.scope,original,'original-peak')
        p=dict(original,base_revision=self.state()['state_revision'],direction='unchanged',effects={},reason='修正为无影响',supersedes_call_key='original-peak')
        with self.assertRaises(SessionFailure) as failure:
            self.life.experience.submit(self.scope,p,'revision-one')
        self.assertEqual(failure.exception.code,'revision_boundary')
        self.assertEqual(self.state()['peak_count'],0)
        self.assertEqual(self.state()['phase'],'awaiting_permission')
        self.assertEqual(self.life.experience.submit(self.scope,original,'original-peak'),dict(peak,duplicate=True))
        self.assertEqual(len(self.state()['transitions']),1)

    def test_hourly_review_and_repeated_source_cannot_add_fast_effect_again(self):
        self.enter();r=self.impulse(.2,confidence=1)
        state=self.state()['arousal']
        for _ in range(3):
            review=self.life.experience.review(self.scope,[self.event.id])
            self.assertEqual(review['mode'],'review_only')
        with self.assertRaises(SessionFailure):
            self.impulse(.2,confidence=1)
        self.assertEqual(self.state()['arousal'],state)
        with self.assertRaises(SessionFailure):
            self.life.appraise_structured(self.scope,self.event,dict(source_event_ids=[self.event.id],effects={'arousal':.2}))

    def test_channel_failure_and_restart_validation_do_not_resume_user_pause(self):
        self.enter();self.start()
        self.at+=30
        self.life.experience.system_pause('channel_unavailable')
        level=self.state()['arousal']
        self.at+=300
        self.life.experience.system_resume()
        self.assertLess(self.state()['arousal'],level)
        self.assertEqual(self.state()['run']['execution_state'],'running')
        self.source('用户暂停')
        self.submit('pause',run_ref=self.state()['run']['simulation_run_id'],reason='user_pause')
        self.life.experience.system_resume()
        self.assertEqual(self.state()['run']['execution_state'],'paused')

    def test_restart_reconciles_durable_stop_even_if_receive_commit_was_interrupted(self):
        self.enter();self.start()
        # Simulate a crash after events committed but before the experience
        # hook, without changing production data or inventing stop receipts.
        from unittest.mock import patch
        with patch.object(self.life.experience,'receive'):
            self.source('关闭持续输入')
        self.life.close();self.at+=300
        self.life=LifeRuntime(self.path,clock=lambda:self.at)
        self.assertFalse(self.state()['run']['desired_active'])
        self.life.experience.system_resume()
        self.assertFalse(self.state()['run']['desired_active'])

    def test_legacy_cutover_does_not_import_episode_values_or_consent(self):
        from life.drives import initial_state
        legacy=initial_state(self.scope.scope_id,self.life.drive_config(),at=self.at)
        legacy.drives['libido'].stimulation=.7
        legacy.drives['libido'].scene_climaxes=4
        self.life.repo.save_state(self.life.state_key('drive',self.scope),legacy.to_dict())
        self.enter()
        self.assertAlmostEqual(self.state()['arousal'],0)
        self.at+=10;self.life.experience.advance()
        self.assertEqual(self.state()['peak_count'],0)
        self.assertNotIn('legacy_origin',self.state())

    def test_restart_keeps_stop_and_does_not_simulate_offline(self):
        self.enter(); self.start()
        self.at += 100
        self.life.experience.advance()
        self.life.close()
        self.at += 4000
        self.life = LifeRuntime(self.path, clock=lambda:self.at)
        s = self.state()
        self.assertTrue(s['run']['desired_active'])
        self.assertEqual(s['run']['execution_state'], 'paused')
        self.assertEqual(s['peak_count'], 0)
        self.source('关闭持续输入')
        self.life.close()
        self.at += 100
        self.life = LifeRuntime(self.path, clock=lambda:self.at)
        self.assertFalse(self.state()['run']['desired_active'])

    def test_long_and_short_advances_have_identical_state_and_logical_transitions(self):
        self.enter(); self.start()
        # Two isolated copies, same sources, run identity and UTC anchor.
        other_path = Path(self.tmp.name)/'other.db'
        import sqlite3
        copy = sqlite3.connect(other_path)
        self.life.repo.db.backup(copy); copy.close()
        other = LifeRuntime(other_path, clock=lambda:self.at)
        try:
            # Restart is deliberately paused; validate the same running state
            # in each database before comparing their known-online intervals.
            self.life.experience.recover()
            self.source('恢复')
            self.submit('resume', run_ref=self.state()['run']['simulation_run_id'], willingness='interested')
            other.close()
            copy=sqlite3.connect(other_path); self.life.repo.db.backup(copy); copy.close()
            from life.experience import ExperienceSimulation
            other=LifeRuntime(other_path, clock=lambda:self.at)
            with other.repo.tx():
                # Recover() at the same instant is a pause, so restore only the
                # exact already-validated pre-advance checkpoint for this test.
                row=self.life.repo._one('SELECT * FROM experience_state')
                other.repo._exec('UPDATE experience_state SET revision=?,payload=?',(row['revision'],row['payload']))
            end = self.at+5400
            self.at=end
            self.life.experience.advance()
            for target in range(1030,6401,30):
                other.experience.advance(target)
            a,b=self.state(),other.experience.read(self.scope)
            for field in (*METRICS, 'peak_count', 'phase', 'integrated_until', 'recovery_until'):
                self.assertEqual(a[field],b[field],field)
            self.assertEqual(a['transitions'],b['transitions'])
        finally:
            other.close()


if __name__=='__main__':
    unittest.main()
