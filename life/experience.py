"""ADD-08: one persistent adult_intimate reducer per role.

Only this service writes short-cycle fields. Journals contain boundaries and
source references, not one event/model call per tick. Numeric constants are
configurable engineering parameters, not physiological assertions.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, fields
import json
import math

from .agent_session import SessionFailure, canonical, digest
from .contracts import Scope
from .text import stable_id
from .experience_math import arousal_step, clip, peak_values, reset_episode, settling_values

DOMAIN = 'adult_intimate'
METRICS = ('arousal', 'valence', 'momentary_satiety', 'engagement', 'scene_load',
           'adaptation', 'sensitivity', 'recovery')
RULE = 'adult-intimate-2'
PROTOCOL = 2


def invalid(field, expected):
    raise SessionFailure('invalid_input', f'{field} 必须是{expected}；修正后重提', field=field, expected=expected)


def finite(value, field, low=None, high=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        invalid(field, '有限数值')
    if low is not None and value < low or high is not None and value > high:
        invalid(field, f'{low}到{high}之间的数值')
    return float(value)


@dataclass(frozen=True)
class ExperienceConfig:
    simulation_enabled: bool = True
    expression_policy: str = 'existing_chat'
    threshold: float = .55
    release_threshold: float = .35
    min_hold_seconds: float = 0.
    peak_keep_ratio: float = .4
    recovery_seconds: float = 120.
    arousal_half_life_seconds: float = 1800.
    satiety_half_life_seconds: float = 600.
    valence_half_life_seconds: float = 900.
    adaptation_seconds: float = 300.
    adaptation_recovery_seconds: float = 600.
    load_recovery_seconds: float = 900.
    engagement_seconds: float = 180.
    input_rate_per_second: float = .003
    load_rate_per_second: float = .0002
    sensitivity_min: float = .25
    sensitivity_max: float = 1.5
    sensitivity_gain: float = .06
    threshold_gain: float = .015
    threshold_cap: float = .75
    round_interval_initial: float = 120.
    round_interval_min: float = 30.
    round_interval_k: float = .9
    rearm_hysteresis: float = .05
    permission_timeout: float = 90.
    denial_gain: float = .25
    denial_cap: float = 2.
    settle_tau_min: float = 60.
    settle_tau_max: float = 120.
    settling_arousal_half_life: float = 90.
    self_init_threshold: float = .65
    self_init_rearm: float = .55
    relief_amount: float = .25
    relief_half_life: float = 1800.
    rate_gain_per_run: float = .05
    rate_gain_cap: float = 1.
    private_sensitivity_gain_per_run: float = .02
    private_sensitivity_gain_cap: float = .5
    private_enabled: bool = True

    @classmethod
    def parse(cls, raw=None):
        if raw is None:
            return cls()
        if not isinstance(raw, dict) or set(raw)-{f.name for f in fields(cls)}:
            invalid('experience_config', '已注册参数的对象')
        result = cls(**raw)
        if (type(result.simulation_enabled) is not bool or type(result.private_enabled) is not bool
                or result.expression_policy != 'existing_chat'):
            invalid('experience_config', 'simulation_enabled布尔值及expression_policy=existing_chat')
        for f in fields(cls):
            if f.name not in ('simulation_enabled', 'private_enabled', 'expression_policy'):
                finite(getattr(result, f.name), 'experience_config.'+f.name, 0., 86400.)
        if (not 0 < result.release_threshold < result.threshold <= result.threshold_cap <= .75
                or not 0 < result.peak_keep_ratio < 1
                or not .25 <= result.sensitivity_min <= 1 <= result.sensitivity_max <= 1.5
                or not 0 <= result.sensitivity_gain <= .1 or not 0 <= result.threshold_gain <= .03
                or not 30 <= result.round_interval_min <= result.round_interval_initial <= 600
                or not .8 <= result.round_interval_k < 1
                or not 0 < result.rearm_hysteresis < result.threshold-result.release_threshold
                or not 30 <= result.permission_timeout <= 300
                or not 0 <= result.denial_gain <= .5 or not 1 <= result.denial_cap <= 2
                or not 60 <= result.settle_tau_min <= result.settle_tau_max <= 120
                or not 30 <= result.settling_arousal_half_life <= 300
                or not 0 <= result.self_init_rearm < result.self_init_threshold <= 1
                or not 0 <= result.relief_amount <= .5 or not 300 <= result.relief_half_life <= 7200
                or not 0 <= result.rate_gain_per_run <= .1 or not 0 <= result.rate_gain_cap <= 1
                or not 0 <= result.private_sensitivity_gain_per_run <= .1
                or not 0 <= result.private_sensitivity_gain_cap <= .5
                or result.input_rate_per_second > .1 or result.load_rate_per_second > .1
                or any(getattr(result, key) <= 0 for key in ('recovery_seconds', 'arousal_half_life_seconds',
                    'satiety_half_life_seconds', 'valence_half_life_seconds', 'adaptation_seconds',
                    'adaptation_recovery_seconds', 'load_recovery_seconds', 'engagement_seconds'))):
            invalid('experience_config', '有效阈值、恢复时常和有界响应参数')
        return result


def validate_proposal(p):
    if not isinstance(p, dict):
        invalid('simulation', '对象')
    allowed={'experience_protocol','experience_domain','action','base_revision','sources','subject_actor_id',
        'adult_participants','context_confirmed','willingness','profile_confirmed','input_profile','run_ref',
        'reason','direction','effects','confidence','supersedes_call_key','transition_refs','handled_input_ids','activity_changes','meaning_kind',
        'request_id','decision','evidence_ref','evidence_text','run_id','outcome','candidate_id','episode_id','authorization_mode'}
    if set(p)-allowed:
        invalid('simulation.'+sorted(set(p)-allowed)[0], '已注册的模型输入字段；来源身份、规则和积分游标由宿主签发')
    if p.get('experience_protocol') != PROTOCOL or type(p.get('experience_protocol')) is not int:
        raise SessionFailure('protocol_mismatch', '专项体验需要 experience_protocol:2；旧协议不能绕过许可门')
    if p.get('experience_domain') != DOMAIN:
        raise SessionFailure('experience_domain_mismatch', '刺激度与峰值仅适用于 adult_intimate；普通行为请使用情绪/需要评价')
    if not isinstance(p.get('action'),str) or p['action'] not in {'enter', 'exit', 'episode.end', 'start', 'adjust', 'pause', 'resume', 'stop', 'appraise_input', 'ack', 'permission.respond', 'private.begin', 'private.end', 'initiation.select'}:
        invalid('simulation.action', '已注册专项动作')
    if type(p.get('base_revision')) is not int or p['base_revision'] < 0:
        invalid('simulation.base_revision', '当前非负状态版本')
    for key in ('sources', 'transition_refs'):
        if key in p and (not isinstance(p[key], list) or any(not isinstance(v, str) for v in p[key])):
            invalid('simulation.'+key, '字符串数组')
    for key in ('reason', 'subject_actor_id', 'run_ref', 'supersedes_call_key'):
        if key in p and not isinstance(p[key], str):
            invalid('simulation.'+key, '字符串')
    for key in ('adult_participants', 'context_confirmed'):
        if key in p and type(p[key]) is not bool:
            invalid('simulation.'+key, '布尔值')
    if 'willingness' in p and p['willingness'] not in ('interested', 'not_now', 'uncertain'):
        invalid('simulation.willingness', 'interested/not_now/uncertain')
    for key in ('request_id','evidence_ref','evidence_text','run_id','candidate_id','episode_id'):
        if key in p and (not isinstance(p[key], str) or not p[key].strip()):
            invalid('simulation.'+key, '非空字符串')
    if p['action']=='permission.respond':
        if p.get('decision') not in ('allow','deny','uncertain') or not p.get('request_id') or not p.get('evidence_ref'):
            invalid('simulation.permission', 'request_id、decision=allow/deny/uncertain、evidence_ref')
    if p['action']=='private.end' and (not p.get('run_id') or p.get('outcome') not in ('completed','aborted')):
        invalid('simulation.private.end', 'run_id及outcome=completed/aborted')
    if p['action']=='initiation.select' and (not p.get('candidate_id') or p.get('decision') not in ('act','defer','decline')):
        invalid('simulation.initiation.select', 'candidate_id及decision=act/defer/decline')
    if p['action'] in ('start', 'adjust'):
        if p.get('authorization_mode','one_shot') not in ('one_shot','until_cancel'):
            invalid('simulation.authorization_mode','one_shot/until_cancel')
        profile = p.get('input_profile')
        if not isinstance(profile, dict):
            invalid('simulation.input_profile', '已明确确认的输入方案对象')
        if set(profile)-{'method','intensity','valence','rhythm','on_seconds','off_seconds'}:
            invalid('simulation.input_profile', '已注册方式、强度、感受和节奏字段；运行范围和参数版本由宿主绑定')
        if not isinstance(profile.get('method'), str) or not profile['method'].strip():
            invalid('simulation.input_profile.method', '已确认方式标签')
        finite(profile.get('intensity'), 'simulation.input_profile.intensity', 0, 1)
        finite(profile.get('valence'), 'simulation.input_profile.valence', -1, 1)
        if profile.get('rhythm') not in ('continuous', 'intermittent'):
            invalid('simulation.input_profile.rhythm', 'continuous/intermittent')
        if profile['rhythm'] == 'intermittent':
            finite(profile.get('on_seconds'), 'simulation.input_profile.on_seconds', 1, 86400)
            finite(profile.get('off_seconds'), 'simulation.input_profile.off_seconds', 1, 86400)
        if p.get('profile_confirmed') is not True:
            invalid('simulation.profile_confirmed', '基于当前用户原话确认方式的true；未明确方式先澄清')
    if p['action'] == 'appraise_input':
        if p.get('direction') not in ('increase', 'decrease', 'unchanged'):
            invalid('simulation.direction', 'increase/decrease/unchanged')
        effects = p.get('effects', {})
        if not isinstance(effects, dict) or set(effects)-set(METRICS):
            invalid('simulation.effects', '已注册即时指标的对象')
        # Recovery/sensitivity/adaptation are endogenous, not model controls.
        if set(effects) & {'recovery', 'sensitivity', 'adaptation', 'momentary_satiety', 'scene_load'}:
            invalid('simulation.effects', '仅arousal/valence/engagement语义冲量；其他字段由reducer计算')
        for key, value in effects.items():
            finite(value, 'simulation.effects.'+key, -.25, .25)
        finite(p.get('confidence', .5), 'simulation.confidence', 0, 1)
        delta = effects.get('arousal', 0)
        if (p['direction']=='increase' and delta <= 0 or p['direction']=='decrease' and delta >= 0
                or p['direction']=='unchanged' and any(effects.values())):
            invalid('simulation.direction/effects', '方向一致的语义增量；无影响使用unchanged及空effects')
        if not isinstance(p.get('reason'), str) or not p['reason'].strip():
            invalid('simulation.reason', '本条语义依据或无影响原因')


def initial(at, config):
    state = dict(experience_domain=DOMAIN, experience_context='closed', scope=None, scene_generation=0,
        arousal=0., valence=0., momentary_satiety=0., engagement=0., scene_load=0., adaptation=0.,
        sensitivity=1., recovery=1., willingness='uncertain', willingness_evidence=[], phase='settled',
        peak_count=0, scene_peak_count=0, armed=True, released=True, candidate_since=None, recovery_until=0., last_impulse_refs=[],
        integrated_until=at, last_activity_at=None, last_increment_at=None, last_transition_seq=0,
        run=None, fences={}, last_exit_seq=0, control_barrier_seq=0, rule_version=RULE, cutover_at=at,
        field_owner='experience_reducer', config=asdict(config), config_version=digest(asdict(config)),
        binding_generation=0, binding_epoch=0, stop_latched=False, control_revision=0)
    reset_episode(state, config, at)
    return state


def decay(value, seconds, half_life):
    return value * 2 ** (-seconds / half_life)


class ExperienceSimulation:
    def __init__(self, life, config=None):
        self.life, self.repo = life, life.repo
        self.config_error = None
        try:
            self.config = ExperienceConfig.parse(config)
        except SessionFailure as error:
            self.config, self.config_error = ExperienceConfig(), str(error)
        schema=self.repo._one('SELECT v FROM meta WHERE k=?',('experience_schema:'+life.agent_id,))
        if schema and int(schema['v']) not in (1, 2):
            raise SessionFailure('protocol_mismatch','专项体验持久格式不兼容，保留数据库并使用匹配代码')
        for sql in (
            'CREATE TABLE IF NOT EXISTS experience_state(agent_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,payload TEXT NOT NULL,initial_state TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS experience_journal(seq INTEGER PRIMARY KEY AUTOINCREMENT,agent_id TEXT NOT NULL,call_key TEXT NOT NULL,kind TEXT NOT NULL,logical_at REAL NOT NULL,payload TEXT NOT NULL,receipt TEXT NOT NULL,UNIQUE(agent_id,call_key))',
            'CREATE TABLE IF NOT EXISTS experience_inputs(agent_id TEXT NOT NULL,event_id TEXT NOT NULL,source_version TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,call_key TEXT,PRIMARY KEY(agent_id,event_id))',
            'CREATE TABLE IF NOT EXISTS experience_transitions(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,seq INTEGER NOT NULL,payload TEXT NOT NULL,ack_state TEXT NOT NULL,status TEXT NOT NULL DEFAULT \'active\')',
            'CREATE TABLE IF NOT EXISTS experience_faults(id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,payload TEXT NOT NULL,logical_at REAL NOT NULL)'):
            self.repo.db.execute(sql)
        from .experience_private import PrivateExperience
        self.private = PrivateExperience(self)
        if self.config_error:
            with self.repo.tx():
                current=self.repo._one('SELECT payload FROM experience_state WHERE agent_id=?',(life.agent_id,))
                if current:
                    try:
                        self.config=ExperienceConfig.parse(json.loads(current['payload'])['config'])
                    except (ValueError,TypeError,KeyError,SessionFailure):
                        pass
                sequence=self.repo._one('SELECT COALESCE(MAX(seq),0)+1 AS n FROM experience_journal')['n']
                self._journal('config-rejected:'+str(sequence), 'config_rejected', life.clock(),
                    dict(reason=self.config_error, fallback=asdict(self.config)), {})

    def _load(self):
        row = self.repo._one('SELECT * FROM experience_state WHERE agent_id=?', (self.life.agent_id,))
        if not row:
            return 0, initial(self.life.clock(), self.config)
        try:
            state = json.loads(row['payload'])
            if state.get('rule_version') != RULE:
                state = initial(self.life.clock(), self.config)
                state.update(scope=json.loads(row['payload']).get('scope'), stop_latched=True,
                    migration_required=True)
            for metric in METRICS:
                finite(state[metric], metric, -1. if metric=='valence' else 0.,
                    1.5 if metric=='sensitivity' else 1.)
            cfg = ExperienceConfig.parse(state['config'])
            finite(state['current_threshold'], 'current_threshold', cfg.threshold, cfg.threshold_cap)
            finite(state['round_interval_seconds'], 'round_interval_seconds', cfg.round_interval_min, cfg.round_interval_initial)
            for field in ('denied_count', 'completed_rounds'):
                if type(state[field]) is not int or not 0 <= state[field] <= 65535:
                    raise ValueError(field)
            if state['mode'] not in ('none','shared') or state['phase'] not in ('idle','building','plateau','awaiting_permission','settling'):
                raise ValueError('phase')
            finite(state['integrated_until'], 'integrated_until', 0.)
            for field in ('control_revision','last_transition_seq','binding_generation','binding_epoch','scene_generation'):
                if type(state[field]) is not int or not 0<=state[field]<2147483647:
                    raise ValueError(field)
            if state['scope'] is not None:
                Scope(**state['scope'])
            if type(state['stop_latched']) is not bool or not isinstance(state['fences'],dict):
                raise ValueError('controls')
            for at in state['fences'].values():
                finite(at,'fence',0.)
            request=state['permission_request']
            if state['phase']=='awaiting_permission':
                if (not isinstance(request,dict) or request['status']!='pending'
                        or request['scope']!=state['scope'] or request['control_revision']!=state['control_revision']
                        or request['episode_id']!=state['episode_id'] or not request['request_id']
                        or request['delivery_status'] not in ('unsent','confirmed')):
                    raise ValueError('permission_request')
                finite(request['created_at'],'request.created_at',0.)
                if request['delivery_status']=='confirmed':
                    finite(request['delivered_at'],'delivered_at',0.)
                    finite(request['expires_at'],'expires_at',request['delivered_at'])
            elif request is not None:
                raise ValueError('unexpected_request')
            if state['phase']=='settling':
                anchor=state['settle_anchor']
                finite(anchor['at'],'settle.at',0.)
                finite(anchor['deadline']-anchor['at'],'settle.duration',60.,120.)
                for field in ('arousal','momentary_satiety','engagement','valence'):
                    finite(anchor[field],field,-1. if field=='valence' else 0.,1.)
            return row['revision'], state
        except (ValueError, TypeError, KeyError, SessionFailure):
            state = initial(self.life.clock(), self.config)
            state.update(stop_latched=True, state_fault=True)
            return row['revision'], state

    def _ensure(self):
        revision, state = self._load()
        if not revision:
            # Historical stimulation is never imported as a new episode/consent.
            self.repo._exec('INSERT INTO experience_state VALUES(?,?,?,?)',
                (self.life.agent_id, 1, canonical(state), canonical(state)))
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('experience_schema:'+self.life.agent_id,'2'))
            for row in self.repo._all("SELECT d.root_id FROM drive_settlements d JOIN events e ON e.id=d.root_id "
                    "WHERE e.agent_id=? AND d.effect_kind='stimulation_change' AND d.revoked_at IS NULL", (self.life.agent_id,)):
                e = self.repo.get_event(row['root_id'])
                self.repo._exec('INSERT OR IGNORE INTO experience_inputs VALUES(?,?,?,?,?,?)',
                    (self.life.agent_id, e.id, digest(e.payload), 'legacy_settled', 'cutover以前已结算，仅作背景，不回灌', 'legacy:'+e.id))
            return 1, state
        return revision, state

    def _save(self, state, revision):
        cur = self.repo._exec('UPDATE experience_state SET revision=?,payload=? WHERE agent_id=? AND revision=?',
            (revision+1, canonical(state), self.life.agent_id, revision))
        if cur.rowcount != 1:
            raise SessionFailure('stale_context', '专项状态版本冲突，请重读simulation')
        run = state.get('run')
        if run:
            # The until_cancel activity is the same input owner/checkpoint.
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',
                ('activity_run:'+run['simulation_run_id'], canonical(dict(run,
                    experience_domain=DOMAIN, scope=state['scope'], integrated_until=state['integrated_until'],
                    last_transition_seq=state['last_transition_seq'], pending_input_fence=state['fences']))))

    def _effective_sensitivity(self, s):
        bonus=0.
        if s['scope']:
            bonus=self.private.sensitivity_bonus(Scope(**s['scope']),s['binding_generation'])
        cfg=ExperienceConfig.parse(s['config'])
        return min(cfg.sensitivity_max,s['sensitivity']+bonus)

    def _metrics(self, s):
        return dict({k:s[k] for k in METRICS},sensitivity=self._effective_sensitivity(s))

    def _transition(self, s, at, key, refs, out):
        cfg = ExperienceConfig.parse(s['config'])
        if (not s['armed'] or s['experience_context']!='active' or s['willingness']!='interested'
                or s['mode']!='shared' or s['stop_latched'] or s.get('permission_request')
                or at < s['next_peak_eligible_at'] or s['rearm_requires_intent']):
            return
        if s['arousal'] < s['current_threshold']:
            s['candidate_since'] = None
            return
        if s['candidate_since'] is None:
            s['candidate_since'] = at
        if at-s['candidate_since']+1e-9 < cfg.min_hold_seconds:
            s['phase'] = 'plateau'
            return
        previous = s['phase']
        s['last_transition_seq'] += 1
        run = s.get('run') or {}
        identity = stable_id('experience-transition', self.life.agent_id, key, str(s['last_transition_seq']))
        request_id = stable_id('permission', s['episode_id'], s['attempt_id'])
        s['permission_request'] = dict(request_id=request_id, episode_id=s['episode_id'], attempt_id=s['attempt_id'],
            status='pending', delivery_status='unsent', expression_id=None, created_at=at,
            delivered_at=None, expires_at=None, control_revision=s['control_revision'],
            source_refs=list(refs), scope=s['scope'], consumed_by_transition_id=None)
        out.append(dict(id=identity, seq=s['last_transition_seq'], run_id=run.get('simulation_run_id') if run.get('desired_active') else None,
            control_revision=run.get('control_revision', 0), from_state=previous, to_state='peak',
            logical_at=at, evidence_refs=refs, effect_origin='authorized_simulation' if key.startswith('interval:') else 'event_impulse',
            rule_version=RULE, scope=s['scope'], request_id=request_id))
        out[-1]['to_state'] = 'awaiting_permission'
        s['phase'], s['armed'], s['candidate_since'] = 'awaiting_permission', False, None

    def _close_request(self, s, at, reason, out, *, denied=False, source_seq=0):
        request = s.get('permission_request')
        if not request or request['status']!='pending':
            return
        request.update(status=reason, closed_at=at)
        if denied:
            s['denied_count'] = min(65535, s['denied_count']+1)
            s.update(rearm_requires_intent=True, rearm_after_seq=source_seq,
                rearm_evidence_seq=0)
        s['last_transition_seq'] += 1
        out.append(dict(id=stable_id('permission-close', request['request_id'], reason),
            seq=s['last_transition_seq'], from_state='awaiting_permission', to_state='building',
            logical_at=at, scope=s['scope'], run_id=None, control_revision=s['control_revision'],
            request_id=request['request_id'], reason=reason, evidence_refs=request['source_refs'], rule_version=RULE))
        s.update(phase='building', permission_request=None, armed=False, candidate_since=None,
            attempt_id=stable_id('attempt', s['episode_id'], request['request_id'], reason), armed_after_seq=source_seq)

    def _permission_response(self, s, p, at, key, refs, out):
        request = s.get('permission_request')
        if (not request or request['request_id']!=p['request_id'] or request['status']!='pending'
                or request['delivery_status']!='confirmed' or request['expires_at']<=at
                or s['phase']!='awaiting_permission' or request['control_revision']!=s['control_revision']):
            raise SessionFailure('permission_invalid', '当前请求未送达、已过期或控制版本失效；沉默不是许可')
        if p['decision']=='uncertain':
            return
        if p['decision']=='deny':
            self._close_request(s, at, 'denied', out, denied=True, source_seq=p['_source_seq'])
            return
        cfg = ExperienceConfig.parse(s['config'])
        before = dict(arousal=s['arousal'], denied_count=s['denied_count'])
        values = peak_values(s, cfg)
        s.update({k:v for k,v in values.items() if k not in ('release_factor','release_amplitude')})
        s['last_transition_seq'] += 1
        identity = stable_id('permission-peak', request['request_id'])
        request.update(status='consumed',consumed_by_transition_id=identity,response_source=refs[-1])
        out.append(dict(id=identity, seq=s['last_transition_seq'], from_state='awaiting_permission', to_state='peak',
            logical_at=at, evidence_refs=refs, permission_request=deepcopy(request),
            permission_response=refs[-1], before=before,
            after=dict(values,sensitivity=self._effective_sensitivity(s)), scope=s['scope'],
            rule_version=RULE, effect_origin='explicit_permission', run_id=None, control_revision=s['control_revision']))
        s.update(phase='building', armed=False, permission_request=None, denied_count=0,
            completed_rounds=min(65535,s['completed_rounds']+1),
            peak_count=min(2147483647,s['peak_count']+1), scene_peak_count=min(65535,s['scene_peak_count']+1),
            recovery=0., recovery_until=at+s['round_interval_seconds'],
            next_peak_eligible_at=at+s['round_interval_seconds'],
            attempt_id=stable_id('attempt', identity), armed_after_seq=p['_source_seq'], candidate_since=None)
        if s['arousal']<=s['current_threshold']-cfg.rearm_hysteresis:
            s['armed']=True

    def _integrate(self, s, until, key, out, *, final=False, allow_input=True, stop_on_transition=False,
                   effective_sensitivity=None):
        cfg = ExperienceConfig.parse(s['config'])
        sensitivity=self._effective_sensitivity(s) if effective_sensitivity is None else effective_sensitivity
        target = until if final else math.floor(until)
        if s['phase']=='settling':
            anchor = s['settle_anchor']
            if target >= anchor['deadline']:
                reset_episode(s, cfg, max(s['integrated_until'], target))
            else:
                s.update(settling_values(anchor, max(s['integrated_until'],target), cfg))
                s['integrated_until'] = max(s['integrated_until'],target)
            return
        if s['mode']=='none' and not any(s[k] for k in ('arousal','valence','engagement','momentary_satiety','adaptation','scene_load')):
            s['integrated_until']=max(s['integrated_until'],target)
            return
        while s['integrated_until'] < target-1e-9:
            start = s['integrated_until']
            request = s.get('permission_request')
            end = min(target, math.floor(start+1e-9)+1.)
            if request and request.get('expires_at'):
                end = min(end, max(start, request['expires_at']))
            if request and request.get('expires_at') and start>=request['expires_at']:
                self._close_request(s, start, 'timeout', out, denied=True,
                    source_seq=self._latest_source_seq(s))
                request = None
                end = min(target, math.floor(start+1e-9)+1.)
            dt = end-start
            old = {k:s[k] for k in METRICS}
            run = s.get('run') or {}
            on = (allow_input and cfg.simulation_enabled and s['experience_context']=='active'
                  and s['willingness']=='interested' and run.get('desired_active')
                  and run.get('execution_state')=='running' and not request
                  and not s['rearm_requires_intent'] and not s['stop_latched'])
            if run.get('authorization_mode')=='one_shot' and start>=run['profile_started_at']+1.:
                on=False
            profile = run.get('profile') or {}
            if on and profile.get('rhythm')=='intermittent':
                period = profile['on_seconds']+profile['off_seconds']
                on = (start-run['profile_started_at']) % period < profile['on_seconds']
            intensity = profile.get('intensity', 0.) if on else 0.
            recovering = start < s['recovery_until']
            response = ((1.-.75*old['adaptation'])*(1.-.6*old['scene_load'])*sensitivity
                        *max(0., .5+.5*old['valence'])*(0. if recovering else 1.))
            s['arousal'] = arousal_step(old['arousal'], dt, cfg.input_rate_per_second*intensity*response,
                cfg.arousal_half_life_seconds)
            if request:
                s['arousal'] = max(cfg.release_threshold, s['arousal'])
            s['valence'] = max(-1., min(1., decay(old['valence'], dt, cfg.valence_half_life_seconds)
                + (profile.get('valence', 0.)-old['valence'])*intensity*(1-math.exp(-dt/300.))))
            s['adaptation'] = (1.-(1.-old['adaptation'])*math.exp(-intensity*dt/cfg.adaptation_seconds)
                if intensity else old['adaptation']*math.exp(-dt/cfg.adaptation_recovery_seconds))
            s['scene_load'] = min(1., old['scene_load']*math.exp(-dt/cfg.load_recovery_seconds)
                + cfg.load_rate_per_second*intensity*dt)
            s['engagement'] = old['engagement']+(intensity-old['engagement'])*(1-math.exp(-dt/cfg.engagement_seconds))
            s['momentary_satiety'] = decay(old['momentary_satiety'], dt, cfg.satiety_half_life_seconds)
            s['recovery'] = clip(1.-(s['recovery_until']-end)/s['round_interval_seconds'])
            s['integrated_until'] = end
            if s['arousal'] <= cfg.release_threshold:
                s['released'] = True
            if not s['armed'] and not request:
                if not s['rearm_requires_intent'] and s['arousal'] <= s['current_threshold']-cfg.rearm_hysteresis:
                    s['armed'] = True
                    s['candidate_since'] = None
                    s['phase'] = 'building' if s['mode']=='shared' else 'idle'
            elif not request and s['arousal'] < s['current_threshold']:
                s['phase'] = 'building' if s['mode']=='shared' else 'idle'
            # No peak is invented while offline, paused or outside this domain.
            if allow_input and s['experience_context']=='active' and (on or s['candidate_since'] is not None):
                count=len(out)
                self._transition(s, end, ('interval:' if on else 'held-impulse:')+str(run.get('simulation_run_id'))+':'+str(end),
                    run.get('source_refs', []) if on else s.get('last_impulse_refs', []), out)
                if stop_on_transition and len(out)>count:
                    return
        request = s.get('permission_request')
        if request and request.get('expires_at') and target>=request['expires_at']:
            self._close_request(s, request['expires_at'], 'timeout', out, denied=True,
                source_seq=self._latest_source_seq(s))

    def _latest_source_seq(self, s):
        if not s.get('scope'):
            return 0
        row = self.repo._one('SELECT MAX(ingest_seq) AS n FROM events WHERE agent_id=? AND conversation_id=?',
            (self.life.agent_id,s['scope']['conversation_id']))
        return (row['n'] or 0) if row else 0

    def _store_transitions(self, transitions, *, replay=False):
        for t in transitions:
            self.repo._exec('INSERT INTO experience_transitions VALUES(?,?,?,?,?,?) '
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,status='active',ack_state=CASE "
                "WHEN excluded.ack_state='revision_only' AND experience_transitions.ack_state='pending' THEN 'revision_only' ELSE experience_transitions.ack_state END",
                (t['id'], self.life.agent_id, t['seq'], canonical(t), 'revision_only' if replay else 'pending', 'active'))

    def _journal(self, key, kind, at, p, receipt):
        self.repo._exec('INSERT INTO experience_journal(agent_id,call_key,kind,logical_at,payload,receipt) VALUES(?,?,?,?,?,?)',
            (self.life.agent_id, key, kind, at, canonical(p), canonical(receipt)))

    def _advance(self, s, until, key, *, final=False, allow_input=True):
        run = s.get('run') or {}
        if allow_input and run.get('execution_state')=='running' and not self.valid_run(s):
            # Every caller (including context preparation and input receipt)
            # must honor revoked sources, not only the periodic maintenance tick.
            allow_input=False
            self._apply(s,dict(action='pause',reason='source_or_scope_invalid'),s['integrated_until'],key,[],[])
            self._journal(key+':invalid-source','proposal',s['integrated_until'],
                dict(action='pause',reason='source_or_scope_invalid',sources=run['source_refs'],_scope=s['scope']),{})
            self._cancel_wakes()
        transitions = []
        fence = min(s['fences'].values(), default=until)
        until = min(until, fence)
        target = until if final else math.floor(until)
        while s['integrated_until'] < target-1e-9:
            start = s['integrated_until']
            end = min(target, (math.floor(start/3600)+1)*3600.)
            recent = []
            self._integrate(s, end, key, recent, final=True, allow_input=allow_input, stop_on_transition=True)
            transitions.extend(recent)
            run = s.get('run') or {}
            payload = dict(effect_origin='authorized_simulation' if allow_input and
                run.get('desired_active') and run.get('execution_state')=='running' else 'natural_change',
                run_id=run.get('simulation_run_id'), input_revision=run.get('input_revision'),
                **{'from':start, 'to':s['integrated_until']}, source_refs=run.get('source_refs', []),
                rule_version=RULE, input_kind='continuous_input' if allow_input and run.get('desired_active') and run.get('execution_state')=='running' else 'natural_change',
                allow_input=allow_input, transition_refs=[t['id'] for t in recent])
            payload['effective_sensitivity']=self._effective_sensitivity(s)
            # Keep one open interval checkpoint per profile/hour, sealing it on
            # controls, inputs and significant transitions. No row per tick.
            last = self.repo._one('SELECT * FROM experience_journal WHERE agent_id=? ORDER BY seq DESC LIMIT 1', (self.life.agent_id,))
            old = json.loads(last['payload']) if last and last['kind']=='interval' else None
            merge = (old and old['to']==start and math.floor(old['from']/3600)==math.floor(start/3600)
                and not old.get('transition_refs') and not recent and all(old.get(k)==payload.get(k)
                    for k in ('effect_origin', 'run_id', 'input_revision', 'source_refs', 'rule_version', 'allow_input','effective_sensitivity')))
            if merge:
                old['to'] = payload['to']
                self.repo._exec('UPDATE experience_journal SET logical_at=?,payload=? WHERE seq=?',
                    (s['integrated_until'], canonical(old), last['seq']))
            else:
                self._journal(key+':'+str(start), 'interval', s['integrated_until'], payload, {})
        self._store_transitions(transitions)
        return transitions

    def advance(self, until=None, *, expected_control_revision=None):
        until = finite(self.life.clock() if until is None else until, 'until')
        with self.repo.tx():
            revision, s = self._load()
            if not revision:
                return None
            if expected_control_revision is not None and (s.get('run') or {}).get('control_revision') != expected_control_revision:
                raise SessionFailure('stale_context', '旧持续输入控制版本已失效')
            before = canonical(s)
            if s.get('state_fault'):
                self._quarantine(revision,s)
                self._save(s,revision)
                return dict(state_revision=revision+1,integrated_until=s['integrated_until'],transition_refs=[])
            transitions = self._advance(s, until, 'advance:'+str(revision))
            if before != canonical(s):
                self._save(s, revision)
                revision += 1
            return dict(integrated_until=s['integrated_until'], state_revision=revision,
                        transition_refs=[t['id'] for t in transitions])

    def read(self, scope, after_transition_seq=None):
        if after_transition_seq is not None and (type(after_transition_seq) is not int or after_transition_seq<0):
            invalid('after_transition_seq','非负整数')
        revision, s = self._load()
        # The one role's private state must not leak across audience bindings.
        if s['scope'] and s['scope'] != asdict(scope):
            return dict(experience_protocol=PROTOCOL, available=False, reason='bound_to_another_audience')
        run=s.get('run') or {}
        if revision and (s['fences'] or run.get('execution_state')!='running' or s['experience_context']!='active'):
            checkpoint=s['integrated_until']
            self._integrate(s,self.life.clock(),'read-projection',[],final=True,allow_input=False)
            if s['integrated_until']>checkpoint:
                s['projection']=dict(from_checkpoint=checkpoint,through=s['integrated_until'],external_input=False,committed=False)
                s['integrated_until']=checkpoint
        transitions = [dict(json.loads(r['payload']), ack_state=r['ack_state']) for r in self.repo._all(
            "SELECT * FROM experience_transitions WHERE agent_id=? AND status='active' AND seq>? ORDER BY seq",
            (self.life.agent_id, after_transition_seq or 0))]
        transitions = [t for t in transitions if t['scope']==asdict(scope) and t.get('rule_version')==RULE]
        inputs = [dict(r) for r in self.repo._all('SELECT event_id,status,reason,source_version FROM experience_inputs '
            'WHERE agent_id=? ORDER BY rowid DESC LIMIT 32', (self.life.agent_id,))]
        s['sensitivity']=self._effective_sensitivity(s)
        s['config']={k:v for k,v in s['config'].items() if k not in
            ('private_enabled','relief_amount','relief_half_life','rate_gain_per_run','rate_gain_cap',
             'private_sensitivity_gain_per_run','private_sensitivity_gain_cap')}
        return dict(experience_protocol=PROTOCOL, available=True, state_revision=revision, **deepcopy(s),
                    transitions=transitions, input_coverage=inputs)

    def bind_request_expression(self, scope, request_id, logical):
        """Host-only outbox binding. The model cannot mark a request delivered."""
        with self.repo.tx():
            rev,s=self._load()
            r=s.get('permission_request')
            if (s['scope']!=asdict(scope) or s['stop_latched'] or not r or r['request_id']!=request_id
                    or r['expression_id'] not in (None,logical)):
                raise SessionFailure('cancel_requested','许可请求已失效或已有冻结表达；不得重复请求')
            if r['expression_id'] is None:
                r['expression_id']=logical
                self._save(s,rev)

    def request_delivered(self, scope, request_id, logical):
        """Called by confirmed transport/reconciliation, never by life.simulation."""
        with self.repo.tx():
            rev,s=self._load()
            r=s.get('permission_request')
            if (s['scope']!=asdict(scope) or s['stop_latched'] or not r or r['request_id']!=request_id
                    or r['expression_id']!=logical or r['delivery_status']=='confirmed'):
                return
            now=max(s['integrated_until'],self.life.clock())
            # Expiry begins after actual delivery, not when the candidate was made.
            cfg=ExperienceConfig.parse(s['config'])
            r.update(delivery_status='confirmed',delivered_at=now,expires_at=now+cfg.permission_timeout,
                delivery_source_seq=self._latest_source_seq(s))
            self._journal('permission-delivered:'+request_id,'permission_delivery',now,
                dict(request_id=request_id,expression_id=logical,scope=asdict(scope)),deepcopy(r))
            self._save(s,rev)

    def request_failed(self, scope, request_id, logical):
        with self.repo.tx():
            rev,s=self._load()
            r=s.get('permission_request')
            if s['scope']!=asdict(scope) or not r or r['request_id']!=request_id or r['expression_id']!=logical:
                return
            out=[]
            self._close_request(s,self.life.clock(),'delivery_failed',out)
            s.update(rearm_requires_intent=True,rearm_after_seq=self._latest_source_seq(s),rearm_evidence_seq=0)
            self._store_transitions(out)
            self._save(s,rev)

    def _sources(self, scope, refs):
        if not refs:
            raise SessionFailure('source_removed', '专项操作需要当前实际用户原话sources')
        events = []
        for ref in refs:
            e = self.repo.get_event(ref)
            resource = self.life.catalog.resolve(ref)
            context = self.life.life_access.bind(scope,mode='internal')
            if (not e or e.redacted_at is not None or e.scope.private_key != scope.private_key
                    or e.scope.audience_policy_id != scope.audience_policy_id or e.source_kind!='user_report'
                    or e.actor_id != scope.participant_id or not resource or not self.life.catalog.live(resource)
                    or not self.life.life_access.internal_allowed(context,resource)):
                raise SessionFailure('source_removed', '专项输入须引用当前受众真实用户交流；想象、引用和自身输出不能冒充对方行为')
            events.append(e)
        return events

    def _apply(self, s, p, at, key, refs, transitions):
        action = p['action']
        if action == 'enter':
            previous_scope = s['scope']
            reset_episode(s, ExperienceConfig.parse(s['config']), at)
            s.pop('state_fault',None)
            s.pop('migration_required',None)
            if previous_scope != asdict(p['_scope']):
                s['binding_generation'] += 1
            s.update(experience_context='active', scene_generation=s['scene_generation']+1,
                scope=asdict(p['_scope']), willingness=p['willingness'], willingness_evidence=refs,
                scene_peak_count=0, last_activity_at=at, stop_latched=False,
                control_revision=s['control_revision']+1, phase='building', mode='shared',
                episode_id=stable_id('episode', self.life.agent_id,key),
                attempt_id=stable_id('attempt', key), binding_epoch=self.repo.forget_epoch(p['_scope'].scope_id),
                episode_start_seq=p['_source_seq'])
        elif action == 'appraise_input':
            s['last_impulse_refs'] = refs
            conf = p.get('confidence', .5)
            for k, v in p.get('effects', {}).items():
                gain = p.get('_effective_sensitivity',s['sensitivity']) if k=='arousal' and v>0 else 1.
                s[k] = max(-1. if k=='valence' else 0., min(1., s[k]+v*conf*gain))
            cfg = ExperienceConfig.parse(s['config'])
            if s.get('permission_request'):
                s['arousal'] = max(cfg.release_threshold,s['arousal'])
            s['last_activity_at'] = at
            if any(p.get('effects', {}).values()) and conf > 0:
                s['last_increment_at'] = at
            if 'willingness' in p:
                s['willingness'], s['willingness_evidence'] = p['willingness'], refs
                s['experience_context'] = 'active' if p['willingness']=='interested' else 'paused'
                if p['willingness'] != 'interested' and s.get('run'):
                    s['run'].update(execution_state='paused', blocked_reason='willingness', resume_condition='当前意愿支持后明确resume')
                if p['willingness']!='interested':
                    self._close_request(s, at, 'willingness_revoked', transitions,
                        denied=p['willingness']=='not_now',source_seq=p['_source_seq'])
                elif s['rearm_requires_intent'] and p['_source_seq']>s['rearm_after_seq']:
                    s['rearm_evidence_seq'] = p['_source_seq']
            if (s['rearm_requires_intent'] and s['rearm_evidence_seq']
                    and s['arousal']<=s['current_threshold']-cfg.rearm_hysteresis):
                s.update(rearm_requires_intent=False, armed=True, armed_after_seq=p['_source_seq'])
            elif (not s.get('permission_request') and not s['rearm_requires_intent'] and not s['armed']
                    and s['arousal']<=s['current_threshold']-cfg.rearm_hysteresis):
                s.update(armed=True, armed_after_seq=p['_source_seq'])
            if p['_source_seq'] <= s['armed_after_seq']:
                return
            self._transition(s, at, key, refs, transitions)
        elif action == 'start':
            old = s.get('run') or {}
            s['run'] = dict(simulation_run_id=stable_id('simulation', self.life.agent_id, key),
                activity_type='until_cancel', desired_active=True, execution_state='running',
                origin_event_id=refs[-1], subject_actor_id=p['subject_actor_id'], audience_binding=s['scope']['audience_policy_id'],
                profile=deepcopy(p['input_profile']), input_profile_ref=digest(p['input_profile']),
                profile_revision=1, input_revision=1, control_revision=old.get('control_revision', 0)+1,
                profile_started_at=at, source_refs=refs, source_versions=p['_source_versions'],
                blocked_reason='', resume_condition='',authorization_mode=p.get('authorization_mode','one_shot'))
        elif action == 'adjust':
            run = s['run']
            run.update(profile=deepcopy(p['input_profile']), input_profile_ref=digest(p['input_profile']),
                profile_revision=run['profile_revision']+1, input_revision=run['input_revision']+1,
                control_revision=run['control_revision']+1, profile_started_at=at, source_refs=refs,
                source_versions=p['_source_versions'],authorization_mode=p.get('authorization_mode','one_shot'))
        elif action=='permission.respond':
            self._permission_response(s,p,at,key,refs,transitions)
        elif action in ('stop', 'exit', 'episode.end'):
            cfg = ExperienceConfig.parse(s['config'])
            self._close_request(s, at, 'stopped' if action=='stop' else 'ended', transitions)
            s['fences'] = {}
            s['control_barrier_seq'] = max(s.get('control_barrier_seq', 0), p.get('_source_seq', 0))
            s['control_revision'] += 1
            if s.get('run'):
                s['run'].update(desired_active=False, execution_state='stopped',
                    control_revision=s['run']['control_revision']+1, blocked_reason='user_control', resume_condition='新的明确开启')
            s['last_exit_seq'] = p.get('_source_seq', s['last_exit_seq'])
            if action=='stop':
                reset_episode(s,cfg,at)
                s['stop_latched'] = True
            else:
                tau=cfg.settle_tau_min+(cfg.settle_tau_max-cfg.settle_tau_min)*s['arousal']
                s.update(phase='settling', experience_context='settling', denied_count=0,
                    armed=False, settle_anchor=dict(at=at,deadline=at+tau,
                    **{k:s[k] for k in ('arousal','momentary_satiety','valence','engagement')}))
        elif action=='pause':
            self._close_request(s, at, 'paused', transitions)
            s['run'].update(execution_state='paused', control_revision=s['run']['control_revision']+1,
                blocked_reason=p.get('reason') or 'user_pause', resume_condition='明确resume且原开启意图仍有效')
        elif action=='resume':
            s['willingness'] = p.get('willingness', s['willingness'])
            s['experience_context'] = 'active'
            s['run'].update(execution_state='running', control_revision=s['run']['control_revision']+1,
                blocked_reason='', resume_condition='', profile_started_at=at)

    def submit(self, scope, p, call_key):
        try:
            return self._submit(scope, p, call_key)
        except SessionFailure as error:
            # Rejection is observable coverage, but cannot overwrite a valid
            # prior claim or consume the call id needed for correction.
            if isinstance(p, dict) and isinstance(p.get('sources'), list):
                with self.repo.tx():
                    for ref in p['sources']:
                        if isinstance(ref, str):
                            self.repo._exec("UPDATE experience_inputs SET status='rejected',reason=? WHERE agent_id=? AND event_id=? AND status IN ('pending','rejected')",
                                (error.code+': '+str(error), self.life.agent_id, ref))
            raise

    def _submit(self, scope, p, call_key):
        validate_proposal(p)
        if p['action'] in ('private.begin','private.end','initiation.select'):
            return self.private.submit(scope,p,call_key)
        with self.repo.tx():
            prior = self.repo._one('SELECT payload,receipt FROM experience_journal WHERE agent_id=? AND call_key=?', (self.life.agent_id, call_key))
            fingerprint = digest(p)
            if prior:
                prior_payload=json.loads(prior['payload'])
                if prior_payload.get('_scope') not in (None,asdict(scope)):
                    raise SessionFailure('scope_mismatch','调用回执不属于当前受众')
                if prior_payload.get('_fingerprint') != fingerprint:
                    raise SessionFailure('idempotency_conflict', '专项调用编号已对应另一提案')
                return dict(json.loads(prior['receipt']), duplicate=True)
            rev, s = self._ensure()
            if rev != p['base_revision'] and not (rev==1 and p['base_revision']==0):
                raise SessionFailure('stale_context', '先读life.read(kind=simulation)后按当前版本重提', state_revision=rev)
            action = p['action']
            if s['scope'] and s['scope']!=asdict(scope):
                raise SessionFailure('scope_mismatch', '专项状态仅绑定一个私聊伴侣；先显式重置绑定')
            if action=='ack':
                refs = p.get('transition_refs', [])
                for ref in refs:
                    row = self.repo._one("SELECT payload FROM experience_transitions WHERE id=? AND agent_id=? AND status='active'", (ref, self.life.agent_id))
                    if not row or json.loads(row['payload'])['scope']['audience_policy_id']!=scope.audience_policy_id:
                        raise SessionFailure('scope_mismatch', '转移回执不属于当前受众')
                    self.repo._exec("UPDATE experience_transitions SET ack_state='consumed' WHERE id=?", (ref,))
                receipt=dict(evaluated=True, applied=False, acknowledged=refs, state_revision=rev)
                self._journal(call_key,'ack',self.life.clock(),dict(_fingerprint=fingerprint),receipt)
                return receipt
            events = self._sources(scope, p.get('sources', []))
            if action in ('start','adjust') and p.get('authorization_mode')=='until_cancel':
                if not any(any(word in e.text for word in ('一直','持续','直到我说停','直到我叫停'))
                        and not any(word in e.text for word in ('不要','不允许','不想','不可以','不同意','别','如果','假如','应该','他说','她说','“','”','"'))
                        for e in events):
                    raise SessionFailure('continuous_authorization_required','持续输入须引用明确持续授权；默认单次')
            if action=='appraise_input' and s['rearm_requires_intent'] and p.get('willingness')=='interested':
                if not any(any(word in e.text for word in ('愿意继续','可以继续','再来','继续吧'))
                        and not any(word in e.text for word in ('不','别','如果','假如','“','”','"')) for e in events):
                    raise SessionFailure('renewed_intent_required','拒绝或超时后需要新的明确继续意愿，不能自行推断')
            maxseq = max(e.ingest_seq for e in events)
            if action not in ('enter','stop','exit','episode.end') and any(e.ingest_seq<s.get('episode_start_seq',0) for e in events):
                raise SessionFailure('stale_source','旧事件来源不能用于本次场景')
            if not scope.conversation_id.startswith('qq:private:') or p.get('subject_actor_id') != scope.participant_id:
                raise SessionFailure('scope_mismatch', '当前专项仅绑定明确的私聊参与者及受众')
            if action=='enter':
                if (p.get('adult_participants') is not True or p.get('context_confirmed') is not True
                        or p.get('willingness')!='interested' or maxseq <= s['last_exit_seq']):
                    raise SessionFailure('experience_context_required', '进入需要当前明确成年亲密情境、参与者和当次意愿；旧结果不能重开场景')
                if s['experience_context'] in ('active','paused'):
                    raise SessionFailure('experience_busy', '当前场景仍有效；先明确退出，不能切换联系人重开')
            elif (action!='stop' and (s['scope'] != asdict(scope) or s['experience_context']!='active' and not p.get('supersedes_call_key')
                    and not (s['experience_context']=='paused' and action in ('stop','exit','episode.end','pause','resume','appraise_input')))):
                raise SessionFailure('experience_context_required', '当前受众没有有效adult_intimate情境；退出后旧结果不可提交')
            if action=='stop' and s['scope'] is None:
                s['scope']=asdict(scope)
            if action=='enter' and self.private.running(scope):
                raise SessionFailure('experience_busy', '先结束私密事件，不能迁移或绕过共享许可')
            if action=='enter' and s['phase']=='settling':
                raise SessionFailure('experience_context_required', '当前受众没有有效adult_intimate情境；退出后旧结果不可提交')
            run = s.get('run') or {}
            if action in ('start','adjust','resume') and maxseq <= s.get('control_barrier_seq', 0):
                raise SessionFailure('stale_context', '明确停止之后需要新的用户原话开启，不能重用旧指令')
            if action=='appraise_input' and not p.get('supersedes_call_key') and maxseq <= s.get('control_barrier_seq', 0):
                raise SessionFailure('stale_context','停止以前的迟到效果不能提交；之后的新原话仍可独立评价')
            if action in ('adjust', 'pause', 'resume') and (not run.get('desired_active') or p.get('run_ref')!=run.get('simulation_run_id')):
                raise SessionFailure('stale_context', '持续运行已停止或运行编号失效；不能通过resume重新开启')
            if action=='start' and (not self.config.simulation_enabled or run.get('desired_active') or s['willingness']!='interested'):
                raise SessionFailure('experience_busy', '持续输入不可重复启动；核对开关、当次意愿及已有运行')
            if action=='resume' and p.get('willingness', s['willingness'])!='interested':
                raise SessionFailure('experience_context_required', '当前意愿不支持继续')
            if action=='appraise_input' and p.get('willingness', s['willingness'])!='interested' and any(v>0 for v in p.get('effects', {}).values()):
                raise SessionFailure('experience_paused', '当前意愿不支持正向推进；可评价无影响、降低或先确认新的意愿')
            for e in events:
                if action=='appraise_input':
                    claimed = self.repo._one("SELECT call_key FROM experience_inputs WHERE agent_id=? AND event_id=? AND status IN ('evaluated','unchanged','legacy_settled')", (self.life.agent_id, e.id))
                    if claimed and claimed['call_key'] != p.get('supersedes_call_key'):
                        raise SessionFailure('stimulus_overlap', '此原话已评价；修订使用supersedes_call_key及新调用编号')
            at = max(s['integrated_until'], max(e.received_at for e in events))
            earlier = [ref for ref,received in s['fences'].items() if received < at and ref not in p.get('sources',[])]
            if earlier and action not in ('stop','exit','episode.end') and not p.get('supersedes_call_key'):
                raise SessionFailure('input_pending','先按接收顺序评价较早输入，再提交本条效果或运行调整；明确stop/exit仍可立即执行',
                    pending_source_refs=earlier)
            if action=='stop':
                transitions=[]
                s['integrated_until']=at
            else:
                transitions = self._advance(s, at, call_key+':before', final=True,
                    allow_input=action not in ('exit','episode.end'))
            if action=='permission.respond':
                request=s.get('permission_request')
                evidence=next((e for e in events if e.id==p['evidence_ref']),None)
                if (not request or not evidence or evidence.received_at < (request.get('delivered_at') or float('inf'))
                        or evidence.ingest_seq<=request.get('delivery_source_seq',0)):
                    raise SessionFailure('permission_invalid','必须引用当前请求送达之后的真实对方回应')
                text=evidence.text.strip().rstrip('。.!！?？').strip()
                affirmative={'可以','同意','允许','好','好的','行','可以了','可以继续','同意本次请求','允许本次请求'}
                negative={'不可以','不同意','不允许','不行','不要','还不行','还不可以','暂时不行'}
                intervening=self.repo._one("SELECT 1 FROM events WHERE agent_id=? AND conversation_id=? "
                    "AND source_kind='self_output' AND kind='delivered_segment' AND redacted_at IS NULL "
                    "AND ingest_seq>? AND ingest_seq<?",(self.life.agent_id,scope.conversation_id,
                    request.get('delivery_source_seq',0),evidence.ingest_seq))
                if p['decision']=='allow' and intervening and text not in {'同意本次请求','允许本次请求'}:
                    raise SessionFailure('permission_ambiguous','请求后已有其他表达，简短回应不能推定仍针对该许可')
                if (p['decision']=='allow' and text not in affirmative
                        or p['decision']=='deny' and text not in negative
                        or p.get('evidence_text', evidence.text)!=evidence.text):
                    raise SessionFailure('permission_ambiguous','回应原文未明确回答本次请求；不得推测、截句或自行代答')
            before = self._metrics(s)
            pending_before = list(s['fences'])
            raw = deepcopy(p)
            raw.update(_scope=scope, _source_seq=maxseq, _source_versions={e.id:digest(e.payload) for e in events},
                _input_kind='event_impulse' if action=='appraise_input' else 'control', _effect_origin='user_report',
                _effective_sensitivity=self._effective_sensitivity(s))
            if p.get('supersedes_call_key'):
                return self._revise(scope, raw, call_key, fingerprint, rev, s)
            self._apply(s, raw, at, call_key, [e.id for e in events], transitions)
            if action in ('stop','exit','episode.end','pause'):
                self._cancel_wakes()
            if action in ('stop','exit','episode.end'):
                for ref in pending_before:
                    self.repo._exec("UPDATE experience_inputs SET status='rejected',reason='cancelled_by_explicit_control' WHERE agent_id=? AND event_id=? AND status IN ('pending','rejected')",
                        (self.life.agent_id,ref))
            for e in events:
                s['fences'].pop(e.id, None)
                if action in ('appraise_input','permission.respond'):
                    self.repo._exec('INSERT OR REPLACE INTO experience_inputs VALUES(?,?,?,?,?,?)',
                        (self.life.agent_id, e.id, digest(e.payload), 'unchanged' if p.get('direction')=='unchanged' or p.get('confidence', .5)==0 else 'evaluated', p.get('reason', ''), call_key))
                else:
                    self.repo._exec('INSERT OR IGNORE INTO experience_inputs VALUES(?,?,?,?,?,NULL)',
                        (self.life.agent_id,e.id,digest(e.payload),'pending','控制已解释，单次语义效果尚待评价'))
            self._store_transitions(transitions)
            if not s['fences'] and self.life.clock() > s['integrated_until']:
                # Waiting for semantics is known to be blocked, not authorized
                # continuous input. Preserve natural recovery during that gap.
                self._advance(s, self.life.clock(), call_key+':waiting', final=True, allow_input=False)
            self._save(s, rev)
            if action=='enter':
                self.private.bind(scope,s,at)
                self.private.partner_response(scope,True)
            elif action=='stop':
                self.private.stop(scope,at)
            after=self._metrics(s)
            actual = {k:after[k]-before[k] for k in METRICS}
            receipt = dict(evaluated=True, applied=any(actual.values()) or action!='appraise_input',
                unchanged_reason=('zero_confidence' if p.get('confidence')==0 else p.get('reason', '')) if not any(actual.values()) else '',
                before=before, after=after, actual_delta=actual,
                transition_refs=[t['id'] for t in transitions], state_revision=rev+1,
                integrated_until=s['integrated_until'], experience_context=s['experience_context'], run=s['run'],
                call_key=call_key, source_refs=[e.id for e in events], rule_version=RULE)
            coverage=self.repo._one('SELECT status,reason,call_key FROM experience_inputs WHERE agent_id=? AND event_id=?',
                (self.life.agent_id,events[-1].id))
            receipt['event_impulse']=dict(coverage) if coverage else dict(status='pending',reason='未提交本条语义效果')
            receipt['control_result']=dict(action=action,experience_context=s['experience_context'],run=s['run']) if action!='appraise_input' else None
            raw['_scope'] = asdict(scope)
            raw['_fingerprint'] = fingerprint
            self._journal(call_key, 'proposal', at, raw, receipt)
            return receipt

    def _revise(self, scope, raw, key, fingerprint, revision, current):
        target = self.repo._one("SELECT * FROM experience_journal WHERE agent_id=? AND call_key=?", (self.life.agent_id, raw['supersedes_call_key']))
        if target and target['kind']=='revision':
            target = self.repo._one("SELECT * FROM experience_journal WHERE agent_id=? AND call_key=? AND kind='proposal'",
                (self.life.agent_id, json.loads(target['payload'])['target']))
        if not target:
            raise SessionFailure('stale_context', '原专项评价不存在')
        # A correction cannot recreate a delivered request or rewrite consumed consent.
        if self.repo._one("SELECT 1 FROM experience_transitions WHERE agent_id=? AND status='active'",
                (self.life.agent_id,)):
            raise SessionFailure('revision_boundary','历史包含许可/转移；保留审计，不回放控制或重造峰值')
        old = json.loads(target['payload'])
        if old['action']!='appraise_input' or set(old['sources'])!=set(raw['sources']) or old['_scope']!=asdict(scope):
            raise SessionFailure('scope_mismatch', '只可修订相同来源、受众的单次评价；控制不能回写')
        raw['_scope'], raw['_fingerprint'] = asdict(scope), old['_fingerprint']
        raw.pop('supersedes_call_key', None)
        raw['_revised_by'] = key
        self.repo._exec('UPDATE experience_journal SET payload=? WHERE seq=?', (canonical(raw), target['seq']))
        base = self.repo._one('SELECT initial_state FROM experience_state WHERE agent_id=?', (self.life.agent_id,))
        s, transitions = json.loads(base['initial_state']), []
        if s.get('rule_version')!=RULE:
            raise SessionFailure('revision_boundary','旧规则的历史评价不能回灌到许可门版本')
        for entry in self.repo._all("SELECT * FROM experience_journal WHERE agent_id=? AND kind IN ('proposal','interval','gap','config') ORDER BY seq", (self.life.agent_id,)):
            p = json.loads(entry['payload'])
            if entry['kind'] in ('interval', 'gap'):
                self._integrate(s, p['to'], entry['call_key'], transitions, final=True, allow_input=p.get('allow_input', False),
                    effective_sensitivity=p.get('effective_sensitivity',s['sensitivity']))
            elif entry['kind']=='config':
                s.update(config=p['config'], config_version=digest(p['config']), config_effective_at=entry['logical_at'])
            else:
                p['_scope'] = Scope(**p['_scope'])
                self._apply(s, p, entry['logical_at'], entry['call_key'], p['sources'], transitions)
        if transitions:
            raise SessionFailure('revision_boundary','数学修订不能凭历史输入生成新的许可请求；需要新的实际输入')
        # Never resurrect a current input fence or stop by historical replay.
        s['fences'] = current['fences']
        for ref in raw['sources']:
            s['fences'].pop(ref, None)
            self.repo._exec('UPDATE experience_inputs SET status=?,reason=?,call_key=? WHERE agent_id=? AND event_id=?',
                ('unchanged' if raw['direction']=='unchanged' or raw.get('confidence')==0 else 'evaluated', raw['reason'], key, self.life.agent_id, ref))
        self.repo._exec("UPDATE experience_transitions SET status='revoked' WHERE agent_id=?", (self.life.agent_id,))
        self._store_transitions(transitions, replay=True)
        self._cancel_wakes(revoked_only=True)
        self._save(s, revision)
        receipt = dict(evaluated=True, applied=True, revised=True, replayed_at=target['logical_at'],
            before=self._metrics(current), after=self._metrics(s),
            actual_delta={k:self._metrics(s)[k]-self._metrics(current)[k] for k in METRICS}, transition_refs=[t['id'] for t in transitions],
            state_revision=revision+1, integrated_until=s['integrated_until'], call_key=key, external_actions_replayed=False)
        self._journal(key, 'revision', self.life.clock(), dict(_fingerprint=fingerprint, target=target['call_key']), receipt)
        return receipt

    def receive(self, event, *, recovering=False):
        """A persisted user input is a time barrier before semantic resolution."""
        with self.repo.tx():
            revision, s = self._load()
            text = event.text.strip().rstrip('。.!！')
            action = ('exit' if text in {'退出亲密场景', '结束亲密场景'} else 'stop'
                if text in {'关闭持续输入', '停止持续输入', '关闭持续模式', '停止模拟', '停下', '停止', '不要继续', '停'} else None)
            if (event.source_kind!='user_report' or event.actor_id!=event.scope.participant_id
                    or not event.scope.conversation_id.startswith('qq:private:')
                    or s['scope'] is not None and s['scope']!=asdict(event.scope)):
                return
            if action=='stop' and not revision:
                revision,s=self._ensure()
                s['scope']=asdict(event.scope)
            if not revision or not action and s['experience_context'] not in ('active','paused'):
                return
            if action=='exit' and s['mode']!='shared':
                return
            if self.repo._one('SELECT 1 FROM experience_inputs WHERE agent_id=? AND event_id=?', (self.life.agent_id, event.id)):
                return
            if action=='stop':
                s['integrated_until']=max(s['integrated_until'],event.received_at)
            else:
                self._advance(s, event.received_at, 'receive:'+event.id, final=True, allow_input=not recovering and not action)
            s['fences'][event.id] = event.received_at
            self.repo._exec('INSERT INTO experience_inputs VALUES(?,?,?,?,?,NULL)',
                (self.life.agent_id, event.id, digest(event.payload), 'pending', '等待本条语义评价或控制解释'))
            if action:
                pending_before=list(s['fences'])
                self._apply(s, dict(action=action, _source_seq=event.ingest_seq), event.received_at,
                    'inbound:'+event.id, [event.id], [])
                for ref in pending_before:
                    self.repo._exec("UPDATE experience_inputs SET status='rejected',reason='cancelled_by_explicit_control' WHERE agent_id=? AND event_id=? AND status IN ('pending','rejected')",
                        (self.life.agent_id,ref))
                s['fences'].pop(event.id, None)
                self.repo._exec('UPDATE experience_inputs SET status=?,reason=? WHERE agent_id=? AND event_id=?',
                    ('evaluated', 'explicit_'+action, self.life.agent_id, event.id))
                self._journal('inbound:'+event.id, 'proposal', event.received_at,
                    dict(action=action, sources=[event.id], _scope=asdict(event.scope), _source_seq=event.ingest_seq), {})
                self._cancel_wakes()
                if action=='stop':
                    self.private.stop(event.scope,event.received_at)
            self._save(s, revision)

    def _cancel_wakes(self, *, revoked_only=False):
        if self.repo._one("SELECT 1 FROM sqlite_master WHERE name='role_wakes'"):
            self.repo._exec("UPDATE role_wakes SET state='cancelled' WHERE kind='experience' AND state IN ('pending','waiting_resource') "
                "AND semantic_key IN (SELECT id FROM experience_transitions WHERE agent_id=?"+
                (" AND status='revoked'" if revoked_only else '')+")", (self.life.agent_id,))

    def recover(self):
        """No input is simulated for the process-offline interval."""
        with self.repo.tx():
            self.private.recover()
            revision, s = self._load()
            if not revision:
                return
            if s.get('migration_required') or s.get('state_fault'):
                self._quarantine(revision,s)
                self.repo._exec("UPDATE experience_transitions SET status='revoked',ack_state='retained_for_context' WHERE agent_id=?",
                    (self.life.agent_id,))
                self._cancel_wakes()
                self._journal('safe-reset:'+str(revision),'state_reset',self.life.clock(),
                    dict(reason='old_rule_or_invalid_state'),{})
                self._save(s,revision)
                self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('experience_schema:'+self.life.agent_id,'2'))
                return
            # Input persistence precedes this service. Reconcile a crash between
            # those commits before validating any retained opening intention.
            if s['scope']:
                for row in self.repo._all("SELECT id FROM events WHERE agent_id=? AND conversation_id=? AND received_at>=? "
                        "AND source_kind='user_report' AND redacted_at IS NULL AND NOT EXISTS "
                        "(SELECT 1 FROM experience_journal j,json_each(j.payload,'$.sources') s WHERE j.agent_id=events.agent_id AND s.value=events.id) ORDER BY ingest_seq",
                        (self.life.agent_id, s['scope']['conversation_id'], s['integrated_until'])):
                    self.receive(self.repo.get_event(row['id']), recovering=True)
                revision, s = self._load()
            start, until = s['integrated_until'], max(s['integrated_until'], self.life.clock())
            ignored = []
            self._close_request(s,start,'restart_cancelled',ignored)
            self._integrate(s, until, 'restart', ignored, final=True, allow_input=False)
            run = s.get('run') or {}
            if run.get('desired_active'):
                run.update(execution_state='paused', blocked_reason='restart_validation_required',
                    resume_condition='先处理持久收件箱及费用/渠道状态，再以当前来源明确resume；不补造停机期输入')
            self._journal('restart:'+str(revision), 'gap', until, dict(**{'from':start, 'to':until}, allow_input=False), {})
            self.repo._exec("UPDATE experience_transitions SET ack_state='retained_for_context' WHERE agent_id=? AND ack_state='pending'",
                (self.life.agent_id,))
            if s['config'] != asdict(self.config):
                s.update(config=asdict(self.config), config_version=digest(asdict(self.config)), config_effective_at=until)
                self._journal('config:'+str(revision), 'config', until, dict(config=asdict(self.config)), {})
            self._save(s, revision)

    def valid_run(self, s):
        run = s.get('run') or {}
        if not run.get('desired_active') or s['experience_context']!='active' or s['willingness']!='interested':
            return False
        ctx = self.life.life_access.bind(Scope(**s['scope']), mode='internal')
        for ref, version in run.get('source_versions', {}).items():
            e = self.repo.get_event(ref)
            r = self.life.catalog.resolve(ref)
            if (not e or e.redacted_at is not None or digest(e.payload)!=version
                    or not r or not self.life.catalog.live(r) or not self.life.life_access.internal_allowed(ctx, r)):
                return False
        return bool(run.get('source_versions')) and s['config']['simulation_enabled']

    def system_pause(self, reason):
        with self.repo.tx():
            revision, s = self._load()
            run = s.get('run') or {}
            if not revision or not run.get('desired_active') or run.get('execution_state')!='running':
                return
            at = self.life.clock()
            self._advance(s, at, 'system-boundary:'+str(revision), final=True, allow_input=reason=='channel_unavailable')
            p = dict(action='pause', reason=reason, sources=run['source_refs'], _scope=s['scope'])
            self._apply(s, p, at, 'system-pause:'+str(revision), p['sources'], [])
            run['resume_condition'] = '核实原开启来源、范围、渠道及停止屏障后接续；用户暂停需明确resume'
            self._journal('system-pause:'+str(revision), 'proposal', at, p, {})
            self._cancel_wakes()
            self._save(s, revision)

    def system_resume(self):
        with self.repo.tx():
            revision, s = self._load()
            run = s.get('run') or {}
            if (not revision or run.get('execution_state')!='paused' or s['fences'] or not self.valid_run(s)
                    or run.get('blocked_reason') not in {'channel_unavailable','restart_validation_required'}):
                return
            at = self.life.clock()
            self._advance(s, at, 'system-gap:'+str(revision), final=True, allow_input=False)
            p = dict(action='resume', sources=run['source_refs'], _scope=s['scope'])
            self._apply(s, p, at, 'system-resume:'+str(revision), p['sources'], [])
            self._journal('system-resume:'+str(revision), 'proposal', at, p, {})
            self._save(s, revision)

    def resolve_input(self, scope, event_ids):
        # An omitted semantic assessment stays pending and holds its barrier.
        # Only explicit simulation appraisals/controls release it.
        return None

    def review(self, scope, source_refs):
        """Hourly reviewers read coverage; this entry cannot write fast fields."""
        return dict(field_owner='experience_reducer', mode='review_only', state=self.read(scope),
            source_refs=list(source_refs), instruction='核对覆盖；慢指标按来源版本结算一次，快指标修订走appraise_input.supersedes_call_key')

    def guard_expression(self, scope, proposal):
        request_id=proposal.get('experience_request_ref')
        invitation_id=proposal.get('experience_initiation_ref')
        if self.private.running(scope):
            raise SessionFailure('cancel_requested','私密事件不产生任何对外表达；先自行结束')
        if invitation_id:
            self.private.guard_invitation(scope,invitation_id)
        if request_id:
            s=self.read(scope)
            r=s.get('permission_request')
            if (not r or r['request_id']!=request_id or s['stop_latched']
                    or s['phase']!='awaiting_permission' or r['delivery_status']=='confirmed'):
                raise SessionFailure('cancel_requested','许可请求已关闭或已经送达；不能重复询问')
            # A request must be spoken as an actual question, not an inferred answer.
            text=proposal.get('text') or ''.join(p.get('text','') for p in proposal.get('parts',[]))
            if not text.strip().endswith(('?','？')):
                raise SessionFailure('permission_question_required','请亲口提出本次许可问题，不能替对方回答')
            if not any(word in text for word in ('可以','允许','同意','许可')) or any(
                    word in text for word in ('默认同意','算你同意','当你同意','我替你','你已经同意')):
                raise SessionFailure('permission_question_required','表达须明确询问本次许可，不能用普通问题或代答替代')
        if 'experience_run_ref' not in proposal and 'experience_control_revision' not in proposal:
            return
        s = self.read(scope)
        run = s.get('run') or {}
        if (s.get('experience_context')!='active' or run.get('execution_state')!='running'
                or not run.get('desired_active') or run.get('simulation_run_id')!=proposal.get('experience_run_ref')
                or run.get('control_revision')!=proposal.get('experience_control_revision') or not self.valid_run(s)):
            raise SessionFailure('cancel_requested','该持续体验的控制版本已停止或改变；先理解当前回执再表达')

    def erase(self, scope):
        """Called inside the source-forgetting transaction, before projections rebuild."""
        self.repo._exec('DELETE FROM experience_private_state WHERE agent_id=? AND scope_id=?',
            (self.life.agent_id,scope.scope_id))
        self.repo._exec('DELETE FROM experience_private_journal WHERE agent_id=? AND scope_id=?',
            (self.life.agent_id,scope.scope_id))
        self.repo._exec('DELETE FROM drive_contact_attempts WHERE agent_id=? AND scope_id=?',
            (self.life.agent_id,scope.scope_id))
        self.repo._exec('DELETE FROM experience_faults WHERE agent_id=?',(self.life.agent_id,))
        _,s=self._load()
        if s['scope']!=asdict(scope) and not s.get('state_fault'):
            return
        self._cancel_wakes()
        if s.get('run'):
            self.repo._exec('DELETE FROM meta WHERE k=?',('activity_run:'+s['run']['simulation_run_id'],))
        for table in ('experience_state','experience_journal','experience_inputs','experience_transitions','experience_faults'):
            self.repo._exec('DELETE FROM '+table+' WHERE agent_id=?',(self.life.agent_id,))

    def _quarantine(self, revision, state):
        row=self.repo._one('SELECT payload FROM experience_state WHERE agent_id=?',(self.life.agent_id,))
        if row:
            self.repo._exec('INSERT OR IGNORE INTO experience_faults VALUES(?,?,?,?)',
                (stable_id('experience-fault',self.life.agent_id,str(revision)),self.life.agent_id,row['payload'],self.life.clock()))
        self.repo._exec("UPDATE experience_transitions SET status='revoked' WHERE agent_id=?",(self.life.agent_id,))
        self._cancel_wakes()
