"""Scoped application read model; all persistent state belongs to the runtime."""
import json
import time


class RuntimeState:
    def __init__(self, runtime):
        self.runtime, self.repo, self.db = runtime, runtime.repo, runtime.repo.db

    def scope(self, peer):
        from .delivery import active_delivery
        ctx = active_delivery.get()
        return ctx.scope if ctx is not None and str(peer) == ctx.scope.participant_id else self.runtime.scope(f'qq:private:{peer}', str(peer))

    def get_kv(self, key):
        row = self.repo._one('SELECT v FROM meta WHERE k=?', ('state:' + key,))
        return row['v'] if row else ''

    def set_kv(self, key, value):
        with self.repo.tx():
            self.repo._exec('INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)', ('state:' + key, str(value)))
        if key.startswith('self:') and value:
            peer = int(key.split(':')[1])
            scope = self.scope(peer)
            event, _ = self.runtime.ingest(scope, 'reflection', 'inference', str(value))
            self.runtime.recollection.annotate(scope, 'interpretation', str(value), [event.id])

    def facts(self, peer, n=40):
        scope = self.scope(peer)
        return [m.summary for m in self.repo.recent_memories(scope, ['active','disputed'], limit=n)]

    def recent(self, peer, n, scope=None, allow_legacy=False):
        selected = self.runtime.scope(scope, str(peer)) if scope else self.scope(peer)
        rows = self.repo._all("SELECT kind,payload FROM events WHERE agent_id=? AND conversation_id=? "
            "AND redacted_at IS NULL AND kind IN ('message','delivered_segment') ORDER BY ingest_seq DESC LIMIT ?",
            (*selected.private_key, n))
        return [('user' if r['kind']=='message' else 'assistant', json.loads(r['payload']).get('text','')) for r in reversed(rows)]

    def last_msg_ts(self, peer):
        scope = self.scope(peer)
        row = self.repo._one("SELECT MAX(received_at) AS t FROM events WHERE agent_id=? AND conversation_id=? AND kind='message' AND redacted_at IS NULL", scope.private_key)
        return int(row['t'] or 0)

    def count(self, peer):
        return len(self.recent(peer, 100000))

    def recent_diary(self, peer, n=3):
        from datetime import datetime
        return [(datetime.fromtimestamp(m.recorded_at).date().isoformat(), m.summary)
                for m in self.repo.recent_memories(self.scope(peer), ['active'], limit=200)
                if m.kind == 'diary'][:n]

    def add_agenda(self, peer, item, kind='think', target=''):
        with self.repo.tx():
            return self._add_agenda(peer, item)

    def _add_agenda(self, peer, item):
        from .services.store import Store
        store = Store(self.repo.path, repo=self.repo)
        scope = self.scope(peer)
        from .delivery import active_delivery
        ctx = active_delivery.get()
        event, _ = self.runtime.ingest(scope, 'goal_recorded', 'inference', item,
                                      source_event_ids=list(ctx.sources) if ctx else [])
        iid = store.add_intent(peer, item, '记录的待办', 'commanded', expected=item, energy=.5,
                                scope_id=scope.scope_id, source_event_ids=[event.id], grounded=True)
        from .tasks import TaskService
        TaskService(self.repo).ensure(scope, 'intention_'+str(iid), item, intent_id=iid)
        return iid

    def all_agenda(self, peer, limit=20):
        from .services.store import Store
        rows = Store(self.repo.path, repo=self.repo).visible_intents(limit, scope_id=self.scope(peer).scope_id, peer=peer)
        return [dict(row, item=row['text'], kind=row['origin']) for row in rows]

    def agenda_todo(self, peer, limit=5):
        return [r for r in self.all_agenda(peer, 100) if r['status'] in ('seed','active')][:limit]

    def next_agenda(self, peer):
        return next(iter(self.agenda_todo(peer)), None)

    def get_agenda(self, aid):
        row = self.repo._one('SELECT * FROM intents WHERE id=?', (aid,))
        return dict(row, item=row['text']) if row else None

    def update_agenda(self, aid, item):
        with self.repo.tx():
            self.repo._exec('UPDATE intents SET text=?,updated_at=? WHERE id=?', (item, time.time(), aid))

    def finish_agenda(self, aid):
        self._agenda_transition(aid,'done')

    def drop_agenda(self, aid):
        self._agenda_transition(aid,'cancelled')

    def _agenda_transition(self, aid, status):
        from .tasks import TaskService
        row = self.get_agenda(aid)
        if row:
            scope = self.scope(row['peer'])
            tasks = TaskService(self.repo)
            tasks.ensure(scope,'intention_'+str(aid),row['text'],intent_id=aid)
            tasks.transition(scope.scope_id,'intention_'+str(aid),status,cause='explicit_agenda_update')

    def get_bond(self, peer):
        return self.runtime.relationship(self.scope(peer)).familiarity * 100

    def close(self):
        pass
