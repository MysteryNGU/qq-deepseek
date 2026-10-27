"""Metadata-only catalog over existing events and memories; no second archive."""
from __future__ import annotations

import hashlib
import json

from .contracts import now_ts
from .text import stable_id, terms
from .life_access import RecallError


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class LifeCatalog:
    def __init__(self, repo, agent_id, clock=now_ts):
        self.repo, self.agent_id, self.clock = repo, agent_id, clock

    def epoch(self, domain):
        row = self.repo._one('SELECT epoch FROM life_epochs WHERE agent_id=? AND domain=?', (self.agent_id, domain))
        return row['epoch'] if row else 0

    def bump(self, domain='catalog'):
        self.repo._exec('INSERT INTO life_epochs VALUES(?,?,1) ON CONFLICT(agent_id,domain) '
                        'DO UPDATE SET epoch=epoch+1', (self.agent_id, domain))
        return self.epoch(domain)

    def fence(self):
        epochs = [tuple(r) for r in self.repo._all('SELECT scope_id,epoch FROM forget_epochs WHERE '
                   'substr(scope_id,1,?)=? ORDER BY scope_id', (len(self.agent_id)+1, self.agent_id+'|'))]
        return sha(encode([self.epoch('catalog'), self.epoch('policy'), epochs]))

    def ref(self, kind, native_id):
        return stable_id('life', self.agent_id, kind, native_id)

    def resolve(self, ref):
        row = self.repo._one('SELECT * FROM life_resources WHERE resource_id=? AND agent_id=?', (ref, self.agent_id))
        if row:
            return dict(row)
        for kind, getter in [('event', self.repo.get_event), ('memory', self.repo.get_memory)]:
            item = getter(str(ref))
            if item and item.scope.agent_id == self.agent_id:
                return self.register(kind, item.id)
        return None

    def _native(self, kind, identity):
        return self.repo.get_event(identity) if kind == 'event' else self.repo.get_memory(identity)

    def _describe(self, kind, item):
        if kind == 'event':
            # Keep payload fields out unless they are part of the canonical text.
            header = dict(actor=item.actor_id, occurred_at=item.occurred_at,
                          observed_at=item.received_at, source_kind=item.source_kind, role=item.role)
            header.update(kind=item.kind, **{key:item.payload[key] for key in
                ('status','coverage','meaning_kind','attachments','operation_id','job_id','message_id',
                 'confidential','sensitivity','public_source') if key in item.payload})
            body = encode(header) + '\n' + item.text
            status = 'deleted' if item.redacted_at is not None else 'active'
            refs = list(item.source_event_ids)
            parents = [('event', ref) for ref in refs]
            return body, status, None, item.occurred_at, item.actor_id, parents
        status = {'redacted': 'deleted', 'archived': 'superseded', 'candidate': 'candidate'}.get(item.status, item.status)
        header = dict(source_kind=item.source_kind, kind=item.kind, status=item.status,
                      occurred_at=item.valid_from or item.recorded_at)
        attribution=self.repo._one('SELECT v FROM meta WHERE k=?',('memory_share:'+item.id,))
        if attribution:
            header['attribution']=json.loads(attribution['v'])
        text=item.summary
        if item.kind=='fact' and item.source_kind=='user_report':
            from .learning_evidence import user_statements
            text='\n'.join(q['text'] for q in user_statements(self.repo.events_by_ids(s.event_id for s in item.sources)))
        body = encode(header) + '\n' + text
        # A replacement carries its own root evidence. The superseded record
        # is revision history, not a dependency on that obsolete interpretation.
        parents = [('event', s.event_id) for s in item.sources] + [
            ('memory', ref) for ref in item.derived_from if ref != item.supersedes_id]
        return body, status, item.valid_until, item.valid_from or item.recorded_at, item.subject_id, parents

    def register(self, kind, identity, _visiting=None):
        if kind not in {'event', 'memory'}:
            raise RecallError('not_available')
        visiting = set(_visiting or ())
        ref = self.ref(kind, identity)
        if ref in visiting:
            raise RecallError('source_cycle')
        visiting.add(ref)
        with self.repo.tx():
            item = self._native(kind, identity)
            if not item or item.scope.agent_id != self.agent_id:
                raise RecallError('not_available')
            body, status, expiry, occurred, actor, parents = self._describe(kind, item)
            origin = item.scope.scope_id + '|' + item.scope.audience_policy_id
            fingerprint = sha(encode([body, status, expiry, origin, parents]))
            old = self.repo._one('SELECT * FROM life_resources WHERE resource_id=?', (ref,))
            if old and old['content_digest'] == fingerprint:
                return dict(old)
            version = int(old['source_version']) + 1 if old else 1
            if old:
                # Never revive an explicitly forgotten identifier.
                if old['status'] == 'deleted':
                    status = 'deleted'
                self.invalidate([ref], deleted=status == 'deleted', include_roots=False)
            self.repo._exec('INSERT INTO life_resources VALUES(?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(resource_id) DO UPDATE SET origin_scope=excluded.origin_scope,actor_key=excluded.actor_key,'
                'occurred_at=excluded.occurred_at,updated_at=excluded.updated_at,source_version=excluded.source_version,'
                'content_digest=excluded.content_digest,status=excluded.status,expires_at=excluded.expires_at',
                (ref, self.agent_id, kind, identity, origin, actor, occurred, self.clock(), version, fingerprint, status, expiry))
            self.repo._exec('DELETE FROM life_source_edges WHERE child_id=?', (ref,))
            for parent_kind, native_id in parents:
                parent = self.register(parent_kind, native_id, visiting)
                self.add_edge(ref, parent['resource_id'], parent['source_version'])
            self.repo._exec('DELETE FROM life_terms WHERE resource_id=?', (ref,))
            if status in {'active', 'disputed'}:
                # Index only body, never metadata dates or hidden payload JSON.
                text = body.split('\n',1)[1]
                for term in terms(text):
                    self.repo._exec('INSERT INTO life_terms VALUES(?,?,?,?,?)', (self.agent_id, term, ref, version, 1.0))
            if old:
                self.bump()
            return dict(self.repo._one('SELECT * FROM life_resources WHERE resource_id=?', (ref,)))

    def add_edge(self, child_ref, parent_ref, parent_version, relation='derived'):
        child, parent = self.resolve(child_ref), self.resolve(parent_ref)
        if (not child or not parent or relation not in {'derived', 'published_from'}
                or parent['source_version'] != parent_version or child_ref == parent_ref):
            raise RecallError('stale_source')
        reachable = self.repo._one('WITH RECURSIVE ancestors(id) AS (SELECT parent_id FROM life_source_edges '
            'WHERE child_id=? UNION SELECT e.parent_id FROM life_source_edges e JOIN ancestors a ON e.child_id=a.id) '
            'SELECT 1 FROM ancestors WHERE id=?', (parent_ref, child_ref))
        if reachable:
            raise RecallError('source_cycle')
        self.repo._exec('INSERT INTO life_source_edges VALUES(?,?,?,?) ON CONFLICT(child_id,parent_id,relation) '
                        'DO UPDATE SET parent_version=excluded.parent_version',
                        (child_ref, parent_ref, parent_version, relation))

    def parents(self, ref):
        return self.repo._all('SELECT * FROM life_source_edges WHERE child_id=?', (ref,))

    def body(self, resource):
        item = self._native(resource['kind'], resource['native_id'])
        if not item or item.scope.agent_id != self.agent_id:
            raise RecallError('not_available')
        return self._describe(resource['kind'], item)[0]

    def live(self, resource, visiting=None, *, history=False):
        visiting = set(visiting or ())
        ref = resource['resource_id']
        statuses={'active','disputed','superseded','candidate'} if history else {'active','disputed'}
        if ref in visiting or resource['status'] not in statuses:
            return False
        if not history and resource['expires_at'] is not None and resource['expires_at'] <= self.clock():
            return False
        item = self._native(resource['kind'], resource['native_id'])
        if not item or item.scope.agent_id != self.agent_id:
            return False
        from .memory_policy import reset_boundary
        floor = reset_boundary(self.repo, item.scope)
        if not history and resource['kind']=='memory' and floor and item.recorded_at < floor:
            return False
        if resource['kind']=='memory' and not history:
            from .learning_validity import summary_claims
            claims=summary_claims(self,resource)
            if claims is not None and not any(c['learning']['eligible'] for c in claims):
                return False
            from .memory_policy import interpretation_current
            if not interpretation_current(self.repo, item, self.clock()):
                return False
            if any(t.startswith('media-version:') for t in item.topic_tags):
                if not getattr(self,'provider_current',lambda tags:False)(item.topic_tags):
                    return False
        body, status, expiry, _, _, parents = self._describe(resource['kind'], item)
        origin = item.scope.scope_id+'|'+item.scope.audience_policy_id
        if sha(encode([body, status, expiry, origin, parents])) != resource['content_digest']:
            return False
        for edge in self.parents(ref):
            parent = self.resolve(edge['parent_id'])
            if not parent or parent['source_version'] != edge['parent_version'] or not self.live(parent, visiting | {ref},history=history):
                return False
        return True

    def learning_view(self, resource):
        from .learning_validity import qualification,summary_claims
        if resource['kind']=='event':
            return qualification(self,[resource['native_id']])
        claims=summary_claims(self,resource)
        if claims is not None:
            return dict(eligible=any(c['learning']['eligible'] for c in claims),
                current_claims=sum(c['learning']['eligible'] for c in claims),historical_claims=sum(not c['learning']['eligible'] for c in claims),
                reasons=sorted({reason for c in claims for reason in c['learning']['reasons']}))
        current=self.live(resource)
        return dict(eligible=current,reasons=[] if current else ['memory_not_current'],
            valid_until=resource['expires_at'])

    def current_body(self, resource):
        body=self.body(resource)
        if resource['kind']=='memory':
            from .learning_validity import summary_claims
            claims=summary_claims(self,resource)
            if claims is not None:
                return body.split('\n',1)[0]+'\n'+'\n'.join('['+c['kind']+'] '+c['text'] for c in claims if c['learning']['eligible'])
        return body

    def covered_refs(self, resource):
        if resource['kind']!='memory':
            return set()
        from .learning_validity import summary_claims
        claims=summary_claims(self,resource)
        return {ref for c in claims or [] if c['learning']['eligible'] for ref in c['evidence_refs']}

    def attribution(self, resource):
        if resource['kind']!='memory':
            return None
        row=self.repo._one('SELECT v FROM meta WHERE k=?',('memory_share:'+resource['native_id'],))
        if row:
            return json.loads(row['v'])
        if any(e['relation']=='published_from' for e in self.parents(resource['resource_id'])):
            native=self._native('memory',resource['native_id'])
            return dict(speaker='一位联系人' if native.source_kind=='user_report' else '公开资料',
                experience='reported_by_contact' if native.source_kind=='user_report' else 'observed_public_material',
                quote_voice='original_speaker',bot_experience='heard' if native.source_kind=='user_report' else 'read')
        return None

    def invalidate(self, refs, *, deleted=False, include_roots=True):
        """Synchronous dependency closure, including physical vector erasure."""
        with self.repo.tx():
            affected, pending = set(), list(refs)
            while pending:
                ref = pending.pop()
                row = self.repo._one('SELECT agent_id FROM life_resources WHERE resource_id=?', (ref,))
                if not row or row['agent_id'] != self.agent_id:
                    raise RecallError('not_available')
                if ref in affected:
                    continue
                affected.add(ref)
                pending.extend(e['child_id'] for e in self.repo._all('SELECT child_id FROM life_source_edges WHERE parent_id=?', (ref,)))
            if not include_roots:
                affected.difference_update(refs)
            self._retire_readers(affected | set(refs))
            for ref in affected:
                row = self.repo._one('SELECT * FROM life_resources WHERE resource_id=?', (ref,))
                self.repo._exec('UPDATE life_resources SET status=?,updated_at=? WHERE resource_id=?',
                                ('deleted' if deleted else 'superseded', self.clock(), ref))
                for table in ('life_terms', 'life_semantic_index', 'life_entity_mentions', 'life_activity_index', 'person_facets'):
                    self.repo._exec(f'DELETE FROM {table} WHERE resource_id=?', (ref,))
                # Organized/share bodies are native memories, not side-table copies.
                if deleted and row['kind'] == 'memory':
                    self.repo._exec("UPDATE memories SET status='redacted',summary='',normalized_value='',topic_tags='[]',uncertainty_reason='' WHERE id=?", (row['native_id'],))
                    self.repo._exec('DELETE FROM meta WHERE k=?', ('memory_summary:'+row['native_id'],))
                    self.repo._exec('DELETE FROM meta WHERE k=?', ('memory_share:'+row['native_id'],))
                elif row['kind']=='memory':
                    # Compatibility readers must stop using the same stale
                    # derived summary, even when new recall is switched off.
                    self.repo._exec("UPDATE memories SET status='superseded',version=version+1,updated_at=? WHERE id=? AND status<>'redacted'",
                                    (self.clock(),row['native_id']))
                for job in self.repo._all("SELECT id,payload FROM work_jobs WHERE kind IN ('memory_index','memory_organize') AND status NOT IN ('done','cancelled')"):
                    if ref in json.loads(job['payload']).get('sources', {}):
                        self.repo._exec("UPDATE work_jobs SET status='cancelled',payload='{}',checkpoint='{}',lease_token='' WHERE id=?", (job['id'],))
            if affected:
                snapshots = 'life_recall_snapshot:'+self.agent_id+':'
                self.repo._exec('DELETE FROM meta WHERE substr(k,1,?)=?',(len(snapshots),snapshots))
                prefix = 'memory_embedding:'+self.agent_id+':'
                for cached in self.repo._all('SELECT k,v FROM meta WHERE substr(k,1,?)=?', (len(prefix),prefix)):
                    value = json.loads(cached['v'])
                    value.pop('result',None)
                    self.repo._exec('UPDATE meta SET v=? WHERE k=?',(encode(value),cached['k']))
                self.bump()
            return affected

    def _retire_readers(self, affected):
        """Fence live model histories before returning a source mutation."""
        prefix='life_read_session:'+self.agent_id+':'
        for row in self.repo._all('SELECT k,v FROM meta WHERE substr(k,1,?)=?',(len(prefix),prefix)):
            if not affected.intersection(json.loads(row['v'])):
                continue
            sid=row['k'][len(prefix):]
            binding=self.repo._one('SELECT generation,status FROM agent_sessions WHERE session_id=?',(sid,))
            if binding and binding['status'] in {'current','delegate'}:
                self.repo._exec("UPDATE agent_sessions SET status='retired' WHERE session_id=?",(sid,))
                self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',
                    ('life_retire_reader:'+sid,encode(dict(generation=binding['generation'],agent_id=self.agent_id))))
                for table,column in (('agent_events','payload'),('life_calls','receipt')):
                    self.repo._exec(f"UPDATE {table} SET {column}='{{}}' WHERE session_id=?",(sid,))
            self.repo._exec('DELETE FROM meta WHERE k=?',(row['k'],))

    def backfill(self, *, limit=100):
        """Bounded, resumable scan. New writes are registered by Repository."""
        count = 0
        with self.repo.tx():
            for kind, table in [('event', 'events'), ('memory', 'memories')]:
                key = 'life_backfill:'+self.agent_id+':'+kind
                saved = self.repo._one('SELECT v FROM meta WHERE k=?', (key,))
                cursor = int(saved['v']) if saved else 0
                rows = self.repo._all(f'SELECT rowid,id FROM {table} WHERE agent_id=? AND rowid>? ORDER BY rowid LIMIT ?',
                                      (self.agent_id, cursor, max(0, limit-count)))
                for row in rows:
                    self.repo.project_life(kind,row['id'])
                    cursor = row['rowid']
                    count += 1
                self.repo._exec('INSERT INTO meta VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (key, str(cursor)))
        return count
