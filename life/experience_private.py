"""Binding-local R5/R6 state. Never registered in recall, shared transitions or wakes."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
import math

from .agent_session import SessionFailure, canonical, digest
from .drives import growth_k
from .experience_math import clip
from .text import stable_id


class PrivateExperience:
    def __init__(self, experience):
        self.experience, self.life, self.repo = experience, experience.life, experience.repo
        self.repo.db.execute('CREATE TABLE IF NOT EXISTS experience_private_state('
            'agent_id TEXT PRIMARY KEY,scope_id TEXT NOT NULL,payload TEXT NOT NULL)')
        self.repo.db.execute('CREATE TABLE IF NOT EXISTS experience_private_journal('
            'agent_id TEXT NOT NULL,scope_id TEXT NOT NULL,call_key TEXT NOT NULL,fingerprint TEXT NOT NULL,'
            'payload TEXT NOT NULL,receipt TEXT NOT NULL,PRIMARY KEY(agent_id,call_key))')
        self.repo.db.execute('CREATE TABLE IF NOT EXISTS drive_contact_attempts('
            'id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,scope_id TEXT NOT NULL,dimension TEXT NOT NULL,logical_at REAL NOT NULL)')

    def _load(self, scope):
        row=self.repo._one('SELECT payload FROM experience_private_state WHERE agent_id=? AND scope_id=?',
            (self.life.agent_id,scope.scope_id))
        if not row:
            return None
        try:
            s=json.loads(row['payload'])
            if s['scope']!=asdict(scope) or s['epoch']!=self.repo.forget_epoch(scope.scope_id):
                return None
            # Older snapshots predate this trait; never infer it from libido rate.
            s.setdefault('sensitivity_gain',0.)
            for key in ('tension','relief_debt','rate_gain'):
                if type(s[key]) not in (float,int) or not math.isfinite(s[key]) or not 0<=s[key]<=1:
                    raise ValueError(key)
            value=s['sensitivity_gain']
            if type(value) not in (float,int) or not math.isfinite(value) or not 0<=value<=.5:
                raise ValueError('sensitivity_gain')
            return s
        except (ValueError,KeyError,TypeError):
            return None

    def _save(self, scope, state):
        self.repo._exec('INSERT OR REPLACE INTO experience_private_state VALUES(?,?,?)',
            (self.life.agent_id,scope.scope_id,canonical(state)))

    def bind(self, scope, shared, at):
        s=self._load(scope)
        if s and s['generation']==shared['binding_generation']:
            # A new explicit opening clears a previous stop, not prior gains.
            s.update(stopped=False, private_enabled=self.experience.config.private_enabled,selected_expression=None)
        else:
            raw=self.life.drive_view(scope,at)['libido']
            s=dict(scope=asdict(scope),epoch=self.repo.forget_epoch(scope.scope_id),
                generation=shared['binding_generation'],tension=raw['tension'],relief_debt=0.,rate_gain=0.,sensitivity_gain=0.,
                activation=raw['activation'],learned_offset=raw['learned_offset'],satiety=raw['satiety'],
                recovery_until=raw['recovery_until'],cycle_id=raw['cycle_id'],
                integrated_until=at,run=None,stopped=False,private_enabled=self.experience.config.private_enabled,
                high_cycle=0,initiation_armed=True,unanswered=False,declined=False,last_invitation_at=None,
                candidate_id=None,candidate_status=None,selected_expression=None)
        self._save(scope,s)

    def _advance(self, scope, s, at):
        cfg=self.experience.config
        at=max(at,s['integrated_until'])
        dt=at-s['integrated_until']
        drive_cfg=self.life.drive_config()
        dynamics=drive_cfg.dynamics('libido')
        recovery_dt=min(dt,max(0.,s.get('recovery_until',0.)-s['integrated_until']))
        exponent=(growth_k(dynamics,in_recovery=True)*recovery_dt
            +growth_k(dynamics)*(dt-recovery_dt))*(1+min(cfg.rate_gain_cap,s['rate_gain']))/3600.
        s['tension']=clip(dynamics.ceiling-(dynamics.ceiling-s['tension'])*math.exp(-exponent),0.,dynamics.ceiling)
        s['relief_debt'] *= 2**(-dt/cfg.relief_half_life)
        s['activation'] *= 2**(-dt/(3600.*drive_cfg.activation_half_life_hours['libido']))
        s['satiety'] *= 2**(-dt/(3600.*drive_cfg.satiety_half_life_hours['libido']))
        s['integrated_until']=at
        pressure=clip(s['tension']+s['activation']+s['learned_offset'])
        s['effective_pressure']=clip(pressure-s['relief_debt'])
        return pressure

    def read_local(self, scope):
        """Host-local inspection only; intentionally absent from life.read schemas."""
        s=self._load(scope)
        if s:
            self._advance(scope,s,self.life.clock())
        return s

    def sensitivity_bonus(self, scope, generation):
        s=self._load(scope)
        return min(self.experience.config.private_sensitivity_gain_cap,s['sensitivity_gain']) if s and s['generation']==generation else 0.

    def running(self, scope):
        s=self._load(scope)
        return bool(s and s['run'])

    def stop(self, scope, at):
        s=self._load(scope)
        if s:
            s.update(stopped=True,private_enabled=False,run=None,candidate_status='cancelled',selected_expression=None)
            self._save(scope,s)

    def recover(self):
        row=self.repo._one('SELECT payload FROM experience_private_state WHERE agent_id=?',(self.life.agent_id,))
        if not row:
            return
        from .contracts import Scope
        try:
            raw=json.loads(row['payload'])
            scope=Scope(**raw['scope'])
        except (ValueError,KeyError,TypeError):
            self.repo._exec('DELETE FROM experience_private_state WHERE agent_id=?',(self.life.agent_id,))
            return
        s=self._load(scope)
        if s:
            self._advance(scope,s,self.life.clock())
            if s['run']:
                self._journal(scope,'abort-restart:'+s['run']['id'],{},dict(aborted=True),dict(reason='restart'))
            s['run']=None
            s['selected_expression']=None
            self._save(scope,s)

    def apply_evidence(self, scope, ev, at):
        """Called only after the ordinary reducer claims a new effect in its transaction."""
        if ev.dimension!='libido' or getattr(self.life,'_replaying',False):
            return
        s=self._load(scope)
        if not s:
            return
        self._advance(scope,s,at)
        from .drives import DriveState, apply_one_evidence, initial_state
        state=initial_state(scope.scope_id,self.life.drive_config(),at=at)
        state.drives['libido']=DriveState(dimension='libido',updated_at=at,
            **{k:s[k] for k in ('tension','activation','learned_offset','satiety','recovery_until','cycle_id')})
        state,_=apply_one_evidence(state,ev,self.life.drive_config(),now=at)
        for field in ('tension','activation','learned_offset','satiety','recovery_until','cycle_id'):
            s[field]=getattr(state.drives['libido'],field)
        self._save(scope,s)

    def guard_evidence(self, scope, root, kind):
        if kind not in ('internal_regulation','interaction_satisfaction','verified_task_satisfaction'):
            return
        s=self._load(scope)
        completed=self.repo._one("SELECT 1 FROM experience_private_journal WHERE agent_id=? AND scope_id=? "
            "AND json_extract(payload,'$.before.run.id')=?",(self.life.agent_id,scope.scope_id,root))
        if completed or s and s['run'] and s['run']['id']==root:
            raise SessionFailure('private_effect_overlap','私密事件仅结算私密动力学，不能再作为驱力满足或内调扣减')

    def candidate(self, scope, at=None):
        with self.repo.tx():
            s=self._load(scope)
            if not s:
                return None
            self._advance(scope,s,self.life.clock() if at is None else at)
            cfg=self.experience.config
            if s['effective_pressure']<cfg.self_init_rearm and not s['unanswered'] and not s['declined']:
                if not s['initiation_armed']:
                    s['high_cycle']+=1
                s['initiation_armed']=True
                s['candidate_id']=None
            if (s['effective_pressure']>=cfg.self_init_threshold and s['initiation_armed']
                    and not s['candidate_id']):
                s.update(candidate_id=stable_id('initiation',scope.scope_id,str(s['epoch']),str(s['high_cycle'])),
                    candidate_status='pending')
            self._save(scope,s)
            if not s['candidate_id']:
                return None
            _,shared=self.experience._load()
            contact_cfg=self.life.drive_config()
            dynamics=contact_cfg.dynamics('libido')
            cutoff=self.life.clock()-contact_cfg.contact_window_hours*3600.
            attempts=self.repo._one('SELECT COUNT(*) AS total,SUM(CASE WHEN dimension=? THEN 1 ELSE 0 END) AS dimension '
                'FROM drive_contact_attempts WHERE agent_id=? AND logical_at>?',('libido',self.life.agent_id,cutoff))
            eligible=(shared['mode']=='none' and not shared['stop_latched']
                and not s['stopped'] and not s['unanswered'] and not s['declined']
                and not s['run'] and s['effective_pressure']>=cfg.self_init_threshold
                and attempts['total']<contact_cfg.global_contact_per_day
                and (attempts['dimension'] or 0)<dynamics.max_contact_attempts_per_window
                and (s['last_invitation_at'] is None or self.life.clock()>=s['last_invitation_at']+dynamics.contact_cooldown_hours*3600.))
            return dict(candidate_id=s['candidate_id'],desire_present=True,
                eligible=eligible,status=s['candidate_status'],pressure=s['effective_pressure'])

    def _journal(self, scope, key, proposal, receipt, payload):
        self.repo._exec('INSERT INTO experience_private_journal VALUES(?,?,?,?,?,?)',
            (self.life.agent_id,scope.scope_id,key,digest(proposal),canonical(payload),canonical(receipt)))

    def submit(self, scope, p, key):
        with self.repo.tx():
            s=self._load(scope)
            rev,shared=self.experience._load()
            if not s or shared['scope']!=asdict(scope):
                raise SessionFailure('scope_mismatch','先在唯一成年伴侣绑定内建立边界')
            old=self.repo._one('SELECT * FROM experience_private_journal WHERE agent_id=? AND call_key=?',
                (self.life.agent_id,key))
            if old:
                if old['scope_id']!=scope.scope_id or json.loads(old['payload']).get('before',{}).get('scope',asdict(scope))!=asdict(scope):
                    raise SessionFailure('scope_mismatch','私密回执不属于当前绑定')
                if old['fingerprint']!=digest(p):
                    raise SessionFailure('idempotency_conflict','私密调用编号已冻结')
                return dict(json.loads(old['receipt']),duplicate=True)
            if p['base_revision']!=rev:
                raise SessionFailure('stale_context','先读当前simulation版本')
            if s['stopped'] or shared['stop_latched']:
                raise SessionFailure('cancel_requested','停止屏障仍有效，欲望不能自行解除')
            raw=self._advance(scope,s,self.life.clock())
            before=deepcopy(s)
            action=p['action']
            if action=='private.begin':
                if shared['mode']!='none' or s['run'] or not s['private_enabled'] or not self.experience.config.simulation_enabled:
                    raise SessionFailure('experience_busy','私密与共享事件互斥；自我边界必须允许')
                s['run']=dict(id=stable_id('private-run',scope.scope_id,key),started_at=self.life.clock())
                receipt=dict(applied=True,run_id=s['run']['id'])
            elif action=='private.end':
                if not s['run'] or s['run']['id']!=p['run_id']:
                    raise SessionFailure('stale_context','私密运行已结束或失效')
                cfg=self.experience.config
                if p['outcome']=='completed':
                    s['relief_debt']=min(raw,s['relief_debt']+cfg.relief_amount)
                    s['rate_gain']=min(cfg.rate_gain_cap,s['rate_gain']+cfg.rate_gain_per_run)
                    s['sensitivity_gain']=min(cfg.private_sensitivity_gain_cap,
                        s['sensitivity_gain']+cfg.private_sensitivity_gain_per_run)
                s['run']=None
                receipt=dict(applied=True,completed=p['outcome']=='completed')
            else:
                candidate=self.candidate(scope)
                if not candidate or candidate['candidate_id']!=p['candidate_id'] or p['decision']=='act' and not candidate['eligible']:
                    raise SessionFailure('cancel_requested','候选不可执行，未回应或停止不能绕过')
                s['candidate_status']='selected' if p['decision']=='act' else 'deferred'
                receipt=dict(applied=True,candidate_id=s['candidate_id'],status=s['candidate_status'])
            self._save(scope,s)
            # Advance the shared CAS so simultaneous decisions cannot both commit.
            self.experience._save(shared,rev)
            receipt['state_revision']=rev+1
            self._journal(scope,key,p,receipt,dict(origin='self_decision',action=action,
                before=before,after=s,rule_version='adult-intimate-2'))
            return receipt

    def guard_invitation(self, scope, candidate_id, logical=None):
        s=self._load(scope)
        candidate=self.candidate(scope)
        if (not s or not candidate or candidate_id!=s['candidate_id'] or not candidate['eligible']
                or s['candidate_status']!='selected'
                or logical is not None and s['selected_expression'] not in (None,logical)):
            raise SessionFailure('cancel_requested','主动表达候选已失效或已经发送')
        if logical:
            s['selected_expression']=logical
            self._save(scope,s)

    def invitation_delivered(self, scope, candidate_id):
        with self.repo.tx():
            s=self._load(scope)
            if s and s['candidate_id']==candidate_id:
                if s['candidate_status']=='delivered':
                    return
                s.update(unanswered=True,initiation_armed=False,candidate_status='delivered',last_invitation_at=self.life.clock())
                self._save(scope,s)
                self.life.advance_time(scope,self.life.clock(),online_seconds=0.)
                self.life.mark_contact_attempt(scope,'libido',at=self.life.clock(),contact_id=candidate_id)

    def partner_response(self, scope, interested):
        s=self._load(scope)
        if s:
            s.update(unanswered=False,declined=not interested)
            self._save(scope,s)
