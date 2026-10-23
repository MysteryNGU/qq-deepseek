#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""状态引擎：纯函数。

对应设计第 5 节：
- 情绪按时间半衰期回落，再叠加本次事件刺激，最后限幅；
- 衰减、拟刺激、实际刺激、总变化分开返回，谁都别冒充谁；
- 欲望分 baseline / learned_offset / activation / satiety 四层，
  压力只做候选排序，不是行为命令；
- 同一真实事件只能结算一次（由 repo.claim_effect 保证）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .contracts import (
    EMOTIONS, AffectChange, AffectState, Appraisal, DesireEvidence, DesireState,
    Scope, clamp, clamp01, new_id, now_ts,
)


@dataclass
class AffectConfig:
    half_life: dict[str, float] = field(default_factory=lambda: {
        "joy": 6 * 3600.0,
        "concern": 4 * 3600.0,
        "frustration": 2 * 3600.0,
        "hurt": 8 * 3600.0,
        "anticipation": 3 * 3600.0,
    })
    baseline: dict[str, float] = field(default_factory=lambda: {
        "joy": 0.20, "concern": 0.15, "frustration": 0.10,
        "hurt": 0.10, "anticipation": 0.20,
    })
    event_cap: float = 1.0
    cause_threshold: float = 0.01


@dataclass
class DesireConfig:
    activation_half_life: float = 3 * 3600.0
    satiety_half_life: float = 6 * 3600.0
    daily_learning_budget: float = 0.02
    max_learned_offset: float = 0.15
    pressure_floor: float = 0.0


DEFAULT_AFFECT = AffectConfig()
DEFAULT_DESIRE = DesireConfig()
from .text import stable_id

# 十四维欲望对情绪的基线/敏感度影响。只做小幅度偏移。
def _push(desires: dict[str, float], key: str, ref: float = 6.0) -> float:
    return max(0.0, (float(desires.get(key, 0.0) or 0.0) - ref) / 4.0)


def bounded_desire_baseline(desires: dict[str, float], base: dict[str, float]) -> dict[str, float]:
    b = dict(base)
    b["concern"] += 0.10 * _push(desires, "嫉妒")
    b["hurt"] += 0.06 * _push(desires, "嫉妒")
    b["joy"] += 0.07 * _push(desires, "贪婪")
    b["anticipation"] += 0.06 * _push(desires, "色欲")
    b["frustration"] -= 0.06 * _push(desires, "温和")
    b["frustration"] -= 0.05 * _push(desires, "宽厚")
    b["hurt"] -= 0.04 * _push(desires, "忠贞")
    return {k: clamp01(v) for k, v in b.items()}


def bounded_sensitivity(emotion: str, desires: dict[str, float]) -> float:
    if emotion == "frustration":
        return max(0.35, 1.0 - 0.5 * _push(desires, "温和") - 0.4 * _push(desires, "宽厚"))
    if emotion == "concern":
        return 1.0 + 0.6 * _push(desires, "嫉妒")
    if emotion == "hurt":
        return 1.0 + 0.4 * _push(desires, "嫉妒")
    if emotion == "joy":
        return 1.0 + 0.5 * _push(desires, "贪婪")
    if emotion == "anticipation":
        return 1.0 + 0.4 * _push(desires, "色欲")
    return 1.0


def _drive_pressure(drive_pressure: dict[str, float], dim: str) -> float:
    return max(0.0, float((drive_pressure or {}).get(dim, 0.0) or 0.0))


def bounded_drive_baseline(drive_pressure: dict[str, float],
                           base: dict[str, float]) -> dict[str, float]:
    """drive_v2 的有界情绪基线。亲密与性欲分别进入不同通道。"""
    b = dict(base)
    b["concern"] += 0.08 * max(0.0, _drive_pressure(drive_pressure, "relatedness") - 0.45)
    b["hurt"] += 0.05 * max(0.0, _drive_pressure(drive_pressure, "intimacy") - 0.50)
    b["joy"] += 0.07 * max(0.0, _drive_pressure(drive_pressure, "intimacy") - 0.40)
    b["joy"] += 0.04 * max(0.0, _drive_pressure(drive_pressure, "relatedness") - 0.50)
    b["anticipation"] += 0.09 * max(0.0, _drive_pressure(drive_pressure, "libido") - 0.45)
    b["anticipation"] += 0.04 * max(0.0, _drive_pressure(drive_pressure, "exploration") - 0.45)
    b["frustration"] += 0.04 * max(0.0, _drive_pressure(drive_pressure, "competence") - 0.55)
    return {k: clamp01(v) for k, v in b.items()}


def bounded_drive_sensitivity(emotion: str, drive_pressure: dict[str, float]) -> float:
    if emotion == "concern":
        return 1.0 + 0.5 * max(0.0, _drive_pressure(drive_pressure, "relatedness") - 0.50)
    if emotion == "hurt":
        return 1.0 + 0.4 * max(0.0, _drive_pressure(drive_pressure, "intimacy") - 0.50)
    if emotion == "joy":
        return 1.0 + 0.35 * max(0.0, _drive_pressure(drive_pressure, "intimacy") - 0.40)
    if emotion == "anticipation":
        return 1.0 + 0.45 * max(0.0, _drive_pressure(drive_pressure, "libido") - 0.45)
    if emotion == "frustration":
        return max(0.35, 1.0 - 0.20 * max(0.0, _drive_pressure(drive_pressure, "self_direction") - 0.60))
    return 1.0


def validated_evidence_weight(appraisal: Appraisal) -> float:
    """来源可信度：不是模型自报概率，而是结构性折扣。"""
    if appraisal.status != "valid":
        return 0.0
    weight = 0.5 + 0.5 * clamp01(appraisal.certainty)
    return clamp01(weight)


def independent_event_weight(appraisal: Appraisal) -> float:
    # 去重由 applied_effects 负责；这里只区分"转述/假设"。
    return 1.0


def _update_causes(previous: list[dict[str, Any]], appraisal: Appraisal,
                   applied: dict[str, float], now: float) -> list[dict[str, Any]]:
    causes = [dict(c) for c in (previous or [])]
    resolved = set(appraisal.resolves_cause_ids or [])
    for cause in causes:
        if cause.get("id") in resolved:
            cause["resolved_at"] = now
    for emotion, delta in applied.items():
        if abs(delta) < 0.01 and emotion not in (appraisal.resolves_cause_ids or []):
            continue
        causes.append({
            "id": stable_id('cause',appraisal.stimulus_event_id or appraisal.source_event_ids[0],emotion),
            "appraisal_id": appraisal.id,
            "emotion": emotion,
            "target": appraisal.scope.participant_id,
            "source_event_ids": list(appraisal.source_event_ids),
            "strength": round(float(delta), 4),
            "created_at": now,
            "resolved_at": None,
        })
    # 只保留少量活跃原因，避免无限堆积
    active = [c for c in causes if not c.get("resolved_at")]
    done = [c for c in causes if c.get("resolved_at")]
    return (done[-20:] + active[-12:])[-32:]


def reduce_affect(previous: AffectState, appraisal: Appraisal, now: float,
                  config: AffectConfig | None = None,
                  desires: dict[str, float] | None = None,
                  drive_pressure: dict[str, float] | None = None) -> AffectChange:
    """纯函数：不读库、不调用模型、不改变传入对象。

    desires 是旧 14 维兼容参数；drive_pressure 是 drive_v2 的八维压力。
    两者都只做有界调制，不把欲望强度复制成情绪。
    """
    cfg = config or DEFAULT_AFFECT
    desire_snapshot = desires or {}
    use_drive = bool(drive_pressure)
    effective_now = max(float(now), float(previous.updated_at))
    dt = max(0.0, effective_now - float(previous.updated_at))
    baseline = previous.baseline or cfg.baseline

    decayed: dict[str, float] = {}
    for emotion in EMOTIONS:
        half = max(1.0, float(previous.recovery_half_life_seconds.get(emotion,
                             cfg.half_life.get(emotion, 3600.0))))
        prev = float(previous.intensity.get(emotion, baseline[emotion]))
        decayed[emotion] = baseline[emotion] + (prev - baseline[emotion]) * (2 ** (-dt / half))

    reliability = validated_evidence_weight(appraisal)
    repetition = independent_event_weight(appraisal)
    stimulus: dict[str, float] = {}
    result: dict[str, float] = {}
    clipping_reasons = {}
    for emotion in EMOTIONS:
        proposed = float((appraisal.proposed_affect_delta or {}).get(emotion, 0.0) or 0.0)
        sensitivity = (bounded_drive_sensitivity(emotion, drive_pressure) if use_drive
                       else bounded_sensitivity(emotion, desire_snapshot))
        weighted = proposed * sensitivity * reliability * repetition
        stimulus[emotion] = clamp(weighted, -cfg.event_cap, cfg.event_cap)
        result[emotion] = clamp01(decayed[emotion] + stimulus[emotion])
        reasons = []
        if abs(weighted-stimulus[emotion])>1e-9:
            reasons.append('configured_event_cap')
        if abs(result[emotion]-decayed[emotion]-stimulus[emotion])>1e-9:
            reasons.append('state_boundary_0_1')
        if reasons:
            clipping_reasons[emotion] = reasons

    applied_stimulus = {k: result[k] - decayed[k] for k in EMOTIONS}
    applied_total = {k: result[k] - float(previous.intensity.get(k, 0.0)) for k in EMOTIONS}
    causes = _update_causes(previous.causes, appraisal, applied_stimulus, effective_now)
    state = AffectState(
        scope=previous.scope,
        intensity={k: round(result[k], 6) for k in EMOTIONS},
        causes=causes,
        updated_at=effective_now,
        version=previous.version + 1,
        recovery_half_life_seconds=dict(previous.recovery_half_life_seconds or cfg.half_life,
                                        **appraisal.recovery_half_life_seconds),
        baseline=dict(cfg.baseline),
    )
    return AffectChange(
        state=state,
        decay_delta={k: round(decayed[k] - float(previous.intensity.get(k, 0.0)), 6)
                     for k in EMOTIONS},
        stimulus_delta={k: round(stimulus[k], 6) for k in EMOTIONS},
        applied_stimulus_delta={k: round(applied_stimulus[k], 6) for k in EMOTIONS},
        applied_delta={k: round(applied_total[k], 6) for k in EMOTIONS},
        source_appraisal_id=appraisal.id,
        proposed_delta=dict(appraisal.proposed_affect_delta or {}),
        clipping_reasons=clipping_reasons,
    )


def affect_view(previous: AffectState, now: float,
                config: AffectConfig | None = None,
                drive_pressure: dict[str, float] | None = None) -> dict[str, float]:
    """只读情绪视图：只做时间衰减，不叠加刺激、不写原因、不写库。"""
    cfg = config or DEFAULT_AFFECT
    effective_now = max(float(now), float(previous.updated_at))
    dt = max(0.0, effective_now - float(previous.updated_at))
    baseline = previous.baseline or cfg.baseline
    out: dict[str, float] = {}
    for emotion in EMOTIONS:
        half = max(1.0, float(previous.recovery_half_life_seconds.get(emotion,
                             cfg.half_life.get(emotion, 3600.0))))
        prev = float(previous.intensity.get(emotion, baseline[emotion]))
        out[emotion] = round(clamp01(
            baseline[emotion] + (prev - baseline[emotion]) * (2 ** (-dt / half))), 6)
    return out


def _decay_map(values: dict[str, float], dt: float, half_life: float) -> dict[str, float]:
    factor = 2 ** (-max(0.0, dt) / max(1.0, half_life))
    return {k: clamp01(float(v) * factor) for k, v in values.items()}


def desire_pressure(state: DesireState, config: DesireConfig | None = None) -> dict[str, float]:
    cfg = config or DEFAULT_DESIRE
    dims = set(state.baseline) | set(state.learned_offset) | set(state.activation) | set(state.satiety)
    out = {}
    for dim in dims:
        value = (float(state.baseline.get(dim, 0.0))
                 + float(state.learned_offset.get(dim, 0.0))
                 + float(state.activation.get(dim, 0.0))
                 - float(state.satiety.get(dim, 0.0)))
        out[dim] = clamp01(max(cfg.pressure_floor, value))
    return out


def reduce_desires(previous: DesireState, evidence: list[DesireEvidence],
                   now: float, config: DesireConfig | None = None,
                   settle_window: bool = False) -> DesireState:
    """欲望推进：激活/满足按时间衰减；长期偏移只在结算窗口内、按独立经历累加。"""
    cfg = config or DEFAULT_DESIRE
    effective_now = max(float(now), float(previous.updated_at))
    dt = max(0.0, effective_now - float(previous.updated_at))
    activation = _decay_map(previous.activation, dt, cfg.activation_half_life)
    satiety = _decay_map(previous.satiety, dt, cfg.satiety_half_life)
    offsets = {k: float(v) for k, v in previous.learned_offset.items()}
    seen = set(previous.evidence_ids)

    experience_ids = set(previous.experience_ids)
    pending_learning = list(previous.pending_learning)
    fresh = []
    for e in evidence:
        root = e.independent_experience_group or (e.root_event_ids[0] if e.root_event_ids else e.id)
        if e.id in seen or root in experience_ids:
            continue
        experience_ids.add(root)
        fresh.append(e)
        record = asdict(e)
        record.pop("scope")
        pending_learning.append(record)
    for e in fresh:
        delta = clamp(float(e.supported_delta) * clamp01(e.confidence), -0.25, 0.25)
        dim = e.dimension
        if e.kind == "satisfaction":
            satiety[dim] = clamp01(satiety.get(dim, 0.0) + abs(delta))
        else:
            activation[dim] = clamp01(activation.get(dim, 0.0) + abs(delta))

    from .work_queue import calendar_day
    window_id = max(calendar_day(effective_now), previous.last_window_id)
    used = previous.daily_learning_used if window_id == previous.last_window_id else 0.0
    if settle_window:
        groups: dict[str, DesireEvidence] = {}
        for record in pending_learning:
            e = DesireEvidence(scope=previous.scope, **record)
            key = e.independent_experience_group or (e.root_event_ids[0] if e.root_event_ids else e.id)
            groups.setdefault(key, e)
        budget = max(0.0, cfg.daily_learning_budget - used)
        for e in groups.values():
            dim = e.dimension
            step = clamp(float(e.supported_delta) * clamp01(e.confidence), -budget, budget)
            old_offset = float(offsets.get(dim, 0.0))
            new_offset = clamp(old_offset + step, -cfg.max_learned_offset, cfg.max_learned_offset)
            spent = abs(new_offset - old_offset)
            offsets[dim] = new_offset
            used += spent
            budget = max(0.0, budget - spent)
        for dim in list(offsets):
            offsets[dim] = clamp(offsets[dim], -cfg.max_learned_offset, cfg.max_learned_offset)
        pending_learning = []

    return DesireState(
        scope=previous.scope,
        baseline=dict(previous.baseline),
        learned_offset=offsets,
        activation=activation,
        satiety=satiety,
        evidence_ids=list(dict.fromkeys(list(seen) + [e.id for e in fresh]))[-500:],
        last_window_id=window_id,
        daily_learning_used=used,
        experience_ids=sorted(experience_ids),
        pending_learning=pending_learning,
        updated_at=effective_now,
        version=previous.version + 1,
    )


# ---------------------------------------------------------------- 保守的事件评估

_PRAISE = ("真好", "太棒", "谢谢你", "喜欢你", "厉害", "辛苦", "乖", "好听", "好看")
_CORRECTION = ("记错了", "不是这样", "我说过", "你搞错", "弄错了", "不是这个意思")
_FAILURE = ("失败了", "没成", "报错", "出错了", "打不开", "发不出去")
_REPAIR = ("对不起", "抱歉", "不是怪你", "别难过", "误会", "我没生气")
_NEGATED = ("不是", "没有", "别", "不用")
# 亲密与性欲分开：前者是关系中的亲近/被抱/被回应，后者是身体欲望。
_INTIMACY = ("抱抱", "抱我", "抱一下", "亲亲", "亲我", "亲一下", "贴贴",
             "蹭蹭", "想你", "黏着", "撒娇", "靠着你", "陪我", "搂着")
_LIBIDO = ("做爱", "上床", "想要你", "身体想要", "色情", "撩我", "调教",
           "湿了", "硬了", "高潮", "被我弄", "弄我", "睡我", "操我")
_EXPLORATION = ("好奇", "研究", "查资料", "为什么", "学习", "探索", "弄懂", "原理")
_COMPETENCE = ("任务", "修好", "测试", "没成", "失败", "报错", "做好", "改进")
_RELATEDNESS = ("在吗", "陪我", "孤单", "想我", "理我", "别走", "回我")
_COHERENCE = ("答应", "承诺", "提醒我", "交报告", "约好", "说好", " deadline")
_SELF_DIRECTION = ("我自己想", "自己的事", "计划", "安排", "想做点", "空下来")
_RESERVE = ("好累", "累了", "想休息", "没睡", "困了", "歇一会", "先歇")
_QUIET = ("安静", "别问", "不想说", "想静静", "先别管", "别吵", "一个人待")
_BOUNDARY = ("今天不行", "不要了", "不可以", "别碰", "停下", "先不要")


def heuristic_appraisal(text: str, scope: Scope, event_ids: list[str],
                        participant_id: str = "") -> Appraisal:
    """没有模型时也能跑的最小评估。

    只认长度 >= 2 的词组，不做单字匹配；默认中性，低置信度。
    这是给影子模式/离线测试用的兜底，不是最终的情绪理解。
    """
    s = (text or "").strip()
    # Quoted or reported speech is retained as evidence, not interpreted as
    # the speaker addressing the character directly.
    import re
    reported = bool(re.search(r"(?:同事|朋友|亲戚|他|她|别人)(?:说|抱怨|觉得|告诉)", s))
    evaluated = re.sub(r'“[^”]*”|「[^」]*」|"[^"\n]*"|‘[^’]*’', "", s)
    event_type = "ordinary"
    delta: dict[str, float] = {}
    certainty = 0.45
    negated = False

    def hit(words: tuple[str, ...]) -> str:
        for w in words:
            if not reported and w in evaluated:
                return w
        return ""

    praise, correction = hit(_PRAISE), hit(_CORRECTION)
    failure, repair = hit(_FAILURE), hit(_REPAIR)
    if repair:
        event_type, certainty = "repair", 0.6
        delta = {"hurt": -0.35, "concern": -0.2}
    elif correction:
        event_type, certainty = "correction", 0.6
        delta = {"concern": 0.1, "frustration": 0.2}
    elif failure:
        event_type, certainty = "failure", 0.55
        delta = {"frustration": 0.25}
    elif praise:
        event_type, certainty = "praise", 0.55
        delta = {"joy": 0.3, "hurt": -0.15}

    if event_type != "ordinary" and any(n in s for n in _NEGATED):
        # "不是怪你" 这类要按修复处理，不能被否定词一票否决
        if event_type != "repair":
            negated = True
            certainty = min(certainty, 0.3)

    def has(words: tuple[str, ...]) -> bool:
        return any(w in evaluated for w in words)

    user_need = "unclear"
    if has(_QUIET):
        user_need = "quiet_company"
    elif has(_BOUNDARY):
        user_need = "boundary"
    elif has(_RELATEDNESS):
        user_need = "listening"
    elif any(w in s for w in ("帮我", "提醒我", "查一下", "去做", "帮我做")):
        user_need = "assistance"
    elif any(w in s for w in ("今天", "跟你讲", "分享一下", "我跟你说")):
        user_need = "sharing"

    drive_evidence: list[dict[str, Any]] = []
    def add_drive(dim: str, delta: float, conf: float = 0.55, note: str = "") -> None:
        if not negated and user_need not in {"boundary"}:
            drive_evidence.append({"dimension": dim, "kind": "activation_change",
                                   "delta": delta, "confidence": conf, "note": note})
    # 明确的亲密/身体线索才激活相应维度；普通安慰不会自动算满足。
    if has(_INTIMACY):
        add_drive("intimacy", 0.06, 0.62, "明确亲密线索")
        add_drive("relatedness", 0.03, 0.55, "关系联系线索")
    if has(_LIBIDO):
        add_drive("libido", 0.07, 0.62, "明确身体欲望线索")
    if has(_EXPLORATION):
        add_drive("exploration", 0.05, 0.58, "未解问题/学习线索")
    if has(_COMPETENCE):
        add_drive("competence", 0.04 if event_type != "failure" else 0.02, 0.55, "任务/能力线索")
    if has(_RELATEDNESS):
        add_drive("relatedness", 0.04, 0.58, "联系线索")
    if has(_COHERENCE):
        add_drive("coherence", 0.04, 0.56, "承诺/未完成事项线索")
    if has(_SELF_DIRECTION):
        add_drive("self_direction", 0.04, 0.55, "自主项目线索")
    if has(_RESERVE):
        add_drive("resource_reserve", 0.05, 0.58, "真实负荷线索")

    return Appraisal(
        id=new_id("appr"),
        scope=scope,
        source_event_ids=list(event_ids),
        event_type=event_type,
        relevance=0.5 if event_type != "ordinary" else 0.1,
        goal_congruence=0.3 if event_type == "praise" else (-0.2 if event_type in ("correction", "failure") else 0.0),
        certainty=certainty,
        novelty=0.4 if event_type != "ordinary" else 0.0,
        interpretation=(f"规则评估：{event_type}" if event_type != "ordinary" else ""),
        quoted_or_hypothetical=reported,
        negated=negated,
        proposed_affect_delta=delta,
        relation_evidence=({"comfort": 0.008, "trust": 0.004} if event_type == "praise" and not negated
                           else {"repair": 0.0} if event_type == "repair" else None),
        user_need=user_need,
        target=participant_id,
        affected_need_ids=[e["dimension"] for e in drive_evidence],
        drive_evidence=drive_evidence,
    )


# ---------------------------------------------------------------- 慢变量

@dataclass
class LegacyRelationConfig:
    """life-state-4 decoding only; never used by current relationship writes."""
    daily_cap: float = 0.10
    repair_cap: float = 0.30
    min_confidence: float = 0.35


def reduce_relationship_legacy(previous, evidence: list, now: float,
                        config: LegacyRelationConfig | None = None):
    """关系慢变量：按独立经历、每日限额推进；修复与信任分开。

    这里只处理 familiarity/trust/comfort 三个可观察维度。
    亲近不授予权限（权限仍由配置/授权决定）。
    """
    from .contracts import LegacyRelationshipState

    cfg = config or LegacyRelationConfig()
    effective_now = max(float(now), float(previous.updated_at))
    seen = set(previous.evidence_ids)
    fresh = []
    groups: set[str] = set(previous.experience_ids)
    for e in evidence:
        if e.id in seen:
            continue
        group = e.independent_experience_group or (
            e.root_event_ids[0] if e.root_event_ids else e.id)
        group = e.kind + ":" + group
        if group in groups:
            continue          # 同一真实经历的多条消息只算一次
        if float(e.confidence) < cfg.min_confidence:
            continue
        groups.add(group)
        fresh.append(e)

    deltas = {"familiarity": 0.0, "trust": 0.0, "comfort": 0.0}
    import datetime as _dt
    from .work_queue import calendar_day
    day_key = calendar_day(effective_now)
    used = dict(previous.daily_used) if previous.daily_key == day_key else {}
    repairs = list(previous.open_repairs)
    for e in fresh:
        if e.kind in deltas:
            remaining = max(0.0, cfg.daily_cap - float(used.get(e.kind, 0.0)))
            step = clamp(float(e.delta) * float(e.confidence), -remaining, remaining)
            deltas[e.kind] += step
            used[e.kind] = float(used.get(e.kind, 0.0)) + abs(step)
        elif e.kind == "repair":
            repairs.append({
                "id": e.id, "note": e.note[:120],
                "root_event_ids": e.root_event_ids[:8],
                "resolved_at": effective_now,
            })
        elif e.kind == "boundary":
            # 明确边界只记录偏好，不直接扣信任
            repairs.append({"id": e.id, "kind": "boundary", "note": e.note[:120],
                            "resolved_at": None})

    state = LegacyRelationshipState(
        scope=previous.scope,
        relationship_id=previous.relationship_id,
        familiarity=clamp01(previous.familiarity + deltas["familiarity"]),
        trust=clamp01(previous.trust + deltas["trust"]),
        comfort=clamp01(previous.comfort + deltas["comfort"]),
        preferred_address=previous.preferred_address,
        conversation_preferences=dict(previous.conversation_preferences),
        open_repairs=repairs[-20:],
        daily_key=day_key,
        daily_used=used,
        last_meaningful_interaction_at=(
            effective_now if fresh else previous.last_meaningful_interaction_at),
        evidence_ids=list(dict.fromkeys(list(seen) + [e.id for e in fresh]))[-500:],
        experience_ids=sorted(groups)[-5000:],
        version=previous.version + 1,
        updated_at=effective_now,
    )
    return state, deltas


def reduce_relationship(previous, impacts, *, actor_id, policy, registry, config,
                        settlement_day, settlement_at):
    """Pure v2 settlement: confidence once, actual absolute daily use, host bounds."""
    from copy import deepcopy
    from .relationships import normalize_state,validate_impacts
    validate_impacts(impacts,registry)
    normalized,adjustments = normalize_state(previous.metrics,actor_id,registry,policy)
    if adjustments:
        raise ValueError('relationship_requires_policy_rebase')
    state = deepcopy(previous)
    state.metrics = normalized
    if settlement_day!=state.daily_key:
        state.daily_key = settlement_day
        state.daily_used = {key:0. for key in registry}
    effects = []
    for impact in impacts:
        key = impact['metric']
        before = state.metrics[key]
        proposed = {'increase':1,'decrease':-1,'unchanged':0}[impact['direction']]*config.magnitudes[impact['magnitude']]
        proposed *= impact['confidence']*config.sensitivity[key]
        eligible = impact['confidence']>=config.min_confidence
        remaining = max(0.,config.daily_cap-state.daily_used.get(key,0.))
        limited = max(-remaining,min(remaining,proposed)) if eligible else 0.
        lo,hi = policy.bounds(actor_id)
        candidate = before+limited
        after = max(lo,min(hi,round(candidate,6)))
        actual = round(after-before,6)
        state.metrics[key] = after
        state.daily_used[key] = round(state.daily_used.get(key,0.)+abs(actual),6)
        if eligible:
            old_basis = state.metric_basis[key]
            state.metric_basis[key] = ('policy_and_recorded_experiences' if old_basis in
                {'policy_initialization','policy_and_recorded_experiences'} else 'recorded_experiences')
        effects.append(dict(metric=key,before=before,after=after,proposed_delta=round(proposed,6),
            limited_delta=round(limited,6),actual_delta=actual,confidence_filtered=not eligible,
            budget_limited=eligible and abs(proposed)>remaining,
            boundary_reason=('owner_floor' if lo==.7 else 'minimum') if candidate<lo else
                ('other_ceiling' if hi==.9 else 'maximum') if candidate>hi else None,
            daily_used=state.daily_used[key]))
    state.updated_at = settlement_at
    return state,effects


@dataclass
class MoodConfig:
    half_life_hours: float = 8.0
    max_hourly_drift: float = 0.15


def integrate_mood(previous, affect, now: float, config: MoodConfig | None = None):
    """慢心境：快情绪的连续驻留只做有界积分，不按消息条数回流人格。"""
    from .contracts import MoodState

    cfg = config or MoodConfig()
    effective_now = max(float(now), float(previous.updated_at))
    dt_h = min(24.0, max(0.0, (effective_now - float(previous.updated_at)) / 3600.0))
    if dt_h <= 0:
        return previous
    intensity = affect.intensity if affect is not None else {}
    positive = float(intensity.get("joy", 0)) + 0.5 * float(intensity.get("anticipation", 0))
    negative = (float(intensity.get("hurt", 0))
                + float(intensity.get("frustration", 0))
                + 0.5 * float(intensity.get("concern", 0)))
    target = clamp(positive - negative, -1.0, 1.0)
    blend = min(1.0, dt_h / max(1.0, cfg.half_life_hours))
    pleasure = clamp(previous.pleasure + (target - previous.pleasure) * blend, -1.0, 1.0)
    drift = clamp((positive - negative) * 0.05 - 0.02 * dt_h,
                  -cfg.max_hourly_drift, cfg.max_hourly_drift)
    activation = clamp01(previous.activation + drift)
    energy = clamp01(previous.energy + (0.06 if activation < 0.3 else -0.02) * dt_h)
    return MoodState(
        scope=previous.scope, pleasure=pleasure, activation=activation,
        energy=energy, updated_at=effective_now, version=previous.version + 1,
    )


def propose_profile_revision(profile: dict, weekly_experiences: list[dict]):
    """人格主体变更：证据不足就返回 None；第一版不自动改写人格。"""
    days = {str(e.get("local_date") or "") for e in weekly_experiences}
    days.discard("")
    if len(days) < 3:
        return None
    return {
        "before": dict(profile or {}),
        "suggested_small_change": "待人工评审：多条跨天经历指向同一变化",
        "evidence_ids": [e.get("id") for e in weekly_experiences[:20] if e.get("id")],
        "review_required": True,
    }
