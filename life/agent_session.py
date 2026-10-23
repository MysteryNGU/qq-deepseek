"""Durable DSH conversation bindings, input receipts and contiguous event application."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid

from .contracts import Scope


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class SessionFailure(RuntimeError):
    def __init__(self, code, detail='', **data):
        super().__init__(detail or code)
        self.code, self.data = code, data

    def receipt(self):
        return dict(ok=False, code=self.code, message=str(self), **self.data)


SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_sessions(
 session_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, scope_json TEXT NOT NULL,
 generation INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'current',
 parent_session_id TEXT, intent_ref TEXT, intent_revision INTEGER,
 cursor INTEGER NOT NULL DEFAULT -1, forget_epoch INTEGER NOT NULL,
 created REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_role_session ON agent_sessions(scope_id)
 WHERE status='current' AND parent_session_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS role_generation ON agent_sessions(scope_id,generation)
 WHERE parent_session_id IS NULL;
CREATE TABLE IF NOT EXISTS agent_inputs(
 session_id TEXT NOT NULL, input_id TEXT NOT NULL, digest TEXT NOT NULL,
 payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', message_id TEXT,
 claimed_turn INTEGER, claimed_step INTEGER, handled INTEGER NOT NULL DEFAULT 0,
 ordinal INTEGER NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(session_id,input_id));
CREATE TABLE IF NOT EXISTS agent_events(
 session_id TEXT NOT NULL, seq INTEGER NOT NULL, digest TEXT NOT NULL,
 payload TEXT NOT NULL, applied REAL NOT NULL, PRIMARY KEY(session_id,seq));
CREATE TABLE IF NOT EXISTS life_calls(
 session_id TEXT NOT NULL, call_id TEXT NOT NULL, digest TEXT NOT NULL,
 receipt TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(session_id,call_id));
CREATE TABLE IF NOT EXISTS input_resolutions(
 session_id TEXT NOT NULL,call_id TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL,
 PRIMARY KEY(session_id,call_id));
CREATE TABLE IF NOT EXISTS role_wakes(
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, intent_ref TEXT,
 reason TEXT NOT NULL, due REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS role_operations(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, scope_id TEXT NOT NULL,
 intent_ref TEXT, intent_revision INTEGER, tool TEXT NOT NULL,
 request_hash TEXT NOT NULL, arguments TEXT NOT NULL, status TEXT NOT NULL,
 cancel_requested INTEGER NOT NULL DEFAULT 0, result TEXT, created REAL NOT NULL);
"""


class AgentSessions:
    def __init__(self, runtime, bridge=None):
        self.runtime, self.repo, self.bridge = runtime, runtime.repo, bridge
        self.repo.db.executescript(SCHEMA)
        self._locks = {}

    def binding(self, scope):
        with self.repo.tx():
            row = self.repo._one("SELECT * FROM agent_sessions WHERE scope_id=? "
                "AND status='current' AND parent_session_id IS NULL", (scope.scope_id,))
            if row:
                return dict(row)
            generation = self.repo._one('SELECT COALESCE(MAX(generation),0)+1 AS n '
                'FROM agent_sessions WHERE scope_id=?', (scope.scope_id,))['n']
            sid = 'role-' + uuid.uuid4().hex
            self.repo._exec('INSERT INTO agent_sessions(session_id,scope_id,scope_json,generation,'
                'forget_epoch,created) VALUES(?,?,?,?,?,?)',
                (sid, scope.scope_id, canonical(scope.__dict__), generation,
                 self.repo.forget_epoch(scope.scope_id), time.time()))
            return dict(self.repo._one('SELECT * FROM agent_sessions WHERE session_id=?', (sid,)))

    def require(self, session_id, *, current=True):
        row = self.repo._one('SELECT * FROM agent_sessions WHERE session_id=?', (session_id,))
        if not row or (current and row['status'] not in {'current', 'delegate'}):
            raise SessionFailure('stale_context', '会话已经退役或不属于此运行时')
        if current and row['forget_epoch'] != self.repo.forget_epoch(row['scope_id']):
            raise SessionFailure('stale_context', '会话包含已遗忘的上下文')
        return dict(row)

    def enqueue(self, binding, event, *, mode='steer',content_blocks=None):
        sid = binding['session_id']
        payload = dict(session_id=sid, input_id=event.id,
            generation=binding['generation'], source=event.source_kind,
            content=event.text, source_event_ids=[event.id], mode=mode)
        if content_blocks is not None:
            payload['content_blocks'] = content_blocks
        elif event.payload.get('parts'):
            from .media import visible_parts
            payload['content'] = canonical(dict(event_id=event.id,actor_id=event.actor_id,actor=event.payload.get('actor'),
                account_id=event.payload.get('account_id'),
                parts=visible_parts(event.payload['parts']),source_kind=event.source_kind))
        if not payload['content']:
            payload['content'] = canonical(dict(event_id=event.id,kind=event.kind,payload=event.payload))
        checksum = digest(payload)
        with self.repo.tx():
            self.require(sid)
            prior = self.repo._one('SELECT * FROM agent_inputs WHERE session_id=? AND input_id=?',
                                   (sid, event.id))
            if prior:
                if prior['digest'] != checksum:
                    raise SessionFailure('idempotency_conflict', '输入编号对应不同内容')
                return dict(prior)
            ordinal = self.repo._one('SELECT COALESCE(MAX(ordinal),0)+1 AS n FROM agent_inputs '
                'WHERE session_id=?', (sid,))['n']
            self.repo._exec('INSERT INTO agent_inputs(session_id,input_id,digest,payload,ordinal,created) '
                'VALUES(?,?,?,?,?,?)', (sid, event.id, checksum, canonical(payload), ordinal, time.time()))
            self.runtime.delivery.new_input(event.scope,event.id,[event.id])
            return dict(self.repo._one('SELECT * FROM agent_inputs WHERE session_id=? AND input_id=?',
                                      (sid, event.id)))

    async def deliver(self, item,*,reconcile=False):
        sid = item['session_id']
        async with self._locks.setdefault(sid, asyncio.Lock()):
            self.require(sid)
            current = self.repo._one('SELECT * FROM agent_inputs WHERE session_id=? AND input_id=?',
                                     (sid, item['input_id']))
            if current['state'] != 'pending' and not (reconcile and current['state']=='accepted'):
                return dict(current)
            payload = json.loads(current['payload'])
            try:
                if reconcile:
                    receipt = await asyncio.to_thread(self.bridge.input_status,sid,item['input_id'])
                    if receipt.get('state')=='not_found':
                        if current['state']=='accepted':
                            raise SessionFailure('history_gap','已确认接收的输入在 DSH 中丢失')
                        receipt = await asyncio.to_thread(self.bridge.session_input,payload)
                else:
                    receipt = await asyncio.to_thread(self.bridge.session_input, payload)
            except SessionFailure:
                raise
            except Exception as original:
                try:
                    receipt = await asyncio.to_thread(self.bridge.input_status, sid, item['input_id'])
                    if receipt.get('state') == 'not_found':
                        if current['state']=='accepted':
                            raise SessionFailure('history_gap','已确认接收的输入在 DSH 中丢失')
                        if getattr(original,'rpc_code',None) is not None:
                            raise SessionFailure('input_rejected',str(original),input_id=item['input_id'],
                                                 rpc_code=original.rpc_code) from original
                        receipt = await asyncio.to_thread(self.bridge.session_input, payload)
                except SessionFailure:
                    raise
                except Exception:
                    raise SessionFailure('outcome_unknown', '输入已保留，等待核对原投递结果',
                                         input_id=item['input_id']) from original
            if receipt.get('code'):
                code = 'input_rejected' if receipt['code']=='invalid_input' else receipt['code']
                raise SessionFailure(code, receipt.get('message', ''))
            with self.repo.tx():
                self.require(sid)
                # A live consumed notification may have arrived before the ACK.
                self.repo._exec("UPDATE agent_inputs SET state=CASE WHEN state='pending' THEN 'accepted' "
                    "ELSE state END,message_id=? WHERE session_id=? AND input_id=?",
                    (receipt['message_id'], sid, item['input_id']))
                if receipt.get('state') in {'consumed', 'settled'}:
                    self.repo._exec('UPDATE agent_inputs SET state=?,claimed_turn=?,claimed_step=? '
                        'WHERE session_id=? AND input_id=?',
                        (receipt['state'], receipt.get('turn'), receipt.get('step'), sid, item['input_id']))
            return receipt

    def pending(self, session_id):
        return [dict(r) for r in self.repo._all('SELECT * FROM agent_inputs WHERE session_id=? '
            "AND handled=0 AND state!='rejected' ORDER BY ordinal", (session_id,))]

    def apply_event(self, sid, event):
        checksum, seq = digest(event), int(event['seq'])
        with self.repo.tx():
            binding = self.require(sid)
            prior = self.repo._one('SELECT digest FROM agent_events WHERE session_id=? AND seq=?', (sid, seq))
            if prior:
                if prior['digest'] != checksum:
                    raise SessionFailure('idempotency_conflict', '会话事件编号对应不同内容')
                return False
            if seq != binding['cursor'] + 1:
                raise SessionFailure('history_gap', '会话事件不连续', after_seq=binding['cursor'])
            self.repo._exec('INSERT INTO agent_events VALUES(?,?,?,?,?)',
                (sid, seq, checksum, canonical(event), time.time()))
            kind, data = event['type'], event.get('data', {})
            if kind == 'user/message':
                message = data.get('message', data)
                start = self.repo._one("SELECT payload FROM agent_events WHERE session_id=? "
                    "AND json_extract(payload,'$.type')='step/start' ORDER BY seq DESC LIMIT 1", (sid,))
                step = json.loads(start['payload'])['data'] if start else {}
                self.repo._exec("UPDATE agent_inputs SET state='consumed',claimed_turn=?,claimed_step=? "
                    'WHERE session_id=? AND message_id=?',
                    (step.get('turn'), step.get('step'), sid, message.get('id')))
            elif kind == 'turn/end':
                self.repo._exec("UPDATE agent_inputs SET state='settled' WHERE session_id=? "
                    "AND claimed_turn=? AND state='consumed'", (sid, data.get('turn')))
            self.repo._exec('UPDATE agent_sessions SET cursor=? WHERE session_id=?', (seq, sid))
        return True

    def resolve(self, sid, resolution):
        """Release only input holds that this model request actually consumed."""
        ids = resolution.get('handled_input_ids', [])
        if not isinstance(ids, list):
            raise SessionFailure('invalid_input', 'handled_input_ids 必须是数组')
        with self.repo.tx():
            self.require(sid)
            for iid in ids:
                row = self.repo._one('SELECT state FROM agent_inputs WHERE session_id=? AND input_id=?', (sid, iid))
                if not row or row['state'] not in {'consumed', 'settled'}:
                    raise SessionFailure('input_pending', '不能声称处理尚未进入模型的输入', input_id=iid)
            for iid in ids:
                self.repo._exec('UPDATE agent_inputs SET handled=1 WHERE session_id=? AND input_id=?', (sid, iid))
            return dict(handled=ids, pending=[i['input_id'] for i in self.pending(sid)])

    async def replay(self, sid):
        while True:
            binding = self.require(sid)
            page = await asyncio.to_thread(self.bridge.session_events, sid, binding['cursor'],generation=binding['generation'])
            if page.get('code'):
                raise SessionFailure(page['code'], page.get('message', ''))
            for event in page['events']:
                self.apply_event(sid, event)
            if not page.get('has_more'):
                return
            if self.require(sid)['cursor']<=binding['cursor']:
                raise SessionFailure('history_gap','事件分页未能推进连续游标')

    def retire(self, scope):
        with self.repo.tx():
            self.repo._exec("UPDATE agent_sessions SET status='retired' WHERE scope_id=? "
                            "AND status IN ('current','delegate')", (scope.scope_id,))
            self.repo._exec("UPDATE role_wakes SET state='cancelled' WHERE scope_id=?", (scope.scope_id,))

