"""Host relationship policy, registered metrics, scoped materials and projections."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re

from .agent_session import SessionFailure, canonical
from .contracts import RelationshipState, Scope
from .text import stable_id, terms

PROTOCOL_VERSION = 2
SEMANTICS_VERSION = 'relationship-semantics-2'
BASIS = {'policy_initialization','recorded_experiences','policy_and_recorded_experiences','legacy_unverified'}
LABELS = {'familiarity':'熟悉度','trust':'信任度','comfort':'舒适度','bond':'情感联结'}
DEFINITIONS = {
    'familiarity':'对实际人物的习惯、偏好和共同经历了解多少；熟悉不等于喜欢或全部猜得准。',
    'trust':'经历支持的可靠性、尊重约定和关系边界；不表示事实正确率，不授予权限。',
    'comfort':'通常相处时的放松和自然表达；不抹掉当下的委屈、紧张和分歧。',
    'bond':'这段具体关系的重要性与长期牵挂；不是愉悦、连接压力、亲密需要或性欲。',
}
BANDS = {
    'familiarity':['几乎没有足够了解，很多事需要询问','初步认识，知道少量信息','已有若干共同经历，了解部分习惯',
                   '相当熟悉，知道较多偏好和经历','深度熟悉，共同背景丰富；仍可能有新变化'],
    'trust':['记录显示明显戒心；无证据时只能说尚不清楚','谨慎信任，需要结合事情核对','有一定可靠经历，也有不确定或分歧',
             '通常愿意信赖，沟通自然，乐于配合明确的请求','稳定而深厚的信赖，沟通坦诚，配合自然'],
    'comfort':['相处常感拘谨、紧张或不自在','可以交流，但表达较保留','日常交流尚自然，部分情境仍谨慎',
               '通常放松，交流自然，愿意分享真实感受','通常很自在，亲近自然，沟通轻松坦诚'],
    'bond':['尚未形成稳定的个人牵挂','有一些好感或关心，联结较轻','这段关系已值得维持，开始有持续关注',
            '关系比较重要，愿意延续相处和分享生活','深厚而持久的牵挂，分歧与短暂分别不自动抹去联结'],
}


def number(value,field_name,*,lo=None,hi=None):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
        raise SessionFailure('invalid_relation_input','关系数值必须有限且不能是布尔值',field=field_name)
    if (lo is not None and value<lo) or (hi is not None and value>hi):
        raise SessionFailure('invalid_relation_input','关系参数超出范围',field=field_name)
    return float(value)


def semantic_band(value):
    value = number(value,'metric',lo=0,hi=1)
    return sum(value>=edge for edge in (.2,.4,.6,.8))


def actor_id(value):
    if isinstance(value,bool) or not re.fullmatch(r'[1-9][0-9]*',str(value)):
        raise SessionFailure('actor_mismatch','关系对象必须是宿主核实的实际 QQ 账号')
    return str(value)


@dataclass(frozen=True)
class MetricSpec:
    id: str
    label: str
    default_value: float
    bands: tuple[str,...]
    definition: str
    family: str = 'positive_relationship'
    sensitivity: float = 1.0
    semantics_version: str = SEMANTICS_VERSION


def default_registry():
    return {key:MetricSpec(key,LABELS[key],value,tuple(BANDS[key]),DEFINITIONS[key])
            for key,value in dict(familiarity=.3,trust=.5,comfort=.5,bond=0.).items()}


def validate_registry(registry):
    for key,spec in registry.items():
        if (key!=spec.id or spec.family!='positive_relationship' or not re.fullmatch('[a-z][a-z0-9_]*',key)
                or len(spec.bands)!=5 or not all(spec.bands) or not spec.definition or not spec.label):
            raise SessionFailure('unknown_metric','指标须声明正向关系归属、定义及完整五档语义',metric=key)
        number(spec.default_value,key,lo=0,hi=1)
        number(spec.sensitivity,key,lo=0,hi=2)
        if spec.sensitivity==0:
            raise SessionFailure('invalid_relation_input','指标敏感度必须大于零',metric=key)


@dataclass
class RelationConfig:
    magnitudes: dict = field(default_factory=lambda:dict(slight=.005,meaningful=.02,major=.06))
    min_confidence: float = .35
    daily_cap: float = .10
    sensitivity: dict = field(default_factory=dict)
    timezone: str = 'Asia/Shanghai'

    @classmethod
    def from_mapping(cls,raw=None,registry=None):
        registry = registry or default_registry()
        raw = {} if raw is None else raw
        if not isinstance(raw,dict) or set(raw)-{'magnitudes','min_confidence','daily_cap','sensitivity','timezone'}:
            raise SessionFailure('invalid_relation_input','relations 含未知配置；人物与 0.7/0.9 政策不能在此修改')
        defaults = cls()
        magnitudes = raw.get('magnitudes',{})
        sensitivity = raw.get('sensitivity',{})
        if (not isinstance(magnitudes,dict) or set(magnitudes)-set(defaults.magnitudes)
                or not isinstance(sensitivity,dict) or set(sensitivity)-set(registry)):
            raise SessionFailure('invalid_relation_input','未知关系幅度或指标参数')
        cfg = cls(dict(defaults.magnitudes,**magnitudes),raw.get('min_confidence',.35),raw.get('daily_cap',.1),
                  {key:sensitivity.get(key,spec.sensitivity) for key,spec in registry.items()},raw.get('timezone','Asia/Shanghai'))
        for key,value in cfg.magnitudes.items():
            number(value,'magnitudes.'+key,lo=0,hi=1)
        number(cfg.min_confidence,'min_confidence',lo=0,hi=1)
        number(cfg.daily_cap,'daily_cap',lo=0,hi=1)
        if not 0<cfg.magnitudes['slight']<=cfg.magnitudes['meaningful']<=cfg.magnitudes['major']<=cfg.daily_cap<=1:
            raise SessionFailure('invalid_relation_input','须满足 0<slight<=meaningful<=major<=daily_cap<=1')
        for key,value in cfg.sensitivity.items():
            if number(value,'sensitivity.'+key,lo=0,hi=2)==0:
                raise SessionFailure('invalid_relation_input','敏感度须在 (0,2]')
        cfg.day(0)
        return cfg

    def day(self,at):
        from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
        try:
            zone = timezone(timedelta(hours=8)) if self.timezone=='Asia/Shanghai' else ZoneInfo(self.timezone)
            return datetime.fromtimestamp(at,zone).strftime('%Y-%m-%d')
        except (ValueError,TypeError,ZoneInfoNotFoundError) as error:
            raise SessionFailure('invalid_relation_input','关系结算时区无效') from error

    @property
    def version(self):
        return 'relation-config-'+hashlib.sha256(canonical(asdict(self)).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class RelationshipPolicy:
    owner_actor_id: str
    agent_id: str
    registry_hash: str
    owner_floor: float = .7
    other_ceiling: float = .9

    @property
    def version(self):
        return 'relationship-policy-2-'+hashlib.sha256(canonical(asdict(self)).encode()).hexdigest()[:16]

    def bounds(self,actor):
        return (.7,1.) if actor==self.owner_actor_id else (0.,.9)

    def payload(self):
        return dict(asdict(self),version=self.version)


def normalize_state(values,actor,registry,policy):
    if not isinstance(values,dict) or set(values)-set(registry):
        raise SessionFailure('unknown_metric','持久关系含未注册指标，不能静默丢弃')
    result,adjustments = {},[]
    for key,spec in registry.items():
        raw = number(values.get(key,spec.default_value),key)
        lo,hi = policy.bounds(actor)
        result[key] = max(lo,min(hi,round(raw,6)))
        if key not in values or result[key]!=raw:
            adjustments.append(dict(metric=key,before=values.get(key),after=result[key]))
    return result,adjustments


def validate_impacts(impacts,registry):
    if not isinstance(impacts,list):
        raise SessionFailure('invalid_relation_input','relation_impacts 必须是数组')
    seen = set()
    for item in impacts:
        if (not isinstance(item,dict) or set(item)!={'metric','direction','magnitude','confidence'}):
            raise SessionFailure('invalid_relation_input','每项关系影响需要 metric/direction/magnitude/confidence，不能附带 delta 或绝对分数')
        key = item['metric']
        if not isinstance(key,str) or key not in registry:
            raise SessionFailure('unknown_metric','未注册关系指标',metric=key)
        if (key in seen or not isinstance(item['direction'],str) or not isinstance(item['magnitude'],str)
                or item['direction'] not in {'increase','decrease','unchanged'} or item['magnitude'] not in {'slight','meaningful','major'}):
            raise SessionFailure('invalid_relation_input','指标不能重复，方向或程度枚举无效')
        number(item['confidence'],'confidence',lo=0,hi=1)
        seen.add(key)


class Relationships:
    def __init__(self,life,owner='0',config=None,registry=None):
        self.life,self.repo = life,life.repo
        self.registry = dict(registry or default_registry())
        validate_registry(self.registry)
        self.config = RelationConfig.from_mapping(asdict(config) if isinstance(config,RelationConfig) else config,self.registry)
        self.owner = str(owner)
        if self.owner!='0':
            actor_id(self.owner)
        self.policy = self.make_policy()
        self.repo.relationship_validator = self.assert_payload
        with self.repo.tx():
            marker = self.repo._one('SELECT v FROM meta WHERE k=?',('relationship_schema:'+life.agent_id,))
            if marker and int(marker['v'])>PROTOCOL_VERSION:
                raise SessionFailure('protocol_mismatch','当前程序不能写入更高版本关系数据库')
            self.record_policy()
            for key in self.keys():
                actor = actor_id(key.rsplit(':',1)[1])
                self.load(life.scope('qq:private:'+actor,actor))
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('relationship_schema:'+life.agent_id,'2'))

    def make_policy(self):
        checksum = hashlib.sha256(canonical({k:asdict(v) for k,v in self.registry.items()}).encode()).hexdigest()
        return RelationshipPolicy(self.owner,self.life.agent_id,checksum)

    def record_policy(self):
        self.repo._exec('INSERT OR IGNORE INTO meta VALUES(?,?)',
            ('relationship_policy:'+self.life.agent_id+':'+self.policy.version,canonical(self.policy.payload())))
        self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',
            ('relationship_policy_current:'+self.life.agent_id,self.policy.version))

    def keys(self):
        return [r['aggregate_id'] for r in self.repo._all('SELECT DISTINCT aggregate_id FROM state_snapshots WHERE aggregate_id LIKE ?',
            ('relationship:'+self.life.agent_id+'|person:%',))]

    def payload(self,state):
        data = asdict(state)
        data.pop('scope');data.pop('version')
        return data

    def assert_payload(self,key,payload):
        if getattr(self.life,'_replaying',False):
            return
        actor = actor_id(key.rsplit(':',1)[1])
        if payload.get('schema_version')!=2 or payload.get('actor_id')!=actor or any(k in payload for k in ('trust','comfort','familiarity','bond')):
            raise SessionFailure('protocol_mismatch','当前关系只能保存 v2 metrics；旧标量写入已停用')
        metrics,changes = normalize_state(payload.get('metrics'),actor,self.registry,self.policy)
        if changes or set(metrics)!=set(payload['metrics']):
            raise SessionFailure('invalid_relation_input','关系保存值越界或指标不完整')
        if payload.get('policy_version')!=self.policy.version or set(payload.get('metric_basis',{}))!=set(metrics):
            raise SessionFailure('stale_context','关系政策或依据投影不完整')
        if any(v not in BASIS for v in payload['metric_basis'].values()):
            raise SessionFailure('invalid_relation_input','关系依据类型无效')
        for k,v in payload.get('daily_used',{}).items():
            number(v,'daily_used.'+k,lo=0)

    def from_payload(self,scope,payload,version):
        fields = dict(payload)
        fields.pop('scope',None);fields.pop('version',None)
        return RelationshipState(scope=scope,version=version,**fields)

    def load(self,scope,*,create=True):
        actor = actor_id(scope.participant_id)
        key = self.life.state_key('relationship',scope)
        with self.repo.tx():
            got = self.repo.load_state(key)
            if not got:
                metrics,_ = normalize_state({},actor,self.registry,self.policy)
                state = RelationshipState(scope,actor,metrics,policy_version=self.policy.version,
                    metric_basis={k:'policy_initialization' for k in metrics},daily_used={k:0. for k in metrics},updated_at=self.life.clock())
                if create:
                    self.host_transition(scope,'relationship_initialization',None,state,[])
                return state
            version,raw,_ = got
            old = raw.get('schema_version',1)<2
            values = {k:raw.get(k,self.registry[k].default_value) for k in ('familiarity','trust','comfort')} if old else raw.get('metrics')
            metrics,adjustments = normalize_state(values,actor,self.registry,self.policy)
            if old:
                covered = self.legacy_covered(actor)
                basis = {k:('policy_and_recorded_experiences' if metrics[k]!=values.get(k) else 'recorded_experiences')
                    if k in covered else 'legacy_unverified' if k in values else 'policy_initialization' for k in metrics}
                state = RelationshipState(scope,actor,metrics,policy_version=self.policy.version,metric_basis=basis,
                    daily_key=raw.get('daily_key',''),daily_used={k:raw.get('daily_used',{}).get(k,0.) for k in metrics},
                    updated_at=float(raw.get('updated_at',self.life.clock())),version=version)
                self.migrate_materials(scope,raw)
                self.host_transition(scope,'relationship_migration',got,state,adjustments,legacy_metric_keys=list(values))
            elif adjustments or raw.get('policy_version')!=self.policy.version:
                data = dict(raw,metrics=metrics,policy_version=self.policy.version,
                    metric_basis={k:raw.get('metric_basis',{}).get(k,'policy_initialization') for k in metrics},
                    daily_used={k:raw.get('daily_used',{}).get(k,0.) for k in metrics})
                state = self.from_payload(scope,data,version)
                self.host_transition(scope,'policy_rebase',got,state,adjustments)
            else:
                self.assert_payload(key,raw)
                state = self.from_payload(scope,raw,version)
            return state

    def host_transition(self,scope,kind,before,state,adjustments,**extra):
        event,_ = self.life.ingest(scope,kind,'inference','',payload=dict(host_relationship_event=True))
        raw = dict(_kind=kind,scope=asdict(scope),_relation_policy=self.policy.payload(),
            _relation_registry={k:asdict(v) for k,v in self.registry.items()},_relation_config=asdict(self.config),
            _adjustments=adjustments,**extra)
        journal = self.repo._exec('INSERT INTO state_journal(scope_id,event_id,appraisal,before_state,after_state,rule_version,at) VALUES(?,?,?,?,?,?,?)',
            (scope.scope_id,event.id,canonical(raw),canonical(dict(relationship=before)),'{}','life-state-5',self.life.clock())).lastrowid
        state.last_journal_id = journal
        state.updated_at = max(state.updated_at,self.life.clock())
        state.version = self.repo.save_state(self.life.state_key('relationship',scope),self.payload(state))
        self.repo._exec('UPDATE state_journal SET after_state=? WHERE id=?',
            (canonical(dict(relationship=self.repo.load_state(self.life.state_key('relationship',scope)))),journal))

    def register(self,spec):
        original = self.registry,self.config,self.policy
        try:
            with self.repo.tx():
                if spec.id in self.registry:
                    raise SessionFailure('unknown_metric','指标已注册')
                candidate = dict(self.registry,**{spec.id:spec})
                validate_registry(candidate)
                self.registry = candidate
                self.config = RelationConfig.from_mapping(asdict(self.config),candidate)
                self.policy = self.make_policy();self.record_policy()
                for key in self.keys():
                    actor = key.rsplit(':',1)[1]
                    self.load(self.life.scope('qq:private:'+actor,actor))
        except BaseException:
            self.registry,self.config,self.policy = original
            raise

    def subject(self,scope,actor,sources):
        actor = actor_id(actor)
        events = self.require_sources(scope,sources)
        # User-report actors originate in the platform adapter. Quoted messages,
        # arbitrary tools and a model's own assertions do not prove an action.
        actual = {e.actor_id for e in events if e.source_kind=='user_report' and e.kind=='message'}
        if actor not in actual:
            raise SessionFailure('actor_mismatch','关系对象须对应本次真实说话人；引用、网页和模型自述不能证明其行为')
        return actor

    def require_sources(self,scope,sources):
        if not isinstance(sources,list) or not sources:
            raise SessionFailure('source_removed','关系操作需要仍存在的真实来源')
        result = []
        for eid in dict.fromkeys(sources):
            event = self.repo.get_event(eid)
            if not event or event.redacted_at:
                raise SessionFailure('source_removed','关系来源已不存在或已遗忘')
            if event.scope.private_key!=scope.private_key or event.scope.audience_policy_id!=scope.audience_policy_id:
                raise SessionFailure('scope_mismatch','关系来源不属于当前受众')
            result.append(event)
        return result

    def migrate_materials(self,scope,raw):
        records = []
        if raw.get('preferred_address'):
            records.append(('preference',str(raw['preferred_address']),[],False))
        if raw.get('conversation_preferences'):
            records.append(('preference',canonical(raw['conversation_preferences']),[],False))
        for repair in raw.get('open_repairs',[]):
            records.append(('repair',str(repair.get('note','')),repair.get('root_event_ids',[]),bool(repair.get('resolved_at'))))
        for i,(kind,text,sources,resolved) in enumerate(records):
            sources = [eid for eid in sources if (event:=self.repo.get_event(eid)) and not event.redacted_at and event.scope.scope_id==scope.scope_id]
            identity = stable_id('legacy-relation-note',scope.agent_id,scope.participant_id,str(i))
            self.repo._exec("INSERT OR IGNORE INTO memory_annotations(id,scope_id,kind,text,cues,sources,created,status) VALUES(?,?,'relationship_note',?,'[]',?,?,?)",
                (identity,scope.scope_id,text,canonical(sources),self.life.clock(),'resolved' if resolved else 'active'))
            self.repo._exec('INSERT OR IGNORE INTO relationship_annotation_links VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (identity,scope.agent_id,scope.participant_id,identity,1,kind,'legacy_unverified',0.,'normal',None,None,self.life.clock(),''))

    def legacy_covered(self,actor):
        covered = set()
        for row in self.repo._all("SELECT j.appraisal FROM state_journal j JOIN events e ON e.id=j.event_id WHERE e.agent_id=? AND j.rule_version='life-state-4'",(self.life.agent_id,)):
            raw = json.loads(row['appraisal'])
            if raw.get('_kind') or (raw.get('subject_actor_id') or raw['scope'].get('participant_id'))!=actor:
                continue
            ids = raw.get('source_event_ids',[])
            sources = self.repo.events_by_ids(ids)
            if len(sources)==len(set(ids)) and sources and all(not s.redacted_at for s in sources) and raw.get('certainty',0)>=.35:
                covered.update(k for k in (raw.get('relation_evidence') or {}) if k in self.registry)
        return covered

    def mutate_note(self,scope,op,call_key,index=0,interpretation_ref=None):
        with self.repo.tx():
            return self._mutate_note(scope,op,call_key,index,interpretation_ref)

    def _mutate_note(self,scope,op,call_key,index=0,interpretation_ref=None):
        if not isinstance(op,dict) or op.get('operation','create') not in {'create','replace','resolve','revoke'}:
            raise SessionFailure('invalid_relation_input','关系材料操作无效')
        if set(op)-{'operation','actor_id','relation_kind','origin','text','sources','confidence','salience','lifetime_days','base_note_ref','applicability','intent_ref'}:
            raise SessionFailure('invalid_relation_input','关系材料包含未知字段')
        if not call_key:
            raise SessionFailure('invalid_relation_input','关系材料缺少宿主调用幂等键')
        identity = stable_id('relation-note',call_key,str(index),str(self.repo.forget_epoch(scope.scope_id)))
        prior = self.repo._one('SELECT * FROM relationship_annotation_links WHERE annotation_id=?',(identity,))
        checksum = hashlib.sha256(canonical(op).encode()).hexdigest()
        if prior:
            if prior['operation_hash']!=checksum:
                raise SessionFailure('idempotency_conflict','关系操作编号对应不同提案')
            return self.note_receipt(identity)
        actor = self.subject(scope,op.get('actor_id'),op.get('sources',[]))
        action = op.get('operation','create')
        base = op.get('base_note_ref')
        previous = self.repo._one('SELECT a.*,l.actor_id,l.relation_kind,l.origin,l.root_id,l.revision '
            'FROM memory_annotations a JOIN relationship_annotation_links l ON l.annotation_id=a.id WHERE a.id=? AND a.scope_id=?',
            (base,scope.scope_id)) if isinstance(base,str) else None
        if action!='create' and not previous:
            raise SessionFailure('scope_mismatch','关系材料不存在或在其他受众中')
        if action=='create' and base:
            raise SessionFailure('invalid_relation_input','create 不能引用旧版材料')
        if previous:
            if previous['actor_id']!=actor:
                raise SessionFailure('actor_mismatch','不能修改其他人物的关系材料')
            self.require_sources(scope,json.loads(previous['sources']))
            if previous['status']!='active' or (previous['expires'] and previous['expires']<=self.life.clock()):
                raise SessionFailure('stale_context','关系材料已被替换、解决、撤销或过期')
        relation_kind = op.get('relation_kind',previous['relation_kind'] if previous else '')
        origin = op.get('origin','interpretation')
        if relation_kind not in {'preference','agreement','repair','expectation','role_description'} or origin not in {'explicit_statement','interpretation'}:
            raise SessionFailure('invalid_relation_input','关系材料类型或来源归属无效')
        if previous and relation_kind!=previous['relation_kind']:
            raise SessionFailure('invalid_relation_input','替换不能改变材料类型')
        if action=='resolve' and relation_kind not in {'repair','agreement'}:
            raise SessionFailure('invalid_relation_input','只有 repair/agreement 可以 resolve；偏好取消使用 revoke')
        confidence = number(op.get('confidence',1.),'note.confidence',lo=0,hi=1)
        days = number(op.get('lifetime_days',0),'note.lifetime_days',lo=0,hi=3650)
        salience = op.get('salience','normal')
        if salience not in {'low','normal','high'}:
            raise SessionFailure('invalid_relation_input','关系材料重要性无效')
        text = op.get('text',previous['text'] if previous else '')
        if not isinstance(text,str) or not text.strip() or len(text)>2000:
            raise SessionFailure('invalid_relation_input','关系材料需要非空正文，最长 2000 字符')
        sources = list(dict.fromkeys(op['sources']+(json.loads(previous['sources']) if previous else [])))
        events=self.require_sources(scope,sources)
        from .learning_evidence import applicability_binding,user_statements
        from .memory_policy import INFERENCE_DAYS,source_deadline
        if origin=='explicit_statement' and len(user_statements(events))!=len(events):
            raise SessionFailure('invalid_relation_input','explicit_statement_requires_direct_user_text')
        application,reference,context_revision=applicability_binding(self.repo,scope,sources,
            op.get('applicability','task') if relation_kind in {'preference','role_description'} else 'ongoing',op.get('intent_ref'))
        if origin=='interpretation':
            days=min(days or INFERENCE_DAYS,INFERENCE_DAYS)
        elif application!='ongoing':
            days=min(days or INFERENCE_DAYS,INFERENCE_DAYS)
        status = {'create':'active','replace':'active','resolve':'resolved','revoke':'revoked'}[action]
        now = self.life.clock()
        expires=now+days*86400 if days else None
        if origin=='interpretation':
            expires=source_deadline(self.repo,sources,expires,now,scope)
        self.repo._exec('INSERT INTO memory_annotations(id,scope_id,kind,text,cues,sources,created,expires,status,always_load) '
            "VALUES(?,?,'relationship_note',?,'[]',?,?,?,?,?)",(identity,scope.scope_id,text.strip(),canonical(sources),now,expires,status,0))
        self.repo._exec('UPDATE memory_annotations SET applicability=?,context_ref=?,context_revision=? WHERE id=?',
            (application,reference,context_revision,identity))
        root = previous['root_id'] if previous else identity
        revision = previous['revision']+1 if previous else 1
        self.repo._exec('INSERT INTO relationship_annotation_links VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (identity,scope.agent_id,actor,root,revision,relation_kind,origin,confidence,salience,
             interpretation_ref if origin=='interpretation' else None,base,now,checksum))
        for term in set(terms(text)):
            self.repo._exec('INSERT OR IGNORE INTO annotation_terms VALUES(?,?)',(identity,term))
        if previous:
            self.repo._exec("UPDATE memory_annotations SET status='superseded' WHERE id=? AND status='active'",(base,))
        return self.note_receipt(identity)

    def note_receipt(self,identity):
        row = self.repo._one('SELECT a.status,l.root_id,l.revision,l.actor_id FROM memory_annotations a '
            'JOIN relationship_annotation_links l ON l.annotation_id=a.id WHERE a.id=?',(identity,))
        return dict(note_ref=identity,**dict(row))

    def visible_notes(self,scope,actor):
        rows = self.repo._all("SELECT a.*,l.relation_kind,l.actor_id,l.origin,l.confidence,l.salience,l.revision,l.root_id,l.interpretation_ref "
            'FROM memory_annotations a JOIN relationship_annotation_links l ON l.annotation_id=a.id '
            "WHERE a.scope_id=? AND l.agent_id=? AND l.actor_id=? AND a.status='active' AND l.origin!='legacy_unverified' "
            "AND (a.expires IS NULL OR a.expires>?) ORDER BY CASE l.salience WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,l.updated_at DESC",
            (scope.scope_id,scope.agent_id,actor,self.life.clock()))
        result = []
        for row in rows:
            try:
                self.require_sources(scope,json.loads(row['sources']))
            except SessionFailure:
                continue
            from .learning_evidence import annotation_view
            view=annotation_view(self.repo,scope,row,self.life.clock())
            if view is None:
                continue
            result.append(dict(note_ref=row['id'],actor_id=actor,kind=row['relation_kind'],text=view['text'],
                authority=view['authority'],applicability=row['applicability'],source_quotes=view['source_quotes'],
                status='open' if row['relation_kind']=='repair' else 'active',origin=row['origin'],confidence=row['confidence'],
                salience=row['salience'],revision=row['revision'],sources=json.loads(row['sources'])))
        return result

    def view(self,scope,actor):
        actor = actor_id(actor)
        person = Scope(scope.agent_id,scope.conversation_id,actor,scope.audience_policy_id)
        state = self.load(person)
        evidence = {k:{} for k in self.registry}
        for row in self.repo._all("SELECT event_id,appraisal FROM state_journal WHERE scope_id=? AND rule_version='life-state-5' ORDER BY id",(scope.scope_id,)):
            raw = json.loads(row['appraisal'])
            if raw.get('subject_actor_id')!=actor:
                continue
            roots = raw.get('_stimuli',[])
            try:
                self.require_sources(scope,roots+raw.get('context_refs',[]))
            except SessionFailure:
                continue
            for effect in raw.get('_relation_effects',[]):
                if effect['metric'] in evidence and not effect.get('confidence_filtered'):
                    evidence[effect['metric']].update({eid:raw['id'] for eid in roots})
        metrics = {}
        for key,spec in self.registry.items():
            count = len(evidence[key])
            basis = state.metric_basis[key]
            if not count and scope.conversation_id.startswith('qq:group:'):
                basis = 'unobserved'
            meaning = spec.bands[semantic_band(state.metrics[key])]
            if basis in {'policy_initialization','legacy_unverified','unobserved'}:
                meaning = ('政策保留的基础位置；当前没有足够共同经历依据，不能据此声称了解其习惯。' if basis=='policy_initialization' else
                    '本场合没有可引用依据；不据此编造共同经历。' if basis=='unobserved' else '遗留值缺少可核实依据；保留解释空间。')
            metrics[key] = dict(value=state.metrics[key],meaning=meaning,basis=basis,
                evidence_status='supported' if count>=3 else 'limited' if count else 'unobserved',
                visible_evidence_count=count,visible_reason_refs=list(dict.fromkeys(evidence[key].values()))[-2:])
        notes = self.visible_notes(scope,actor)
        prefs = [n for n in notes if n['kind'] in {'preference','role_description'}]
        matters = [n for n in notes if n['kind'] in {'repair','expectation','agreement'}]
        commitments = [json.loads(r['v']) for r in self.repo._all("SELECT v FROM meta WHERE k LIKE 'action_task:%' "
            "AND json_extract(v,'$.scope_id')=? AND json_extract(v,'$.status') IN ('active','waiting') ORDER BY rowid DESC LIMIT 3",(scope.scope_id,))]
        return dict(actor_id=actor,role='owner' if actor==self.owner else 'other',relation_protocol=2,
            relationship_version=state.version,policy=dict(version=self.policy.version,minimum=self.policy.bounds(actor)[0],maximum=self.policy.bounds(actor)[1]),
            metrics=metrics,semantics_version=SEMANTICS_VERSION,active_preferences=prefs[:4],open_relationship_matters=matters[:3],
            additional_visible_note_refs=[n['note_ref'] for n in prefs[4:]+matters[3:]],commitments=commitments)

    def semantic_reference(self):
        return dict(relation_protocol=2,semantics_version=SEMANTICS_VERSION,owner_actor_id=self.owner,
            owner_range=[.7,1.],other_range=[0.,.9],bands=['[0,.2)','[.2,.4)','[.4,.6)','[.6,.8)','[.8,1]'],
            metrics={key:dict(label=s.label,definition=s.definition,bands=list(s.bands)) for key,s in self.registry.items()})
