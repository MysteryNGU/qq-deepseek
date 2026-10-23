"""Actual DSH schemas, unique model aliases and scoped, validated execution."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time

from .text import terms, stable_id
from .tool_boundary import check_tool_arguments, outcome_ok


class CapabilityCatalog:
    def __init__(self, hub):
        self.hub = hub
        self.rows = {}
        self.aliases = {}
        self.generation = ''
        self.loaded_at = 0
        self.lock = asyncio.Lock()

    async def refresh(self, *, force=False, scope=None):
        self.hub._bridge_loop = asyncio.get_running_loop()
        async with self.lock:
            bridge = self.hub.dsh_bridge
            if bridge is None:
                return []
            generation = str(getattr(bridge, '_generation', ''))
            if self.rows and not force and time.monotonic() - self.loaded_at < 60 and generation == self.generation:
                return list(self.rows.values())
            rows = await asyncio.to_thread(lambda: bridge.tools(scope=scope))
            values = {}
            aliases = {}
            for row in rows:
                fn = row.get('function', row)
                name = fn.get('name')
                schema = fn.get('parameters')
                if not isinstance(name, str) or not isinstance(schema, dict):
                    continue
                alias = 'dsh__' + re.sub('[^a-zA-Z0-9_-]', '_', name)[:39] + '_' + hashlib.sha256(name.encode()).hexdigest()[:8]
                if alias in aliases:
                    raise ValueError('duplicate_dsh_alias')
                aliases[alias] = name
                values[name] = {'name': name, 'description': fn.get('description', ''), 'parameters': schema, 'alias': alias}
            self.rows, self.aliases = values, aliases
            self.generation = str(getattr(bridge, '_generation', ''))
            self.loaded_at = time.monotonic()
            return list(values.values())

    def select(self, query, limit=8, offset=0):
        q = set(terms(query))
        ranked = sorted(self.rows.values(), key=lambda r: (
            -len(q & set(terms(r['name'] + ' ' + r['description']))),
            r['name'] not in {'read', 'web_search', 'web_fetch', 'shell', 'grep', 'glob'}, r['name']))
        offset = max(0, int(offset))
        return ranked[offset:offset + max(1, min(16, limit))]

    def schemas(self, query):
        return [{'type': 'function', 'function': {'name': r['alias'], 'description': r['description'],
                    'parameters': r['parameters']}} for r in self.select(query)]

    def validate(self, name, arguments):
        from jsonschema import Draft202012Validator
        real = self.aliases.get(name, name)
        row = self.rows.get(real)
        if row is None:
            raise ValueError('工具未发现或generation失效，请先find_capabilities')
        Draft202012Validator(row['parameters']).validate(arguments)
        check_tool_arguments(real, arguments)
        return real

    async def call(self, task, step_id, name, arguments, operation_id, current=None):
        real = self.validate(name, arguments)
        bridge = self.hub.dsh_bridge
        if bridge is None:
            raise ValueError('DSH unavailable')
        await asyncio.to_thread(bridge._ensure_started)
        wire_scope = stable_id('qqexec', task['scope_id'], task['ref'], str(bridge._generation))
        if current is not None and not current():
            from .services.dsh_bridge import request_hash
            snap = {'operationId': operation_id, 'settled': True, 'status': 'cancelled', 'error': 'task changed before dispatch'}
            bridge._ledger_put(operation_id, real, arguments, request_hash(real, arguments), 'cancelled',
                scope=wire_scope, plan_id=task['ref'], step_id=step_id)
            bridge._ledger_update(operation_id, 'cancelled', snap=snap)
            return snap
        return await asyncio.to_thread(lambda: bridge.execute(real, arguments, operation_id=operation_id,
            scope=wire_scope, plan_id=task['ref'], step_id=step_id))


def manager_value(snapshot):
    result = snapshot.get('result') or {}
    value = result.get('value')
    if isinstance(value, dict) and ('application' in value or 'entries' in value):
        return value
    for block in result.get('content') or []:
        if block.get('type') == 'text':
            try:
                value = json.loads(block.get('text', ''))
                if isinstance(value, dict):
                    return value
            except (TypeError, ValueError):
                pass
    return {}
