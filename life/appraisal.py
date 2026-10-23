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


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def validate_appraisal(raw: dict[str, Any] | None, scope: Scope,
                       known_event_ids: Iterable[str],
                       fallback_sources: list[str] | None = None, *, relation_registry=None) -> Appraisal | None:
    if not isinstance(raw, dict):
        return None
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
