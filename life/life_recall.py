"""One permission set for exact, lexical, semantic and evidence-related recall."""
from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import json
import secrets
import logging

from .life_access import RecallError
from .life_catalog import encode, sha
from .resource_policy import estimate_tokens
from .text import terms
import os

log = logging.getLogger(__name__)


def _route_report(mode, coverage, reasons, items):
    """Unified per-route report (amendment section 5 statistics contract).

    status: skipped_mode / unavailable / success / partial.
    timeout and error are recorded by the caller on the exception path, not invented here.
    """
    coverage = coverage or {}
    reasons = reasons or {}
    contributed = {}
    returned = {}
    for _ref, routes in reasons.items():
        for route in (routes or ()):
            contributed[route] = contributed.get(route, 0) + 1
    for item in (items or ()):
        for route in (item.get('match_routes') or ()):
            returned[route] = returned.get(route, 0) + 1
    report = {}
    for route in ('text', 'semantic', 'related', 'exact'):
        if route == 'exact':
            attempted = bool(contributed.get('exact'))
            state = 'success' if attempted else 'unavailable'
        else:
            state = coverage.get(route)
            if state is None:
                state = 'unavailable' if route == 'text' else 'skipped_mode'
            attempted = state not in ('unavailable', 'skipped_mode')
        report[route] = dict(attempted=bool(attempted), status=state,
                             candidate_count=contributed.get(route, 0),
                             returned_count=returned.get(route, 0))
    report['mode'] = mode
    return report


_RECALL_MODES = ('auto', 'text', 'semantic', 'related')

# Whether context() skips the paging cursor. Default on;
# set RECALL_CONTEXT_CURSORLESS=0 to turn only this path off.
CONTEXT_USE_CURSORLESS = os.environ.get('RECALL_CONTEXT_CURSORLESS', '1') != '0'


def resolve_mode(mode=None, *, ignore_override=False):
    """把一次召回请求的 mode 规范化成一个确定值。

    优先级：环境变量 RECALL_MODE_OVERRIDE（生产 override）> 显式 mode > 'auto'。
    search_async 和 search 都从这里取，保证"要不要生成 query vector"和
    "实际走哪几条路线"用的是同一个值。此前前者看入口默认、后者看环境变量，会打架：
    _prefetch_recall 显式传 mode='auto' 时，环境变量已改成 text，但向量照样算一遍。

    ignore_override=True 只给测试和控制台用：显式绕过生产 override，
    而不是靠传一个会被覆盖的 mode 去隐式绕过。
    """
    override = os.environ.get('RECALL_MODE_OVERRIDE') if not ignore_override else ''
    if override:
        if override in _RECALL_MODES:
            if mode is not None and mode != override:
                log.warning('召回 mode 被生产 override 改写：请求=%s 生效=%s', mode, override)
            mode = override
        else:
            log.warning('忽略无法识别的 RECALL_MODE_OVERRIDE=%r', override)
    if mode is None:
        mode = 'auto'
    return mode


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

    def allowed(self, ctx, *, high_water=None, actor_refs=(), time_range=None, lexical_terms=None, exact=(), candidate_refs=None, _cache=None):
        self.access.require_binding(ctx)
        # One scan memo per recall: candidates share ancestors, and re-validating
        # them per candidate is what made a recall take minutes.
        cache = {} if _cache is None else _cache
        query = 'SELECT rowid,* FROM life_resources WHERE agent_id=?'
        args = [ctx.agent_id]
        if high_water is not None:
            query += ' AND rowid<=?'
            args.append(high_water)
        if candidate_refs is not None:
            query += ' AND resource_id IN (SELECT value FROM json_each(?))'
            args.append(json.dumps(list(candidate_refs)))
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
                _,current_body=self.access.load(ctx, resource['resource_id'], resource['source_version'],track=False,_cache=cache)
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
        # 2026-11-19：先解析出生效的 mode，再决定要不要算 query vector。
        # 此前这里看的是入口默认值、而 search() 走环境变量，text 模式下会白算一次 embedding。
        mode = resolve_mode(options.get('mode'),
                            ignore_override=bool(options.pop('ignore_mode_override', False)))
        options['mode'] = mode
        if mode in {'auto', 'semantic'} and self.index.adapter and not options.get('cursor'):
            try:
                vector = await self.index.query_vector(ctx, query)
            except (RecallError, TimeoutError, ConnectionError):
                pass
        options['_mode_resolved'] = True
        return await asyncio.to_thread(self.search,ctx,query,query_vector=vector,**options)

    def search(self, ctx, query='', *, mode=None, budget=2400, limit=10, cursor=None,
               actor_refs=(), entity_refs=(), time_range=None, exact_refs=(), query_vector=None, exclude=(),
               intent='auto', diagnostics=False, include_cursor=True, _mode_resolved=False):
        # 2026-11-19：mode 解析集中到 resolve_mode()，与 search_async 共用同一个入口，
        # 避免"算不算向量"和"走哪几条路线"读到不同值。
        # _mode_resolved=True 表示上层（search_async）已经解析过并包含诊断绕过标志，
        # 这里不能再解析一次——否则环境变量二次覆盖，ignore_mode_override 就失效。
        if not _mode_resolved:
            mode = resolve_mode(mode)
        if (not isinstance(query, str) or len(query) > 2000 or mode not in {'auto', 'text', 'semantic', 'related'}
                or type(budget) is not int or not 0 <= budget <= 32000
                or type(limit) is not int or not 1 <= limit <= 40):
            raise RecallError('invalid_input')
        if intent not in {'auto', 'facts', 'recent', 'history'} or type(diagnostics) is not bool:
            raise RecallError('invalid_input')
        # 修订版 §4：非分页路径不接受 cursor——它不生成游标，也没有可续的页。
        if not include_cursor and cursor:
            raise RecallError('invalid_input')
        self.access.require_binding(ctx)
        signature = sha(encode([query, mode, list(actor_refs), list(entity_refs), time_range, list(exact_refs), list(exclude), intent, diagnostics]))
        fence = self.catalog.fence()
        coverage = dict(text='ready', semantic='unavailable', related='ready')
        if cursor:
            saved = self._open(cursor, ctx, 'recall')
            if saved['query'] != signature or saved['index_version'] != [self.index.model_key, self.index.chunker_version]:
                raise RecallError('stale_cursor')
            ranked, versions, coverage, high_water = saved['ranked'], saved['versions'], saved['coverage'], saved['high_water']
            previous_coverage = set(saved.get('covered',[]))
            reasons = saved.get('reasons', {})
        else:
            previous_coverage = set()
            scan_cache = {}
            high_water = self.repo._one('SELECT COALESCE(MAX(rowid),0) AS n FROM life_resources WHERE agent_id=?', (ctx.agent_id,))['n']
            qterms = set(terms(query))
            exact_ids = []
            for ref in exact_refs:
                resource = self.catalog.resolve(ref, _cache=scan_cache)
                if resource:
                    exact_ids.append(resource['resource_id'])
            candidate_refs=None
            if query_vector is not None and qterms and not entity_refs and not actor_refs:
                # Only these resources can participate in a text/vector route.
                # This is candidate selection, never an authorization shortcut.
                candidate_refs=set(exact_ids)
                candidate_refs.update(r['resource_id'] for r in self.repo._all(
                    "SELECT DISTINCT i.resource_id FROM life_semantic_index i JOIN life_resources r ON r.resource_id=i.resource_id "
                    "AND r.source_version=i.source_version WHERE i.agent_id=? AND i.model_key=? AND i.chunker_version=? "
                    "AND i.state='ready'",(ctx.agent_id,self.index.model_key,self.index.chunker_version)))
                candidate_refs.update(r['resource_id'] for r in self.repo._all(
                    'SELECT DISTINCT resource_id FROM life_terms WHERE agent_id=? AND term IN (SELECT value FROM json_each(?))',
                    (ctx.agent_id,json.dumps(sorted(qterms)))))
            allowed = self.allowed(ctx, high_water=high_water, actor_refs=actor_refs, time_range=time_range,
                lexical_terms=qterms if query_vector is None and not entity_refs and not actor_refs else None,exact=exact_ids,
                candidate_refs=candidate_refs, _cache=scan_cache)
            by_id = {r['resource_id']: r for r in allowed}
            lexical = {}
            for resource in allowed:
                ref = resource['resource_id']
                stored = {r['term'] for r in self.repo._all('SELECT term FROM life_terms WHERE resource_id=? AND source_version=?', (ref, resource['source_version']))}
                hits = len(qterms & stored)
                if hits or not query:
                    lexical[ref] = hits
            text_route = sorted(lexical, key=lambda ref: (-lexical[ref], -by_id[ref]['occurred_at'], ref))[:self.index.config.route_limit]
            # 2026-11-23（修订版 §5）：只有这次真的算出了 query vector 才查向量。
            # 否则显式跳过：不调用、不发 SQL，status 记 skipped_mode 而不是 unavailable——
            # unavailable 的语义是"走向量了但后端不可用"，把这个模式的选择记成故障会污染统计。
            if query_vector is not None:
                semantic, coverage['semantic'] = self.index.search_vectors(allowed, query_vector)
            else:
                semantic, coverage['semantic'] = [], 'skipped_mode'
            if candidate_refs is not None and coverage['semantic']=='ready':
                omitted=self.repo._one('SELECT 1 FROM life_resources WHERE agent_id=? AND rowid<=? '
                    'AND resource_id NOT IN (SELECT value FROM json_each(?)) LIMIT 1',
                    (ctx.agent_id,high_water,json.dumps(list(candidate_refs))))
                if omitted:
                    coverage['semantic']='partial'  # Omitted records are not certified covered.
            related = self.index.find_related(allowed, [*entity_refs, *actor_refs])
            routes = [text_route, semantic, related] if mode == 'auto' else (
                [related, text_route] if mode == 'related' else [semantic, text_route] if mode == 'semantic' else [text_route])
            scores = {}
            reasons = {}
            route_names = ['text', 'semantic', 'related'] if mode == 'auto' else (
                ['related', 'text'] if mode == 'related' else ['semantic', 'text'] if mode == 'semantic' else ['text'])
            for route_name, route in zip(route_names, routes):
                for rank, ref in enumerate(route, 1):
                    scores[ref] = scores.get(ref, 0)+1/(self.index.config.rrf_k+rank)
                    reasons.setdefault(ref, []).append(route_name)
            def rank_key(ref):
                resource = by_id[ref]
                fact = self.repo.get_memory(resource['native_id']) if resource['kind'] == 'memory' else None
                preferred = bool(intent == 'facts' and fact and fact.kind == 'fact' and fact.status == 'active')
                return (-int(preferred), -resource['occurred_at'] if intent == 'recent' else -scores[ref],
                        -scores[ref], -resource['occurred_at'], ref)
            exact = []
            for ref in exact_refs:
                resource = self.catalog.resolve(ref)
                if resource and resource['resource_id'] in by_id:
                    exact.append(resource['resource_id'])
            ranked = list(dict.fromkeys(exact + sorted(scores, key=rank_key)))
            for ref in exact:
                reasons.setdefault(ref, []).append('exact')
            ranked = [ref for ref in ranked if ref not in exclude and by_id[ref]['native_id'] not in exclude]
            versions = {ref: by_id[ref]['source_version'] for ref in ranked}
        items, remaining, covered = [], [], set(previous_coverage)
        for pos, ref in enumerate(ranked):
            # 2026-11-20：把本轮扫描的 memo 一起传下去。此前这里不带 _cache，
            # load → allowed → _dependencies → catalog.live 会重新递归整条依赖链，
            # 每次都重跑 _describe（其中有两处 DB 查询）。实测 context('热液')
            # 2630 次 live() 里 83% 不带缓存；传下去后 1355ms → 521ms。
            # 安全性已逐条验证：17291 条资源 × audience/internal，allowed 结果完全一致。
            resource, body = self.access.load(ctx, ref, versions[ref], _cache=scan_cache)
            # Root evidence overlap is only compressed if a valid summary
            # explicitly declares that source covered (not merely in its range).
            if ref in covered and not exact_refs:
                continue
            item = self._item(resource, body, ctx)
            if diagnostics:
                item['match_routes'] = reasons.get(ref, [])
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
            # include_cursor=False: do not build the paging cursor. The empty-result
            # skeleton measures 1154 tokens and almost all of it is this signed
            # base64 cursor; callers that only want text must not pay for it.
            next_cursor = None
            if include_cursor and pending:
                next_cursor = self._cursor(ctx,'recall',query=signature,ranked=pending,
                    versions={r:versions[r] for r in pending},coverage=coverage,high_water=high_water,
                    covered=sorted(covered),reasons={r:reasons.get(r, []) for r in pending},
                    index_version=[self.index.model_key,self.index.chunker_version])
            result=dict(items=items,coverage=coverage,next_cursor=next_cursor,has_more=bool(pending),budget_truncated=bool(pending))
            if diagnostics:
                result['diagnostics'] = dict(intent=intent, returned=len(items), remaining=len(pending),
                    visibility='current_audience_only', empty_reason='no_visible_match' if not ranked else None,
                    history='superseded_records_require_exact_source_read',
                    # 2026-11-19 (design section 5): expose per-route counts for the recall record.
                    # Additive only; contains no query text and no private body.
                    mode=mode, candidates=len(ranked), authorized=len(ranked),
                    # Legacy keys kept for compatibility, but their meaning differs:
                    # route_counts maps resource_ref -> number of routes that matched it.
                    route_counts={k: len(v) for k, v in (reasons or {}).items()},
                    route_status=dict(coverage),
                    # Amendment section 5: one unified per-route object.
                    routes=_route_report(mode, coverage, reasons, items))
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

    def _item(self, resource, body, ctx=None):
        ref = resource['resource_id']
        native = self.catalog._native(resource['kind'], resource['native_id'])
        original=self.catalog.body(resource)
        result = dict(resource_ref=ref, source_version=resource['source_version'], resource_kind=resource['kind'],source_kind=native.source_kind,
                    learning=self.catalog.learning_view(resource),
                    attribution=self.catalog.attribution(resource),
                    actor_refs=['actor:qq:'+native.actor_id] if resource['kind'] == 'event' and native.source_kind=='user_report' and native.actor_id.isdigit() and native.scope.conversation_id.startswith('qq:') else [],
                    occurred_at=resource['occurred_at'], observed_at=native.received_at if resource['kind'] == 'event' else native.recorded_at,
                    preview=body, preview_complete=body==original,
                    preview_scope='source_record' if body==original else 'current_claims',preview_hash=sha(body),
                    evidence_refs=[e['parent_id'] for e in self.catalog.parents(ref)
                        if e['relation'] != 'published_from'], claim_kind=native.source_kind,
                    read_ref=ref, content_hash=sha(original), status=resource['status'], authority='memory_material')
        if resource['kind'] == 'memory' and native.kind == 'fact':
            result['fact'] = dict(actor_id=native.subject_id, topic_key=native.predicate,
                                  value=native.normalized_value, status=native.status)
            if ctx and native.supersedes_id:
                try:
                    previous, _ = self.access.load(ctx, native.supersedes_id, history=True, track=False)
                    result['fact']['previous_ref'] = previous['resource_id']
                except RecallError:
                    pass
        return result

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
            # Text only: do not pay for the paging cursor (see search's include_cursor).
            # RECALL_CONTEXT_CURSORLESS=0 restores the old path without reverting code.
            packet = self.search(ctx, ' '.join(cues), budget=max(0, budget), exclude=exclude,
                                 include_cursor=CONTEXT_USE_CURSORLESS)
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
