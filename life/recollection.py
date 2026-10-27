"""Scoped associations and self reminders; annotations never rewrite evidence."""
from __future__ import annotations

import json
import math

from .contracts import now_ts
from .text import stable_id, terms, estimate_tokens, one_line
from .resource_policy import estimate_tokens as context_tokens
from .memory_policy import (topic_key as resolve_topic, MAX_REMINDERS, MAX_STANDING_REMINDERS,
                            STANDING_TOKEN_BUDGET, INFERENCE_DAYS, reset_boundary, source_deadline)
from .learning_evidence import annotation_view, annotation_preview, applicability_binding, binding_current, user_statements,validate_assets

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
        if self.repo.life_denied(scope,memory.id):
            return None
        if memory.scope.private_key != scope.private_key or memory.scope.audience_policy_id != scope.audience_policy_id or memory.status == 'redacted':
            return None
        sources = self.repo.events_by_ids(s.event_id for s in memory.sources)
        return memory if sources and len(sources) == len({s.event_id for s in memory.sources}) and all(e.scope.private_key == scope.private_key and e.redacted_at is None for e in sources) else None

    def annotate(self, scope, kind, text, sources, *, memory_id='', cues=(), lifetime_days=0, always_load=False, skill_refs=(), topic_key='', applicability='task', intent_ref=None,reference_asset_ids=()):
        if kind not in {'reminder', 'importance', 'interpretation'}:
            raise ValueError('annotation_kind_not_allowed')
        events = self.repo.events_by_ids(sources)
        if not events or len(events) != len(set(sources)) or any(e.scope.private_key != scope.private_key or e.scope.audience_policy_id != scope.audience_policy_id for e in events):
            raise ValueError('annotation_requires_current_scoped_sources')
        if memory_id and self.valid_memory(scope, memory_id) is None:
            raise ValueError('memory_not_visible')
        text = one_line(text, 400)
        if not text:
            raise ValueError('empty_reminder')
        key = resolve_topic(topic_key, text)
        if type(always_load) is not bool:
            raise ValueError('always_load_requires_boolean')
        explicit = all(e.source_kind == 'user_report' for e in events)
        if explicit and not any(e.received_at >= reset_boundary(self.repo,scope) for e in events):
            raise ValueError('cleared_reminder_requires_new_user_evidence')
        if always_load and not explicit:
            raise ValueError('standing_reminder_requires_direct_user_sources')
        if always_load and applicability!='ongoing':
            raise ValueError('standing_reminder_requires_ongoing_applicability')
        if applicability=='ongoing' and not explicit:
            raise ValueError('ongoing_reminder_requires_direct_user_sources')
        if explicit and len(user_statements(events))!=len(events):
            raise ValueError('user_preference_requires_original_text')
        application,reference,revision = applicability_binding(self.repo,scope,sources,applicability,intent_ref)
        assets=validate_assets(self.repo,scope,reference_asset_ids)
        days = float(lifetime_days)
        if not math.isfinite(days):
            raise ValueError('invalid_lifetime')
        days = max(0, min(3650, days))
        if not explicit or applicability!='ongoing':
            days = min(days or INFERENCE_DAYS, INFERENCE_DAYS)
        expires = now_ts()+days*86400 if days else None
        if not explicit:
            expires = source_deadline(self.repo,sources,expires,now_ts(),scope)
        cues = list(dict.fromkeys(one_line(str(x), 80) for x in cues if str(x).strip()))[:8]
        identity = stable_id('annotation', scope.scope_id, scope.audience_policy_id, kind, memory_id, text, str(self.repo.forget_epoch(scope.scope_id)))
        with self.repo.tx():
            current = self.repo.events_by_ids(sources)
            if len(current) != len(set(sources)) or any(e.redacted_at is not None or e.scope.private_key != scope.private_key for e in current):
                raise ValueError('annotation_source_removed')
            # Quote projection is also used for capacity accounting; a short
            # invented summary cannot conceal a long mandatory user message.
            projected_text = '\n'.join(q['text'] for q in user_statements(events)) if explicit else text
            expired = self.repo._all("SELECT id FROM memory_annotations WHERE scope_id=? AND kind<>'relationship_note' AND status='active' AND expires<=?",(scope.scope_id,now_ts()))
            for row in expired:
                self.repo._exec("UPDATE memory_annotations SET status='expired' WHERE id=?",(row['id'],))
                self.repo._exec('DELETE FROM annotation_terms WHERE annotation_id=?',(row['id'],))
            for row in self.repo._all("SELECT * FROM memory_annotations WHERE scope_id=? AND status='active' "
                    "AND applicability IN ('task','exchange')",(scope.scope_id,)):
                if not binding_current(self.repo,scope,row['applicability'],row['context_ref'],row['context_revision']):
                    self.repo._exec("UPDATE memory_annotations SET status='archived' WHERE id=?",(row['id'],))
                    self.repo._exec('DELETE FROM annotation_terms WHERE annotation_id=?',(row['id'],))
            prior = self.repo._all("SELECT * FROM memory_annotations a WHERE scope_id=? AND kind=? AND topic_key=? AND status='active' "
                                   "AND NOT EXISTS(SELECT 1 FROM json_each(a.sources) s JOIN events e ON e.id=s.value WHERE e.audience_policy_id<>?)",
                                   (scope.scope_id, kind, key,scope.audience_policy_id))
            for row in prior:
                if (row['id'] == identity and set(sources) <= set(json.loads(row['sources']))
                        and bool(row['always_load']) == always_load and json.loads(row['cues']) == cues
                        and json.loads(row['skill_refs']) == list(skill_refs)
                        and json.loads(row['reference_assets']) == assets
                        and (row['applicability'],row['context_ref'],row['context_revision'])==(application,reference,revision)):
                    return identity  # Replaying the same evidence cannot prolong a temporary rule.
            replaced = {r['id'] for r in prior}
            standing = [dict(r) for r in self.repo._all("SELECT * FROM memory_annotations WHERE scope_id=? AND status='active' AND always_load=1 AND (expires IS NULL OR expires>?)",
                                                      (scope.scope_id, now_ts())) if r['id'] not in replaced]
            candidate = dict(id=identity,kind=kind,text=projected_text,sources=list(sources),
                authority='user_statement' if explicit else 'model_interpretation',applicability=application)
            if assets:
                candidate['reference_asset_ids']=assets
            projected = [annotation_preview(view) for r in standing
                         if (view:=annotation_view(self.repo,scope,r,now_ts()))]
            if always_load and (len(standing) >= MAX_STANDING_REMINDERS or
                                context_tokens(json.dumps(projected+[candidate],ensure_ascii=False)) > STANDING_TOKEN_BUDGET):
                raise ValueError('standing_reminders_full: merge or replace an existing topic, or save on demand')
            count = self.repo._one("SELECT COUNT(*) AS n FROM memory_annotations WHERE scope_id=? AND kind=? AND status='active' AND (expires IS NULL OR expires>?)",
                                   (scope.scope_id,kind,now_ts()))['n']
            if not prior and count >= MAX_REMINDERS:
                raise ValueError('reminders_full: replace or retire an existing topic')
            for row in prior:
                if row['id'] != identity:
                    self.repo._exec("UPDATE memory_annotations SET status='superseded' WHERE id=?",(row['id'],))
                    self.repo._exec('DELETE FROM annotation_terms WHERE annotation_id=?',(row['id'],))
            self.repo._exec('INSERT OR IGNORE INTO memory_annotations '
                '(id,scope_id,kind,memory_id,text,cues,sources,created,expires,always_load,skill_refs,topic_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (identity, scope.scope_id, kind, memory_id, text, json.dumps(cues, ensure_ascii=False),
                 json.dumps(list(sources)), now_ts(), expires,
                 int(always_load), json.dumps([one_line(str(x), 100) for x in skill_refs][:8], ensure_ascii=False),key))
            self.repo._exec('UPDATE memory_annotations SET always_load=?,expires=?,skill_refs=?,topic_key=?,sources=?,cues=?,status=\'active\' WHERE id=? AND scope_id=?',
                (int(bool(always_load)), expires,
                 json.dumps([one_line(str(x), 100) for x in skill_refs][:8], ensure_ascii=False),key,
                 json.dumps(list(sources)),json.dumps(cues,ensure_ascii=False), identity, scope.scope_id))
            self.repo._exec('UPDATE memory_annotations SET applicability=?,context_ref=?,context_revision=?,reference_assets=? WHERE id=?',
                (application,reference,revision,json.dumps(assets),identity))
            self.repo._exec('DELETE FROM annotation_terms WHERE annotation_id=?',(identity,))
            for term in set(terms(text + ' ' + projected_text + ' ' + ' '.join(cues))):
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
            if any(self.repo.life_denied(scope,source) for source in sources):
                continue
            events = self.repo.events_by_ids(sources)
            if not events or len(events) != len(set(sources)) or any(e.redacted_at is not None or e.scope.private_key != scope.private_key or e.scope.audience_policy_id!=scope.audience_policy_id for e in events):
                continue
            if mandatory and any(e.source_kind != 'user_report' for e in events):
                continue
            if row['memory_id'] and not self.valid_memory(scope, row['memory_id']):
                continue
            cues = json.loads(row['cues'])
            overlap = len(q & set(terms(' '.join(cues) + ' ' + row['text'])))
            # Explicit trigger reminders are dormant outside their context.
            if cues and not overlap and not row['always_load']:
                continue
            view = annotation_view(self.repo,scope,row,now_ts())
            if view is not None:
                valid.append((overlap,view))
        selected = []
        for _, row in sorted(valid, key=lambda x: (-x[1]['always_load'], -x[0], -x[1]['created'])):
            if limit is not None and len(selected) >= limit:
                break
            if mandatory:
                preview = [annotation_preview(r) for r in selected+[row]]
                if len(selected) >= MAX_STANDING_REMINDERS or context_tokens(json.dumps(preview,ensure_ascii=False)) > STANDING_TOKEN_BUDGET:
                    continue
            selected.append(row)
        return selected

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
            line = f"- [{row['id']} {row['kind']} {row['authority']} 范围:{row.get('applicability','legacy')}] {row['text']} [依据:{','.join(json.loads(row['sources'])[:2])}]" + (' [方法:' + ','.join(skills) + ']' if skills else '')
            if row['reference_asset_ids']:
                line+=' [参考素材:'+','.join(row['reference_asset_ids'])+']'
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
        ctx=self.runtime.life_access.bind(scope)
        packet=self.runtime.life_recall.search(ctx,cue,budget=1200,limit=3)
        ids=[item['resource_ref'] for item in packet['items']]
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
