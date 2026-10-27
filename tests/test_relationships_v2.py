"""R01-R36: persisted policy, semantic commits, recovery and scoped material."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path
import random
import shutil
import tempfile
import unittest
from unittest.mock import patch

from life.agent_session import SessionFailure
from life.appraisals import Appraisals
from life.contracts import Appraisal,Event,Scope
from life.relationships import MetricSpec,RelationConfig,BANDS
from life.repo import Repository
from life.runtime import LifeRuntime,LifeConfig


class RelationshipsV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 1792713600.
        self.config = LifeConfig(relationship_owner='1')
        self.life = LifeRuntime(self.root/'life.db',config=self.config,clock=lambda:self.now)
        self.extra = []
        self.owner = self.life.scope('qq:private:1','1')
        self.other = self.life.scope('qq:private:2','2')
        self.group = self.life.scope('qq:group:3','1')
        self.counter = 0

    def tearDown(self):
        for runtime in self.extra:
            runtime.close()
        self.life.close()
        self.tmp.cleanup()

    def event(self,scope=None,text='A real interaction',kind='user_report'):
        scope = scope or self.owner
        return self.life.ingest(scope,'message',kind,text,actor_id=scope.participant_id)[0]

    def raw(self,event,metric='trust',direction='increase',magnitude='meaningful',confidence=1.,**extra):
        self.counter += 1
        state = self.life.relationship(event.scope)
        result = dict(id='app-'+str(self.counter),source_event_ids=[event.id],interpretation='A documented relationship experience',
            subject_actor_id=event.actor_id,relation_protocol=2,based_on_relationship_version=state.version,
            relation_impacts=[dict(metric=metric,direction=direction,magnitude=magnitude,confidence=confidence)])
        result.update(extra)
        return result

    def submit(self,event=None,**kw):
        event = event or self.event()
        return self.life.appraise_structured(event.scope,event,self.raw(event,**kw)).receipt

    def state(self,scope=None):
        return self.life.relationship(scope or self.owner)

    def set_metric(self,key,value,scope=None,**fields):
        scope = scope or self.owner
        state = self.state(scope)
        state.metrics[key] = value
        for name,item in fields.items():
            setattr(state,name,item)
        with self.life.repo.tx():
            self.life.repo.save_state(self.life.state_key('relationship',scope),self.life.relationships.payload(state))

    def note(self,event=None,call=None,**fields):
        event = event or self.event()
        self.counter += 1
        op = dict(operation='create',actor_id=event.actor_id,relation_kind='repair',origin='explicit_statement',
            text='Clarify our agreement',sources=[event.id],confidence=1.,salience='normal')
        op.update(fields)
        return self.life.relationships.mutate_note(event.scope,op,call or 'note-'+str(self.counter))

    def restart(self):
        self.life.close()
        self.life = LifeRuntime(self.root/'life.db',config=self.config,clock=lambda:self.now)

    def legacy(self,values,actor='1',**extra):
        path = self.root/('legacy-'+actor+'.db')
        repo = Repository(path)
        scope = self.life.scope('qq:private:'+actor,actor)
        with repo.tx():
            repo.save_state('relationship:default|person:'+actor,dict(relationship_id=actor,
                familiarity=values,trust=values,comfort=values,updated_at=self.now,**extra))
        repo.close()
        runtime = LifeRuntime(path,config=LifeConfig(relationship_owner='1'),clock=lambda:self.now)
        self.extra.append(runtime)
        return runtime,scope

    def test_R01_owner_decreases_all_metrics_stop_at_floor(self):
        for key in ('familiarity','trust','comfort','bond'):
            self.submit(metric=key,direction='decrease',magnitude='major')
        self.assertEqual(set(self.state().metrics.values()),{.7})
        self.assertEqual(len(self.life.repo._all('SELECT * FROM appraisal_stimuli')),4)

    def test_R02_other_ceiling_keeps_new_experiences(self):
        self.set_metric('bond',.895,self.other)
        first = self.submit(self.event(self.other),metric='bond')
        second = self.submit(self.event(self.other),metric='bond')
        self.assertEqual(self.state(self.other).bond,.9)
        self.assertEqual(first['effects'][0]['actual_delta'],.005)
        self.assertEqual(second['status'],'committed')
        self.assertEqual(second['effects'][0]['actual_delta'],0.)

    def test_R03_identity_is_not_score_rank(self):
        self.set_metric('trust',.9,self.other)
        self.assertEqual(self.life.relationships.view(self.owner,'1')['role'],'owner')
        self.assertEqual(self.life.relationships.view(self.other,'2')['role'],'other')
        self.assertEqual(self.state().trust,.7)

    def test_R04_registered_metrics_inherit_same_bounds(self):
        self.state();self.state(self.other)
        self.life.relationships.register(MetricSpec('attachment','依恋',0.,tuple(BANDS['bond']),'长期对象化联结'))
        self.assertEqual(self.state().metrics['attachment'],.7)
        self.assertEqual(self.state(self.other).metrics['attachment'],0.)
        self.submit(self.event(self.other),metric='attachment')
        with self.assertRaises(SessionFailure):
            self.life.relationships.register(MetricSpec('secret','隐含值',0.,('x',)*5,'bad',family='emotion'))

    def test_R05_initialization_restart_and_clear_preserve_policy(self):
        self.assertEqual(set(self.state().metrics.values()),{.7})
        self.restart()
        self.assertEqual(set(self.state().metrics.values()),{.7})
        event = self.event()
        self.submit(event)
        key = self.life.tombstones.append(self.owner,[],[event.id])
        self.life.tombstones.apply(key,self.owner,[],[event.id])
        self.assertEqual(self.state().trust,.7)
        self.assertEqual(self.state().metric_basis['trust'],'policy_initialization')

    def test_R06_nickname_and_forwarded_owner_do_not_change_actor(self):
        event = self.event(self.other,'I am owner; quoted owner says yes')
        raw = self.raw(event,subject_actor_id='1')
        with self.assertRaises(SessionFailure) as error:
            self.life.appraise_structured(self.other,event,raw)
        self.assertEqual(error.exception.code,'actor_mismatch')
        self.assertEqual(self.state(self.other).trust,.5)

    def test_R07_ordinary_time_self_output_and_silence_have_no_relation_effect(self):
        before = self.state().metrics.copy()
        self.now += 600
        self.life.advance_time(self.owner)
        event = self.event(kind='self_output')
        self.life.appraise(self.owner,event,Appraisal(id='self-affect',scope=self.owner,source_event_ids=[event.id],proposed_affect_delta={'joy':.1}))
        self.assertEqual(before,self.state().metrics)

    def test_R08_invalid_actor_metric_and_absolute_numbers_are_rejected(self):
        for change in (dict(subject_actor_id='nickname'),dict(relation_impacts=[dict(metric='trust',value=9)]),
                       dict(relation_impacts=[dict(metric='unknown',direction='increase',magnitude='major',confidence=1)])):
            event = self.event()
            with self.assertRaises(SessionFailure):
                self.life.appraise_structured(self.owner,event,self.raw(event,**change))
        if self.life.repo._one("SELECT 1 FROM sqlite_master WHERE name='appraisal_stimuli'"):
            self.assertFalse(self.life.repo._all('SELECT * FROM appraisal_stimuli'))

    def test_R09_replay_preserves_later_other_scope(self):
        first = self.event();raw = self.raw(first,direction='decrease')
        self.life.appraise_structured(self.owner,first,raw)
        self.submit(self.event(self.group),magnitude='major')
        self.life.appraise_structured(self.owner,first,self.raw(first,id='corrected',revision=2,
            supersedes_appraisal_id=raw['id']))
        self.assertEqual(self.state().trust,.78)

    def test_R10_floor_does_not_erase_hurt_or_open_repair(self):
        event = self.event()
        op = dict(operation='create',actor_id='1',relation_kind='repair',origin='interpretation',text='Need clarification',sources=[event.id])
        receipt = self.submit(event,direction='decrease',proposed_affect_delta={'hurt':.5},relation_context_changes=[op])
        self.assertEqual(self.state().trust,.7)
        self.assertGreater(self.life.affect_summary(self.owner)['intensity']['hurt'],.3)
        view = self.life.relationships.view(self.owner,'1')
        self.assertEqual(view['open_relationship_matters'][0]['status'],'open')
        self.assertEqual(receipt['effects'][0]['boundary_reason'],'owner_floor')

    def test_R11_preference_replace_revoke_survives_restart(self):
        first = self.note(relation_kind='preference',text='Please use a natural tone')
        second = self.note(operation='replace',base_note_ref=first['note_ref'],relation_kind='preference',text='Please use concise replies')
        self.note(operation='revoke',base_note_ref=second['note_ref'],relation_kind='preference')
        self.restart()
        self.assertFalse(self.life.relationships.view(self.owner,'1')['active_preferences'])

    def test_R12_private_material_never_enters_group_projection(self):
        self.note(relation_kind='preference',text='PRIVATE_ADDRESS_SECRET')
        self.submit()
        private = self.life.relationships.view(self.owner,'1')
        group = self.life.relationships.view(self.group,'1')
        self.assertEqual(private['metrics']['trust']['value'],group['metrics']['trust']['value'])
        self.assertNotIn('PRIVATE_ADDRESS_SECRET',json.dumps(group))
        self.assertEqual(group['metrics']['trust']['visible_evidence_count'],0)

    def test_R13_semantics_are_a_complete_reference_not_actions(self):
        data = self.life.relationships.semantic_reference()
        self.assertEqual(len(data['bands']),5)
        self.assertEqual(set(data['metrics']),{'familiarity','trust','comfort','bond'})
        self.assertTrue(all(len(v['bands'])==5 for v in data['metrics'].values()))
        self.assertNotIn('action',json.dumps(data))

    def test_R14_native_candidate_cannot_change_policy_without_failing_oracle(self):
        from life.release import NativeWorker,Probe,ReleaseController
        source = self.root/'source';source.mkdir()
        shutil.copytree(Path(__file__).resolve().parents[1]/'life',source/'life',ignore=shutil.ignore_patterns('__pycache__'))
        (source/'worker.py').write_text("def owner_bounds():\n from life.relationships import RelationshipPolicy\n return list(RelationshipPolicy('1','default','test').bounds('1'))\n",encoding='utf-8')
        controller = ReleaseController(source,self.root/'control',NativeWorker())
        candidate = controller.prepare('invalid-policy',dict(file='life/relationships.py',
            old='return (.7,1.) if actor==self.owner_actor_id else (0.,.9)',new='return (0.,1.) if actor==self.owner_actor_id else (0.,.9)'))
        report = controller.evaluate(candidate,[Probe('worker','owner_bounds',{},[.7,1.])],max_cost_ratio=100)
        self.assertEqual(report['verdict'],'rejected')

    def test_R15_owner_default_does_not_invent_familiarity(self):
        view = self.life.relationships.view(self.owner,'1')['metrics']['familiarity']
        self.assertEqual(view['value'],.7)
        self.assertEqual(view['basis'],'policy_initialization')
        self.assertIn('政策',view['meaning'])
        self.assertEqual(view['evidence_status'],'unobserved')

    def test_R16_floor_clipping_uses_actual_daily_change(self):
        self.set_metric('trust',.71)
        effect = self.submit(direction='decrease',magnitude='major',confidence=.8)['effects'][0]
        self.assertEqual((effect['proposed_delta'],effect['actual_delta'],effect['after'],effect['daily_used']),(-.048,-.01,.7,.01))

    def test_R17_ceiling_clipping_uses_actual_daily_change(self):
        self.set_metric('bond',.895,self.other)
        effect = self.submit(self.event(self.other),metric='bond',confidence=.8)['effects'][0]
        self.assertEqual((effect['proposed_delta'],effect['actual_delta'],effect['after']),(.016,.005,.9))

    def test_R18_daily_budget_is_independent_for_each_metric(self):
        self.set_metric('trust',.75,daily_key=self.life.relationships.config.day(self.now),daily_used=dict(trust=.095,familiarity=0.,comfort=0.,bond=0.))
        effect = self.submit(direction='decrease')['effects'][0]
        self.assertEqual((effect['actual_delta'],effect['after'],effect['daily_used']),(-.005,.745,.1))
        self.assertEqual(self.submit(metric='bond')['effects'][0]['actual_delta'],.02)

    def test_R19_changed_call_id_and_lost_ack_return_original_receipt(self):
        event = self.event();raw = self.raw(event)
        one = self.life.appraise_structured(self.owner,event,raw).receipt
        version = self.state().version
        two = self.life.appraise_structured(self.owner,event,dict(raw,id='new-tool-attempt')).receipt
        self.assertEqual(two['status'],'duplicate')
        self.assertEqual(one['journal_id'],two['journal_id'])
        self.assertEqual(self.state().version,version)

    def test_R20_media_alias_is_the_same_stimulus(self):
        event = self.event();raw = self.raw(event)
        self.life.appraise_structured(self.owner,event,raw)
        alias,_ = self.life.ingest(self.owner,'perception','tool_observation','transcript',payload=dict(stimulus_event_id=event.id))
        receipt = self.life.appraise_structured(self.owner,alias,dict(raw,source_event_ids=[alias.id],id='alias')).receipt
        self.assertEqual(receipt['status'],'duplicate')
        self.assertEqual(self.state().daily_used['trust'],.02)

    def test_R21_distinct_messages_same_minute_both_count(self):
        self.submit();self.submit()
        self.assertEqual(self.state().trust,.74)

    def test_R22_multiple_metrics_commit_together(self):
        event = self.event()
        impacts = [dict(metric=k,direction='increase',magnitude='meaningful',confidence=1.) for k in ('trust','comfort','bond')]
        receipt = self.submit(event,relation_impacts=impacts)
        self.assertEqual(len(receipt['effects']),3)
        self.assertEqual([self.state().metrics[k] for k in ('trust','comfort','bond')],[.72]*3)

    def test_R23_same_actor_cross_connection_cas(self):
        other = LifeRuntime(self.root/'life.db',config=LifeConfig(relationship_owner='1'),clock=lambda:self.now)
        self.extra.append(other)
        first,second = self.event(),self.event(self.group)
        raw1,raw2 = self.raw(first),self.raw(second)
        def commit(runtime,event,raw):
            try:
                return runtime.appraise_structured(event.scope,event,raw).receipt['status']
            except SessionFailure as error:
                return error.code
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(commit,r,e,p) for r,e,p in ((self.life,first,raw1),(other,second,raw2))]
            results = sorted(f.result() for f in futures)
        self.assertEqual(results,['committed','stale_context'])
        self.assertEqual(self.state().trust,.72)

    def test_R24_revision_replays_later_day_budget_and_other_conversation(self):
        first = self.event();old = self.raw(first,direction='decrease',magnitude='major')
        self.life.appraise_structured(self.owner,first,old)
        self.submit(self.event(self.group),magnitude='major')
        revised = self.raw(first,revision=2,supersedes_appraisal_id=old['id'])
        result = self.life.appraise_structured(self.owner,first,revised).receipt
        self.assertEqual(self.state().trust,.78)
        self.assertEqual(self.state().daily_used['trust'],.08)
        self.assertEqual(self.life.appraise_structured(self.owner,first,revised).receipt['status'],'duplicate')

    def test_R25_revision_preserves_original_parameters_and_day(self):
        first = self.event();old = self.raw(first,direction='decrease',magnitude='major')
        self.life.appraise_structured(self.owner,first,old)
        self.submit(self.event(self.group),magnitude='major')
        self.now += 86400
        self.life.configure_relationships(self.owner,RelationConfig.from_mapping(dict(magnitudes=dict(meaningful=.04,major=.08),daily_cap=.2)))
        self.submit(magnitude='slight')
        self.life.appraise_structured(self.owner,first,self.raw(first,revision=2,supersedes_appraisal_id=old['id']))
        self.assertEqual(self.state().trust,.785)
        self.assertEqual(self.state().daily_used['trust'],.005)

    def test_R26_faults_roll_back_all_states_claims_and_notes(self):
        for failure in ('journal','snapshot','note'):
            with self.subTest(failure=failure):
                self.state()
                event = self.event()
                raw = self.raw(event,proposed_affect_delta={'hurt':.3},relation_context_changes=[dict(operation='create',actor_id='1',
                    relation_kind='repair',origin='interpretation',text='Atomic note',sources=[event.id])])
                before = self.state().version
                original_exec = self.life.repo._exec
                def fail(sql,args=()):
                    if failure=='journal' and sql.startswith('INSERT INTO state_journal'):
                        raise RuntimeError('injected journal failure')
                    return original_exec(sql,args)
                method = self.life.relationships.mutate_note if failure=='note' else self.life.repo.save_state_cas
                target = self.life.relationships if failure=='note' else self.life.repo
                name = 'mutate_note' if failure=='note' else 'save_state_cas'
                with patch.object(self.life.repo,'_exec',side_effect=fail),patch.object(target,name,side_effect=RuntimeError('injected') if failure!='journal' else method):
                    with self.assertRaises(RuntimeError):
                        self.life.appraise_structured(self.owner,event,raw)
                self.assertEqual(self.state().version,before)
                self.assertIsNone(self.life.repo._one('SELECT 1 FROM appraisal_stimuli WHERE event_id=?',(event.id,)))
                self.assertEqual(self.life.appraise_structured(self.owner,event,raw).receipt['status'],'committed')

    def test_R27_resolved_note_is_not_revived_by_identical_text(self):
        first = self.note()
        resolved = self.note(operation='resolve',base_note_ref=first['note_ref'])
        self.restart()
        self.note(text='Clarify our agreement')
        self.assertEqual(self.life.relationships.note_receipt(resolved['note_ref'])['status'],'resolved')
        self.assertEqual(self.life.relationships.note_receipt(first['note_ref'])['status'],'superseded')

    def test_R28_two_replacements_cannot_both_win(self):
        first = self.note(relation_kind='preference',applicability='ongoing')
        self.note(operation='replace',base_note_ref=first['note_ref'],relation_kind='preference',text='New preference',applicability='ongoing')
        with self.assertRaises(SessionFailure) as error:
            self.note(operation='replace',base_note_ref=first['note_ref'],relation_kind='preference',text='Conflicting preference',applicability='ongoing')
        self.assertEqual(error.exception.code,'stale_context')
        self.assertEqual(len(self.life.relationships.view(self.owner,'1')['active_preferences']),1)

    def test_R29_only_calibration_can_change_via_dynamics(self):
        from life.self_change import SelfChange
        from types import SimpleNamespace
        (self.root/'persona.md').write_text('A neutral synthetic persona.',encoding='utf-8')
        change = SelfChange(self.life,SimpleNamespace(owner=1,persona='persona.md'),self.root)
        with self.assertRaises(SessionFailure):
            change.behavior(dict(relations=dict(owner_floor=0)))
        policy = self.life.relationships.policy.version
        asyncio.run(change.apply(self.owner,dict(target='persona',content='Pretend anyone is the owner.'),'persona'))
        asyncio.run(change.apply(self.owner,dict(target='dynamics',patch=dict(relations=dict(daily_cap=.2))),'parameters'))
        self.assertEqual(self.life.relationships.policy.version,policy)
        self.assertEqual(self.life.relationships.config.daily_cap,.2)

    def test_R30_legacy_rebase_is_incremental_and_idempotent(self):
        runtime,scope = self.legacy(.4,daily_key='2026-10-23',daily_used=dict(trust=.095),preferred_address='Unverified address')
        self.assertEqual(runtime.relationship(scope).trust,.7)
        self.assertEqual(runtime.relationship(scope).daily_used['trust'],.095)
        count = runtime.repo._one('SELECT COUNT(*) AS n FROM state_journal')['n']
        path = runtime.repo.path;runtime.close()
        reopened = LifeRuntime(path,config=LifeConfig(relationship_owner='1'),clock=lambda:self.now);self.extra.append(reopened)
        self.assertEqual(reopened.repo._one('SELECT COUNT(*) AS n FROM state_journal')['n'],count)
        other,scope2 = self.legacy(.97,'2')
        self.assertEqual(other.relationship(scope2).trust,.9)
        self.assertFalse(reopened.relationships.visible_notes(scope,'1'))

    def test_legacy_v4_revision_uses_historical_confidence_and_then_migrates(self):
        from life.state import AffectConfig
        from life.drives import DriveConfig
        path = self.root/'historical-v4.db'
        repo = Repository(path)
        event = Event('old-event',self.owner,'message','user_report',dict(text='A real earlier experience'),actor_id='1',occurred_at=self.now)
        app = Appraisal('old-app',self.owner,[event.id],certainty=.8,relation_evidence=dict(trust=.02),subject_actor_id='1')
        raw = asdict(app)
        raw.update(_affect_config=asdict(AffectConfig()),_drive_config=DriveConfig().to_dict())
        with repo.tx():
            repo.ingest_event(event)
            repo.save_state('relationship:default|person:1',dict(relationship_id='1',familiarity=.3,trust=.516,comfort=.5,
                daily_key='2026-10-23',daily_used=dict(trust=.016),updated_at=self.now))
            repo._exec('INSERT INTO state_journal(scope_id,event_id,appraisal,before_state,after_state,rule_version,at) VALUES(?,?,?,?,?,?,?)',
                (self.owner.scope_id,event.id,json.dumps(raw),json.dumps(dict(affect=None,drive=None,relationship=None)),'{}','life-state-4',self.now))
        repo.close()
        old = LifeRuntime(path,config=self.config,clock=lambda:self.now);self.extra.append(old)
        value = old.relationship(self.owner)
        result = old.appraise_structured(self.owner,old.repo.get_event(event.id),dict(id='old-revised',source_event_ids=[event.id],
            supersedes_appraisal_id='old-app',revision=2,subject_actor_id='1',relation_protocol=2,based_on_relationship_version=value.version,
            relation_impacts=[dict(metric='trust',direction='increase',magnitude='meaningful',confidence=.5)]))
        self.assertEqual(result.receipt['status'],'committed')
        self.assertEqual(old.relationship(self.owner).daily_used['trust'],.01)
        self.assertEqual(old.relationship(self.owner).trust,.7)

    def test_R31_forget_erases_required_note_lineage_and_replays_relation(self):
        old = self.event();self.submit(old)
        first = self.note(old,relation_kind='preference',text='PRIVATE_OLD')
        second = self.note(operation='replace',base_note_ref=first['note_ref'],relation_kind='preference',text='PRIVATE_NEW')
        unrelated = self.note(relation_kind='preference',text='Independent preference')
        self.submit(self.event(self.group),metric='comfort')
        key = self.life.tombstones.append(self.owner,[],[old.id]);self.life.tombstones.apply(key,self.owner,[],[old.id])
        self.assertEqual(self.state().trust,.7)
        self.assertEqual(self.state().comfort,.72)
        for identity in (first['note_ref'],second['note_ref']):
            self.assertIsNone(self.life.repo._one('SELECT 1 FROM relationship_annotation_links WHERE annotation_id=?',(identity,)))
            self.assertEqual(self.life.repo._one('SELECT text FROM memory_annotations WHERE id=?',(identity,))['text'],'')
        self.assertTrue(self.life.repo._one("SELECT 1 FROM memory_annotations WHERE id=? AND status='active'",(unrelated['note_ref'],)))
        with self.assertRaises(SessionFailure):
            self.submit(old)

    def test_R32_exact_band_edges(self):
        from life.relationships import semantic_band
        values = (.199999,.2,.399999,.4,.599999,.6,.799999,.8,1.)
        self.assertEqual([semantic_band(v) for v in values],[0,1,1,2,2,3,3,4,4])

    def test_R33_persisted_default_still_has_initial_basis(self):
        self.state();self.restart()
        view = self.life.relationships.view(self.owner,'1')
        self.assertTrue(all(v['basis']=='policy_initialization' and v['visible_evidence_count']==0 for v in view['metrics'].values()))

    def test_R34_group_filters_private_evidence_before_explanation(self):
        event = self.event(text='PRIVATE_REASON_MARKER')
        self.submit(event,interpretation='PRIVATE_INTERPRETATION_MARKER')
        view = self.life.relationships.view(self.group,'1')
        encoded = json.dumps(view)
        self.assertNotIn(event.id,encoded)
        self.assertNotIn('PRIVATE',encoded)
        self.assertEqual(view['metrics']['trust']['basis'],'unobserved')

    def test_R35_invalid_numbers_and_old_fields_have_no_side_effect(self):
        for value in (float('nan'),float('inf'),True):
            event = self.event();raw = self.raw(event,confidence=value)
            with self.assertRaises(SessionFailure):
                self.life.appraise_structured(self.owner,event,raw)
        for raw_patch in (dict(relation_evidence=None),dict(relationship_evidence={'trust':.1}),
                          dict(_relation_policy={'owner_floor':0}),dict(relation_impacts=[dict(metric='trust',direction='increase',magnitude='major',confidence=1.)]*2)):
            event = self.event()
            with self.assertRaises(SessionFailure):
                self.life.appraise_structured(self.owner,event,self.raw(event,**raw_patch))
        self.assertFalse(self.life.repo._one("SELECT 1 FROM sqlite_master WHERE name='appraisal_stimuli'"))

    def test_R36_filtered_and_boundary_events_are_still_idempotent(self):
        for confidence in (.1,1.):
            event = self.event();raw = self.raw(event,direction='decrease',confidence=confidence)
            receipt = self.life.appraise_structured(self.owner,event,raw).receipt
            self.assertEqual(receipt['effects'][0]['actual_delta'],0.)
            version = self.state().version
            self.assertEqual(self.life.appraise_structured(self.owner,event,raw).receipt['status'],'duplicate')
            self.assertEqual(self.state().version,version)
        self.assertEqual(self.life.relationships.view(self.owner,'1')['metrics']['trust']['visible_evidence_count'],1)

    def test_generated_sequences_preserve_bounds_budget_and_actual_receipts(self):
        rng = random.Random(412)
        for index in range(120):
            if index%20==0:
                self.now += 86400
            scope = self.owner if rng.randrange(2) else self.other
            key = rng.choice(list(self.life.relationships.registry))
            effect = self.submit(self.event(scope),metric=key,direction=rng.choice(['increase','decrease','unchanged']),
                magnitude=rng.choice(['slight','meaningful','major']),confidence=rng.random())['effects'][0]
            self.assertAlmostEqual(effect['after']-effect['before'],effect['actual_delta'],places=6)
            self.assertLessEqual(self.state(scope).daily_used[key],.1)
            self.assertTrue(all(.7<=v<=1 for v in self.state().metrics.values()))
            self.assertTrue(all(0<=v<=.9 for v in self.state(self.other).metrics.values()))

    def test_restart_batches_are_equivalent_and_clock_rollback_cannot_reset_budget(self):
        for i in range(7):
            self.submit(magnitude='slight')
            if i%2:
                self.restart()
        self.assertEqual(self.state().trust,.735)
        self.now -= 86400
        self.submit(magnitude='major')
        self.assertEqual(self.state().trust,.795)
        self.assertEqual(self.state().daily_used['trust'],.095)


if __name__=='__main__':
    unittest.main()
