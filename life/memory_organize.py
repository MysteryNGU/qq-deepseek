"""Host-owned coverage manifests and atomic, evidence-backed summary commits."""
from __future__ import annotations

import json

from .contracts import Memory, MemorySource, Scope
from .life_access import RecallError
from .life_catalog import encode, sha
from .text import stable_id, terms


class MemoryOrganize:
    def __init__(self, catalog, access, jobs):
        self.catalog, self.repo, self.access, self.jobs = catalog, catalog.repo, access, jobs

    def _internal(self, ctx):
        self.access.require_binding(ctx)
        if ctx.mode != 'internal':
            raise RecallError('not_available')

    def prepare(self, ctx, source_refs, *, kind='episode', topic_ref='', rule_version='summary-v1',coverage_excluded=()):
        self._internal(ctx)
        if kind not in {'episode', 'period'} or not source_refs or len(source_refs) > 10000:
            raise RecallError('invalid_input')
        manifest = {}
        for ref in source_refs:
            resource, _ = self.access.load(ctx, ref)
            # Root events, not old summaries of summaries, are the evidence.
            if resource['kind'] != 'event':
                raise RecallError('raw_evidence_required')
            manifest[resource['resource_id']] = resource['source_version']
        checkpoint = dict(sources=manifest, fence=self.catalog.fence(), kind=kind,
                          topic_ref=topic_ref, rule_version=rule_version,coverage_excluded=list(coverage_excluded))
        coverage_digest = sha(encode(checkpoint))
        job_id = stable_id('memory-organize', ctx.agent_id, topic_ref, coverage_digest)
        self.jobs.enqueue(Scope(ctx.agent_id, '__memory_internal__'), 'memory_organize',
                          dict(checkpoint, coverage_digest=coverage_digest), key=job_id)
        return dict(job_ref=job_id, coverage_digest=coverage_digest, sources=manifest)

    def _validate(self, ctx, lease):
        self._internal(ctx)
        if (lease.kind != 'memory_organize' or not self.jobs.current(lease)
                or lease.payload['fence'] != self.catalog.fence()):
            raise RecallError('stale_source')
        with self.repo.tx():
            for ref, version in lease.payload['sources'].items():
                self.access.load(ctx, ref, version)

    def read_batch(self, ctx, lease, *, after=None, limit=20, cursor=None, max_tokens=4000):
        self._validate(ctx, lease)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise RecallError('invalid_input')
        if type(max_tokens) is not int or not 1200 <= max_tokens <= 32000:
            raise RecallError('budget_requires_page')
        refs = sorted(lease.payload['sources'])
        if after is not None and after not in refs:
            raise RecallError('stale_cursor')
        start = refs.index(after)+1 if after else 0
        from .life_recall import LifeRecall
        from .resource_policy import estimate_tokens
        reader = LifeRecall(self.catalog,self.access,None)
        source_cursor = None
        if cursor:
            saved = reader._open(cursor,ctx,'organize')
            if saved['job'] != lease.id:
                raise RecallError('stale_cursor')
            start, source_cursor = saved['position'], saved.get('source_cursor')
        page = refs[start:start+limit]
        items, finished = [], []
        next_source_cursor, next_position = None, start
        for ref in page:
            remaining = max_tokens-estimate_tokens(items)-500
            if remaining < 800:
                break
            item = reader.read(ctx,ref,cursor=source_cursor,max_tokens=remaining)
            items.append(item)
            if item['has_more']:
                next_source_cursor = item['next_cursor']
                break
            finished.append(ref)
            next_position += 1
            source_cursor = None
        with self.jobs.commit(lease):
            self._validate(ctx, lease)
            row = self.repo._one('SELECT checkpoint FROM work_jobs WHERE id=?', (lease.id,))
            checkpoint = json.loads(row['checkpoint'])
            checkpoint['read_refs'] = sorted(set(checkpoint.get('read_refs', [])) | set(finished))
            self.repo._exec('UPDATE work_jobs SET checkpoint=? WHERE id=? AND lease_token=?',
                            (encode(checkpoint), lease.id, lease.token))
        more = next_position < len(refs)
        next_cursor = reader._cursor(ctx,'organize',job=lease.id,position=next_position,source_cursor=next_source_cursor) if more else None
        return dict(items=items, has_more=more, next_cursor=next_cursor,
                    excluded_count=len(lease.payload.get('coverage_excluded',[])),
                    coverage_digest=lease.payload['coverage_digest'])

    def commit(self, ctx, lease, proposal, call_key):
        self._internal(ctx)
        checksum = sha(encode(proposal))
        receipt_key = 'organized_commit:'+ctx.agent_id+':'+call_key
        with self.repo.tx():
            prior = self.repo._one('SELECT v FROM meta WHERE k=?', (receipt_key,))
            if prior:
                receipt = json.loads(prior['v'])
                if receipt['checksum'] != checksum:
                    raise RecallError('idempotency_conflict')
                self.access.load(ctx, receipt['resource_ref'],history=True)
                return receipt
            self._validate(ctx, lease)
            payload = lease.payload
            sources = set(payload['sources'])
            if proposal.get('coverage_digest') != payload['coverage_digest']:
                raise RecallError('incomplete_coverage')
            checkpoint = json.loads(self.repo._one('SELECT checkpoint FROM work_jobs WHERE id=?', (lease.id,))['checkpoint'])
            if set(checkpoint.get('read_refs', [])) != sources:
                raise RecallError('incomplete_coverage')
            claims = proposal.get('claims', [])
            omitted = set(proposal.get('omitted_refs', []))
            if not claims or not omitted <= sources:
                raise RecallError('invalid_summary')
            included, lines, qualified = set(), [], []
            for claim in claims:
                refs = set(claim.get('evidence_refs', []))
                if (claim.get('kind') not in {'observed', 'reported', 'interpreted', 'hypothetical'}
                        or not isinstance(claim.get('text'), str) or not claim['text'].strip()
                        or len(claim['text']) > 4000 or not refs or not refs <= sources or refs & omitted):
                    raise RecallError('invalid_summary')
                included.update(refs)
                from .learning_validity import qualification
                learning=qualification(self.catalog,[self.catalog.resolve(ref)['native_id'] for ref in refs],
                    interpreted=claim['kind'] in {'interpreted','hypothetical'})
                qualified.append(dict(claim,learning=learning))
                lines.append('['+claim['kind']+'] '+claim['text'])
            if included | omitted != sources:
                raise RecallError('incomplete_coverage')
            from .stable_facts import StableFacts
            fact_proposals = proposal.get('stable_facts', [])
            facts = []
            if not isinstance(fact_proposals, list) or len(fact_proposals) > 8:
                facts.append(dict(saved=False, reason='invalid_facts'))
            else:
                extractor = StableFacts(self.catalog, self.access)
                for index, fact in enumerate(fact_proposals):
                    try:
                        with self.repo.tx():
                            facts.extend(extractor.commit(ctx, [fact], included))
                    except RecallError as error:
                        # Optional extraction must not strand a valid summary.
                        facts.append(dict(index=index, saved=False, reason=error.code))
            topic = payload.get('topic_ref') or lease.id
            pointer_key = 'memory_summary_current:'+ctx.agent_id+':'+payload['kind']+':'+topic
            pointer = self.repo._one('SELECT v FROM meta WHERE k=?', (pointer_key,))
            current = json.loads(pointer['v']) if pointer else dict(revision=0, memory_id='')
            if proposal.get('base_revision', 0) != current['revision']:
                raise RecallError('stale_source')
            identity = stable_id('summary', ctx.agent_id, lease.id)
            native_sources = [self.catalog.resolve(ref)['native_id'] for ref in sorted(included)]
            scope = Scope(ctx.agent_id, '__memory_internal__', audience_policy_id='__memory_internal__')
            # Individual claims keep their original evidence deadlines. A mixed
            # historical/current summary is not renewed by its commit time.
            expiry = None
            memory = Memory(id=identity, scope=scope, kind='episode', summary='\n'.join(lines),
                source_kind='inference', status='active',
                valid_until=expiry, supersedes_id=current['memory_id'], event_group_id=topic,
                sources=[MemorySource(identity, ref) for ref in native_sources])
            self.repo.insert_memory(memory, terms(memory.summary))
            resource = self.catalog.register('memory', identity)
            metadata = dict(kind=payload['kind'], sources=payload['sources'], included=sorted(included),
                omitted=sorted(omitted), unresolved=proposal.get('unresolved_refs', []), claims=qualified,
                coverage_manifest=payload['coverage_digest'], complete_for_range=not omitted and not payload.get('coverage_excluded'),
                excluded=payload.get('coverage_excluded',[]),
                rule_version=payload['rule_version'], author_kind='model_interpretation',
                source_digest=sha(encode(payload['sources'])))
            self.repo._exec('INSERT INTO meta VALUES(?,?)', ('memory_summary:'+identity, encode(metadata)))
            if current['memory_id']:
                self.repo.update_memory(current['memory_id'], status='superseded')
            self.repo._exec('INSERT INTO meta VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
                (pointer_key, encode(dict(revision=current['revision']+1, memory_id=identity))))
            receipt = dict(resource_ref=resource['resource_id'], memory_id=identity,
                           revision=current['revision']+1, checksum=checksum, complete_for_range=metadata['complete_for_range'],
                           stable_facts=facts)
            self.repo._exec('INSERT INTO meta VALUES(?,?)', (receipt_key, encode(receipt)))
            self.jobs.finish(lease, checkpoint=dict(resource_ref=resource['resource_id']))
            return receipt

    def prepare_share(self, ctx, source_ref, spans, *, evaluator):
        """Evaluate a concrete ordinary-content span under HOST policy.

        The policy adapter examines the full source and selected bytes. A role
        proposal cannot set ACL or turn a whole private archive into public data.
        The evaluator is injected by the host, never accepted in tool arguments.
        """
        self._internal(ctx)
        resource, body = self.access.load(ctx, source_ref)
        if resource['kind'] != 'event' or not spans or evaluator is None:
            raise RecallError('not_available')
        data = body.encode('utf-8')
        selected = []
        for start, end in spans:
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(data):
                raise RecallError('invalid_span')
            try:
                data[:start].decode('utf-8')
                selected.append(data[start:end].decode('utf-8'))
            except UnicodeDecodeError:
                raise RecallError('invalid_span') from None
        fence = self.catalog.fence()
        decision = evaluator(source=body, spans=tuple(spans), selected=tuple(selected))
        if decision != 'ordinary':
            raise RecallError('not_available')
        with self.repo.tx():
            self.access.load(ctx, source_ref, resource['source_version'])
            if fence != self.catalog.fence():
                raise RecallError('stale_source')
            identity = stable_id('ordinary-share', resource['resource_id'], str(resource['source_version']), encode(spans))
            existing = self.repo.get_memory(identity)
            if existing:
                return self.catalog.resolve(identity)['resource_id']
            scope = Scope(ctx.agent_id, '__memory_internal__', audience_policy_id='__memory_internal__')
            memory = Memory(id=identity, scope=scope, kind='episode', summary='\n'.join(selected),
                status='active', source_kind=self.repo.get_event(resource['native_id']).source_kind,valid_from=resource['occurred_at'],
                recorded_at=self.repo.get_event(resource['native_id']).received_at,
                sources=[MemorySource(identity, resource['native_id'])])
            attribution=dict(speaker='一位联系人' if memory.source_kind=='user_report' else '公开资料',
                experience='reported_by_contact' if memory.source_kind=='user_report' else 'observed_public_material',
                quote_voice='original_speaker',bot_experience='heard' if memory.source_kind=='user_report' else 'read')
            if memory.source_kind=='self_output':
                attribution.update(speaker='自己',experience='confirmed_self_expression',
                    quote_voice='self',bot_experience='said')
            self.repo._exec('INSERT INTO meta VALUES(?,?)',('memory_share:'+identity,encode(attribution)))
            self.repo.insert_memory(memory, terms(memory.summary))
            snippet = self.catalog.register('memory', identity)
            self.repo._exec('DELETE FROM life_source_edges WHERE child_id=?', (snippet['resource_id'],))
            self.catalog.add_edge(snippet['resource_id'], resource['resource_id'], resource['source_version'], 'published_from')
            self.access.set_rule(snippet['resource_id'], 'disclose', '*', 'allow')
            return snippet['resource_id']
