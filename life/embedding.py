"""Opt-in embedding HTTP adapter with durable admission and no implicit retry."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields
import json
import os
from urllib.parse import urlsplit
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


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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
        fence = self.life.catalog.fence()
        with self.repo.tx():
            generation=0
            previous = self.repo._one('SELECT v FROM meta WHERE k=?', (state_key,))
            if previous:
                saved = json.loads(previous['v'])
                if saved['fingerprint'] != fingerprint:
                    raise RecallError('idempotency_conflict')
                if saved.get('fence') != fence:
                    raise RecallError('stale_source')
                if saved['state']=='completed' and saved.get('result') and saved['expires']>self.life.clock():
                    return saved['result']
                if saved['state'] not in {'completed','evicted'}:
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
            except SessionFailure:
                raise RecallError('embedding_budget_unavailable') from None
            saved = dict(fingerprint=fingerprint, fence=fence, state='submitted', sources=source_refs,generation=generation,billing_key=billing_key,
                         created=self.life.clock(), expires=self.life.clock()+3600)
            self.repo._exec('INSERT OR REPLACE INTO meta VALUES(?,?)', (state_key, encode(saved)))
        try:
            # Verify source epochs once more immediately before external I/O.
            if self.life.catalog.fence() != fence:
                raise RecallError('stale_source')
            for ref in source_refs:
                self.life.life_access.load(self.life.life_access.bind(self.life.scope('__memory_internal__'), mode='internal'), ref)
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
                if self.life.catalog.fence() != fence or not current:
                    raise RecallError('stale_source')
                saved.update(state='completed', result=result)
                self.repo._exec('UPDATE meta SET v=? WHERE k=?', (encode(saved), state_key))
            self.verified = True
            return result
        except asyncio.CancelledError:
            raise
        except Exception as error:
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
