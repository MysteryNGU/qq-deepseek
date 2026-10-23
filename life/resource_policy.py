"""Deterministic attention pacing and bounded reading; never plans the role's life."""
from dataclasses import asdict,dataclass,fields
import hashlib
import json
import math
import time

from .agent_session import SessionFailure,canonical,digest
from .text import stable_id


@dataclass(frozen=True)
class ResourcePolicy:
    idle_base_seconds: int = 900
    idle_max_seconds: int = 7200
    unchanged_per_tier: int = 2
    text_preview_target_tokens: int = 1500
    text_result_max_tokens: int = 2000
    text_result_max_utf8_bytes: int = 12000
    dynamic_context_target_tokens: int = 3000
    memory_context_target_tokens: int = 1200

    @classmethod
    def parse(cls,raw=None):
        raw = {} if raw is None else raw
        if not isinstance(raw,dict) or set(raw)-{f.name for f in fields(cls)}:
            raise ValueError('unknown_resource_policy')
        result = cls(**raw)
        if any(type(v) is not int or v<=0 for v in asdict(result).values()):
            raise ValueError('resource_policy_requires_positive_integers')
        if (result.idle_max_seconds<result.idle_base_seconds or result.text_result_max_tokens<result.text_preview_target_tokens
                or result.text_result_max_tokens<128 or result.text_result_max_utf8_bytes<512):
            raise ValueError('invalid_resource_policy_bounds')
        return result

    @property
    def version(self):
        return 'resources-'+digest(asdict(self))[:16]

    def idle_delay(self,streak):
        tier = min(streak//self.unchanged_per_tier,math.ceil(math.log2(self.idle_max_seconds/self.idle_base_seconds)))
        return min(self.idle_max_seconds,self.idle_base_seconds*2**tier)


def estimate_tokens(value):
    """Conservative UTF-8 estimate, explicitly separate from provider billing."""
    body = value if isinstance(value,str) else canonical(value)
    return max(1,math.ceil(len(body.encode('utf-8'))/2))


class AttentionPlans:
    KINDS = {'idle_probe','self_scheduled','task_due','delegate_result','tool_result','recovery'}

    def __init__(self,role,policy):
        self.role,self.repo,self.policy = role,role.repo,policy
        with self.repo.tx():
            migrate = not self.repo._one("SELECT 1 FROM meta WHERE k='attention_plan_schema'")
            columns = {r['name'] for r in self.repo._all('PRAGMA table_info(role_wakes)')}
            additions = dict(kind="TEXT NOT NULL DEFAULT 'self_scheduled'",semantic_key="TEXT NOT NULL DEFAULT ''",
                generation='INTEGER NOT NULL DEFAULT 0',forget_epoch='INTEGER NOT NULL DEFAULT 0',
                plan_revision='INTEGER NOT NULL DEFAULT 0',source_refs="TEXT NOT NULL DEFAULT '[]'",input_id='TEXT')
            for name,definition in additions.items():
                if name not in columns:
                    self.repo._exec(f'ALTER TABLE role_wakes ADD COLUMN {name} {definition}')
            self.repo._exec('CREATE TABLE IF NOT EXISTS idle_states(scope_id TEXT PRIMARY KEY,unchanged_streak INTEGER NOT NULL,'
                'last_attention_at REAL NOT NULL,last_outcome_ref TEXT)')
            # Only exact former host entry texts are classified as default idle.
            # Unknown historical reasons remain explicit plans, never guessed.
            known = ('空闲时觉察自己的状态与想法，自行选择接下来做什么，也可以休息。',
                '空闲时觉察自己想做的事；由自己选择行动、表达或休息。',
                '安静时觉察当前感受和想法，自行选择下一步，也可以继续休息。')
            for row in self.repo._all("SELECT * FROM role_wakes WHERE plan_revision=0"):
                kind = 'task_due' if row['intent_ref'] else 'idle_probe' if row['reason'] in known else 'self_scheduled'
                binding = self.repo._one("SELECT generation FROM agent_sessions WHERE scope_id=? AND status='current'",(row['scope_id'],))
                self.repo._exec('UPDATE role_wakes SET kind=?,semantic_key=?,generation=?,forget_epoch=?,plan_revision=1 WHERE id=?',
                    (kind,('idle:'+row['scope_id']) if kind=='idle_probe' else 'legacy:'+row['id'],binding['generation'] if binding else 0,
                     self.repo.forget_epoch(row['scope_id']),row['id']))
            for row in self.repo._all("SELECT scope_id,MIN(due) AS due FROM role_wakes WHERE kind='idle_probe' AND state='pending' GROUP BY scope_id") if migrate else []:
                self.repo._exec("UPDATE role_wakes SET state='cancelled' WHERE scope_id=? AND kind='idle_probe' AND state='pending'",(row['scope_id'],))
                binding = self.repo._one("SELECT scope_json FROM agent_sessions WHERE scope_id=? AND status='current'",(row['scope_id'],))
                if binding:
                    from .contracts import Scope
                    self.schedule(Scope(**json.loads(binding['scope_json'])),None,policy.idle_base_seconds,'恢复后的空闲思考机会',kind='idle_probe')
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('resource_policy_current',canonical(dict(version=policy.version,parameters=asdict(policy),at=time.time()))))
            self.repo._exec("INSERT OR REPLACE INTO meta VALUES('attention_plan_schema','1')")

    def schedule(self,scope,ref,seconds,reason,*,kind='self_scheduled',sources=None,semantic_key=None):
        if kind not in self.KINDS or isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or seconds<0:
            raise SessionFailure('invalid_input','唤醒种类或时间无效')
        if ref and self.role.tasks.blocker(ref,scope.scope_id):
            return
        binding = self.role.sessions.binding(scope)
        semantic = semantic_key or ('idle:'+scope.scope_id if kind=='idle_probe' else 'task:'+ref if ref else kind+':'+scope.scope_id)
        key = stable_id('wake-plan',scope.scope_id,semantic)
        with self.repo.tx():
            old = self.repo._one('SELECT plan_revision FROM role_wakes WHERE id=?',(key,))
            self.repo._exec('INSERT INTO role_wakes(id,scope_id,intent_ref,reason,due,state,kind,semantic_key,generation,forget_epoch,plan_revision,source_refs,input_id) '
                "VALUES(?,?,?,?,?,'pending',?,?,?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET intent_ref=excluded.intent_ref,reason=excluded.reason,due=excluded.due,state='pending',"
                'kind=excluded.kind,generation=excluded.generation,forget_epoch=excluded.forget_epoch,plan_revision=excluded.plan_revision,source_refs=excluded.source_refs,input_id=NULL',
                (key,scope.scope_id,ref,reason,time.time()+seconds,kind,semantic,binding['generation'],self.repo.forget_epoch(scope.scope_id),
                 old['plan_revision']+1 if old else 1,canonical(sources or [])))
            if sources is not None:
                self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('wake_reply_sources:'+key,canonical(self.role.reply_sources(sources))))
        return key

    def external_input(self,scope):
        with self.repo.tx():
            self.repo._exec('DELETE FROM meta WHERE k=?',('attention_explicit:'+scope.scope_id,))
            self.repo._exec("UPDATE role_wakes SET state='cancelled' WHERE scope_id=? AND kind='idle_probe' AND state='pending'",(scope.scope_id,))
            self.repo._exec('INSERT INTO idle_states VALUES(?,0,?,NULL) ON CONFLICT(scope_id) DO UPDATE SET unchanged_streak=0',
                (scope.scope_id,time.time()))

    async def dispatch_due(self,now):
        import asyncio
        from .contracts import Scope
        rows = self.repo._all("SELECT * FROM role_wakes WHERE state IN ('pending','dispatching') AND due<=? ORDER BY due",(now,))
        scopes = dict.fromkeys(r['scope_id'] for r in rows)
        for scope_id in scopes:
            binding = self.repo._one("SELECT * FROM agent_sessions WHERE scope_id=? AND status='current' AND parent_session_id IS NULL",(scope_id,))
            if not binding:
                continue
            batch = []
            for plan in (r for r in rows if r['scope_id']==scope_id):
                invalid = plan['forget_epoch']!=self.repo.forget_epoch(scope_id) or plan['generation']!=binding['generation']
                invalid = invalid or bool(plan['intent_ref'] and self.role.tasks.blocker(plan['intent_ref'],scope_id))
                if invalid:
                    with self.repo.tx():
                        self.repo._exec("UPDATE role_wakes SET state='cancelled' WHERE id=? AND plan_revision=?",(plan['id'],plan['plan_revision']))
                else:
                    batch.append(plan)
            if not batch:
                continue
            pending = self.role.sessions.pending(binding['session_id'])
            last = self.repo._one('SELECT payload FROM agent_events WHERE session_id=? '
                "AND json_extract(payload,'$.type') IN ('turn/start','turn/end') ORDER BY seq DESC LIMIT 1",(binding['session_id'],))
            busy = bool(pending or last and json.loads(last['payload'])['type']=='turn/start')
            inflight = next((p for p in batch if p['state']=='dispatching' and p['input_id']),None)
            if inflight:
                # A lost ACK is reconciled before admitting additional reasons.
                batch = [p for p in batch if p['input_id']==inflight['input_id']]
                event = self.repo.get_event(inflight['input_id'])
                if not event or event.redacted_at:
                    raise SessionFailure('source_removed','派发中的唤醒来源已失效')
            else:
                if busy:
                    real = [p for p in batch if p['kind'] in {'tool_result','delegate_result'}]
                    if real:
                        batch = real
                    elif any(p['kind']=='recovery' for p in batch):
                        if any(p['state']=='accepted' for p in pending):
                            for item in pending:
                                if item['state']=='accepted':
                                    await self.role.sessions.deliver(item,reconcile=True)
                            continue
                        if any(p['state'] not in {'consumed','settled'} for p in pending):
                            continue
                        status = await asyncio.to_thread(self.role.bridge.session_events,binding['session_id'],binding['cursor'],generation=binding['generation'])
                        if status.get('agent_status')!='idle':
                            continue
                        batch = [p for p in batch if p['kind']=='recovery']
                    else:
                        continue
                scope = Scope(**json.loads(binding['scope_json']))
                reasons = [dict(plan_id=p['id'],revision=p['plan_revision'],kind=p['kind'],reason=p['reason'],intent_ref=p['intent_ref']) for p in batch]
                sources = []
                for p in batch:
                    source = self.repo._one('SELECT v FROM meta WHERE k=?',('wake_reply_sources:'+p['id'],))
                    sources.extend(json.loads(source['v']) if source else json.loads(p['source_refs']))
                sources = list(dict.fromkeys(sources))
                payload = dict(wake_kind=batch[0]['kind'] if len({p['kind'] for p in batch})==1 else 'mixed',wake_plans=reasons,reply_source_ids=sources)
                identity = stable_id('wake-batch',*[p['id']+':'+str(p['plan_revision']) for p in batch])
                with self.repo.tx():
                    event,_ = self.role.life.ingest(scope,'runtime_wake','inference','\n'.join(p['reason'] for p in batch),
                        external_id=identity,source_event_ids=sources,payload=payload)
                    for p in batch:
                        if not self.repo._exec("UPDATE role_wakes SET state='dispatching',input_id=? WHERE id=? AND plan_revision=? AND state='pending'",
                                (event.id,p['id'],p['plan_revision'])).rowcount:
                            raise SessionFailure('stale_context','唤醒计划已改变，将重新合并')
            receipt = await self.role.ingest(event)
            if not isinstance(receipt,dict) or receipt.get('state') not in {'accepted','consumed','settled'}:
                continue  # Preserve dispatching + exact input identity for reconciliation.
            with self.repo.tx():
                for p in batch:
                    self.repo._exec("UPDATE role_wakes SET state='delivered' WHERE id=? AND plan_revision=? AND input_id=?",
                        (p['id'],p['plan_revision'],event.id))

    def finish(self,scope,root,proposal,outcome_ref,*,expressed=False):
        mode = proposal.get('next_attention')
        legacy = proposal.get('wake_after_seconds')
        if mode is not None and legacy is not None:
            raise SessionFailure('invalid_input','next_attention 与 wake_after_seconds 不能同时设置')
        if legacy is not None:
            mode = dict(mode='after',after_seconds=legacy)
        mode = mode or dict(mode='adaptive')
        if not isinstance(mode,dict) or mode.get('mode') not in {'adaptive','after','event','none'}:
            raise SessionFailure('invalid_input','next_attention.mode 无效')
        outcome = proposal.get('reflection_outcome','unspecified')
        if outcome not in {'unchanged','new_thought','action','unspecified'}:
            raise SessionFailure('invalid_input','reflection_outcome 无效')
        with self.repo.tx():
            old = self.repo._one('SELECT * FROM idle_states WHERE scope_id=?',(scope.scope_id,))
            if old and old['last_outcome_ref']==outcome_ref:
                return dict(duplicate=True)
            turn = self.role.turn_number(root['session_id'])
            inputs = self.repo._all('SELECT i.input_id,e.source_kind,e.payload FROM agent_inputs i JOIN events e ON e.id=i.input_id '
                'WHERE i.session_id=? AND i.claimed_turn IS ?',(root['session_id'],turn))
            trigger_idle = bool(inputs) and all(json.loads(i['payload']).get('wake_kind')=='idle_probe' for i in inputs)
            # Count only explicit committed changes; time, reads and accounting do not reset backoff.
            effects = expressed or bool(self.repo._one("SELECT 1 FROM life_calls WHERE session_id=? AND created>=? AND "
                "(json_extract(receipt,'$.intent') IS NOT NULL OR json_extract(receipt,'$.saved')=1 OR json_extract(receipt,'$.sent_confirmed')=1 OR json_extract(receipt,'$.status')='committed')",
                (root['session_id'],old['last_attention_at'] if old else time.time())))
            streak = old['unchanged_streak'] if old else 0
            if trigger_idle and outcome=='unchanged' and not effects:
                streak += 1
            elif effects or outcome in {'new_thought','action'} or any(i['source_kind']=='user_report' for i in inputs):
                streak = 0
            self.repo._exec('INSERT INTO idle_states VALUES(?,?,?,?) ON CONFLICT(scope_id) DO UPDATE SET unchanged_streak=excluded.unchanged_streak,'
                'last_attention_at=excluded.last_attention_at,last_outcome_ref=excluded.last_outcome_ref',
                (scope.scope_id,streak,time.time(),outcome_ref))
            selected = mode['mode']
            if selected=='after':
                key = self.schedule(scope,None,mode.get('after_seconds'),str(mode.get('reason','已明确安排的再思考')),kind='self_scheduled')
                self.repo._exec("UPDATE role_wakes SET state='cancelled' WHERE scope_id=? AND kind='idle_probe' AND state='pending'",(scope.scope_id,))
                return dict(plan_id=key,mode='after')
            if selected in {'none','event'}:
                if selected=='event' and not (mode.get('intent_ref') and self.role.tasks.get(mode['intent_ref'],scope.scope_id)):
                    raise SessionFailure('invalid_input','等待真实事件需要现有 intent_ref')
                self.repo._exec("UPDATE role_wakes SET state='cancelled' WHERE scope_id=? AND kind='idle_probe' AND state='pending'",(scope.scope_id,))
                self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('attention_explicit:'+scope.scope_id,canonical(dict(mode=selected,outcome_ref=outcome_ref))))
                return dict(mode=selected)
            explicit = self.repo._one("SELECT id FROM role_wakes WHERE scope_id=? AND kind IN ('self_scheduled','task_due') AND state IN ('pending','dispatching')",(scope.scope_id,))
            if explicit:
                return dict(mode='existing_explicit',plan_id=explicit['id'])
            if expressed and self.repo._one('SELECT 1 FROM meta WHERE k=?',('attention_explicit:'+scope.scope_id,)):
                return dict(mode='existing_explicit')
            delay = self.policy.idle_delay(streak)
            key = self.schedule(scope,None,delay,'空闲时可以继续想一想，也可以休息。',kind='idle_probe')
            return dict(mode='adaptive',plan_id=key,after_seconds=delay,unchanged_streak=streak)


def read_observation(role,scope,proposal,policy,*,original_text=None):
    repo = role.repo
    event = repo.get_event(str(proposal.get('event_id','')))
    if not event or event.redacted_at:
        raise SessionFailure('source_removed','观察已不存在或已遗忘')
    if event.scope.private_key!=scope.private_key or event.scope.audience_policy_id!=scope.audience_policy_id:
        raise SessionFailure('scope_mismatch','观察不属于当前受众')
    value = event.payload
    if value.get('dsh_session'):
        row = repo._one('SELECT payload FROM agent_events WHERE session_id=? AND seq=?',(value['dsh_session'],value['dsh_seq']))
        if row:
            value = json.loads(row['payload']).get('data',{})
    # The original tool value stays untouched. JSON is displayed as valid text,
    # and each read provides a hash and exact original line positions.
    body = value['text'] if isinstance(value.get('text'),str) else canonical(value)
    blocks = value.get('message',{}).get('content',[]) if isinstance(value,dict) else []
    if blocks and all(b.get('type')=='text' for b in blocks):
        body = '\n'.join(b['text'] for b in blocks)
    if original_text is not None:
        body = original_text
    content_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
    lines = body.splitlines() or ['']
    mode = proposal.get('mode','lines' if proposal.get('include_full') else 'summary')
    if mode not in {'summary','search','lines'}:
        raise SessionFailure('invalid_input','观察读取模式无效')
    for key in ('start_line','offset_chars','limit','before_lines','after_lines','limit_matches','max_tokens'):
        if key in proposal and (type(proposal[key]) is not int or proposal[key]<0):
            raise SessionFailure('invalid_input','分页参数必须是非负整数')
    cap = min(policy.text_result_max_tokens,proposal.get('max_tokens',policy.text_preview_target_tokens))
    cap = max(128,cap)
    cursor = proposal.get('cursor')
    start,offset = proposal.get('start_line',1)-1,proposal.get('offset_chars',0)
    if cursor:
        try:
            token = json.loads(cursor)
            if token['content_hash']!=content_hash:
                raise ValueError()
            start,offset = token['line']-1,token['offset_chars']
            if type(start) is not int or type(offset) is not int or start<0 or offset<0:
                raise ValueError()
        except (ValueError,KeyError,TypeError):
            raise SessionFailure('stale_context','正文已变化或分页 cursor 无效')
    selected = list(range(max(0,start),min(len(lines),max(0,start)+min(200,proposal.get('limit',80)))))
    if mode=='summary':
        selected = sorted(set(range(min(15,len(lines))))|set(range(max(0,len(lines)-15),len(lines))))
    elif mode=='search':
        query = proposal.get('query')
        if not isinstance(query,str) or not query:
            raise SessionFailure('invalid_input','search 需要 query')
        hits = [i for i,line in enumerate(lines) if query in line][:min(20,proposal.get('limit_matches',5))]
        selected = sorted({j for i in hits for j in range(max(0,i-min(20,proposal.get('before_lines',3))),min(len(lines),i+min(20,proposal.get('after_lines',6))+1))})
    result = dict(event_id=event.id,mode=mode,content_hash=content_hash,total_lines=len(lines),coverage='partial',
        source_coverage='partial' if value.get('truncated') else 'reported',lines=[],next_cursor=None,has_more=False,
        estimate_kind='utf8_estimate',include_full_adapted=bool(proposal.get('include_full')))
    if estimate_tokens(result)+80>cap or len(canonical(result).encode())+160>policy.text_result_max_utf8_bytes:
        raise SessionFailure('invalid_input','读取预算不足以保留来源、覆盖范围和完整回执')
    for i in selected:
        line = lines[i]
        at = offset if i==start else 0
        if mode=='search' and not cursor and proposal['query'] in line:
            # A JSON log may contain an entire transcript on one physical line.
            # Show the actual match rather than repeatedly returning its head.
            at = max(0,line.find(proposal['query'])-min(64,cap//8))
        end = min(len(line),at+max(1,cap))
        item = dict(line=i+1,offset_chars=at,text=line[at:end])
        candidate = dict(result,lines=result['lines']+[item],next_cursor=canonical(dict(content_hash=content_hash,line=i+1,offset_chars=end)))
        while item['text'] and (estimate_tokens(candidate)>cap or len(canonical(candidate).encode())>policy.text_result_max_utf8_bytes):
            end = at+len(item['text'])//2;item['text']=line[at:end]
        if not item['text'] and at<len(line):
            break
        result['lines'].append(item)
        result['has_more'] = end<len(line) or i<len(lines)-1
        result['next_cursor'] = canonical(dict(content_hash=content_hash,line=i+1 if end<len(line) else i+2,offset_chars=end if end<len(line) else 0)) if result['has_more'] else None
        if end<len(line) or estimate_tokens(result)>cap-128:
            break
    if mode=='lines' and not result['has_more'] and start==0 and len(result['lines'])==len(lines):
        result['coverage']='complete'
    return result
