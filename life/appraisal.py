#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结构化事件评估的校验层。

模型只负责提出 JSON；这里负责：
- 来源 ID 必须真实存在于本批次；
- 枚举、数值范围、原因引用由代码校验；
- 引用、否定和想象保留归属；语义由主角色判断，不使用关键词折扣。
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from .contracts import EMOTIONS, Appraisal, Scope, clamp01, new_id
from .drives import EFFECT_KINDS, canonical_dimension

ALLOWED_EVENT_TYPES = {
    "ordinary", "praise", "correction", "failure", "repair", "request",
    "commitment", "boundary", "shared_success", "conflict", "absence",
}


def validate_shape(raw, path='appraisal'):
    """Validate model JSON before any input acknowledgement or immutable digest.

    Missing optional fields have defaults. Wrong types and non-finite numbers
    are correctable errors, never evidence that may be silently discarded.
    """
    from .agent_session import SessionFailure
    def fail(field, expected):
        raise SessionFailure('invalid_input', f'{field} 必须是{expected}；修正该字段后重新提交',
                             field=field, expected=expected, correction='修正后重提；结构校验失败未占用调用编号')
    if not isinstance(raw, dict):
        fail(path, 'JSON对象')
    if any(key in raw for key in ('experience', 'experience_domain', 'event_impulse', 'continuous_input', 'effects')):
        raise SessionFailure('experience_protocol_required','专项快字段仅由life.simulation写入；小时评价不能再次累计')
    def number(value, field):
        if type(value) not in (int, float) or not math.isfinite(value):
            fail(field, '有限数值')
    for key in ('sources', 'source_event_ids', 'context_refs', 'resolves_cause_ids', 'affected_need_ids'):
        if key in raw and (not isinstance(raw[key], list) or any(not isinstance(v, str) for v in raw[key])):
            fail(path+'.'+key, '字符串数组')
    for key in ('id', 'subject_actor_id', 'meaning_kind', 'event_type', 'interpretation', 'user_need',
                'target', 'persistence_reason', 'self_text', 'supersedes_appraisal_id'):
        if key in raw and not isinstance(raw[key], str):
            fail(path+'.'+key, '字符串')
    for key in ('revision', 'based_on_state_version', 'based_on_relationship_version'):
        if key in raw and (type(raw[key]) is not int or raw[key] < (1 if key=='revision' else 0)):
            fail(path+'.'+key, '有效非负整数' if key!='revision' else '正整数')
    for key in ('certainty', 'relevance', 'novelty', 'goal_congruence'):
        if key in raw:
            number(raw[key], path+'.'+key)
            lo = -1 if key=='goal_congruence' else 0
            if not lo <= raw[key] <= 1:
                fail(path+'.'+key, f'{lo}到1的有限数值')
    for key in ('relation_impacts', 'relation_context_changes'):
        if key in raw and (not isinstance(raw[key], list) or any(not isinstance(v, dict) for v in raw[key])):
            fail(path+'.'+key, '操作对象数组')
    for i,op in enumerate(raw.get('relation_context_changes',[])):
        for key in ('operation','actor_id','relation_kind','origin','text','salience','base_note_ref','applicability','intent_ref'):
            if key in op and not isinstance(op[key],str):
                fail(f'{path}.relation_context_changes[{i}].{key}', '字符串')
        if 'sources' in op and (not isinstance(op['sources'],list) or any(not isinstance(v,str) for v in op['sources'])):
            fail(f'{path}.relation_context_changes[{i}].sources','字符串数组')
        for key in ('confidence','lifetime_days'):
            if key in op:
                number(op[key],f'{path}.relation_context_changes[{i}].{key}')
    for key in ('quoted_or_hypothetical', 'negated'):
        if key in raw and type(raw[key]) is not bool:
            fail(path+'.'+key, '布尔值')
    for key in ('affect_impulse', 'proposed_affect_delta', 'recovery_half_life_seconds'):
        if key in raw:
            if not isinstance(raw[key], dict):
                fail(path+'.'+key, '指标到有限数值的对象')
            for metric, value in raw[key].items():
                if metric not in EMOTIONS:
                    fail(path+'.'+key+'.'+metric, '已注册情绪指标')
                number(value, path+'.'+key+'.'+metric)
                if key=='recovery_half_life_seconds' and value <= 0:
                    fail(path+'.'+key+'.'+metric,'正的有限时常')
    evidence = raw.get('drive_evidence', [])
    if not isinstance(evidence, list) or len(evidence) > 8:
        fail(path+'.drive_evidence', '最多8个证据对象的数组')
    for i, item in enumerate(evidence):
        field = f'{path}.drive_evidence[{i}]'
        if not isinstance(item, dict):
            fail(field, '证据对象')
        dim = item.get('dimension', item.get('need'))
        kind = item.get('kind', item.get('effect_kind', 'activation_change'))
        if not isinstance(dim, str) or not canonical_dimension(dim):
            fail(field+'.dimension', '已注册需要名称')
        if not isinstance(kind, str) or kind not in EFFECT_KINDS:
            fail(field+'.kind', '已注册效果名称')
        if kind=='stimulation_change':
            raise SessionFailure('experience_protocol_required', field+'.kind: stimulation_change 已迁移；仅在明确adult_intimate场景使用life.simulation的appraise_input，不可通过普通驱力入口计入', field=field+'.kind')
        for key in ('delta', 'confidence'):
            if key in item:
                number(item[key], field+'.'+key)
        if 'confidence' in item and not 0 <= item['confidence'] <= 1:
            fail(field+'.confidence', '0到1的有限数值')
        for key in ('confirmed', 'real_participant', 'relationship_allows'):
            if key in item and type(item[key]) is not bool:
                fail(field+'.'+key, '布尔值')
        for key in ('participant_id', 'note'):
            if key in item and not isinstance(item[key], str):
                fail(field+'.'+key, '字符串')


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def validate_appraisal(raw: dict[str, Any] | None, scope: Scope,
                       known_event_ids: Iterable[str],
                       fallback_sources: list[str] | None = None, *, relation_registry=None) -> Appraisal | None:
    validate_shape(raw)
    from .agent_session import SessionFailure
    from .relationships import default_registry,validate_impacts
    if any(key.startswith('_') for key in raw):
        raise SessionFailure('invalid_relation_input','下划线账本参数只由宿主写入')
    if 'relation_evidence' in raw or 'relationship_evidence' in raw:
        raise SessionFailure('protocol_mismatch','旧关系 delta 协议已停用；读取关系版本后使用 relation_protocol=2')
    if any(k.startswith('relation_') and k not in {'relation_protocol','relation_impacts','relation_context_changes'} for k in raw):
        raise SessionFailure('invalid_relation_input','未知关系提案字段')
    if 'relation_protocol' in raw and (type(raw['relation_protocol']) is not int or raw['relation_protocol']!=2):
        raise SessionFailure('protocol_mismatch','关系协议版本必须是整数 2')
    impacts,changes = raw.get('relation_impacts',[]),raw.get('relation_context_changes',[])
    validate_impacts(impacts,relation_registry or default_registry())
    if not isinstance(changes,list) or not all(isinstance(v,dict) for v in changes):
        raise SessionFailure('invalid_relation_input','relation_context_changes 必须是操作对象数组')
    if impacts or changes:
        if type(raw.get('relation_protocol')) is not int or raw['relation_protocol']!=2:
            raise SessionFailure('protocol_mismatch','关系操作需要 relation_protocol=2')
        if not raw.get('subject_actor_id'):
            raise SessionFailure('actor_mismatch','关系评价需要实际人物 subject_actor_id')
    version = raw.get('based_on_relationship_version')
    if (version is not None and (type(version) is not int or version<0)) or (impacts and version is None):
        raise SessionFailure('invalid_relation_input','关系影响需要非负整数 based_on_relationship_version')
    known = {str(x) for x in known_event_ids}
    sources = raw.get("source_event_ids") or fallback_sources or []
    if impacts or changes:
        if not isinstance(sources,list) or any(not isinstance(x,str) or x not in known for x in sources):
            raise SessionFailure('source_removed','关系提案包含不可核实的来源')
        affect_version = raw.get('based_on_state_version')
        if affect_version is not None and (type(affect_version) is not int or affect_version<0):
            raise SessionFailure('invalid_relation_input','情绪版本必须是非负整数')
    if isinstance(sources, str):
        sources = [sources]
    sources = [str(x) for x in sources if str(x) in known]
    if not sources:
        return None

    event_type = str(raw.get("event_type") or "ordinary")
    if event_type not in ALLOWED_EVENT_TYPES:
        event_type = "ordinary"

    delta_raw = raw.get("affect_impulse") or raw.get("proposed_affect_delta") or {}
    delta: dict[str, float] = {}
    if isinstance(delta_raw, dict):
        for key, value in delta_raw.items():
            if key in EMOTIONS:
                delta[key] = max(-1.0, min(1.0, _num(value)))

    resolves = raw.get("resolves_cause_ids") or []
    if isinstance(resolves, str):
        resolves = [resolves]

    interpretation = str(raw.get("interpretation") or "")[:240]
    confidence = clamp01(_num(raw.get("certainty"), 0.5))
    # Quotation, negation and roleplay are attribution, not fixed discounts.

    user_need = str(raw.get("user_need") or "unclear")
    if user_need not in {"assistance", "listening", "quiet_company", "sharing",
                         "boundary", "unclear"}:
        user_need = "unclear"
    affected = []
    for value in raw.get("affected_need_ids") or []:
        dim = canonical_dimension(str(value))
        if dim and dim not in affected:
            affected.append(dim)
    drive_evidence = []
    for item in (raw.get("drive_evidence") or [])[:8]:
        if not isinstance(item, dict):
            continue
        dim = canonical_dimension(str(item.get("dimension") or item.get("need") or ""))
        kind = str(item.get("kind") or item.get("effect_kind") or "activation_change")
        if not dim or kind not in EFFECT_KINDS:
            continue
        drive_evidence.append({
            "dimension": dim,
            "kind": kind,
            "delta": max(-0.25, min(0.25, _num(item.get("delta"), 0.0))),
            "confidence": clamp01(_num(item.get("confidence"), confidence)),
            "confirmed": bool(item.get("confirmed")),
            "real_participant": bool(item.get("real_participant")),
            "relationship_allows": bool(item.get("relationship_allows", True)),
            "participant_id": str(item.get("participant_id") or "")[:80],
            "note": str(item.get("note") or "")[:160],
        })

    return Appraisal(
        id=str(raw.get("id") or new_id("appr")),
        context_refs=[str(x) for x in raw.get('context_refs',[])],
        subject_actor_id=str(raw.get('subject_actor_id') or ''),
        meaning_kind=str(raw.get('meaning_kind') or 'observation'),
        recovery_half_life_seconds={k: max(1.0,min(30*86400.0,_num(v,3600)))
            for k,v in (raw.get('recovery_half_life_seconds') or {}).items() if k in EMOTIONS},
        persistence_reason=str(raw.get('persistence_reason') or ''),
        revision=max(1,int(raw.get('revision',1))),
        based_on_state_version=raw.get('based_on_state_version'),
        scope=scope,
        source_event_ids=sources,
        event_type=event_type,
        relevance=clamp01(_num(raw.get("relevance"), 0.3)),
        goal_congruence=max(-1.0, min(1.0, _num(raw.get("goal_congruence"), 0.0))),
        certainty=confidence,
        novelty=clamp01(_num(raw.get("novelty"), 0.0)),
        interpretation=interpretation,
        quoted_or_hypothetical=bool(raw.get("quoted_or_hypothetical")),
        negated=bool(raw.get("negated")),
        proposed_affect_delta=delta,
        relation_protocol=raw.get('relation_protocol'),based_on_relationship_version=version,
        relation_impacts=impacts,relation_context_changes=changes,
        resolves_cause_ids=[str(x) for x in resolves][:8],
        status="valid",
        supersedes_appraisal_id=raw.get('supersedes_appraisal_id'),
        user_need=user_need,
        target=str(raw.get("target") or "")[:120],
        affected_need_ids=affected,
        drive_evidence=drive_evidence,
    )
