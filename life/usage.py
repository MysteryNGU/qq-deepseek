"""Provider-reported usage projection. Estimates never fill missing billable counts."""
from datetime import datetime,timedelta,timezone
import json
import time

from .agent_session import SessionFailure,canonical

COUNTS = dict(uncached_input='inputTokens',cache_read='cacheReadTokens',cache_write='cacheWriteTokens',
    output='outputTokens',reasoning='reasoningTokens',provider_total='totalTokens')


class Usage:
    def __init__(self,role):
        self.role,self.repo = role,role.repo
        self._historical_attempts = {}
        self.repo._exec('CREATE TABLE IF NOT EXISTS usage_calls(call_key TEXT PRIMARY KEY,payload TEXT NOT NULL,source_revision INTEGER NOT NULL,updated_at REAL NOT NULL)')
        # Existing durable settlements are retained as historical evidence. New
        # calls are measured at the SDK stream entry, including failed summaries.
        if not self.repo._one("SELECT 1 FROM meta WHERE k='usage_historical_backfill'"):
            with self.repo.tx():
                for row in self.repo._all('SELECT session_id,payload FROM agent_events ORDER BY session_id,seq'):
                    self.historical(row['session_id'],json.loads(row['payload']))
                self.repo._exec("INSERT INTO meta VALUES('usage_historical_backfill','1')")

    def historical(self,sid,event):
        kind,data = event['type'],event.get('data',{})
        position = (sid,data.get('turn'),data.get('step'))
        if kind=='llm/retry-started':
            self._historical_attempts[position] = self._historical_attempts.get(position,0)+1
            return
        if kind not in {'assistant/message','assistant/attempt','compaction/summary'}:
            return
        if kind=='compaction/summary' and not data.get('llmStreamCall'):
            return
        usage = data.get('usage')
        stream = data.get('stream',{})
        if usage is None:
            chunks = stream.get('chunks',[]) if isinstance(stream,dict) else stream if isinstance(stream,list) else []
            for item in chunks:
                chunk = item.get('chunk',item)
                if chunk.get('type')=='usage':
                    usage = chunk.get('usage')
        binding = self.repo._one('SELECT * FROM agent_sessions WHERE session_id=?',(sid,))
        if not binding:
            return
        root = self.repo._one('SELECT * FROM agent_sessions WHERE session_id=?',(binding['parent_session_id'],)) if binding['parent_session_id'] else binding
        if not root:
            return
        identity = str(event['seq']) if kind=='compaction/summary' else f"{data.get('turn')}:{data.get('step')}:{self._historical_attempts.get(position,0)}"
        self.ingest(dict(call_key=f"historical:{sid}:{identity}",session_id=sid,root_session_id=root['session_id'],
            revision=event['seq']+1,started_at=event.get('time',0)/1000,finished_at=event.get('time',0)/1000,
            turn=data.get('turn'),step=data.get('step'),provider=data.get('provider','unknown'),model=data.get('model','unknown'),
            purpose='compaction' if kind=='compaction/summary' else 'unclassified',status='failed' if kind=='assistant/attempt' else 'completed',
            usage=usage,source_event_refs=[str(event['seq'])],historical=True),root)

    def ingest(self,record,root):
        if not isinstance(record,dict) or not isinstance(record.get('call_key'),str) or type(record.get('revision')) is not int:
            raise SessionFailure('invalid_input','用量记录缺少真实调用标识')
        sid = record.get('session_id')
        binding = self.repo._one('SELECT * FROM agent_sessions WHERE session_id=?',(sid,))
        if (not binding or record.get('root_session_id')!=root['session_id'] or
                (sid!=root['session_id'] and binding['parent_session_id']!=root['session_id'])):
            raise SessionFailure('scope_mismatch','用量调用不属于当前角色会话树')
        data = {key:record.get(key) for key in ('call_key','session_id','root_session_id','provider','model','turn','step','attempt',
            'started_at','finished_at','status','source_event_refs','context_estimate','historical','input_refs')}
        data.update(agent_id=self.role.life.agent_id,scope_id=root['scope_id'],parent_session_id=binding['parent_session_id'],
            intent_ref=binding['intent_ref'],price_version=None,price_estimate=None)
        purpose = record.get('purpose')
        if purpose!='compaction':
            purpose = 'delegate' if binding['parent_session_id'] else 'task' if binding['intent_ref'] else 'unclassified'
            refs = record.get('input_refs') or []
            latest = self.repo._one('SELECT e.source_kind,e.payload FROM agent_inputs i JOIN events e ON e.id=i.input_id '
                'WHERE i.session_id=? AND i.input_id IN ('+','.join('?' for _ in refs)+') ORDER BY i.ordinal DESC LIMIT 1',
                (root['session_id'],*refs)) if refs else None
            if purpose=='unclassified' and latest:
                wake = json.loads(latest['payload']).get('wake_kind')
                purpose = 'dialogue' if latest['source_kind']=='user_report' else 'idle_reflection' if wake=='idle_probe' else 'recovery' if wake=='recovery' else 'unclassified'
        data['purpose'] = purpose
        usage = record.get('usage')
        if usage is not None and not isinstance(usage,dict):
            raise SessionFailure('invalid_input','提供方 usage 格式无效')
        for key,provider_key in COUNTS.items():
            value = usage.get(provider_key) if usage else None
            if value is not None and (type(value) is not int or value<0):
                raise SessionFailure('invalid_input','提供方 token 数须为非负整数')
            data[key] = value
        data['coverage'] = 'reported' if data['uncached_input'] is not None and data['output'] is not None else 'partial' if usage else 'unknown'
        known_total = sum(data[k] or 0 for k in ('uncached_input','cache_read','cache_write','output'))
        data['total_mismatch'] = data['provider_total'] is not None and data['provider_total']!=known_total
        with self.repo.tx():
            old = self.repo._one('SELECT * FROM usage_calls WHERE call_key=?',(data['call_key'],))
            if old and json.loads(old['payload'])['session_id']!=sid:
                raise SessionFailure('idempotency_conflict','用量调用标识已用于其他会话')
            if old and old['source_revision']>=record['revision']:
                return
            self.repo._exec('INSERT INTO usage_calls VALUES(?,?,?,?) ON CONFLICT(call_key) DO UPDATE SET payload=excluded.payload,'
                'source_revision=excluded.source_revision,updated_at=excluded.updated_at',
                (data['call_key'],canonical(data),record['revision'],time.time()))

    def read(self,scope,p):
        owner = scope.conversation_id=='qq:private:'+str(self.role.cfg.owner)
        if p.get('global') and not owner:
            raise SessionFailure('scope_mismatch','全局用量仅对主人私聊开放')
        start = p.get('since')
        if start is None:
            zone = timezone(timedelta(hours=8))
            start = datetime.now(zone).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        end = p.get('until',time.time())
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) for v in (start,end)) or end<start:
            raise SessionFailure('invalid_input','用量时间范围无效')
        where,args = ['json_extract(payload,\'$.agent_id\')=?'],[scope.agent_id]
        if not p.get('global'):
            where.append("json_extract(payload,'$.scope_id')=?");args.append(scope.scope_id)
        for field in ('intent_ref','session_id','root_session_id','turn'):
            if field in p:
                where.append(f"json_extract(payload,'$.{field}')=?");args.append(p[field])
        values = [json.loads(r['payload']) for r in self.repo._all('SELECT payload FROM usage_calls WHERE '+' AND '.join(where),args)]
        values = [v for v in values if start<=(v['started_at'] or 0)<=end]
        totals = {k:sum(v[k] or 0 for v in values) for k in COUNTS}
        field_coverage = {k:dict(reported_calls=sum(v[k] is not None for v in values),unknown_calls=sum(v[k] is None for v in values)) for k in COUNTS}
        totals['reported_tokens'] = sum(totals[k] for k in ('uncached_input','cache_read','cache_write','output'))
        purposes = {key:sum(1 for v in values if v['purpose']==key) for key in sorted({v['purpose'] for v in values})}
        missing = sum(v['coverage']!='reported' or v['status'] in {'started','unknown'} for v in values)
        projection_gap = bool(self.repo._one('SELECT 1 FROM meta WHERE k=?',('usage_projection_gap:'+scope.scope_id,)))
        return dict(scope='bot_global' if p.get('global') else 'current_conversation',since=start,until=end,calls=len(values),
            known_totals=totals,field_coverage=field_coverage,unknown_or_partial_calls=missing,projection_gap=projection_gap,
            coverage='partial' if missing or projection_gap else 'reported_for_recorded_calls' if values else 'unknown',
            purposes=purposes,total_mismatches=sum(v['total_mismatch'] for v in values),price_estimate=None,price_status='not_configured',
            excludes=['external_plugins_calling_providers_outside_DSH','Codex'],budget_state='not_configured',
            source='DSH actual stream calls; historical durable settlements where available')

    def hint(self,scope,task=None):
        summary = self.read(scope,dict(intent_ref=task) if task else {})
        if summary['known_totals']['reported_tokens']<25000 and not summary['unknown_or_partial_calls'] and not summary['projection_gap']:
            return None
        return dict(coverage=summary['coverage'],reported_tokens=summary['known_totals']['reported_tokens'],
            unknown_calls=summary['unknown_or_partial_calls'],budget_state='not_configured',main_cost_source='unknown',
            suggestions=['先利用已有证据；大文件和日志可以先读关键片段，再决定是否展开全文。'])
