"""Scoped continuity over existing events, memories, operations and call receipts."""
from __future__ import annotations

import json
import time

from .agent_session import SessionFailure, canonical, digest
from .resource_policy import estimate_tokens
from .text import terms


class Continuity:
    def __init__(self, role):
        self.role, self.life, self.repo = role, role.life, role.repo
        with self.repo.tx():
            self.repo._exec("CREATE INDEX IF NOT EXISTS ix_continuity_operation_events ON events "
                "(agent_id,conversation_id,audience_policy_id,json_extract(payload,'$.operation_id'),ingest_seq)")

    def visible_event(self, scope, ref, seen=None, checked=None):
        checked = {} if checked is None else checked
        if ref in checked:
            return checked[ref]
        # Check the audience in SQL before fetching any body.
        row = self.repo._one('SELECT id FROM events WHERE id=? AND agent_id=? AND conversation_id=? '
            'AND audience_policy_id=? AND redacted_at IS NULL',
            (ref, *scope.private_key, scope.audience_policy_id))
        if not row:
            raise SessionFailure('source_removed', '来源已移除或不属于本受众；请重新读取当前材料')
        event = self.repo.get_event(ref)
        seen = set() if seen is None else seen
        if ref in seen:
            raise SessionFailure('invalid_input', '来源链存在循环；请重新选择来源')
        for parent in event.source_event_ids:
            self.visible_event(scope, parent, seen | {ref}, checked)
        checked[ref] = event
        return event

    def memory(self, scope, ref, *, active=False):
        row = self.repo._one('SELECT id FROM memories WHERE id=? AND agent_id=? AND conversation_id=? '
            'AND audience_policy_id=? AND status<>\'redacted\'',
            (ref, *scope.private_key, scope.audience_policy_id))
        if not row:
            raise SessionFailure('scope_mismatch', '记忆不可见；用 life_read(kind=continuity) 读取当前引用')
        note = self.repo.get_memory(ref)
        checked = {}
        for source in note.sources:
            self.visible_event(scope, source.event_id,checked=checked)
        if active and note.status not in {'active', 'disputed'}:
            raise SessionFailure('stale_context', '记忆已修订或归档；请读取当前版本')
        return note

    def evidence(self, scope, refs):
        if not isinstance(refs, list) or len(refs)>40 or any(not isinstance(r,str) for r in refs):
            raise SessionFailure('invalid_input', 'sources 需要最多 40 个本受众来源引用')
        events, memories, checked = set(), set(), {}
        def add(ref):
            if ref in events:
                return
            event = self.visible_event(scope, ref,checked=checked)
            events.add(ref)
            for parent in event.source_event_ids:
                add(parent)
        for ref in refs:
            if self.repo._one('SELECT 1 FROM memories WHERE id=?', (ref,)):
                memory = self.memory(scope, ref)
                memories.add(ref)
                for source in memory.sources:
                    add(source.event_id)
            else:
                add(ref)
        return sorted(events), sorted(memories)

    @staticmethod
    def cues(value):
        if (not isinstance(value, list) or len(value)>8 or
                any(not isinstance(c,str) or not c.strip() or len(c)>40 for c in value)):
            raise SessionFailure('invalid_input', 'cues 需要最多 8 个非空字符串，每个最多 40 字符')
        return list(dict.fromkeys(c.strip() for c in value))

    def activate(self, scope):
        key = 'continuity:reflection:'+scope.scope_id
        with self.repo.tx():
            row = self.repo._one('SELECT v FROM meta WHERE k=?', (key,))
            value = json.loads(row['v']) if row else None
            epoch = self.repo.forget_epoch(scope.scope_id)
            if not value or value['forget_epoch']!=epoch:
                floor = self.repo.max_ingest_seq(scope)
                value = dict(schema=1, forget_epoch=epoch, version=(value or {}).get('version',0)+1,
                    floor_seq=floor, through_seq=floor, activation_boundary=True)
                self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, canonical(value)))
            return value

    def notes(self, scope, *, query='', ref=None, limit=5, offset=0):
        if ref:
            values = [self.memory(scope,ref)]
            if any(n.kind!='interpretation' or n.source_kind!='inference' or not
                   set(n.topic_tags) & {'continuity:thought','continuity:reflection'} for n in values):
                raise SessionFailure('invalid_input','该引用不是念想或小结；普通记忆请使用 kind=memory')
        else:
            query_terms = list(dict.fromkeys(terms(query)))[:96]
            query_filter = (' AND EXISTS(SELECT 1 FROM memory_terms WHERE memory_id=memories.id '
                'AND term IN ('+','.join('?' for _ in query_terms)+'))') if query_terms else (' AND 0' if query else '')
            rows = self.repo._all("SELECT id FROM memories WHERE agent_id=? AND conversation_id=? "
                "AND audience_policy_id=? AND kind='interpretation' AND status IN ('active','disputed') "
                "AND EXISTS(SELECT 1 FROM json_each(topic_tags) WHERE value IN "
                "('continuity:thought','continuity:reflection'))"+query_filter+" ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
                (*scope.private_key, scope.audience_policy_id, *query_terms, limit, offset))
            values = []
            for row in rows:
                try:
                    values.append(self.memory(scope,row['id']))
                except SessionFailure:
                    continue
        result = [dict(ref=n.id, memory_id=n.id, version=n.version, status=n.status, text=n.summary,
                     type='reflection' if 'continuity:reflection' in n.topic_tags else 'thought',
                     sources=[s.event_id for s in n.sources]) for n in values
                if not query or set(terms(query)) & set(self.repo.memory_terms(n.id))]
        for note in result:
            row = self.repo._one("SELECT v FROM meta WHERE k LIKE 'continuity:expression:%' "
                "AND json_extract(v,'$.scope_id')=? AND EXISTS(SELECT 1 FROM json_each(v,'$.refs') WHERE value=?) LIMIT 1",
                (scope.scope_id,note['ref']))
            note['delivery'] = self.delivery(scope,json.loads(row['v'])['logical_message_id']) if row else 'not_requested'
        return result

    def write_note(self, scope, proposal, call_key, *, batch=False):
        p = proposal.get('continuity')
        if not isinstance(p,dict) or p.get('type') not in {'thought','reflection'}:
            raise SessionFailure('invalid_input', 'continuity.type 必须为 thought 或 reflection')
        allowed = {'type','action','base_ref','base_version','batch_ref','reviewed_ids','outcome'}
        if set(p)-allowed:
            raise SessionFailure('invalid_input', 'continuity 含未知字段；请读取协议说明')
        action, typ = p.get('action','create'), p['type']
        if action not in {'create','replace','archive'}:
            raise SessionFailure('invalid_input', '念想操作为 create、replace 或 archive')
        if not batch and (p.get('batch_ref') or p.get('outcome') or typ=='reflection' and action=='create'):
            raise SessionFailure('invalid_input', '新小结须先读取 reflection_batch 并提交完整 reviewed_ids')
        text = proposal.get('text','')
        if action!='archive' and (not isinstance(text,str) or not text.strip() or len(text)>400):
            raise SessionFailure('invalid_input', '念想或小结正文需要 1～400 字符')
        cues = self.cues(proposal.get('cues',[]))
        base_ref = p.get('base_ref')
        if action=='create' and (base_ref or p.get('base_version') is not None):
            raise SessionFailure('invalid_input','create 不替换旧条目；修订请使用 replace 和当前版本')
        if action in {'replace','archive'} and (not isinstance(base_ref,str) or type(p.get('base_version')) is not int):
            raise SessionFailure('invalid_input', 'replace/archive 需要 base_ref 和 base_version')
        base = self.memory(scope,base_ref) if base_ref else None
        if base and ('continuity:'+typ not in base.topic_tags or base.kind!='interpretation' or base.source_kind!='inference'):
            raise SessionFailure('invalid_input', '只能修改同类念想或小结，不能修改事实或用户叮嘱')
        if action=='archive':
            if base.status=='archived' and base.version==p['base_version']+1:
                return self.note_receipt(base,'duplicate')
            if base.version!=p['base_version'] or base.status not in {'active','disputed'}:
                raise SessionFailure('stale_context', '念想版本已变化；请重新读取后再归档')
            self.repo.update_memory(base.id,status='archived',version=base.version+1,updated_at=time.time())
            self.repo._set_terms(base.id,[])
            return self.note_receipt(self.repo.get_memory(base.id),'archived')
        sources, derived = self.evidence(scope,proposal.get('sources',[]))
        signature = digest(dict(type=typ,text=text.strip(),sources=sources,base_ref=base_ref,
                                epoch=self.repo.forget_epoch(scope.scope_id)))
        old = self.repo._one('SELECT id FROM memories WHERE agent_id=? AND conversation_id=? '
            'AND audience_policy_id=? AND event_group_id=? AND status<>\'redacted\' LIMIT 1',
            (*scope.private_key,scope.audience_policy_id,'continuity:'+signature))
        if old:
            return self.note_receipt(self.memory(scope,old['id']),'duplicate')
        if base and (base.version!=p['base_version'] or base.status not in {'active','disputed'}):
            raise SessionFailure('stale_context', '念想版本已变化；请重新读取当前版本')
        if base:
            previous, links = self.evidence(scope,[base.id])
            sources = sorted(set(sources+previous))
            derived = sorted(set(derived+links+[base.id]))
        origin,_ = self.life.ingest(scope,'reflection' if typ=='reflection' else 'thought','inference',text.strip(),
            external_id='continuity:'+signature,source_event_ids=sources,
            payload=dict(continuity_type=typ,call_ref=call_key))
        note = self.life.memory.remember(scope,'interpretation',text.strip(),[origin.id,*sources],
            source_kind='inference',topic_tags=['continuity:'+typ],extra_terms=cues,
            event_group_id='continuity:'+signature,supersedes_id=base.id if base else '',derived_from=derived)
        if note.status=='redacted':
            raise SessionFailure('source_removed', '来源已失效，念想未保存；请重新读取材料')
        if base:
            self.repo.update_memory(base.id,status='superseded',version=base.version+1,updated_at=time.time())
            self.repo._set_terms(base.id,[])
        return self.note_receipt(note,'saved')

    @staticmethod
    def note_receipt(note, result):
        return dict(note_ref=note.id,memory_id=note.id,version=note.version,status=note.status,
                    result=result,saved=result=='saved',archived=result=='archived',duplicate=result=='duplicate')

    def reflection_batch(self, scope, root, limit=12):
        cursor = self.activate(scope)
        # Only the first bounded chronological segment is offered; reading never advances it.
        rows = self.repo._all("SELECT id FROM events WHERE agent_id=? AND conversation_id=? AND audience_policy_id=? "
            "AND ingest_seq>? AND redacted_at IS NULL AND kind IN ('message','tool_result','media_generated','action_result') "
            "AND source_kind IN ('user_report','tool_observation') ORDER BY ingest_seq LIMIT ?",
            (*scope.private_key,scope.audience_policy_id,cursor['through_seq'],limit+1))
        selected, roots = [], set()
        for row in rows[:limit]:
            event = self.visible_event(scope,row['id'])
            root_id = event.payload.get('stimulus_event_id') or event.id
            if root_id not in roots:
                roots.add(root_id)
                selected.append(event)
        if not selected:
            return dict(items=[],has_more=False,cursor=cursor)
        items = [dict(id=e.id,seq=e.ingest_seq,kind=e.kind,occurred_at=e.occurred_at,
            received_at=e.received_at,preview=e.text[:200],truncated=len(e.text)>200,coverage='partial' if len(e.text)>200 else 'reported',
            content_hash=digest(e.payload)) for e in selected]
        value = dict(base_cursor_version=cursor['version'],forget_epoch=cursor['forget_epoch'],
            after_seq=cursor['through_seq'],through_seq=selected[-1].ingest_seq,items=items,
            has_more=len(rows)>limit,root_session_id=root['session_id'])
        value['batch_ref'] = 'reflection-batch:'+digest(value)
        return value

    def commit_reflection(self, scope, proposal, root, call_key):
        p = proposal['continuity']
        if p.get('type')!='reflection' or p.get('action','create') not in {'create','replace'}:
            raise SessionFailure('invalid_input','增量提交必须为 reflection create/replace')
        if p.get('action')=='archive' or p.get('outcome') not in {'summarized','no_new_insight'}:
            raise SessionFailure('invalid_input','批次提交需要 summarized/no_new_insight，归档不处理批次')
        row = self.repo._one("SELECT receipt FROM life_calls WHERE session_id=? AND json_extract(receipt,'$.batch_ref')=? LIMIT 1",
            (root['session_id'],p.get('batch_ref')))
        if not row:
            raise SessionFailure('stale_context','批次不是本会话实际读取的回执；重新读取 reflection_batch')
        batch = json.loads(row['receipt'])
        cursor = self.activate(scope)
        if cursor['forget_epoch']!=batch['forget_epoch'] or cursor['version']!=batch['base_cursor_version']:
            raise SessionFailure('stale_context','小结游标或来源已变化；重新读取 reflection_batch')
        ids = [item['id'] for item in batch['items']]
        reviewed = p.get('reviewed_ids')
        if not isinstance(reviewed,list) or len(reviewed)!=len(set(reviewed)) or set(reviewed)!=set(ids):
            raise SessionFailure('invalid_input','reviewed_ids 须精确覆盖本批全部编号；未读部分可用 observation 分页')
        for item in batch['items']:
            if digest(self.visible_event(scope,item['id']).payload)!=item['content_hash']:
                raise SessionFailure('source_removed','批次正文已变化；请重新读取')
        # Recompute the first eligible segment to reject forged or skipped coverage.
        fresh = self.reflection_batch(scope,root,len(ids))
        if [i['id'] for i in fresh['items']]!=ids or fresh.get('through_seq')!=batch['through_seq']:
            raise SessionFailure('stale_context','批次覆盖已变化；请重新读取')
        note = {}
        if p['outcome']=='summarized':
            source_ids,_ = self.evidence(scope,proposal.get('sources',[]))
            if not set(source_ids) & set(ids):
                raise SessionFailure('invalid_input','小结需要本批实际材料作为依据')
            note = self.write_note(scope,proposal,call_key,batch=True)
        elif proposal.get('text'):
            raise SessionFailure('invalid_input','no_new_insight 不创建记忆，请省略 text')
        cursor.update(version=cursor['version']+1,through_seq=batch['through_seq'])
        self.repo._exec('UPDATE meta SET v=? WHERE k=?',(canonical(cursor),'continuity:reflection:'+scope.scope_id))
        return dict(note,outcome=p['outcome'],cursor_version=cursor['version'],through_seq=cursor['through_seq'])

    def activities(self, scope, limit=5, offset=0, *, ref=None, query=''):
        items = []
        filters, parameters = (' AND id=?',(ref,)) if ref else ('',())
        op_filter = ' AND instr(lower(tool||\' \'||arguments),lower(?))>0' if query else ''
        job_filter = ' AND instr(lower(payload),lower(?))>0' if query else ''
        query_parameters = (query,) if query else ()
        selected = self.repo._all("SELECT id,origin FROM (SELECT id,created,'operation' AS origin FROM role_operations "
            "WHERE scope_id=?"+filters+op_filter+" UNION ALL SELECT id,created,'media' AS origin FROM work_jobs "
            "WHERE scope_id=? AND kind='local_media' AND payload!='{}'"+filters+job_filter+
            ') ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',
            (scope.scope_id,*parameters,*query_parameters,scope.scope_id,*parameters,*query_parameters,limit,offset))
        if ref and not selected:
            raise SessionFailure('scope_mismatch','活动引用不可见；请读取当前受众的 activities')
        operation_ids = [r['id'] for r in selected if r['origin']=='operation']
        media_ids = [r['id'] for r in selected if r['origin']=='media']
        rows = self.repo._all('SELECT * FROM role_operations WHERE scope_id=? AND id IN ('+
            ','.join('?' for _ in operation_ids)+')',(scope.scope_id,*operation_ids)) if operation_ids else []
        for row in rows:
            phase = {'dispatched':'running','succeeded':'result_available','failed':'failed_confirmed',
                     'cancelled':'cancelled_confirmed','unknown':'outcome_unknown'}.get(row['status'],'outcome_unknown')
            results = self.repo._all("SELECT id,payload,received_at FROM events WHERE agent_id=? AND conversation_id=? "
                "AND audience_policy_id=? AND redacted_at IS NULL AND json_extract(payload,'$.operation_id')=? "
                'ORDER BY ingest_seq DESC LIMIT 3',(*scope.private_key,scope.audience_policy_id,row['id']))
            items.append(dict(ref=row['id'],intent_ref=row['intent_ref'],phase=phase,tool=row['tool'],
                started_at=row['created'],result_at=results[0]['received_at'] if results else None,
                summary='工具回执不代表业务目标完成',result_refs=[r['id'] for r in results],asset_refs=[],
                coverage='partial' if any(json.loads(r['payload']).get('truncated') for r in results) else 'reported',
                goal_verified=False,delivery='not_requested'))
            metadata = self.role.initiative.get('initiative:operation:'+row['id'])
            if metadata and metadata.get('coverage')=='partial':
                items[-1]['coverage']='partial'
        jobs = self.repo._all('SELECT * FROM work_jobs WHERE scope_id=? AND id IN ('+
            ','.join('?' for _ in media_ids)+')',(scope.scope_id,*media_ids)) if media_ids else []
        for row in jobs:
            payload, checkpoint = json.loads(row['payload']),json.loads(row['checkpoint'])
            try:
                self.evidence(scope,payload.get('sources',[]))
            except SessionFailure:
                continue
            items.append(dict(ref=row['id'],intent_ref=payload.get('intent_ref'),
                phase={'pending':'queued','waiting':'queued','running':'running','done':'result_available',
                       'failed':'failed_confirmed','cancelled':'cancelled_confirmed'}.get(row['status'],'outcome_unknown'),
                tool='life_media',started_at=row['created'],result_at=row['updated'] if row['status'] not in {'pending','running'} else None,
                summary='媒体资产生成与检查、目标完成和送达分别核对',
                result_refs=[checkpoint['event_id']] if checkpoint.get('event_id') else [],
                asset_refs=[checkpoint['asset_id']] if checkpoint.get('asset_id') else [],
                coverage='reported',goal_verified=False,delivery='not_requested'))
        for item in items:
            row = self.repo._one("SELECT v FROM meta WHERE k LIKE 'continuity:expression:%' "
                "AND json_extract(v,'$.scope_id')=? AND EXISTS(SELECT 1 FROM json_each(v,'$.refs') WHERE value=?) LIMIT 1",
                (scope.scope_id,item['ref']))
            if row:
                item['delivery'] = self.delivery(scope,json.loads(row['v'])['logical_message_id'])
        order = {r['id']:i for i,r in enumerate(selected)}
        return sorted(items,key=lambda i:order[i['ref']])

    def delivery(self, scope, logical):
        records = self.life.delivery.describe(scope,logical)
        states = {r['status'] for r in records}
        if not records:
            return 'not_requested'
        if states=={'confirmed'}:
            return 'confirmed'
        if 'confirmed' in states:
            return 'partial_unknown' if states & {'unknown','dispatching'} else 'partial'
        return 'outcome_unknown' if states & {'unknown','dispatching'} else 'not_confirmed'

    def associate(self, scope, refs, logical=None):
        if not isinstance(refs,list) or len(refs)>5 or any(not isinstance(r,str) for r in refs):
            raise SessionFailure('invalid_input','continuity_refs 最多 5 个当前受众引用')
        for ref in refs:
            if self.repo._one('SELECT 1 FROM memories WHERE id=?',(ref,)):
                note = self.memory(scope,ref,active=True)
                if not set(note.topic_tags) & {'continuity:thought','continuity:reflection'}:
                    raise SessionFailure('invalid_input','continuity_refs 关联念想、小结或真实活动；普通事实请使用 sources')
            elif not (self.repo._one('SELECT 1 FROM role_operations WHERE id=? AND scope_id=?',(ref,scope.scope_id)) or
                      self.repo._one('SELECT 1 FROM work_jobs WHERE id=? AND scope_id=? AND payload!=\'{}\'',(ref,scope.scope_id))):
                raise SessionFailure('scope_mismatch','表达引用不可见；请读取 continuity 当前材料')
        if logical is None:
            return
        self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('continuity:expression:'+logical,
            canonical(dict(schema=1,scope_id=scope.scope_id,forget_epoch=self.repo.forget_epoch(scope.scope_id),
                           logical_message_id=logical,refs=list(dict.fromkeys(refs))))))

    def read(self, scope, p, root):
        view = p.get('view','overview')
        limit = p.get('limit',12 if view=='reflection_batch' else 5)
        if type(limit) is not int or not 1<=limit<=20:
            raise SessionFailure('invalid_input','limit 需要为 1～20 的整数')
        offset = p.get('cursor',0)
        if type(offset) is not int or offset<0:
            raise SessionFailure('invalid_input','cursor 需要为非负整数')
        query, ref = p.get('query',''), p.get('ref')
        if not isinstance(query,str) or len(query)>400 or (ref is not None and (not isinstance(ref,str) or not ref)):
            raise SessionFailure('invalid_input','query 最多 400 字符，ref 需要非空引用字符串')
        if (query or ref) and view not in {'thoughts','activities'}:
            raise SessionFailure('invalid_input','query/ref 仅用于 thoughts 或 activities')
        if ref and (query or offset):
            raise SessionFailure('invalid_input','按 ref 读取时请省略 query/cursor')
        if view=='reflection_batch':
            result = self.reflection_batch(scope,root,limit)
            # Preserve all offered ids/hashes even when previews need shrinking.
            def over_budget():
                return (estimate_tokens(result)>self.role.resources.text_result_max_tokens or
                        len(canonical(result).encode())>self.role.resources.text_result_max_utf8_bytes)
            while over_budget() and any(i['preview'] for i in result['items']):
                for item in result['items']:
                    item.update(preview=item['preview'][:len(item['preview'])//2],truncated=True,coverage='partial')
            while over_budget() and len(result['items'])>1:
                result['items'].pop()
                result['has_more']=True
            if result['items']:
                result['through_seq']=result['items'][-1]['seq']
                result.pop('batch_ref',None)
                result['batch_ref']='reflection-batch:'+digest(result)
            if over_budget():
                raise SessionFailure('invalid_input','当前结果预算不足以保留一个完整材料引用；请调整读取预算')
            return result
        if view=='thoughts':
            result = dict(items=self.notes(scope,query=query,ref=ref,limit=limit,offset=offset))
        elif view=='activities':
            result = dict(items=self.activities(scope,limit,offset,ref=ref,query=query))
        elif view=='overview':
            result = dict(thoughts=self.notes(scope,limit=limit),activities=self.activities(scope,limit),
                interests=[t for t in self.role.list_intents(scope) if t.get('kind')=='ongoing'][:limit],
                reflection_cursor=self.activate(scope))
            result['retry_guards'] = self.role.initiative.guards(scope)
        else:
            raise SessionFailure('invalid_input','continuity view 为 overview/thoughts/activities/reflection_batch')
        result['next_cursor'] = offset+limit
        while estimate_tokens(result)>self.role.resources.text_result_max_tokens:
            groups = [v for v in result.values() if isinstance(v,list) and v]
            if not groups:
                break
            group = max(groups,key=len)
            group.pop()
            if group is result.get('items'):
                result['next_cursor']-=1
            result['truncated'] = True
        return result

    def context(self, scope, binding, required):
        policy = self.role.resources
        total = min(policy.memory_context_target_tokens,max(0,policy.dynamic_context_target_tokens-estimate_tokens(required)))
        thoughts, activities = self.notes(scope,limit=5),self.activities(scope,5)
        interests = [t for t in self.role.list_intents(scope,5) if t.get('kind')=='ongoing' and t['status']=='active']
        turn = self.role.turn_number(binding['session_id'])
        current = self.repo._all('SELECT e.payload FROM agent_inputs i JOIN events e ON e.id=i.input_id '
            "WHERE i.session_id=? AND (i.claimed_turn IS ? OR i.handled=0) AND e.source_kind='user_report' "
            'AND e.redacted_at IS NULL ORDER BY i.ordinal DESC LIMIT 1',(binding['session_id'],turn))
        cues = []
        if current:
            cues.append(json.loads(current[0]['payload']).get('text','')[:400])
        if interests:
            cues.append(' '.join([interests[0].get('goal',''),*interests[0].get('interest',{}).get('cues',[])]))
        self_cues = [thoughts[0]['text']] if thoughts else []
        if activities:
            for ref in activities[0]['result_refs'][:1]:
                self_cues.append(self.visible_event(scope,ref).text[:400])
        if self_cues:
            cues.append(' '.join(self_cues)[:600])
        cues = list(dict.fromkeys(c for c in cues if c))[:3]
        previews = []
        groups = [[dict(kind='interest',ref=i['ref'],text=i.get('checkpoint') or i['goal']) for i in interests],
                  [dict(kind=n['type'],ref=n['ref'],text=n['text']) for n in thoughts],
                  [dict(kind='activity',ref=a['ref'],phase=a['phase'],text=a['summary']) for a in activities]]
        available = [group[index] for index in range(5) for group in groups if len(group)>index]
        for preview in available[:5]:
            item = dict(preview,text=preview['text'][:120])
            if estimate_tokens(previews+[item])<=min(total,policy.continuity_context_target_tokens):
                previews.append(item)
            else:
                reference = dict(kind=item['kind'],ref=item['ref'],read_kind='continuity')
                if estimate_tokens(previews+[reference])<=min(total,policy.continuity_context_target_tokens):
                    previews.append(reference)
            if len(previews)>=3:
                break
        used = estimate_tokens(previews) if previews else 0
        excluded = {p['ref'] for p in previews}
        packet = self.life.memory.retrieve_multi(scope,cues,budget=max(0,total-used),exclude=excluded)
        lines = packet.text.splitlines()
        while lines and estimate_tokens('\n'.join(lines))>max(0,total-used):
            lines.pop()
        memory = '\n'.join(lines)
        return dict(items=previews,read_kind='continuity',query_count=len(cues),
                    optional_estimated_tokens=used+(estimate_tokens(memory) if memory else 0)),memory
