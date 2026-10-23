"""One decision record, shared by conversation and tool execution."""
import json
import time
import uuid


def trace_record(row, *, encoded=False):
    value = json.loads(row['behavior_plan'] or '{}').get('trace')
    if value is None:
        return None
    result = dict(value, id=row['id'], ts=row['created_at'], session_id=row['turn_id'],
                  outcome=row['outcome'])
    for key in ('thoughts', 'options', 'tool_calls', 'evidence'):
        result.setdefault(key, [])
        if encoded:
            result[key] = json.dumps(result[key], ensure_ascii=False)
    return result


class AuditStore:
    def add_trace(self, peer, intent_id, steps, ok, ms, cost=0.0):
        from .services.ctx import current_trace
        tid = current_trace.get()
        if not tid or not self.get_decision_trace(tid):
            tid = self.start_decision_trace(peer, goal='tool execution')
            self.update_decision_trace(tid, tool_calls=steps)
        self.update_decision_trace(tid, latency_ms=ms, cost=cost)
        return tid

    def get_trace(self, tid):
        row = self.get_decision_trace(tid)
        return dict(row,steps=row['tool_calls'],ms=row.get('latency_ms',0),ok=bool(row.get('outcome'))) if row else None

    def traces_since(self, ts):
        return [dict(ts=r['created_at'],ok=bool(r['outcome']),ms=r['duration_ms'])
                for r in self.repo._all('SELECT * FROM decision_records WHERE created_at>=?',(ts,))]

    def start_decision_trace(self, peer, session_id='', origin='commanded', trigger='',
                             goal='', why='', expected='', consent_class='free', consent_reason=''):
        tid = 'trace:' + (session_id or uuid.uuid4().hex)
        with self.repo.tx():
            if self.repo._one('SELECT id FROM decision_records WHERE id=?', (tid,)):
                return tid
            turn = self.repo.get_turn(session_id) if session_id else None
            trace = dict(peer=peer, origin=origin, trigger=trigger, goal=goal, why=why,
                         expected=expected, consent_class=consent_class, consent_reason=consent_reason,
                         thoughts=[], options=[], tool_calls=[], evidence=[], lesson='', latency_ms=0, cost=0)
            self.repo.save_decision(dict(id=tid, turn_id=session_id,
                agent_id=turn.scope.agent_id if turn else 'default',
                conversation_id=turn.scope.conversation_id if turn else f'qq:private:{peer}',
                input_event_ids=turn.input_event_ids if turn else [], behavior_plan={'trace': trace}))
        return tid

    def update_decision_trace(self, tid, **fields):
        with self.repo.tx():
            row = self.repo._one('SELECT * FROM decision_records WHERE id=?', (tid,))
            if row is None:
                return
            body = json.loads(row['behavior_plan'])
            trace = body.setdefault('trace', {})
            trace.update(fields)
            self.repo._exec('UPDATE decision_records SET behavior_plan=?,outcome=?,duration_ms=? WHERE id=?',
                (json.dumps(body, ensure_ascii=False), fields.get('outcome', row['outcome']),
                 int(fields.get('latency_ms', row['duration_ms'])), tid))

    def get_decision_trace(self, tid):
        row = self.repo._one('SELECT * FROM decision_records WHERE id=?', (tid,))
        return trace_record(row) if row else None

    def recent_decision_traces(self, peer=None, n=10):
        rows = self.repo._all("SELECT * FROM decision_records WHERE json_type(behavior_plan,'$.trace')='object' "
            "AND (? IS NULL OR json_extract(behavior_plan,'$.trace.peer')=?) ORDER BY created_at DESC LIMIT ?",
            (peer, peer, n))
        return [trace_record(row) for row in rows]
