"""Durable CNY admission for Bot-owned DSH streams, including descendants.

Amounts are integer nano-yuan. Reservations use the entire supported model
context at the highest tariff, not a character estimate or an assumed cache hit.
An unreported request retains its reservation across midnight and restart.
"""
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import json
import time

from .agent_session import SessionFailure,canonical

ZONE=timezone(timedelta(hours=8))
UNIT=1_000_000_000
PRICE_VERSION='deepseek-flash-2026-10-24'
CONTEXT_MAX=1_048_576
OUTPUT_MAX=393_216


@dataclass(frozen=True)
class SpendingPolicy:
    enabled: bool=False
    daily_target_fen: int=500
    daily_limit_fen: int=1000

    @classmethod
    def parse(cls,raw=None):
        raw=raw or {}
        if not isinstance(raw,dict) or set(raw)-set(cls.__dataclass_fields__):
            raise ValueError('invalid_spending_policy')
        p=cls(**raw)
        if type(p.enabled) is not bool or any(type(v) is not int or v<0 for v in (p.daily_target_fen,p.daily_limit_fen)) or p.daily_target_fen>p.daily_limit_fen:
            raise ValueError('invalid_spending_policy')
        return p


def reported_cost(record):
    """Return conservative priced usage, or None when a complete bill is unknown.

    Weekday holidays may be charged at peak here (an upper bound). Reasoning is
    already part of outputTokens and must not be added twice.
    """
    if record.get('provider')!='deepseek-official' or record.get('model')!='deepseek-flash':
        return None
    usage=record.get('usage')
    if usage is None:
        usage=dict(inputTokens=record.get('uncached_input'),cacheReadTokens=record.get('cache_read'),
                   outputTokens=record.get('output'),cacheWriteTokens=record.get('cache_write'))
    keys=('inputTokens','cacheReadTokens','outputTokens')
    if any(type(usage.get(k)) is not int or usage[k]<0 for k in keys) or usage.get('cacheWriteTokens') not in (None,0):
        return None
    when=datetime.fromtimestamp(record.get('started_at') or 0,ZONE)
    # Any peak interval touched by a request is priced at the higher tariff.
    # Endpoints alone miss, for example, an 08:59--12:01 request.
    end=datetime.fromtimestamp(record.get('finished_at') or record.get('started_at') or 0,ZONE)
    if end<when:return None
    day=when.replace(hour=0,minute=0,second=0,microsecond=0)
    peak=(end-when).days>=7
    while day<=end and not peak:
        peak=day.weekday()<5 and any(when<day+timedelta(hours=b) and end>=day+timedelta(hours=a) for a,b in ((9,12),(14,18)))
        day+=timedelta(days=1)
    factor=2 if peak else 1
    return factor*(usage['inputTokens']*1000+usage['cacheReadTokens']*20+usage['outputTokens']*4000)


class Spending:
    def __init__(self,role):
        self.role,self.repo=role,role.repo
        self.policy=SpendingPolicy.parse(getattr(role.cfg,'spending_policy',None))
        self.repo._exec('CREATE TABLE IF NOT EXISTS spending_calls(call_key TEXT PRIMARY KEY,session_id TEXT NOT NULL,'
            'day TEXT NOT NULL,reserved INTEGER NOT NULL,charged INTEGER,state TEXT NOT NULL,payload TEXT NOT NULL)')
        # A lost callback can be recovered from the independently persisted SDK
        # usage projection. Only an exact call identity with complete counts can
        # release a reservation; time passing is never evidence of zero cost.
        for row in self.repo._all("SELECT s.session_id,u.payload FROM spending_calls s JOIN usage_calls u USING(call_key) WHERE s.state='reserved'"):
            data=json.loads(row['payload'])
            if data.get('finished_at') and reported_cost(data) is not None:
                data['usage']=dict(inputTokens=data['uncached_input'],cacheReadTokens=data['cache_read'],outputTokens=data['output'],cacheWriteTokens=data.get('cache_write'))
                self.settle(dict(session_id=row['session_id'],record=data))

    def snapshot(self,now=None):
        now=time.time() if now is None else now
        day=datetime.fromtimestamp(now,ZONE).date().isoformat()
        start=datetime.fromtimestamp(now,ZONE).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        ledger={r['call_key']:dict(r) for r in self.repo._all('SELECT * FROM spending_calls WHERE day=? OR state=?',(day,'reserved'))}
        charged=sum(r['charged'] or 0 for r in ledger.values() if r['state']=='settled')
        reserved=sum(r['reserved'] for r in ledger.values() if r['state']=='reserved')
        unknown=sum(r['state']=='reserved' for r in ledger.values())
        for r in self.repo._all("SELECT payload FROM usage_calls WHERE json_extract(payload,'$.started_at')>=?",(start,)):
            value=json.loads(r['payload'])
            if value['call_key'] in ledger:
                continue
            price=reported_cost(value)
            if price is None:
                # Existing unmetered requests are never silently treated as free.
                reserved+=CONTEXT_MAX*2000+OUTPUT_MAX*8000;unknown+=1
            else:charged+=price
        return dict(day=day,charged_nano=charged,reserved_nano=reserved,unknown_calls=unknown,
            charged_yuan=round(charged/UNIT,6),reserved_yuan=round(reserved/UNIT,6),
            target_yuan=self.policy.daily_target_fen/100,limit_yuan=self.policy.daily_limit_fen/100,
            state='disabled' if not self.policy.enabled else 'limited' if charged+reserved+CONTEXT_MAX*2000+16384*8000>self.policy.daily_limit_fen*10_000_000
            else 'economy' if charged>=self.policy.daily_target_fen*10_000_000 else 'normal',
            price_version=PRICE_VERSION,scope='bot_owned_DSH_model_streams')

    def reserve(self,root,call):
        if not self.policy.enabled:return dict(ok=True,enabled=False)
        record=call.get('record')
        if not isinstance(record,dict) or not isinstance(record.get('call_key'),str):
            raise SessionFailure('invalid_input','费用预留缺少调用标识')
        if record.get('provider')!='deepseek-official' or record.get('model')!='deepseek-flash' or getattr(self.role.cfg,'base_url','https://api.deepseek.com').rstrip('/') not in {'https://api.deepseek.com','https://api.deepseek.com/v1'}:
            raise SessionFailure('budget_unpriced','当前模型或接口尚无核实价格；预算保护已阻止请求')
        maximum=record.get('max_tokens')
        if type(maximum) is not int or not 1<=maximum<=OUTPUT_MAX:
            raise SessionFailure('budget_unbounded','模型请求缺少有效输出上限；预算保护已阻止请求')
        required=CONTEXT_MAX*2000+maximum*8000
        with self.repo.tx():
            if self.repo._one('SELECT 1 FROM spending_calls WHERE call_key=?',(record['call_key'],)):
                raise SessionFailure('idempotency_conflict','一次预留不能用于第二次模型请求')
            state=self.snapshot()
            if state['charged_nano']+state['reserved_nano']+required>self.policy.daily_limit_fen*10_000_000:
                raise SessionFailure('budget_limit','当日余额不足以覆盖下一次请求的最坏费用，已暂停付费调用；北京时间次日重新计算额度')
            refs=record.get('input_refs') or []
            interactive=bool(refs and self.repo._one('SELECT 1 FROM events WHERE id IN ('+','.join('?' for _ in refs)+") AND source_kind='user_report'",refs))
            # Explicit user work remains eligible; autonomous interests do not
            # become interactive just because an old turn once had user input.
            if not interactive:
                intent=root['intent_ref'] if 'intent_ref' in root.keys() else None
                origin=self.repo._one('SELECT v FROM meta WHERE k=?',('initiative:intent:'+intent,)) if intent else None
                interactive=bool(origin and json.loads(origin['v']).get('classification')=='interactive')
            if not interactive and state['charged_nano']>=self.policy.daily_target_fen*10_000_000:
                raise SessionFailure('budget_target','今日已到节省目标，自主后台工作延后；保留对话额度')
            record=dict(record,session_id=call['session_id'],started_at=time.time(),price_version=PRICE_VERSION)
            self.repo._exec('INSERT INTO spending_calls VALUES(?,?,?,?,?,?,?)',(record['call_key'],call['session_id'],state['day'],required,None,'reserved',canonical(record)))
        return dict(ok=True,enabled=True,reserved_nano=required,price_version=PRICE_VERSION)

    def settle(self,call):
        if not self.policy.enabled:return dict(ok=True,enabled=False)
        record=call.get('record',{})
        with self.repo.tx():
            row=self.repo._one('SELECT * FROM spending_calls WHERE call_key=?',(record.get('call_key'),))
            if not row or row['session_id']!=call['session_id']:
                raise SessionFailure('scope_mismatch','费用结算与预留不匹配')
            if row['state']=='settled':return dict(ok=True)
            saved=json.loads(row['payload'])
            saved.update(usage=record.get('usage'),finished_at=record.get('finished_at') or time.time())
            cost=reported_cost(saved)
            if cost is not None:
                # Charge on completion day, so a call spanning midnight cannot
                # release today's reservation without paying today's ledger.
                day=datetime.fromtimestamp(saved['finished_at'],ZONE).date().isoformat()
                self.repo._exec('UPDATE spending_calls SET day=?,charged=?,state=?,payload=? WHERE call_key=?',
                    (day,cost,'settled',canonical(saved),record['call_key']))
        return dict(ok=True,known=cost is not None)

    def background_allowed(self):
        if not self.policy.enabled:return True
        state=self.snapshot()
        return state['state']=='normal'

    def reserve_external(self, call_key, *, purpose, maximum_nano, price_version, source_refs):
        """Same ledger/hard cap for explicitly priced embedding computation."""
        if (type(maximum_nano) is not int or maximum_nano < 0 or not price_version
                or purpose not in {'memory_index','memory_query','memory_organize'}):
            raise SessionFailure('budget_unpriced','外部计算缺少有效价格版本和最大预留')
        with self.repo.tx():
            if self.repo._one('SELECT 1 FROM spending_calls WHERE call_key=?',(call_key,)):
                raise SessionFailure('idempotency_conflict','此计算已经预留，不可重复提交')
            state = self.snapshot()
            if self.policy.enabled:
                if state['charged_nano']+state['reserved_nano']+maximum_nano > self.policy.daily_limit_fen*10_000_000:
                    raise SessionFailure('budget_limit','预算不足，记忆计算已暂停，原始经历继续保存')
                if state['charged_nano'] >= self.policy.daily_target_fen*10_000_000:
                    raise SessionFailure('budget_target','记忆后台计算已到节省目标')
            payload = dict(origin='memory_embedding', purpose=purpose, price_version=price_version,
                           source_refs=source_refs, started_at=time.time())
            self.repo._exec('INSERT INTO spending_calls VALUES(?,?,?,?,?,?,?)',
                (call_key,'memory:'+self.role.life.agent_id,state['day'],maximum_nano,None,'reserved',canonical(payload)))
        return dict(reserved_nano=maximum_nano,price_version=price_version)

    def settle_external(self, call_key, actual_nano):
        if actual_nano is not None and (type(actual_nano) is not int or actual_nano < 0):
            raise ValueError('invalid_external_cost')
        with self.repo.tx():
            row = self.repo._one('SELECT * FROM spending_calls WHERE call_key=?',(call_key,))
            if not row or row['session_id']!='memory:'+self.role.life.agent_id:
                raise SessionFailure('scope_mismatch','外部计算预留不属于此角色')
            if row['state']=='settled':
                return
            payload=json.loads(row['payload'])
            payload.setdefault('finished_at',time.time())
            if actual_nano is None:
                # The response completion is known even when its bill is not.
                # Keep the reservation, including across days and restarts.
                self.repo._exec('UPDATE spending_calls SET payload=? WHERE call_key=?',(canonical(payload),call_key))
                return
            day=datetime.fromtimestamp(payload['finished_at'],ZONE).date().isoformat()
            self.repo._exec("UPDATE spending_calls SET day=?,charged=?,state='settled',payload=? WHERE call_key=?",
                            (day,actual_nano,canonical(payload),call_key))

    def admission_paused(self):
        """Cheap host work may continue; preserve inputs without retrying the model."""
        if not self.policy.enabled:return False
        state=self.snapshot()
        return state['charged_nano']+state['reserved_nano']+CONTEXT_MAX*2000+16384*8000>self.policy.daily_limit_fen*10_000_000
