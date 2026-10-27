"""One permission set for exact, lexical, semantic and evidence-related recall."""
from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import json
import secrets

from .life_access import RecallError
from .life_catalog import encode, sha
from .resource_policy import estimate_tokens
from .text import terms


class LifeRecall:
    def __init__(self, catalog, access, index):
        self.catalog, self.repo, self.access, self.index = catalog, catalog.repo, access, index
        key = 'life_cursor_key:'+catalog.agent_id
        with self.repo.tx():
            row = self.repo._one('SELECT v FROM meta WHERE k=?', (key,))
            self._key = bytes.fromhex(row['v']) if row else secrets.token_bytes(32)
            if not row:
                self.repo._exec('INSERT INTO meta VALUES(?,?)', (key, self._key.hex()))

    def _binding(self, ctx):
        return [ctx.agent_id, ctx.mode, ctx.scope_id, ctx.audience_key, ctx.policy_revision,
                ctx.membership_revision, ctx.session_id, ctx.generation]

    def _sign(self, payload):
        raw = encode(payload).encode('utf-8')
        signature = hmac.new(self._key, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature+raw).decode('ascii')

    def _open(self, token, ctx, kind):
        try:
            if not isinstance(token, str) or len(token) > 100000:
                raise ValueError()
            raw = base64.b64decode(token, altchars=b'-_', validate=True)
            if not hmac.compare_digest(raw[:32], hmac.new(self._key, raw[32:], hashlib.sha256).digest()):
                raise ValueError()
            payload = json.loads(raw[32:])
            if (payload['binding'] != self._binding(ctx) or payload['kind'] != kind
                    or payload['fence'] != self.catalog.fence()
                    or payload['expires'] <= self.catalog.clock()):
                raise ValueError()
            if kind=='recall':
                row = self.repo._one('SELECT v FROM meta WHERE k=?',
                    ('life_recall_snapshot:'+ctx.agent_id+':'+payload['snapshot'],))
                if not row:
                    raise ValueError()
                snapshot = json.loads(row['v'])
                if snapshot['fence'] != payload['fence'] or snapshot['binding'] != payload['binding']:
                    raise ValueError()
                payload.update(snapshot)
            return payload
        except (ValueError, KeyError, TypeError):
            raise RecallError('stale_cursor') from None

    def _cursor(self, ctx, kind, **values):
        payload = dict(binding=self._binding(ctx), kind=kind, fence=self.catalog.fence(),
                       expires=self.catalog.clock()+900, **values)
        if kind=='recall':
            identity = sha(encode(payload))
            prefix = 'life_recall_snapshot:'+ctx.agent_id+':'
            with self.repo.tx():
                self.repo._exec('INSERT OR IGNORE INTO meta VALUES(?,?)',(prefix+identity,encode(payload)))
                rows = self.repo._all('SELECT k,v FROM meta WHERE substr(k,1,?)=? ORDER BY json_extract(v,\'$.expires\') DESC', (len(prefix),prefix))
                for index,row in enumerate(rows):
                    if index>=128 or json.loads(row['v'])['expires']<=self.catalog.clock():
                        self.repo._exec('DELETE FROM meta WHERE k=?',(row['k'],))
            payload = {k:payload[k] for k in ('binding','kind','fence','expires')}
            payload['snapshot'] = identity
        return self._sign(payload)

    def allowed(self, ctx, *, high_water=None, actor_refs=(), time_range=None, lexical_terms=None, exact=()):
        self.access.require_binding(ctx)
        query = 'SELECT rowid,* FROM life_resources WHERE agent_id=?'
        args = [ctx.agent_id]
        if high_water is not None:
            query += ' AND rowid<=?'
            args.append(high_water)
        if lexical_terms:
            clues = sorted(lexical_terms)[:96]
            sql = 'resource_id IN (SELECT resource_id FROM life_terms WHERE agent_id=? AND term IN ('+','.join('?' for _ in clues)+'))'
            args.extend([ctx.agent_id,*clues])
            if exact:
                sql += ' OR resource_id IN ('+','.join('?' for _ in exact)+')'
                args.extend(exact)
            query += ' AND ('+sql+')'
        query += ' ORDER BY occurred_at DESC,resource_id'
        resources = []
        for row in self.repo._all(query, args):
            resource = dict(row)
            if resource['kind']=='event':
                event = self.repo.get_event(resource['native_id'])
                if event and event.source_kind=='inference':
                    # Raw reflections remain available by exact read. Their
                    # current searchable interpretation owns expiry/task fences.
                    continue
            try:
                _,current_body=self.access.load(ctx, resource['resource_id'], resource['source_version'],track=False)
            except RecallError:
                continue
            if resource['kind']=='memory' and lexical_terms and resource['resource_id'] not in exact:
                if not set(terms(current_body.split('\n',1)[1])).intersection(lexical_terms):
                    continue
            if time_range and not time_range[0] <= resource['occurred_at'] < time_range[1]:
                continue
            if actor_refs:
                linked = {r['entity_key'] for r in self.repo._all("SELECT entity_key FROM life_entity_mentions WHERE resource_id=? AND source_version=? AND state='active'", (resource['resource_id'], resource['source_version']))}
                if not linked.intersection(actor_refs):
                    continue
            resources.append(resource)
        return resources

    async def search_async(self, ctx, query, **options):
        vector = None
        mode = options.get('mode', 'auto')
        if mode in {'auto', 'semantic'} and self.index.adapter and not options.get('cursor'):
            try:
                vector = await self.index.query_vector(ctx, query)
            except (RecallError, TimeoutError, ConnectionError):
                pass
        return await asyncio.to_thread(self.search,ctx,query,query_vector=vector,**options)

    def search(self, ctx, query='', *, mode='auto', budget=2400, limit=10, cursor=None,
               actor_refs=(), entity_refs=(), time_range=None, exact_refs=(), query_vector=None, exclude=()):
        if (not isinstance(query, str) or len(query) > 2000 or mode not in {'auto', 'text', 'semantic', 'related'}
                or type(budget) is not int or not 0 <= budget <= 32000
                or type(limit) is not int or not 1 <= limit <= 40):
            raise RecallError('invalid_input')
        self.access.require_binding(ctx)
        signature = sha(encode([query, mode, list(actor_refs), list(entity_refs), time_range, list(exact_refs), list(exclude)]))
        fence = self.catalog.fence()
        coverage = dict(text='ready', semantic='unavailable', related='ready')
        if cursor:
            saved = self._open(cursor, ctx, 'recall')
            if saved['query'] != signature or saved['index_version'] != [self.index.model_key, self.index.chunker_version]:
                raise RecallError('stale_cursor')
            ranked, versions, coverage, high_water = saved['ranked'], saved['versions'], saved['coverage'], saved['high_water']
            previous_coverage = set(saved.get('covered',[]))
        else:
            previous_coverage = set()
            high_water = self.repo._one('SELECT COALESCE(MAX(rowid),0) AS n FROM life_resources WHERE agent_id=?', (ctx.agent_id,))['n']
            qterms = set(terms(query))
            exact_ids = []
            for ref in exact_refs:
                resource = self.catalog.resolve(ref)
                if resource:
                    exact_ids.append(resource['resource_id'])
            allowed = self.allowed(ctx, high_water=high_water, actor_refs=actor_refs, time_range=time_range,
                lexical_terms=qterms if query_vector is None and not entity_refs and not actor_refs else None,exact=exact_ids)
            by_id = {r['resource_id']: r for r in allowed}
            lexical = {}
            for resource in allowed:
                ref = resource['resource_id']
                stored = {r['term'] for r in self.repo._all('SELECT term FROM life_terms WHERE resource_id=? AND source_version=?', (ref, resource['source_version']))}
                hits = len(qterms & stored)
                if hits or not query:
                    lexical[ref] = hits
            text_route = sorted(lexical, key=lambda ref: (-lexical[ref], -by_id[ref]['occurred_at'], ref))[:self.index.config.route_limit]
            semantic, coverage['semantic'] = self.index.search_vectors(allowed, query_vector)
            related = self.index.find_related(allowed, [*entity_refs, *actor_refs])
            routes = [text_route, semantic, related] if mode == 'auto' else (
                [related, text_route] if mode == 'related' else [semantic, text_route] if mode == 'semantic' else [text_route])
            scores = {}
            for route in routes:
                for rank, ref in enumerate(route, 1):
                    scores[ref] = scores.get(ref, 0)+1/(self.index.config.rrf_k+rank)
            exact = []
            for ref in exact_refs:
                resource = self.catalog.resolve(ref)
                if resource and resource['resource_id'] in by_id:
                    exact.append(resource['resource_id'])
            ranked = list(dict.fromkeys(exact + sorted(scores, key=lambda ref: (-scores[ref], -by_id[ref]['occurred_at'], ref))))
            ranked = [ref for ref in ranked if ref not in exclude and by_id[ref]['native_id'] not in exclude]
            versions = {ref: by_id[ref]['source_version'] for ref in ranked}
        items, remaining, covered = [], [], set(previous_coverage)
        for pos, ref in enumerate(ranked):
            resource, body = self.access.load(ctx, ref, versions[ref])
            # Root evidence overlap is only compressed if a valid summary
            # explicitly declares that source covered (not merely in its range).
            if ref in covered and not exact_refs:
                continue
            item = self._item(resource, body)
            if estimate_tokens(dict(items=items+[item], coverage=coverage)) > budget:
                # A long leading source must not make every page empty.
                # Return the intact reference/status instead of a misleading
                # prefix that might omit its negation or cancellation.
                item.update(preview=None,preview_complete=False)
                if estimate_tokens(dict(items=items+[item],coverage=coverage)) > budget:
                    remaining = ranked[pos:]
                    break
            items.append(item)
            if resource['kind'] == 'memory':
                row = self.repo._one('SELECT v FROM meta WHERE k=?', ('memory_summary:'+resource['native_id'],))
                if row:
                    manifest = json.loads(row['v'])
                    covered.update(self.catalog.covered_refs(resource))
            if len(items) >= limit:
                remaining = ranked[pos+1:]
                break
        if self.catalog.fence() != fence:
            raise RecallError('stale_source')
        def packet():
            pending = [ref for ref in remaining if ref not in covered]
            next_cursor = self._cursor(ctx,'recall',query=signature,ranked=pending,
                versions={r:versions[r] for r in pending},coverage=coverage,high_water=high_water,
                covered=sorted(covered),index_version=[self.index.model_key,self.index.chunker_version]) if pending else None
            result=dict(items=items,coverage=coverage,next_cursor=next_cursor,has_more=bool(pending),budget_truncated=bool(pending))
            result['estimated_tokens']=estimate_tokens(result)+20
            return result
        result=packet()
        while estimate_tokens(result)>budget and items:
            items.pop()
            last = ranked.index(items[-1]['resource_ref'])+1 if items else 0
            remaining=ranked[last:]
            covered=set(previous_coverage)
            for item in items:
                native=self.catalog.resolve(item['resource_ref'])
                if native['kind']=='memory':
                    summary=self.repo._one('SELECT v FROM meta WHERE k=?',('memory_summary:'+native['native_id'],))
                    if summary:
                        covered.update(self.catalog.covered_refs(native))
            result=packet()
        if estimate_tokens(result)>budget:
            raise RecallError('budget_requires_page')
        return result

    def _item(self, resource, body):
        ref = resource['resource_id']
        native = self.catalog._native(resource['kind'], resource['native_id'])
        original=self.catalog.body(resource)
        return dict(resource_ref=ref, source_version=resource['source_version'], resource_kind=resource['kind'],source_kind=native.source_kind,
                    learning=self.catalog.learning_view(resource),
                    attribution=self.catalog.attribution(resource),
                    actor_refs=['actor:qq:'+native.actor_id] if resource['kind'] == 'event' and native.source_kind=='user_report' and native.actor_id.isdigit() and native.scope.conversation_id.startswith('qq:') else [],
                    occurred_at=resource['occurred_at'], observed_at=native.received_at if resource['kind'] == 'event' else native.recorded_at,
                    preview=body, preview_complete=body==original,
                    preview_scope='source_record' if body==original else 'current_claims',preview_hash=sha(body),
                    evidence_refs=[e['parent_id'] for e in self.catalog.parents(ref)
                        if e['relation'] != 'published_from'], claim_kind=native.source_kind,
                    read_ref=ref, content_hash=sha(original), status=resource['status'], authority='memory_material')

    def read(self, ctx, ref, *, cursor=None, max_tokens=4000, max_bytes=24000):
        if type(max_tokens) is not int or not 128 <= max_tokens <= 32000 or type(max_bytes) is not int or not 512 <= max_bytes <= 192000:
            raise RecallError('invalid_input')
        fence = self.catalog.fence()
        resource, body = self.access.load(ctx, ref,history=True)
        data = body.encode('utf-8')
        start = 0
        if cursor:
            saved = self._open(cursor, ctx, 'read')
            if saved['ref'] != resource['resource_id'] or saved['version'] != resource['source_version'] or saved['hash'] != sha(body):
                raise RecallError('stale_cursor')
            start = saved['end']
        header = dict(resource_ref=resource['resource_id'], source_version=resource['source_version'],
                      read_scope='historical_record',learning=self.catalog.learning_view(resource),attribution=self.catalog.attribution(resource),
                      resource_kind=resource['kind'],source_kind=self.catalog._native(resource['kind'],resource['native_id']).source_kind,
                      status=resource['status'], content_hash=sha(body),
                      total_utf8_bytes=len(data), occurred_at=resource['occurred_at'], authority='memory_material')
        if resource['kind']=='event':
            event=self.repo.get_event(resource['native_id'])
            header['source_state']={key:event.payload[key] for key in
                ('status','coverage','meaning_kind','execution_state','effect_state','goal_verified') if key in event.payload}
        def packet(end):
            more = end < len(data)
            return dict(header,text=data[start:end].decode('utf-8'),byte_start=start,byte_end=end,
                has_more=more,next_cursor=self._cursor(ctx,'read',ref=resource['resource_id'],
                version=resource['source_version'],hash=sha(body),end=end) if more else None)
        offsets, position = [start], start
        for char in data[start:].decode('utf-8'):
            position += len(char.encode('utf-8'))
            offsets.append(position)
        complete = packet(len(data))
        if len(encode(complete).encode('utf-8')) <= max_bytes and estimate_tokens(complete) <= max_tokens:
            if self.catalog.fence() != fence:
                raise RecallError('stale_source')
            return complete
        lo, hi = 0, len(offsets)-1
        while lo < hi:
            mid = (lo+hi+1)//2
            trial = packet(offsets[mid])
            if len(encode(trial).encode('utf-8')) <= max_bytes and estimate_tokens(trial) <= max_tokens:
                lo = mid
            else:
                hi = mid-1
        end = offsets[lo]
        if end == start and start < len(data):
            raise RecallError('budget_requires_page')
        if self.catalog.fence() != fence:
            raise RecallError('stale_source')
        return packet(end)

    def track_context(self, binding, scope, text):
        if not isinstance(text,str):
            return
        ctx=self.access.bind(scope,session_id=binding['session_id'],generation=binding['generation'])
        for line in text.splitlines():
            try:
                item=json.loads(line)
            except (ValueError,TypeError):
                continue
            if isinstance(item,dict) and item.get('resource_ref'):
                self.access.load(ctx,item['resource_ref'],item.get('source_version'))

    def context(self, scope, cues, budget, *, exclude=()):
        if not any(cues):
            return ''
        ctx = self.access.bind(scope)
        try:
            packet = self.search(ctx, ' '.join(cues), budget=max(0, budget), exclude=exclude)
        except RecallError as error:
            if error.code in {'budget_requires_page','stale_source','not_available'}:
                return ''
            raise
        lines = []
        for item in packet['items']:
            line = encode(item)
            if estimate_tokens('\n'.join(lines+[line])) <= budget:
                lines.append(line)
        return '\n'.join(lines)
