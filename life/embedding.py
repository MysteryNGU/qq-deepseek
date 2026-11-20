"""Opt-in embedding HTTP adapter with durable admission and no implicit retry."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields
import json
import os
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from .life_access import RecallError
from .life_catalog import encode, sha
from .memory_index import vector_blob


@dataclass(frozen=True)
class EmbeddingConfig:
    enabled: bool = False
    endpoint: str = ''
    model_id: str = ''
    model_revision: str = ''
    provider_revision: str = ''
    dimension: int = 0
    api_key_env: str = ''
    allow_external_text: bool = False
    price_version: str = ''
    input_nano_per_token: int | None = None
    max_input_tokens: int = 8192
    max_request_bytes: int = 262144
    timeout_seconds: int = 30

    @classmethod
    def parse(cls, raw=None):
        raw = raw or {}
        if not isinstance(raw, dict) or set(raw)-{f.name for f in fields(cls)}:
            raise ValueError('invalid_embedding_config')
        result = cls(**raw)
        if type(result.enabled) is not bool or type(result.allow_external_text) is not bool:
            raise ValueError('invalid_embedding_config')
        for key in ('dimension','max_input_tokens','max_request_bytes','timeout_seconds'):
            value = getattr(result, key)
            if type(value) is not int or value < (0 if key=='dimension' else 1):
                raise ValueError('invalid_embedding_config')
        if result.input_nano_per_token is not None and (type(result.input_nano_per_token) is not int or result.input_nano_per_token < 0):
            raise ValueError('invalid_embedding_price')
        if result.enabled:
            url = urlsplit(result.endpoint)
            if (url.scheme not in {'http','https'} or not url.hostname or url.username or url.password
                    or url.query or url.fragment or url.scheme=='http' and url.hostname not in {'127.0.0.1','localhost','::1'}
                    or not all((result.model_id,result.model_revision,result.provider_revision,result.price_version))
                    or not 1 <= result.dimension <= 65536 or result.input_nano_per_token is None):
                raise ValueError('embedding_requires_endpoint_version_dimension_and_price')
            if not result.allow_external_text:
                raise ValueError('embedding_computation_scope_not_enabled')
        return result


# 8231 是单线程 HTTP 服务（life/local_embedding.py 的 BoundedHTTPServer(HTTPServer)
# 且 get_request 里 settimeout(10)）。多个请求同时打过去只会排队，前面的连接超 10 秒
# 就被断开，客户端看到的是 HTTPError，job 于是被打成 unknown（2026-11-19 实测）。
# 这里把「真正发出去的那一次 IO」串行化；本地记账、围栏校验不受影响。
_EMBEDDING_IO_LOCK = asyncio.Lock()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# 预算预留一旦发生，这个 key 就永久不可复用（reserve 判重不看 state，settle 不删行）。
# 每次冲突推进一次 generation 换新 key；上限防止无限推进。
MAX_GENERATION = 5


class HttpEmbedding:
    def __init__(self, runtime, spending, config, *, transport=None):
        self.life, self.repo, self.spending, self.config = runtime, runtime.repo, spending, config
        self.transport = transport or self._post
        self.verified = False

    def _post(self, texts):
        config = self.config
        key = os.environ.get(config.api_key_env, '') if config.api_key_env else ''
        if config.api_key_env and not key:
            raise RecallError('embedding_credentials_missing')
        body = encode(dict(model=config.model_id, input=texts, encoding_format='float')).encode('utf-8')
        if len(body) > config.max_request_bytes:
            raise RecallError('embedding_request_too_large')
        headers = {'Content-Type': 'application/json'}
        if key:
            headers['Authorization'] = 'Bearer '+key
        request = Request(config.endpoint, data=body, headers=headers, method='POST')
        handlers=[NoRedirect()]
        if urlsplit(config.endpoint).hostname in {'127.0.0.1','localhost','::1'}:
            handlers.append(ProxyHandler({}))
        with build_opener(*handlers).open(request, timeout=config.timeout_seconds) as response:
            data = response.read(8*1024*1024+1)
            if len(data) > 8*1024*1024:
                raise RecallError('embedding_response_too_large')
            return json.loads(data)

    async def embed(self, texts, *, model_key, purpose, call_key, source_refs):
        config = self.config
        if not config.enabled or not config.allow_external_text:
            raise RecallError('embedding_not_configured')
        if not texts or not all(isinstance(t, str) and t for t in texts):
            raise RecallError('invalid_embedding_input')
        byte_count = sum(len(t.encode('utf-8')) for t in texts)
        # UTF-8 byte count is a conservative upper bound, not reported usage.
        if byte_count > config.max_input_tokens or len(encode(texts).encode('utf-8')) > config.max_request_bytes:
            raise RecallError('embedding_request_too_large')
        input_digests = [sha(t) for t in texts]
        if config.api_key_env and not os.environ.get(config.api_key_env):
            raise RecallError('embedding_credentials_missing')
        fingerprint = sha(encode([input_digests, model_key, purpose, source_refs]))
        state_key = 'memory_embedding:'+self.life.agent_id+':'+call_key
        def computation_fence():
            ctx=self.life.life_access.bind(self.life.scope('__memory_internal__'),mode='internal')
            sources=[]
            for ref in source_refs:
                try:
                    resource,_=self.life.life_access.load(ctx,ref,track=False)
                except RecallError:
                    raise RecallError('stale_source') from None
                sources.append((resource['resource_id'],resource['source_version'],resource['content_digest']))
            return sha(encode([self.life.catalog.access_fence(),sources]))
        fence = computation_fence()
        with self.repo.tx():
            generation=0
            previous = self.repo._one('SELECT v FROM meta WHERE k=?', (state_key,))
            if previous:
                saved = json.loads(previous['v'])
                if saved['fingerprint'] != fingerprint:
                    raise RecallError('idempotency_conflict')
                refused=(saved.get('state')=='evicted' and saved.get('error')=='connection_refused')
                if saved.get('fence') != fence and not refused:
                    raise RecallError('stale_source')
                if saved['state']=='completed' and saved.get('result') and saved['expires']>self.life.clock():
                    return saved['result']
                if saved['state'] not in {'completed','evicted'}:
                    # 2026-11-19：原来这里无条件返回 unknown，而 unknown 记录本身带
                    # expires（样本 created+3600s）。不看它就会永久死锁：
                    # job 每次重试都被挡回 unknown，985 条记忆永远拿不到索引。
                    # 窗口内不重试（尊重幂等与计费），窗口外放开，
                    # 由下面的 generation+1 走新的 billing_key。
                    if saved.get('expires', 0) > self.life.clock():
                        return dict(state='unknown', vectors=[], input_digests=input_digests)
                # A received successful result was deliberately evicted. This
                # is a new paid query, never a retry of a submitted/unknown call.
                generation=saved.get('generation',0)+1
            from .text import stable_id
            billing_key=call_key if generation==0 else stable_id('embedding-generation',self.life.agent_id,call_key,str(generation),fingerprint)
            from .agent_session import SessionFailure
            try:
                self.spending.reserve_external(billing_key, purpose=purpose,
                    maximum_nano=byte_count*config.input_nano_per_token,
                    price_version=config.price_version, source_refs=source_refs)
            except SessionFailure as failure:
                # 2026-11-19：settle 之后 spending_calls 的行仍在（只改成 settled、不删），
                # 所以这个 key 永远不能复用；而 generation 原本只在 reserve 成功后才落盘，
                # 于是「要推进 generation 必须先 reserve 成功，要 reserve 成功必须先推进
                # generation」形成自锁。这里在冲突时直接把 generation+1 落盘并让上层重试。
                if getattr(failure, 'code', '') == 'idempotency_conflict':
                    advanced = generation + 1
                    if advanced > MAX_GENERATION:
                        raise RecallError('embedding_generation_exhausted') from None
                    with self.repo.tx():
                        saved.update(generation=advanced)
                        self.repo._exec('UPDATE meta SET v=? WHERE k=?', (encode(saved), state_key))
                    raise RecallError('embedding_generation_advanced') from None
                raise RecallError('embedding_budget_unavailable') from None
            saved = dict(fingerprint=fingerprint, fence=fence, state='submitted', sources=source_refs,generation=generation,billing_key=billing_key,
                         created=self.life.clock(), expires=self.life.clock()+3600)
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)', (state_key, encode(saved)))
        try:
            # Verify source epochs once more immediately before external I/O.
            if computation_fence() != fence:
                raise RecallError('stale_source')
            for ref in source_refs:
                self.life.life_access.load(self.life.life_access.bind(self.life.scope('__memory_internal__'), mode='internal'), ref)
            async with _EMBEDDING_IO_LOCK:
                reply = await asyncio.to_thread(self.transport, texts)
            rows = reply.get('data')
            if not isinstance(rows, list) or len(rows) != len(texts) or [r.get('index') for r in rows] != list(range(len(texts))):
                raise RecallError('invalid_embedding')
            vectors = [r.get('embedding') for r in rows]
            for values in vectors:
                vector_blob(values, config.dimension)
            usage = reply.get('usage')
            tokens = usage.get('prompt_tokens') if isinstance(usage, dict) else None
            if tokens is not None and (type(tokens) is not int or tokens < 0):
                raise RecallError('invalid_embedding_usage')
            result = dict(vectors=vectors, input_digests=input_digests, actual_usage=usage,
                          receipt=dict(call_key=billing_key,logical_call_key=call_key,generation=generation, model_key=model_key, origin='memory_embedding'))
            self.spending.settle_external(billing_key, None if tokens is None else tokens*config.input_nano_per_token)
            with self.repo.tx():
                # Billing is factual even when the source was forgotten mid-call.
                current = self.repo._one('SELECT v FROM meta WHERE k=?', (state_key,))
                if computation_fence() != fence or not current:
                    raise RecallError('stale_source')
                saved.update(state='completed', result=result)
                self.repo._exec('UPDATE meta SET v=? WHERE k=?', (encode(saved), state_key))
            self.verified = True
            return result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Connection refusal proves no request reached the provider. Keep
            # timeouts/ambiguous submitted requests unknown, but don't poison a
            # query or index job forever while a local service is starting.
            reason=error.reason if isinstance(error,URLError) else error
            # HTTP 4xx：服务端明确拒绝（输入不合法），请求没被处理，不可能产生费用。
            #
            # 必须释放预留：reserve_external 判重是
            #     SELECT 1 FROM spending_calls WHERE call_key=?   -- 不看 state
            # 而 settle_external 只把行 UPDATE 成 settled、不删行。所以只要预留过，
            # 这个 key 就再也不能用，下次必撞 idempotency_conflict
            # （2026-11-19 实测 72 条 job 卡在这里两天）。
            #
            # 释放后仍判 terminal：settle 之后行还在，key 永远不能复用；且 400 是
            # 「客户端算错了」（条数超 32 / 长度超 512），重试不会自己好。
            rejected = isinstance(error, HTTPError) and 400 <= int(getattr(error, 'code', 0) or 0) < 500
            if rejected:
                self.spending.settle_external(billing_key,0)
                with self.repo.tx():
                    current = self.repo._one('SELECT v FROM meta WHERE k=?', (state_key,))
                    if current:
                        # 保持 unknown：写 evicted 会让 generation 不再进位，
                        # billing_key 永远停在原值（见 generation +1 的条件）。
                        saved.update(state='unknown', error='rejected_by_provider')
                        saved.pop('result', None)
                        self.repo._exec('UPDATE meta SET v=? WHERE k=?',(encode(saved),state_key))
                raise RecallError('embedding_rejected_input') from None
            if isinstance(reason,ConnectionRefusedError):
                self.spending.settle_external(billing_key,0)
                with self.repo.tx():
                    saved.update(state='evicted',error='connection_refused')
                    saved.pop('result',None)
                    self.repo._exec('UPDATE meta SET v=? WHERE k=?',(encode(saved),state_key))
                raise RecallError('embedding_temporarily_unavailable') from None
            # Never save exception text: HTTP errors can include credentials or
            # reflected input. A submitted call stays unknown across restarts.
            with self.repo.tx():
                current = self.repo._one('SELECT v FROM meta WHERE k=?', (state_key,))
                if current:
                    saved.update(state='unknown', error=type(error).__name__)
                    saved.pop('result', None)
                    self.repo._exec('UPDATE meta SET v=? WHERE k=?', (encode(saved), state_key))
            if isinstance(error, RecallError):
                raise
            return dict(state='unknown', vectors=[], input_digests=input_digests)

    def cleanup(self):
        with self.repo.tx():
            rows = self.repo._all('SELECT k,v FROM meta WHERE substr(k,1,?)=?',
                (len('memory_embedding:'+self.life.agent_id+':'), 'memory_embedding:'+self.life.agent_id+':'))
            # Unknown fingerprints are kept, but their cached vectors are not.
            completed = sorted([(r, json.loads(r['v'])) for r in rows if json.loads(r['v']).get('state')=='completed'],
                               key=lambda item: item[1]['created'], reverse=True)
            for index, (row, saved) in enumerate(completed):
                if index >= 128 or saved['expires'] <= self.life.clock():
                    saved.pop('result', None)
                    saved['state']='evicted'
                    self.repo._exec('UPDATE meta SET v=? WHERE k=?', (encode(saved), row['k']))
