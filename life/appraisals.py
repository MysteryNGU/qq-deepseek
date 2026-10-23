"""Atomic semantic commits and versioned, agent-ordered state replay."""
from __future__ import annotations

from dataclasses import asdict
import json

from .contracts import Appraisal,Scope,RelationshipState,LegacyRelationshipState,RelationEvidence
from .agent_session import SessionFailure,canonical,digest
from .state import AffectConfig,reduce_relationship,reduce_relationship_legacy
from .drives import DriveConfig,initial_state
from .relationships import RelationConfig,RelationshipPolicy,MetricSpec,normalize_state,validate_impacts


class Commit(tuple):
    def __new__(cls,app,change,receipt):
        result = super().__new__(cls,(app,change))
        result.receipt = receipt
        return result


class Appraisals:
    def __init__(self,runtime):
        self.life,self.repo = runtime,runtime.repo
        exists = self.repo._one("SELECT 1 FROM sqlite_master WHERE name='appraisal_stimuli'")
        self.repo.db.execute('CREATE TABLE IF NOT EXISTS appraisal_stimuli('
            'scope_id TEXT NOT NULL,event_id TEXT NOT NULL,journal_id INTEGER NOT NULL,PRIMARY KEY(scope_id,event_id))')
        if not exists:
            with self.repo.tx():
                for row in self.repo._all('SELECT id,scope_id,appraisal FROM state_journal ORDER BY id'):
                    raw = json.loads(row['appraisal'])
                    if not raw.get('_kind'):
                        for eid in raw.get('_stimuli',raw.get('source_event_ids',[])):
                            self.repo._exec('INSERT OR IGNORE INTO appraisal_stimuli VALUES(?,?,?)',(row['scope_id'],eid,row['id']))

    def stimuli(self,scope,ids):
        result = set()
        for eid in ids:
            seen = set()
            while True:
                item = self.repo.get_event(eid)
                if not item or item.redacted_at:
                    raise SessionFailure('source_removed','评价来源已不存在或已遗忘')
                if eid in seen or item.scope.private_key!=scope.private_key:
                    raise SessionFailure('scope_mismatch','实际刺激来源不属于当前受众或出现循环')
                seen.add(eid)
                alias = item.payload.get('stimulus_event_id')
                if not alias:
                    result.add(eid)
                    break
                eid = alias
        return sorted(result)

    def proposal_fingerprint(self,app,roots):
        raw = asdict(app)
        for key in ('id','scope','stimulus_event_id','based_on_state_version','based_on_relationship_version'):
            raw.pop(key,None)
        raw['source_event_ids'] = roots
        raw['context_refs'] = sorted(raw['context_refs'])
        raw['relation_impacts'] = sorted(raw['relation_impacts'],key=lambda item:item['metric'])
        return digest(raw)

    def check_relation(self,app):
        if app.relation_evidence is not None:
            raise SessionFailure('protocol_mismatch','旧关系 delta 入口已停用，历史回放之外不再接受')
        validate_impacts(app.relation_impacts,self.life.relationships.registry)
        if app.relation_impacts or app.relation_context_changes:
            if type(app.relation_protocol) is not int or app.relation_protocol!=2:
                raise SessionFailure('protocol_mismatch','关系操作需要 relation_protocol=2')
            if not app.subject_actor_id:
                raise SessionFailure('actor_mismatch','需要实际人物 subject_actor_id')
        if app.relation_impacts and (type(app.based_on_relationship_version) is not int or app.based_on_relationship_version<0):
            raise SessionFailure('invalid_relation_input','关系影响需要非负整数关系版本')

    def check_versions(self,scope,app,*,relationship=False):
        got = self.repo.load_state(self.life.state_key('affect',scope))
        current = got[0] if got else 0
        if app.based_on_state_version is not None and app.based_on_state_version!=current:
            raise SessionFailure('stale_context','情绪版本已改变，请读取后重新理解',state_version=current)
        if relationship:
            got = self.repo.load_state(self.life.state_key('relationship',scope))
            current = got[0] if got else 0
            if type(app.based_on_relationship_version) is not int or app.based_on_relationship_version!=current:
                raise SessionFailure('stale_context','关系版本已改变，请读取后重新理解',relationship_version=current)

    def submit(self,scope,event,app,*,call_key=None):
        if app is None:
            return None
        self.check_relation(app)
        with self.repo.tx():
            if app.scope.private_key!=scope.private_key or event.id not in app.source_event_ids:
                raise SessionFailure('scope_mismatch','评价须引用当前实际事件')
            roots = self.stimuli(scope,app.source_event_ids)
            source = self.repo.get_event(roots[-1])
            self.life.relationships.require_sources(scope,app.source_event_ids+app.context_refs)
            has_relation = bool(app.relation_impacts or app.relation_context_changes)
            actor = self.life.relationships.subject(scope,app.subject_actor_id,roots) if has_relation else source.actor_id
            scope = Scope(scope.agent_id,scope.conversation_id,actor or scope.participant_id,scope.audience_policy_id)
            app.scope = scope
            claims = [self.repo._one('SELECT journal_id FROM appraisal_stimuli WHERE scope_id=? AND event_id=?',(scope.scope_id,eid)) for eid in roots]
            fingerprint = self.proposal_fingerprint(app,roots)
            if any(claims):
                ids = {r['journal_id'] for r in claims if r}
                if len(ids)==1 and all(claims):
                    old = self.repo._one('SELECT appraisal FROM state_journal WHERE id=?',(next(iter(ids)),))
                    raw = json.loads(old['appraisal']) if old else {}
                    if raw.get('_proposal_fingerprint')==fingerprint:
                        return Commit(app,None,dict(raw['_receipt'],status='duplicate'))
                if not app.supersedes_appraisal_id:
                    raise SessionFailure('stimulus_overlap','刺激已有不同有效解释；读取原解释并明确修订')
            if app.supersedes_appraisal_id:
                return self.revise(scope,app,roots,claims,fingerprint,call_key)
            self.check_versions(scope,app,relationship=bool(app.relation_impacts))
            if self.repo._one("SELECT 1 FROM state_journal j JOIN events e ON e.id=j.event_id WHERE e.agent_id=? AND json_extract(j.appraisal,'$.id')=?",(scope.agent_id,app.id)):
                raise SessionFailure('idempotency_conflict','评价编号已用于另一份经历')
            relation = self.life.relationships.load(scope) if app.relation_impacts else None
            latest = self.repo._one("SELECT MAX(at) AS at FROM state_journal j JOIN events e ON e.id=j.event_id WHERE e.agent_id=?",(scope.agent_id,))
            at = max(self.life.clock(),latest['at'] or 0.)
            self.life.advance_time(scope,at)
            if not self.repo.load_state(self.life.state_key('drive',scope)):
                self.repo.save_state(self.life.state_key('drive',scope),self.life.drive_state(scope).to_dict())
            before = self.life._state_packet(scope)
            relation_after,effects = None,[]
            cfg = self.life.relationships.config
            if relation:
                relation_after,effects = reduce_relationship(relation,app.relation_impacts,actor_id=actor,
                    policy=self.life.relationships.policy,registry=self.life.relationships.registry,config=cfg,
                    settlement_day=cfg.day(at),settlement_at=at)
            if source.source_kind not in {'user_report','tool_observation'}:
                app.drive_evidence = [v for v in app.drive_evidence if v.get('kind') in {'activation_change','internal_regulation'}]
            raw = asdict(app)
            raw.update(_proposal_fingerprint=fingerprint,_stimuli=roots,_affect_config=asdict(self.life.config.affect),
                _drive_config=self.life.drive_config().to_dict(),_desire_pressure=self.life.drive_pressure(scope),
                _relation_policy=self.life.relationships.policy.payload(),_relation_config=asdict(cfg),
                _relation_config_version=cfg.version,_relation_effects=effects,_relation_settlement_day=cfg.day(at),
                _relation_registry={k:asdict(v) for k,v in self.life.relationships.registry.items()})
            jid = self.record(scope,roots[-1],raw,before,at)
            result = self.life._appraise(scope,source,app,at=at)
            if result is None:
                raise SessionFailure('stimulus_overlap','该刺激的状态效果已经结算，请读取原回执')
            if relation_after:
                relation_after.last_journal_id = jid
                version = self.repo.save_state_cas(self.life.state_key('relationship',scope),self.life.relationships.payload(relation_after),relation.version)
                if version is None:
                    raise SessionFailure('stale_context','关系提交冲突')
                relation_after.version = version
            notes = []
            for index,op in enumerate(app.relation_context_changes):
                if op.get('actor_id')!=app.subject_actor_id:
                    raise SessionFailure('actor_mismatch','一份评价中的材料须属于同一实际人物')
                notes.append(self.life.relationships.mutate_note(scope,op,call_key or app.id,index,f'{app.id}:{app.revision}'))
            receipt = dict(status='committed',appraisal_id=app.id,revision=app.revision,
                relationship_version=relation_after.version if relation_after else None,
                policy_version=self.life.relationships.policy.version,effects=effects,saved_note_refs=notes,journal_id=jid)
            raw.update(_receipt=receipt,_change=dict(asdict(result[1]),relationship_effects=effects))
            self.repo._exec('UPDATE state_journal SET appraisal=?,after_state=? WHERE id=?',
                (canonical(raw),canonical(self.life._state_packet(scope)),jid))
            for eid in roots:
                self.repo._exec('INSERT INTO appraisal_stimuli VALUES(?,?,?)',(scope.scope_id,eid,jid))
            return Commit(app,result[1],receipt)

    def record(self,scope,event_id,raw,before,at):
        return self.repo._exec('INSERT INTO state_journal(scope_id,event_id,appraisal,before_state,after_state,rule_version,at) VALUES(?,?,?,?,?,?,?)',
            (scope.scope_id,event_id,canonical(raw),canonical(before),canonical(self.life._state_packet(scope)),'life-state-5',at)).lastrowid

    def time(self,scope,before,at,online,context):
        if getattr(self.life,'_replaying',False):
            return
        event,_ = self.life.ingest(scope,'state_time','inference','',occurred_at=at)
        self.record(scope,event.id,dict(_kind='time',scope=asdict(scope),_drive_config=self.life.drive_config().to_dict(),
            online_seconds=online,context_factors=context),before,at)

    def revise(self,scope,app,roots,claims,fingerprint,call_key):
        old = self.repo._one("SELECT * FROM state_journal WHERE scope_id=? AND json_extract(appraisal,'$.id')=?",
            (scope.scope_id,app.supersedes_appraisal_id))
        if not old:
            raise SessionFailure('stale_context','找不到要替换的现行解释')
        prior = json.loads(old['appraisal'])
        if app.revision!=prior.get('revision',1)+1 or any(c and c['journal_id']!=old['id'] for c in claims):
            raise SessionFailure('stimulus_overlap','修订版本错误或覆盖了其他有效解释')
        old_actor = prior.get('subject_actor_id') or prior['scope'].get('participant_id')
        if app.subject_actor_id and old_actor and app.subject_actor_id!=old_actor:
            raise SessionFailure('actor_mismatch','修订不能改变已核实的人物')
        if prior.get('relation_impacts') or prior.get('relation_evidence'):
            self.life.relationships.subject(scope,old_actor,prior['source_event_ids'])
        self.check_versions(scope,app,relationship=bool(app.relation_impacts or prior.get('relation_impacts')))
        raw = asdict(app)
        raw.update({k:v for k,v in prior.items() if k.startswith('_')})
        raw['_proposal_fingerprint'] = fingerprint
        raw['_stimuli'] = sorted(set(prior.get('_stimuli',prior['source_event_ids']))|set(roots))
        raw['source_event_ids'] = list(dict.fromkeys(prior['source_event_ids']+app.source_event_ids))
        if old['rule_version']=='life-state-4' and app.relation_impacts:
            if any(i['metric']=='bond' or i['metric'] not in {'familiarity','trust','comfort'} for i in app.relation_impacts):
                raise SessionFailure('protocol_mismatch','迁移前历史位置不支持 bond；请按迁移后的真实新经历评价')
            cfg = self.life.relationships.config
            raw['relation_evidence'] = {i['metric']:{'increase':1,'decrease':-1,'unchanged':0}[i['direction']]*
                cfg.magnitudes[i['magnitude']]*cfg.sensitivity[i['metric']] for i in app.relation_impacts}
            raw['_legacy_conversion'] = dict(config=asdict(cfg),impacts=app.relation_impacts)
        self.repo._exec('UPDATE state_journal SET appraisal=? WHERE id=?',(canonical(raw),old['id']))
        for eid in raw['_stimuli']:
            self.repo._exec('INSERT OR IGNORE INTO appraisal_stimuli VALUES(?,?,?)',(scope.scope_id,eid,old['id']))
        self.rebuild(scope.agent_id)
        if any(op.get('actor_id')!=app.subject_actor_id for op in app.relation_context_changes):
            raise SessionFailure('actor_mismatch','修订中的材料须属于同一实际人物')
        notes = [self.life.relationships.mutate_note(scope,op,call_key or app.id,i,f'{app.id}:{app.revision}')
            for i,op in enumerate(app.relation_context_changes)]
        self.repo._exec("UPDATE memory_annotations SET status='superseded' WHERE status='active' AND id IN "
            '(SELECT annotation_id FROM relationship_annotation_links WHERE interpretation_ref=?)',
            (f"{prior['id']}:{prior.get('revision',1)}",))
        placeholders = ','.join('?' for _ in prior['source_event_ids'])
        self.repo._exec("UPDATE memories SET status='disputed',uncertainty_reason='appraisal_revised' WHERE kind='interpretation' AND status='active' "
            'AND id IN (SELECT memory_id FROM memory_sources WHERE event_id IN ('+placeholders+'))',prior['source_event_ids'])
        updated = json.loads(self.repo._one('SELECT appraisal FROM state_journal WHERE id=?',(old['id'],))['appraisal'])
        relation = self.repo.load_state(self.life.state_key('relationship',scope))
        receipt = dict(status='committed',appraisal_id=app.id,revision=app.revision,
            relationship_version=relation[0] if relation else None,policy_version=self.life.relationships.policy.version,
            effects=updated.get('_relation_effects',[]),saved_note_refs=notes,journal_id=old['id'])
        updated['_receipt'] = receipt
        self.repo._exec('UPDATE state_journal SET appraisal=? WHERE id=?',(canonical(updated),old['id']))
        return Commit(app,None,receipt)

    def legacy_relation(self,scope,event,raw,at):
        values = raw.get('relation_evidence')
        if not values:
            return
        key = self.life.state_key('relationship',scope)
        got = self.repo.load_state(key)
        fields = dict(got[1]) if got else dict(relationship_id=scope.participant_id,updated_at=at)
        fields.pop('version',None)
        old = LegacyRelationshipState(scope=scope,**fields)
        evidence = []
        for metric,delta in values.items():
            if metric not in {'familiarity','trust','comfort','repair','boundary'}:
                continue
            confidence = raw.get('certainty',.5)
            if raw.get('_legacy_conversion'):
                item = next((i for i in raw['_legacy_conversion']['impacts'] if i['metric']==metric),None)
                confidence = item['confidence'] if item else confidence
            evidence.append(RelationEvidence(id=stable_relation_id(raw['id'],metric),scope=scope,kind=metric,delta=delta,
                confidence=confidence,root_event_ids=raw['source_event_ids'],note=raw.get('interpretation',''),
                independent_experience_group=stable_relation_group(scope,event)))
        state,_ = reduce_relationship_legacy(old,evidence,at)
        payload = asdict(state)
        payload.pop('scope')
        payload.pop('version')
        self.repo.save_state(key,payload)

    def replay_relation(self,scope,raw,at,jid):
        policy = RelationshipPolicy(**{k:v for k,v in raw['_relation_policy'].items() if k!='version'})
        registry = {k:MetricSpec(**v) for k,v in raw['_relation_registry'].items()}
        cfg = RelationConfig.from_mapping(raw['_relation_config'],registry)
        key = self.life.state_key('relationship',scope)
        got = self.repo.load_state(key)
        if got and got[1].get('schema_version')==2:
            state = self.life.relationships.from_payload(scope,got[1],got[0])
        else:
            values = {k:got[1].get(k,s.default_value) for k,s in registry.items() if k!='bond'} if got else {}
            metrics,_ = normalize_state(values,scope.participant_id,registry,policy)
            covered = self.life.relationships.legacy_covered(scope.participant_id) if got else set()
            state = RelationshipState(scope,scope.participant_id,metrics,policy_version=policy.version,
                metric_basis={k:('recorded_experiences' if k in covered else 'legacy_unverified' if got and k in values else 'policy_initialization') for k in metrics},daily_key=got[1].get('daily_key','') if got else '',
                daily_used={k:got[1].get('daily_used',{}).get(k,0.) if got else 0. for k in metrics},updated_at=at)
        if raw.get('_kind'):
            metrics,adjustments = normalize_state(state.metrics,scope.participant_id,registry,policy)
            state.metrics = metrics
            state.metric_basis = {k:state.metric_basis.get(k,'policy_initialization') for k in metrics}
            state.daily_used = {k:state.daily_used.get(k,0.) for k in metrics}
            raw['_adjustments'] = adjustments
            effects = []
        else:
            state,effects = reduce_relationship(state,raw.get('relation_impacts',[]),actor_id=scope.participant_id,
                policy=policy,registry=registry,config=cfg,settlement_day=raw['_relation_settlement_day'],settlement_at=at)
        state.policy_version,state.last_journal_id,state.updated_at = policy.version,jid,at
        self.repo.save_state(key,self.life.relationships.payload(state))
        raw['_relation_effects'] = effects
        return effects

    def rebuild(self,agent_id,*,forgotten_scope=None):
        with self.repo.tx():
            self._rebuild(agent_id,forgotten_scope)

    def _rebuild(self,agent_id,forgotten_scope):
        rows = [dict(r) for r in self.repo._all('SELECT j.* FROM state_journal j JOIN events e ON e.id=j.event_id WHERE e.agent_id=? '
            "AND j.rule_version IN ('life-state-4','life-state-5') ORDER BY j.id",(agent_id,))]
        initial,scopes,versions = {},{},{}
        affected = set()
        if forgotten_scope:
            affected = {r['actor_id'] for r in self.repo._all('SELECT DISTINCT actor_id FROM events WHERE agent_id=? AND conversation_id=? AND redacted_at IS NOT NULL',forgotten_scope.private_key)}
            affected.add(forgotten_scope.participant_id)
        for row in rows:
            raw = json.loads(row['appraisal'])
            scope = Scope(**raw['scope'])
            for kind,snapshot in json.loads(row['before_state']).items():
                key = self.life.state_key(kind,scope)
                initial.setdefault(key,snapshot)
                scopes.setdefault(key,(kind,scope,row['at'],raw))
        for key,snapshot in initial.items():
            latest = self.repo.load_state(key)
            versions[key] = latest[0]+1 if latest else 1
            self.repo._version_floors[key] = versions[key]
            self.repo._exec('DELETE FROM state_snapshots WHERE aggregate_id=?',(key,))
            self.repo._exec('DELETE FROM applied_effects WHERE aggregate_id=?',(key,))
            kind,scope,at,raw = scopes[key]
            if snapshot:
                payload,seq = snapshot[1],snapshot[2]
                if kind=='relationship' and payload.get('schema_version',1)<2 and scope.participant_id in affected:
                    payload = dict(relationship_id=scope.participant_id,familiarity=.3,trust=.5,comfort=.5,updated_at=at)
            else:
                if kind=='relationship':
                    continue
                payload,seq = dict(updated_at=at),0
                if kind=='affect':
                    cfg = raw.get('_affect_config',asdict(self.life.config.affect))
                    payload.update(intensity=cfg['baseline'],baseline=cfg['baseline'],recovery_half_life_seconds=cfg['half_life'])
                elif kind=='drive':
                    payload = initial_state(scope.scope_id,DriveConfig.from_mapping(raw.get('_drive_config')),at=at).to_dict()
            self.repo._exec('INSERT INTO state_snapshots VALUES(?,?,?,?,?)',(key,versions[key],seq,canonical(payload),self.life.clock()))
        roots = [r['event_id'] for r in rows]
        if roots:
            self.repo._exec('DELETE FROM drive_settlements WHERE root_id IN ('+','.join('?' for _ in roots)+')',roots)
        original_clock,original_drives = self.life.clock,self.life.config.drives
        self.life._replaying = True
        try:
            for row in rows:
                raw = json.loads(row['appraisal'])
                scope = Scope(**raw['scope'])
                self.life.clock = lambda:row['at']
                self.life.config.drives = DriveConfig.from_mapping(raw.get('_drive_config'))
                before = self.life._state_packet(scope)
                kind = raw.get('_kind')
                if kind=='time':
                    self.life.advance_time(scope,row['at'],online_seconds=raw['online_seconds'],context_factors=raw['context_factors'])
                elif kind=='configuration':
                    self.life._settle_affect_config(scope,AffectConfig(**raw['_old_affect']),AffectConfig(**raw['_new_affect']),row['at'])
                elif kind=='relationship_configuration':
                    pass
                elif kind in {'relationship_initialization','relationship_migration','policy_rebase'}:
                    self.replay_relation(scope,raw,row['at'],row['id'])
                else:
                    event = self.repo.get_event(row['event_id'])
                    ids = list(dict.fromkeys(raw['source_event_ids']+raw.get('context_refs',[])))
                    sources = self.repo.events_by_ids(ids)
                    if not event or event.redacted_at or len(sources)!=len(ids) or any(e.redacted_at for e in sources):
                        for note in self.repo._all('SELECT annotation_id FROM relationship_annotation_links WHERE interpretation_ref=?',
                                (f"{raw['id']}:{raw.get('revision',1)}",)):
                            self.repo._exec('DELETE FROM annotation_terms WHERE annotation_id=?',(note['annotation_id'],))
                            self.repo._exec("UPDATE memory_annotations SET text='',sources='[]',cues='[]',status='redacted' WHERE id=?",(note['annotation_id'],))
                            self.repo._exec('DELETE FROM relationship_annotation_links WHERE annotation_id=?',(note['annotation_id'],))
                        self.repo._exec('DELETE FROM appraisal_stimuli WHERE journal_id=?',(row['id'],))
                        self.repo._exec('DELETE FROM state_journal WHERE id=?',(row['id'],))
                        continue
                    fields = {k:v for k,v in raw.items() if not k.startswith('_')}
                    fields['scope'] = scope
                    app = Appraisal(**fields)
                    result = self.life._appraise(scope,event,app,at=row['at'],affect_config=AffectConfig(**raw['_affect_config']),desire_snapshot=raw.get('_desire_pressure',{}))
                    if row['rule_version']=='life-state-4':
                        self.legacy_relation(scope,event,raw,row['at'])
                    elif raw.get('relation_impacts'):
                        self.replay_relation(scope,raw,row['at'],row['id'])
                    if result:
                        raw['_change'] = dict(asdict(result[1]),relationship_effects=raw.get('_relation_effects',[]))
                    if raw.get('_receipt'):
                        relation = self.repo.load_state(self.life.state_key('relationship',scope))
                        raw['_receipt'].update(effects=raw.get('_relation_effects',[]),relationship_version=relation[0] if relation else None)
                self.repo._exec('UPDATE state_journal SET before_state=?,after_state=?,appraisal=? WHERE id=?',
                    (canonical(before),canonical(self.life._state_packet(scope)),canonical(raw),row['id']))
        finally:
            self.life.clock,self.life.config.drives = original_clock,original_drives
            self.life._replaying = False
            self.repo._version_floors.clear()
        for key in self.life.relationships.keys():
            actor = key.rsplit(':',1)[1]
            self.life.relationships.load(self.life.scope('qq:private:'+actor,actor))


def stable_relation_id(app_id,metric):
    from .text import stable_id
    return stable_id('rel',app_id,metric)


def stable_relation_group(scope,event):
    from .text import stable_id
    return stable_id('interaction',scope.scope_id,event.actor_id,str(int(event.occurred_at//300)))

