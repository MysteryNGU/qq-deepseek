"""Rebuildable UTF-8 chunks, finite Float32 vectors and evidence mentions."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import struct

from .contracts import Scope
from .life_access import RecallError
from .life_catalog import encode, sha
from .resource_policy import estimate_tokens
from .text import stable_id


@dataclass(frozen=True)
class IndexConfig:
    chunk_tokens: int = 384
    overlap_tokens: int = 48
    route_limit: int = 40
    scan_limit: int = 10000
    rrf_k: int = 60
    related_hops: int = 2
    retire_after_hours: int = 24

    def __post_init__(self):
        if any(type(v) is not int or v < (0 if k=='overlap_tokens' else 1) for k,v in self.__dict__.items()):
            raise ValueError('invalid_memory_index_config')
        if self.overlap_tokens >= self.chunk_tokens or self.related_hops > 2:
            raise ValueError('invalid_memory_index_config')


def vector_blob(values, dimension):
    if len(values) != dimension or any(type(v) not in {int, float} or not math.isfinite(v) for v in values):
        raise RecallError('invalid_embedding')
    norm = math.hypot(*values)
    if not math.isfinite(norm) or norm == 0:
        raise RecallError('invalid_embedding')
    try:
        return struct.pack('<'+'f'*dimension, *(v/norm for v in values))
    except (OverflowError, struct.error):
        raise RecallError('invalid_embedding') from None


def chunks(text, config):
    """Return actual UTF-8 ranges; never cut a multibyte character."""
    offsets = [0]
    for char in text:
        offsets.append(offsets[-1]+len(char.encode('utf-8')))
    start = 0
    while start < len(text):
        end, last_boundary = start, None
        while end < len(text) and estimate_tokens(text[start:end+1]) <= config.chunk_tokens:
            end += 1
            if text[end-1] in '\n。！？!?；;':
                last_boundary = end
        if end == start:
            raise RecallError('chunk_budget_too_small')
        if end < len(text) and last_boundary and last_boundary > start:
            end = last_boundary
        yield offsets[start], offsets[end], text[start:end]
        if end == len(text):
            break
        overlap_start = end
        while overlap_start > start+1 and estimate_tokens(text[overlap_start-1:end]) <= config.overlap_tokens:
            overlap_start -= 1
        start = max(start+1, overlap_start)


class MemoryIndex:
    def __init__(self, catalog, access, jobs, config=None):
        self.catalog, self.repo, self.access, self.jobs = catalog, catalog.repo, access, jobs
        self.config = config or IndexConfig()
        self.adapter = None
        self.model_key, self.dimension = '', 0
        self.chunker_version = 'utf8-paragraph-v1:'+sha(encode([self.config.chunk_tokens, self.config.overlap_tokens]))[:12]

    def configure(self, adapter, *, provider_revision, model_id, model_revision, dimension):
        if type(dimension) is not int or dimension <= 0 or dimension > 65536 or not all((provider_revision, model_id, model_revision)):
            raise ValueError('invalid_embedding_model')
        self.adapter, self.dimension = adapter, dimension
        self.model_key = sha(encode([provider_revision, model_id, model_revision, dimension, 'l2-f32-v1']))

    def enqueue_resource(self, resource_ref, expected_version):
        resource = self.catalog.resolve(resource_ref)
        if not resource or not self.catalog.live(resource) or resource['source_version'] != expected_version:
            raise RecallError('stale_source')
        # The durable job stores identifiers and epochs, never a second body.
        key = stable_id('memory-index', resource_ref, str(expected_version), self.chunker_version, self.model_key or 'lexical')
        scope = Scope(self.catalog.agent_id, '__memory_internal__')
        sources = {resource_ref: expected_version}
        return self.jobs.enqueue(scope, 'memory_index', dict(sources=sources, fence=self.catalog.fence(),
            model_key=self.model_key, chunker_version=self.chunker_version), key=key)

    def _unindexed(self):
        # Materialize the generation's existing jobs once, including protected
        # unknown/paused jobs. They are reconciled separately, never requeued.
        return ("WITH queued AS MATERIALIZED (SELECT s.key AS ref,s.value AS version,j.status,j.due,j.lease_until FROM work_jobs j, "
                "json_each(j.payload,'$.sources') s WHERE j.scope_id=? AND j.kind='memory_index' "
                "AND json_extract(j.payload,'$.model_key')=? AND json_extract(j.payload,'$.chunker_version')=?) "
                "SELECT r.*,q.status AS job_status,q.due AS job_due,q.lease_until AS job_lease_until FROM life_resources r LEFT JOIN queued q "
                "ON q.ref=r.resource_id AND q.version=r.source_version WHERE r.agent_id=? AND r.status='active' "
                "AND NOT EXISTS (SELECT 1 FROM life_semantic_index i WHERE i.resource_id=r.resource_id "
                "AND i.source_version=r.source_version AND i.model_key=? AND i.chunker_version=? AND i.state='ready')")

    def _scan_args(self):
        return (self.catalog.agent_id+'|__memory_internal__',self.model_key,self.chunker_version,
                self.catalog.agent_id,self.model_key,self.chunker_version)

    def scan_enqueue(self, limit=100):
        import json
        key = 'memory_index_scan:'+self.catalog.agent_id+':'+self.model_key+':'+self.chunker_version
        saved = self.repo._one('SELECT v FROM meta WHERE k=?',(key,))
        progress = json.loads(saved['v']) if saved else dict(passes=0, enqueued=0)
        cursor = progress.get('cursor')
        sql, args = self._unindexed()+" AND q.ref IS NULL", self._scan_args()
        if cursor:
            sql += ' AND (r.occurred_at<? OR (r.occurred_at=? AND r.resource_id<?))'
            args += (cursor[0],cursor[0],cursor[1])
        rows = self.repo._all(sql+' ORDER BY r.occurred_at DESC,r.resource_id DESC LIMIT ?',(*args,limit))
        context = self.access.bind(Scope(self.catalog.agent_id,'__memory_internal__'),mode='internal')
        denied, stale, enqueued = 0, 0, 0
        with self.repo.tx():
            for row in rows:
                resource = dict(row)
                if not self.catalog.live(resource):
                    stale += 1
                elif not self.access.internal_allowed(context,resource):
                    denied += 1
                else:
                    self.enqueue_resource(row['resource_id'],row['source_version'])
                    enqueued += 1
            progress.update(cursor=[rows[-1]['occurred_at'],rows[-1]['resource_id']] if rows else None,
                scanned_at=self.catalog.clock(),scanned=len(rows),excluded_denied=denied,excluded_stale=stale,
                enqueued=progress.get('enqueued',0)+enqueued,
                passes=progress.get('passes',0)+(not rows))
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',(key,encode(progress)))
        return progress

    def _validate(self, lease):
        if not self.jobs.current(lease) or lease.payload['fence'] != self.catalog.fence():
            raise RecallError('stale_source')
        for ref, version in lease.payload['sources'].items():
            r = self.catalog.resolve(ref)
            if not r or r['source_version'] != version or not self.catalog.live(r):
                raise RecallError('stale_source')
        if lease.payload['model_key'] != self.model_key or lease.payload['chunker_version'] != self.chunker_version:
            raise RecallError('stale_source')

    def claim_next(self, ttl=120):
        now = self.jobs.clock()
        row = self.repo._one("SELECT id FROM work_jobs WHERE scope_id=? AND kind='memory_index' AND due<=? "
            "AND (status='pending' OR (status='running' AND lease_until<?)) "
            "AND json_extract(payload,'$.model_key')=? AND json_extract(payload,'$.chunker_version')=? ORDER BY due,created LIMIT 1",
            (self.catalog.agent_id+'|__memory_internal__',now,now,self.model_key,self.chunker_version))
        return self.jobs.claim(row['id'],ttl=ttl) if row else None

    async def build(self, lease):
        self._validate(lease)
        ref, version = next(iter(lease.payload['sources'].items()))
        resource = self.catalog.resolve(ref)
        scope = Scope(self.catalog.agent_id, '__memory_internal__')
        ctx = self.access.bind(scope, mode='internal')
        _, text = self.access.load(ctx, ref, version)
        parts = list(chunks(text, self.config))
        self.index_participants(resource)
        if not self.adapter:
            self.jobs.finish(lease, 'paused', error='embedding_not_configured')
            return
        self._validate(lease)
        # Adapter owns preflight external-computation policy and budget
        # reservation. Its stable key survives timeouts; unknown is not retried.
        provider_config = getattr(self.adapter,'config',None)
        batch_limit = min(getattr(provider_config,'max_input_tokens',8192),getattr(provider_config,'max_request_bytes',262144)//2)
        batches, batch, batch_bytes = [], [], 0
        for part in parts:
            size = len(part[2].encode('utf-8'))
            if size>batch_limit:
                raise RecallError('embedding_request_too_large')
            if batch and batch_bytes+size>batch_limit:
                batches.append(batch)
                batch, batch_bytes = [], 0
            batch.append(part)
            batch_bytes += size
        if batch:
            batches.append(batch)
        vectors, receipts, usage = [], [], []
        for batch_no,batch in enumerate(batches):
            self._validate(lease)
            reply = await self.adapter.embed([p[2] for p in batch], model_key=self.model_key,
                purpose='memory_index', call_key=lease.id+':batch:'+str(batch_no), source_refs=[ref])
            if reply.get('state') == 'unknown':
                self.jobs.finish(lease, 'unknown', error='embedding_outcome_unknown')
                return
            if len(reply.get('vectors',[]))!=len(batch) or reply.get('input_digests')!=[sha(p[2]) for p in batch]:
                raise RecallError('invalid_embedding')
            vectors.extend(reply['vectors'])
            receipts.append(reply.get('receipt'))
            usage.append(reply.get('actual_usage'))
            if not self.jobs.renew(lease):
                raise RecallError('stale_source')
        blobs = [vector_blob(v, self.dimension) for v in vectors]
        self._validate(lease)
        with self.jobs.commit(lease):
            self._validate(lease)
            for (start, end, body), blob in zip(parts, blobs):
                self.repo._exec('INSERT INTO life_semantic_index VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(resource_id,source_version,chunker_version,byte_start,model_key) DO NOTHING',
                    (self.catalog.agent_id, ref, version, resource['content_digest'], self.chunker_version,
                     start, end, sha(body), self.model_key, self.dimension, blob, 'ready',
                     encode({'fence': lease.payload['fence']}), self.catalog.epoch('policy'), self.catalog.clock()))
            self.jobs.finish(lease, checkpoint=dict(receipts=receipts, actual_usage=usage))

    def index_participants(self, resource):
        if resource['kind'] != 'event' or not self.catalog.live(resource):
            return
        event = self.repo.get_event(resource['native_id'])
        # QQ numeric actor binding is provided by transport, not nicknames.
        actors = [event.actor_id] if event.source_kind=='user_report' and event.scope.conversation_id.startswith('qq:') and event.actor_id.isdigit() else []
        with self.repo.tx():
            for actor in actors:
                self.add_mention(resource, 'actor:qq:'+actor, 'actor', 'participant', 'observed')

    def add_mention(self, resource, entity_key, entity_kind, relation, evidence_kind, span=None):
        if (entity_kind not in {'actor', 'work', 'topic'} or relation not in {'participant', 'mentions', 'recommends', 'about'}
                or evidence_kind not in {'observed', 'stated', 'inferred'} or not self.catalog.live(resource)):
            raise RecallError('invalid_mention')
        if entity_kind == 'actor' and not re.fullmatch(r'actor:[a-z][a-z0-9_]*:[0-9]+', entity_key):
            raise RecallError('invalid_mention')
        if relation == 'participant':
            event = self.repo.get_event(resource['native_id']) if resource['kind'] == 'event' else None
            if not event or event.source_kind!='user_report' or entity_key != 'actor:qq:'+event.actor_id or not event.scope.conversation_id.startswith('qq:'):
                raise RecallError('invalid_mention')
        elif span is None:
            raise RecallError('invalid_mention')
        start, end = span if span is not None else (None, None)
        if span is not None:
            data = self.catalog.body(resource).encode('utf-8')
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(data):
                raise RecallError('invalid_mention')
            try:
                data[:start].decode('utf-8'); data[start:end].decode('utf-8')
            except UnicodeDecodeError:
                raise RecallError('invalid_mention') from None
        key = stable_id('mention', resource['resource_id'], str(resource['source_version']), entity_key, relation, str(span))
        with self.repo.tx():
            self.repo._exec('INSERT OR IGNORE INTO life_entity_mentions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, self.catalog.agent_id, entity_key, entity_kind, resource['resource_id'], resource['source_version'],
                 relation, evidence_kind, start, end, 'host-evidence-v1', 'candidate' if evidence_kind == 'inferred' else 'active'))
        return key

    async def query_vector(self, ctx, query):
        if not self.adapter:
            return None
        self.access.require_binding(ctx)
        key = stable_id('memory-query', ctx.agent_id, ctx.mode, ctx.audience_key, query, self.model_key,
                        self.catalog.fence())
        reply = await self.adapter.embed([query], model_key=self.model_key, purpose='memory_query',
                                         call_key=key, source_refs=[])
        if reply.get('state') == 'unknown' or reply.get('input_digests') != [sha(query)] or len(reply.get('vectors', [])) != 1:
            raise RecallError('invalid_embedding')
        return vector_blob(reply['vectors'][0], self.dimension)

    def search_vectors(self, allowed, query_blob):
        if not query_blob or not self.model_key:
            return [], 'unavailable'
        query = struct.unpack('<'+'f'*self.dimension, query_blob)
        scored, scanned, missing = {}, 0, False
        for resource in allowed:
            rows = self.repo._all("SELECT * FROM life_semantic_index WHERE resource_id=? AND agent_id=? AND model_key=? "
                "AND chunker_version=? AND source_version=? AND state='ready' ORDER BY byte_start",
                (resource['resource_id'], self.catalog.agent_id, self.model_key, self.chunker_version, resource['source_version']))
            if not rows:
                missing=True
            for row in rows:
                scanned += 1
                if scanned > self.config.scan_limit:
                    return [], 'partial'  # Never pretend an arbitrary prefix is a complete search.
                if row['dimension'] != self.dimension or row['source_digest'] != resource['content_digest']:
                    missing=True
                    continue
                try:
                    values = struct.unpack('<'+'f'*self.dimension, row['vector'])
                except (struct.error, TypeError):
                    missing=True
                    continue
                if not all(math.isfinite(v) for v in values) or not .999 <= math.hypot(*values) <= 1.001:
                    missing=True
                    continue
                score = sum(a*b for a, b in zip(query, values))
                ref = resource['resource_id']
                scored[ref] = max(score, scored.get(ref, -1))
        return sorted(scored, key=lambda ref: (-scored[ref], ref))[:self.config.route_limit], 'partial' if missing else 'ready'

    def find_related(self, allowed, entity_refs):
        allowed_ids = {r['resource_id']: r['source_version'] for r in allowed}
        frontier, seen, result = set(entity_refs), set(), []
        for _ in range(self.config.related_hops):
            new = set()
            for entity in sorted(frontier-seen):
                rows = self.repo._all("SELECT resource_id,source_version FROM life_entity_mentions WHERE agent_id=? AND entity_key=? AND state='active' ORDER BY resource_id",
                                      (self.catalog.agent_id, entity))
                for row in rows:
                    ref = row['resource_id']
                    if allowed_ids.get(ref) != row['source_version']:
                        continue
                    if ref not in result:
                        result.append(ref)
                    # Every intermediate resource was authorized before traversing.
                    new.update(r['entity_key'] for r in self.repo._all("SELECT entity_key FROM life_entity_mentions WHERE resource_id=? AND source_version=? AND state='active'", (ref, row['source_version'])))
            seen.update(frontier)
            frontier = new-seen
        return result[:self.config.route_limit]

    def status(self, *, diagnostic=False):
        result = dict(lexical='ready', semantic='not_configured' if not self.adapter else
                    'verified' if getattr(self.adapter,'verified',False) else 'configured_unverified',
                    related='ready', model_key=self.model_key or None, chunker_version=self.chunker_version,
                    coverage_state='not_evaluated')
        if diagnostic:
            rows = self.repo._all('SELECT kind,status,COUNT(*) AS n FROM life_resources WHERE agent_id=? GROUP BY kind,status', (self.catalog.agent_id,))
            result['catalog'] = [dict(row) for row in rows]
            result['jobs'] = [dict(row) for row in self.repo._all("SELECT status,COUNT(*) AS n FROM work_jobs WHERE scope_id=? AND kind='memory_index' GROUP BY status", (self.catalog.agent_id+'|__memory_internal__',))]
            import json
            row = self.repo._one('SELECT v FROM meta WHERE k=?',('memory_index_scan:'+self.catalog.agent_id+':'+self.model_key+':'+self.chunker_version,))
            result['scan'] = json.loads(row['v']) if row else None
            counts = self.repo._all('SELECT job_status,COUNT(*) AS n FROM ('+self._unindexed()+') GROUP BY job_status',self._scan_args())
            result['missing_by_job_state'] = {r['job_status'] or 'not_enqueued':r['n'] for r in counts}
            result['executable_backlog'] = self.repo._one('SELECT COUNT(*) AS n FROM ('+self._unindexed()+') '
                "WHERE job_due<=? AND (job_status='pending' OR (job_status='running' AND job_lease_until<?))",
                (*self._scan_args(),self.jobs.clock(),self.jobs.clock()))['n']
            result['backlog_meaning'] = '当前代次可领取队列；来源有效性及用途在扫描和build提交前再次核验，尚未入队的候选不等于合格覆盖缺口'
            result['blocked'] = sum(r['n'] for r in counts if r['job_status'] not in (None,'pending','running'))
            result['coverage_state'] = 'candidate_backlog' if counts else 'current_generation_complete_for_active_catalog'
            last = self.repo._one("SELECT MAX(updated) AS at FROM work_jobs WHERE scope_id=? AND kind='memory_index' AND status='done' "
                "AND json_extract(payload,'$.model_key')=? AND json_extract(payload,'$.chunker_version')=?",self._scan_args()[:3])
            result['last_success_at'] = last['at']
        return result

    def retire_replaced_generations(self, limit=500):
        """Drop only rebuildable vectors after a verified replacement's grace period."""
        if not self.adapter or not getattr(self.adapter,'verified',False):return 0
        cutoff=self.catalog.clock()-self.config.retire_after_hours*3600
        with self.repo.tx():
            rows=self.repo._all("SELECT old.rowid AS rid FROM life_semantic_index old JOIN life_resources r ON r.resource_id=old.resource_id "
                "WHERE old.agent_id=? AND r.status='active' AND (old.model_key!=? OR old.chunker_version!=?) "
                "AND EXISTS (SELECT 1 FROM life_semantic_index fresh WHERE fresh.resource_id=r.resource_id AND fresh.source_version=r.source_version "
                "AND fresh.model_key=? AND fresh.chunker_version=? AND fresh.state='ready' AND fresh.updated_at<=?) "
                "AND EXISTS (SELECT 1 FROM work_jobs j,json_each(j.payload,'$.sources') s WHERE j.kind='memory_index' AND j.status='done' "
                "AND s.key=r.resource_id AND s.value=r.source_version AND json_extract(j.payload,'$.model_key')=? "
                "AND json_extract(j.payload,'$.chunker_version')=?) LIMIT ?",
                (self.catalog.agent_id,self.model_key,self.chunker_version,self.model_key,self.chunker_version,cutoff,self.model_key,self.chunker_version,limit))
            for row in rows:self.repo._exec('DELETE FROM life_semantic_index WHERE rowid=?',(row['rid'],))
            if rows:
                self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)',('memory_index_retirement:'+self.catalog.agent_id,
                    encode(dict(at=self.catalog.clock(),removed_chunks=len(rows),current_model_key=self.model_key,source_records_removed=0))))
        return len(rows)
