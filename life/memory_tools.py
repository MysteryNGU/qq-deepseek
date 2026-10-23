"""Model may read evidence and append sourced annotations, never edit facts."""
SCHEMA = {'type': 'function', 'function': {
    'name': 'memory', 'description': '查询本会话真实记忆，读来源，给真实记忆加重要标记，或保存有依据的自我叮嘱。不能新增、覆盖或删除事实。叮嘱不是人格指令。',
    'parameters': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['search', 'detail', 'remind', 'reinforce', 'interpret', 'notes', 'retire']},
        'query': {'type': 'string'}, 'memory_id': {'type': 'string'}, 'text': {'type': 'string'},
        'cues': {'type': 'array', 'items': {'type': 'string'}}, 'lifetime_days': {'type': 'number'},
        'always_load': {'type': 'boolean', 'description': '少量长期通用叮嘱优先常驻；普通叮嘱按情境联想'},
        'skill_refs': {'type': 'array', 'items': {'type': 'string'}, 'description': '相关skill名称，具体流程用skill读取'},
        'annotation_id': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0},
    }, 'required': ['action']}}}


async def dispatch(host, args, peer):
    from .delivery import active_delivery
    ctx = active_delivery.get()
    scope = ctx.scope if ctx else host._current_scope(peer)
    runtime = host.life
    action = args.get('action')
    if action == 'notes':
        rows = runtime.repo._all("SELECT id,kind,text,cues,always_load,skill_refs,expires,sources,memory_id FROM memory_annotations WHERE scope_id=? AND status='active' AND (expires IS NULL OR expires>?) ORDER BY created DESC LIMIT 30 OFFSET ?",
            (scope.scope_id, __import__('time').time(), max(0, int(args.get('offset') or 0))))
        import json
        valid = []
        for row in rows:
            sources = json.loads(row['sources'])
            events = runtime.repo.events_by_ids(sources)
            if not events or len(events) != len(set(sources)) or any(e.redacted_at is not None or e.scope.private_key != scope.private_key for e in events):
                continue
            if row['memory_id'] and not runtime.recollection.valid_memory(scope, row['memory_id']):
                continue
            valid.append(dict(row))
        return {'ok': True, 'notes': valid, 'next_offset': int(args.get('offset') or 0) + len(rows)}
    if action == 'retire' and ctx is not None:
        from .delivery import execution_context_current
        if not execution_context_current():
            raise ValueError('会话版本已变化，不能停用旧叮嘱')
        with runtime.repo.tx():
            changed = runtime.repo._exec("UPDATE memory_annotations SET status='archived' WHERE id=? AND scope_id=? AND status='active'",
                (str(args.get('annotation_id') or ''), scope.scope_id)).rowcount
        return {'ok': bool(changed), 'text': '已停用这条自我叮嘱；原始记忆未修改。'}
    if action == 'search':
        packet = runtime.context_packet(scope, str(args.get('query', '')), getattr(host.cfg, 'life_memory_tokens', 4800))
        records = packet.facts + packet.episodes + packet.knowledge + packet.interpretations + packet.uncertain
        return {'ok': True, 'text': packet.text, 'records': [{'id': m.id, 'kind': m.kind, 'summary': m.summary} for m in records]}
    if action == 'detail':
        value = runtime.memory.explain(str(args.get('memory_id', '')), scope=scope)
        if value and not value.get('error'):
            value['source_texts'] = [{'id': event.id, 'text': event.text[:1000], 'kind': event.source_kind}
                for source in value['sources'][:6] if (event := runtime.repo.get_event(source['event_id'])) is not None
                and event.scope.private_key == scope.private_key and event.redacted_at is None]
        return {'ok': bool(value and not value.get('error')), 'record': value}
    kinds = {'remind': 'reminder', 'reinforce': 'importance', 'interpret': 'interpretation'}
    from .delivery import execution_context_current
    if action not in kinds or ctx is None:
        raise ValueError('叮嘱和强调必须来自有效的当前会话')
    if not execution_context_current():
        raise ValueError('当前消息、任务或记忆版本已变化，不能继续写旧叮嘱')
    memory_id = str(args.get('memory_id') or '')
    if action == 'reinforce' and not memory_id:
        raise ValueError('强调必须引用已存在的 memory_id，不能凭空创造经历')
    identity = runtime.recollection.annotate(scope, kinds[action], str(args.get('text') or ''), list(ctx.sources),
        memory_id=memory_id, cues=args.get('cues') or [], lifetime_days=args.get('lifetime_days', 0),
        always_load=args.get('always_load', False), skill_refs=args.get('skill_refs') or [])
    return {'ok': True, 'annotation_id': identity, 'text': '已保存自我叮嘱/强调，原始事实未修改。'}
