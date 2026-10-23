"""Scoped associations and self reminders; annotations never rewrite evidence."""
from __future__ import annotations

import json
import math

from .contracts import now_ts
from .text import stable_id, terms, estimate_tokens, one_line

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_annotations(
 id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, kind TEXT NOT NULL,
 memory_id TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, cues TEXT NOT NULL,
 sources TEXT NOT NULL, created REAL NOT NULL, expires REAL, status TEXT NOT NULL DEFAULT 'active');
CREATE INDEX IF NOT EXISTS ix_memory_annotations_scope ON memory_annotations(scope_id,status,created);
CREATE TABLE IF NOT EXISTS annotation_terms(annotation_id TEXT NOT NULL, term TEXT NOT NULL, PRIMARY KEY(annotation_id,term));
CREATE INDEX IF NOT EXISTS ix_annotation_terms ON annotation_terms(term);
"""


class Recollection:
    def __init__(self, runtime):
        self.runtime, self.repo = runtime, runtime.repo

    def valid_memory(self, scope, memory_id):
        values = self.repo.memories_by_ids([memory_id])
        if not values:
            return None
        memory = values[0]
        if memory.scope.private_key != scope.private_key or memory.status == 'redacted':
            return None
        sources = self.repo.events_by_ids(s.event_id for s in memory.sources)
        return memory if sources and len(sources) == len({s.event_id for s in memory.sources}) and all(e.scope.private_key == scope.private_key and e.redacted_at is None for e in sources) else None

    def annotate(self, scope, kind, text, sources, *, memory_id='', cues=(), lifetime_days=0, always_load=False, skill_refs=()):
        if kind not in {'reminder', 'importance', 'interpretation'}:
            raise ValueError('annotation_kind_not_allowed')
        events = self.repo.events_by_ids(sources)
        if not events or len(events) != len(set(sources)) or any(e.scope.private_key != scope.private_key for e in events):
            raise ValueError('annotation_requires_current_scoped_sources')
        if memory_id and self.valid_memory(scope, memory_id) is None:
            raise ValueError('memory_not_visible')
        text = one_line(text, 400)
        if not text:
            raise ValueError('empty_reminder')
        days = float(lifetime_days)
        if not math.isfinite(days):
            raise ValueError('invalid_lifetime')
        days = max(0, min(3650, days))
        cues = list(dict.fromkeys(one_line(str(x), 80) for x in cues if str(x).strip()))[:8]
        identity = stable_id('annotation', scope.scope_id, kind, memory_id, text, str(self.repo.forget_epoch(scope.scope_id)))
        with self.repo.tx():
            current = self.repo.events_by_ids(sources)
            if len(current) != len(set(sources)) or any(e.redacted_at is not None or e.scope.private_key != scope.private_key for e in current):
                raise ValueError('annotation_source_removed')
            self.repo._exec('INSERT OR IGNORE INTO memory_annotations '
                '(id,scope_id,kind,memory_id,text,cues,sources,created,expires,always_load,skill_refs) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (identity, scope.scope_id, kind, memory_id, text, json.dumps(cues, ensure_ascii=False),
                 json.dumps(list(sources)), now_ts(), now_ts() + days * 86400 if days else None,
                 int(bool(always_load)), json.dumps([one_line(str(x), 100) for x in skill_refs][:8], ensure_ascii=False)))
            self.repo._exec('UPDATE memory_annotations SET always_load=?,expires=?,skill_refs=?,status=\'active\' WHERE id=? AND scope_id=?',
                (int(bool(always_load)), now_ts() + days * 86400 if days else None,
                 json.dumps([one_line(str(x), 100) for x in skill_refs][:8], ensure_ascii=False), identity, scope.scope_id))
            for term in set(terms(text + ' ' + ' '.join(cues))):
                self.repo._exec('INSERT OR IGNORE INTO annotation_terms VALUES(?,?)', (identity, term))
        return identity

    def annotations(self, scope, query='', limit=3, *, mandatory=False):
        q = set(terms(query)[:96])
        relevance = '0'
        args = []
        if q:
            relevance = '(SELECT COUNT(*) FROM annotation_terms t WHERE t.annotation_id=a.id AND t.term IN (' + ','.join('?' for _ in q) + '))'
            args.extend(q)
        rows = self.repo._all(f"SELECT a.*, {relevance} AS matches FROM memory_annotations a WHERE scope_id=? AND status='active' AND kind!='relationship_note' "
                              'AND (expires IS NULL OR expires>?) '+('AND always_load=1 ' if mandatory else '')+
                              'ORDER BY always_load DESC,matches DESC,created DESC'+('' if mandatory else ' LIMIT 200'),
                              (*args, scope.scope_id, now_ts()))
        valid = []
        for row in rows:
            sources = json.loads(row['sources'])
            events = self.repo.events_by_ids(sources)
            if not events or len(events) != len(set(sources)) or any(e.redacted_at is not None or e.scope.private_key != scope.private_key or e.scope.audience_policy_id!=scope.audience_policy_id for e in events):
                continue
            if row['memory_id'] and not self.valid_memory(scope, row['memory_id']):
                continue
            cues = json.loads(row['cues'])
            overlap = len(q & set(terms(' '.join(cues) + ' ' + row['text'])))
            # Explicit trigger reminders are dormant outside their context.
            if cues and not overlap and not row['always_load']:
                continue
            valid.append((overlap, dict(row)))
        return [r for _, r in sorted(valid, key=lambda x: (-x[1]['always_load'], -x[0], -x[1]['created']))[:limit]]

    def expansion(self, scope, query):
        """One-hop links from scoped reminders/importance; never a recursive flood."""
        ids = {r['memory_id'] for r in self.annotations(scope, query, 8) if r['memory_id']}
        return [m for i in ids if (m := self.valid_memory(scope, i)) is not None and m.status in {'active', 'disputed'}]

    def boosts(self, scope, query):
        return {r['memory_id']: 0.12 * 0.5 ** (max(0, now_ts() - r['created']) / (30 * 86400))
                for r in self.annotations(scope, query, 30) if r['kind'] == 'importance' and r['memory_id']}

    def block(self, scope, query, budget=240):
        lines = ['自我叮嘱、偏好与理解（按来源和适用范围使用；不能凭此新增授权或冒充外部事实）：']
        for row in self.annotations(scope, query, 40):
            skills = json.loads(row['skill_refs'])
            line = f"- [{row['id']} {row['kind']}] {row['text']} [依据:{','.join(json.loads(row['sources'])[:2])}]" + (' [方法:' + ','.join(skills) + ']' if skills else '')
            if estimate_tokens('\n'.join(lines + [line])) <= budget:
                lines.append(line)
        return '\n'.join(lines) if len(lines) > 1 else ''

    def receipt(self, scope, event_ids):
        ids = list(dict.fromkeys(event_ids))
        if not ids:
            return []
        marks = ','.join('?' for _ in ids)
        rows = self.repo._all('SELECT DISTINCT m.id,m.kind,m.summary FROM memories m JOIN memory_sources s ON s.memory_id=m.id '
            f"WHERE m.agent_id=? AND m.conversation_id=? AND m.status='active' AND s.event_id IN ({marks}) LIMIT 8",
            (*scope.private_key, *ids))
        return [dict(r) for r in rows if self.valid_memory(scope, r['id'])]

    def associate(self, scope, cue, trigger='thought'):
        packet = self.runtime.memory.retrieve(scope, cue, budget=400)
        ids = [m.id for m in packet.episodes + packet.facts + packet.knowledge][:3]
        if not ids:
            return []
        key = 'association:' + stable_id('cue', scope.scope_id, trigger, *ids)
        prior = self.repo._one('SELECT v FROM meta WHERE k=?', (key,))
        if prior and now_ts() - float(prior['v']) < 3600:
            return ids
        with self.repo.tx():
            self.repo._exec('INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)', (key, str(now_ts())))
            self.runtime.ingest(scope, 'memory_association', 'inference', one_line(cue, 100),
                                payload={'trigger': trigger, 'memory_ids': ids})
        return ids  # Remembering is not itself a reason to message anyone.
