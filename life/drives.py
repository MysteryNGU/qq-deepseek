#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内稳态驱力引擎（drive_v2，纯函数）。

对应《情绪内驱动与 DSH 工具桥》第 3、6 节与交接 P01/P03/P04/P05/P10：

- 八项需要各有独立状态与参数。用户明确要求把「亲密」与「性欲」拆成
  两项指标：亲密是关系中的亲近、被抱、被回应；性欲是独立的身体欲望。
- 累计型需要在未满足时有界增长，增长速率由「参考强度处每小时涨幅」反解；
- 高位、持续、内部调节与恢复期按真实时间边界切分；
- activation / satisfaction / preference_learning 三种证据分开，带符号；
- satiety 只限制立即重复与表达，不再次扣除已经恢复的 level；
- 所有函数不读库、不调用模型，时间单位为 UTC 秒，增长参数单位为小时。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable

from .contracts import clamp, clamp01, now_ts

LOG2 = math.log(2.0)

# 顺序即界面顺序。亲密与性欲相邻但完全独立。
DRIVE_DIMENSIONS = (
    "exploration", "competence", "relatedness", "intimacy", "libido",
    "coherence", "self_direction", "resource_reserve",
)
DRIVE_LABELS = {
    "exploration": "探索",
    "competence": "胜任",
    "relatedness": "连接",
    "intimacy": "亲密",
    "libido": "性欲",
    "coherence": "连贯",
    "self_direction": "自主",
    "resource_reserve": "余量",
}
DRIVE_LABEL_TO_ID = {v: k for k, v in DRIVE_LABELS.items()}
# 兼容用户与旧界面使用的近义写法；只用于读取，不用于写入新状态。
DRIVE_ALIASES = {
    "探索": "exploration", "好奇": "exploration", "理解": "exploration",
    "胜任": "competence", "能力": "competence",
    "连接": "relatedness", "关系": "relatedness", "陪伴": "relatedness",
    "亲密": "intimacy", "亲近": "intimacy",
    "性欲": "libido", "色欲": "libido", "欲望": "libido",
    "连贯": "coherence", "兑现": "coherence",
    "自主": "self_direction", "自主空间": "self_direction",
    "余量": "resource_reserve", "休整": "resource_reserve", "精力": "resource_reserve",
}

EFFECT_KINDS = {
    "activation_change", "interaction_satisfaction", "verified_task_satisfaction",
    "internal_regulation", "preference_learning",
}


def canonical_dimension(value: str) -> str:
    text = str(value or "").strip()
    if text in DRIVE_DIMENSIONS:
        return text
    return DRIVE_ALIASES.get(text, "")


@dataclass
class DriveDynamics:
    dimension: str
    mode: str = "accumulating"           # accumulating / task_derived / resource_derived
    initial_tension: float = 0.30
    reference_tension: float = 0.50
    ceiling: float = 1.00
    base_growth_per_hour: float = 0.02
    growth_multiplier_min: float = 0.25
    growth_multiplier_max: float = 2.00
    expression_threshold: float = 0.65
    high_threshold: float = 0.90
    high_dwell_hours: float = 2.0
    interaction_satisfaction_gain: float = 0.25
    regulation_relief_gain: float = 0.35
    regulation_enabled: bool = True
    recovery_hours: float = 4.0
    recovery_growth_multiplier: float = 0.25
    contact_cooldown_hours: float = 12.0
    max_contact_attempts_per_window: int = 1
    persona_preference: float = 0.50

    @property
    def label(self) -> str:
        return DRIVE_LABELS.get(self.dimension, self.dimension)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["label"] = self.label
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DriveDynamics":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{str(k): v for k, v in (data or {}).items() if k in allowed})


def validate_drive_config(c: DriveDynamics) -> DriveDynamics:
    """P01：启动时显式校验，错误参数不能悄悄跑。"""
    if c.mode not in {"accumulating", "task_derived", "resource_derived"}:
        raise ValueError(f"invalid_drive_mode:{c.dimension}:{c.mode}")
    if not (0.0 <= float(c.initial_tension) < float(c.ceiling) <= 1.0):
        raise ValueError(f"invalid_initial_tension:{c.dimension}")
    if not (0.0 <= float(c.reference_tension) < float(c.ceiling)):
        raise ValueError(f"invalid_reference_tension:{c.dimension}")
    if not (0.0 < float(c.expression_threshold) < float(c.high_threshold) <= float(c.ceiling)):
        raise ValueError(f"invalid_thresholds:{c.dimension}")
    if float(c.base_growth_per_hour) < 0:
        raise ValueError(f"invalid_growth:{c.dimension}")
    if float(c.high_dwell_hours) <= 0 or float(c.recovery_hours) < 0:
        raise ValueError(f"invalid_timing:{c.dimension}")
    if not (0.0 <= float(c.regulation_relief_gain) <= float(c.ceiling)):
        raise ValueError(f"invalid_relief:{c.dimension}")
    if not (0.0 <= float(c.recovery_growth_multiplier) <= 1.0):
        raise ValueError(f"invalid_recovery_multiplier:{c.dimension}")
    if float(c.contact_cooldown_hours) < 0:
        raise ValueError(f"invalid_contact_cooldown:{c.dimension}")
    if int(c.max_contact_attempts_per_window) < 1:
        raise ValueError(f"invalid_contact_attempts:{c.dimension}")
    if not (0.0 <= float(c.growth_multiplier_min) <= float(c.growth_multiplier_max)):
        raise ValueError(f"invalid_growth_multiplier:{c.dimension}")
    return c


@dataclass
class DriveState:
    dimension: str
    tension: float = 0.30
    activation: float = 0.0          # 有符号，[-0.25, 0.25]
    learned_offset: float = 0.0      # 仅在跨天独立证据后变化，[-0.15, 0.15]
    satiety: float = 0.0             # 只用于立即重复/表达冷却
    phase: str = "accumulating"      # accumulating / high / regulation_pending / recovering
    high_since: float | None = None
    high_elapsed_s: float = 0.0
    recovery_until: float = 0.0
    cycle_id: int = 0
    last_contact_at: float = 0.0
    pending_contact_id: str = ""
    regulation_due: bool = False
    updated_at: float = field(default_factory=now_ts)

    @property
    def level(self) -> float:
        return clamp01(1.0 - float(self.tension))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DriveState":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{str(k): v for k, v in (data or {}).items() if k in allowed})


@dataclass
class DriveConfig:
    dimensions: dict[str, DriveDynamics] = field(default_factory=dict)
    activation_half_life_hours: dict[str, float] = field(default_factory=dict)
    satiety_half_life_hours: dict[str, float] = field(default_factory=dict)
    global_contact_per_day: int = 3
    contact_window_hours: float = 24.0
    schema_version: int = 2
    rule_version: str = "drive-v2"

    def __post_init__(self) -> None:
        if not self.dimensions:
            self.dimensions = default_drive_dynamics()
        for name in DRIVE_DIMENSIONS:
            if name not in self.dimensions:
                self.dimensions[name] = default_drive_dynamics()[name]
            validate_drive_config(self.dimensions[name])
            self.activation_half_life_hours.setdefault(name, 3.0)
            self.satiety_half_life_hours.setdefault(name, 6.0)

    def dynamics(self, dimension: str) -> DriveDynamics:
        dim = canonical_dimension(dimension)
        if not dim:
            raise KeyError(f"unknown_drive_dimension:{dimension}")
        return self.dimensions[dim]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimensions": {k: v.to_dict() for k, v in self.dimensions.items()},
            "activation_half_life_hours": dict(self.activation_half_life_hours),
            "satiety_half_life_hours": dict(self.satiety_half_life_hours),
            "global_contact_per_day": int(self.global_contact_per_day),
            "contact_window_hours": float(self.contact_window_hours),
            "schema_version": int(self.schema_version),
            "rule_version": self.rule_version,
        }

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any] | None) -> "DriveConfig":
        """从 config.toml 的 [life_dynamics] 读取覆盖项。

        兼容两种写法：
          [life_dynamics.drives.intimacy] base_growth_per_hour = 0.02
          [life_dynamics.drives] intimacy = { base_growth_per_hour = 0.02 }
        以及人格文件里的 [persona.drives.intimacy]。参数名不猜。
        """
        mapping = mapping or {}
        dims = default_drive_dynamics()
        raw_dims = mapping.get("drives") or mapping.get("dimensions") or {}
        if isinstance(raw_dims, dict):
            for key, raw in raw_dims.items():
                dim = canonical_dimension(key)
                if not dim:
                    continue
                if isinstance(raw, (int, float)):
                    raw = {"persona_preference": float(raw)}
                if not isinstance(raw, dict):
                    continue
                current = dims[dim]
                allowed = {f for f in current.__dataclass_fields__ if f != "dimension"}
                values = {str(k): v for k, v in raw.items() if k in allowed}
                if "preference" in raw and "persona_preference" not in values:
                    values["persona_preference"] = raw["preference"]
                dims[dim] = replace(current, **values)
        activation = {canonical_dimension(k) or k: float(v)
                      for k, v in (mapping.get("activation_half_life_hours") or {}).items()}
        satiety = {canonical_dimension(k) or k: float(v)
                   for k, v in (mapping.get("satiety_half_life_hours") or {}).items()}
        return cls(dimensions=dims,
                   activation_half_life_hours=activation,
                   satiety_half_life_hours=satiety,
                   global_contact_per_day=int(mapping.get("global_contact_per_day", 3)),
                   contact_window_hours=float(mapping.get("contact_window_hours", 24.0)))


def default_drive_dynamics() -> dict[str, DriveDynamics]:
    """工程回放起点，不是人类生理常数。亲密与性欲各有独立参数。"""
    base = {
        "exploration": dict(initial_tension=.30, base_growth_per_hour=.040,
                            interaction_satisfaction_gain=.20,
                            regulation_relief_gain=.30, recovery_hours=3.0,
                            contact_cooldown_hours=6.0),
        "competence": dict(initial_tension=.35, base_growth_per_hour=.015,
                           interaction_satisfaction_gain=.22,
                           regulation_relief_gain=.28, recovery_hours=3.0,
                           contact_cooldown_hours=6.0),
        "relatedness": dict(initial_tension=.30, base_growth_per_hour=.025,
                            expression_threshold=.62,
                            interaction_satisfaction_gain=.24,
                            regulation_relief_gain=.25, recovery_hours=4.0,
                            contact_cooldown_hours=6.0),
        # 亲密：关系中的亲近、被抱、被回应；不是每一次安慰都算满足。
        "intimacy": dict(initial_tension=.24, base_growth_per_hour=.020,
                         reference_tension=.50, ceiling=1.0,
                         expression_threshold=.62, high_threshold=.88,
                         high_dwell_hours=2.0,
                         interaction_satisfaction_gain=.26,
                         regulation_relief_gain=.32, recovery_hours=4.0,
                         contact_cooldown_hours=12.0),
        # 性欲：独立的身体欲望维度，不与亲密合并。
        "libido": dict(initial_tension=.26, base_growth_per_hour=.025,
                       reference_tension=.50, ceiling=1.0,
                       expression_threshold=.65, high_threshold=.90,
                       high_dwell_hours=2.0,
                       interaction_satisfaction_gain=.30,
                       regulation_relief_gain=.35, recovery_hours=4.0,
                       contact_cooldown_hours=12.0),
        "coherence": dict(initial_tension=.28, base_growth_per_hour=.010,
                          expression_threshold=.66,
                          interaction_satisfaction_gain=.20,
                          regulation_relief_gain=.22, recovery_hours=2.0,
                          contact_cooldown_hours=12.0),
        "self_direction": dict(initial_tension=.30, base_growth_per_hour=.030,
                               expression_threshold=.65,
                               interaction_satisfaction_gain=.24,
                               regulation_relief_gain=.30, recovery_hours=4.0,
                               contact_cooldown_hours=8.0),
        # 余量由真实负荷和休整驱动，不按钟表凭空变累。
        "resource_reserve": dict(mode="resource_derived", initial_tension=.30,
                                 base_growth_per_hour=.0,
                                 expression_threshold=.70, high_threshold=.90,
                                 interaction_satisfaction_gain=.30,
                                 regulation_relief_gain=.35, recovery_hours=4.0,
                                 contact_cooldown_hours=12.0),
    }
    return {name: validate_drive_config(DriveDynamics(dimension=name, **values))
            for name, values in base.items()}


@dataclass
class DriveEvidence:
    id: str
    dimension: str
    kind: str
    delta: float = 0.0
    root_id: str = ""
    cycle_id: int = 0
    confidence: float = 0.5
    confirmed: bool = False
    real_participant: bool = False
    relationship_allows: bool = True
    participant_id: str = ""
    sources: list[str] = field(default_factory=list)
    occurred_at: float = field(default_factory=now_ts)
    note: str = ""

    def normalized(self) -> "DriveEvidence":
        dim = canonical_dimension(self.dimension)
        if not dim:
            raise ValueError(f"unknown_drive_dimension:{self.dimension}")
        if self.kind not in EFFECT_KINDS:
            raise ValueError(f"unknown_drive_effect:{self.kind}")
        return replace(self, dimension=dim, confidence=clamp01(self.confidence))


@dataclass
class DriveStateSet:
    scope_id: str
    drives: dict[str, DriveState] = field(default_factory=dict)
    updated_at: float = field(default_factory=now_ts)
    last_day: str = ""
    day_learning_used: float = 0.0
    schema_version: int = 2
    rule_version: str = "drive-v2"
    version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "schema_version": self.schema_version,
            "rule_version": self.rule_version,
            "updated_at": self.updated_at,
            "last_day": self.last_day,
            "day_learning_used": self.day_learning_used,
            "drives": {k: v.to_dict() for k, v in self.drives.items()},
        }

    @classmethod
    def from_dict(cls, scope_id: str, data: dict[str, Any],
                  config: DriveConfig | None = None) -> "DriveStateSet":
        config = config or DriveConfig()
        raw = data.get("drives") if isinstance(data, dict) else None
        drives: dict[str, DriveState] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                dim = canonical_dimension(key)
                if not dim or not isinstance(value, dict):
                    continue
                payload = dict(value)
                payload["dimension"] = dim
                drives[dim] = DriveState.from_dict(payload)
        for dim in DRIVE_DIMENSIONS:
            if dim not in drives:
                drives[dim] = initial_drive(dim, config)
        data = data or {}
        return cls(
            scope_id=scope_id,
            drives=drives,
            updated_at=float(data.get("updated_at") or now_ts()),
            last_day=str(data.get("last_day") or ""),
            day_learning_used=float(data.get("day_learning_used") or 0.0),
            schema_version=int(data.get("schema_version") or 2),
            rule_version=str(data.get("rule_version") or "drive-v2"),
            version=int(data.get("version") or 0),
        )


def initial_drive(dimension: str, config: DriveConfig | None = None,
                  *, at: float | None = None) -> DriveState:
    config = config or DriveConfig()
    c = config.dynamics(dimension)
    return DriveState(
        dimension=c.dimension,
        tension=clamp(float(c.initial_tension), 0.0, float(c.ceiling)),
        phase="accumulating",
        updated_at=float(now_ts() if at is None else at),
    )


def initial_state(scope_id: str, config: DriveConfig | None = None,
                  *, at: float | None = None) -> DriveStateSet:
    config = config or DriveConfig()
    at = float(now_ts() if at is None else at)
    return DriveStateSet(
        scope_id=scope_id,
        drives={dim: initial_drive(dim, config, at=at) for dim in DRIVE_DIMENSIONS},
        updated_at=at,
    )


def growth_k(c: DriveDynamics, *, context_factor: float = 1.0,
             in_recovery: bool = False, persona_factor: float = 1.0,
             persona_already_in_base: bool = True) -> float:
    if c.mode != "accumulating" or float(c.base_growth_per_hour) <= 0:
        return 0.0
    factor = float(persona_factor) if not persona_already_in_base else 1.0
    factor *= float(context_factor)
    factor = clamp(factor, float(c.growth_multiplier_min), float(c.growth_multiplier_max))
    if in_recovery:
        factor *= float(c.recovery_growth_multiplier)
    denom = max(1e-9, float(c.ceiling) - float(c.reference_tension))
    return float(c.base_growth_per_hour) * factor / denom


def passive_growth(x: float, c: DriveDynamics, dt_seconds: float, *,
                   context_factor: float = 1.0, in_recovery: bool = False) -> float:
    """在参数与情境不变的一段内解析推进；两小时一次 == 两次一小时。"""
    dt_h = max(0.0, float(dt_seconds)) / 3600.0
    k = growth_k(c, context_factor=context_factor, in_recovery=in_recovery)
    if k <= 0 or dt_h <= 0:
        return clamp(float(x), 0.0, float(c.ceiling))
    return clamp(float(c.ceiling) - (float(c.ceiling) - float(x)) * math.exp(-k * dt_h),
                 0.0, float(c.ceiling))


def threshold_crossing_seconds(x: float, threshold: float, c: DriveDynamics, *,
                               context_factor: float = 1.0,
                               in_recovery: bool = False) -> float:
    x = float(x)
    if x >= threshold:
        return 0.0
    k = growth_k(c, context_factor=context_factor, in_recovery=in_recovery)
    if k <= 0:
        return float("inf")
    return 3600.0 * math.log((float(c.ceiling) - x) /
                             max(1e-12, float(c.ceiling) - float(threshold))) / k


def _clear_high(d: DriveState) -> None:
    d.high_since = None
    d.high_elapsed_s = 0.0


def _begin_recovery(d: DriveState, c: DriveDynamics, now: float) -> None:
    _clear_high(d)
    d.recovery_until = float(now) + float(c.recovery_hours) * 3600.0
    d.phase = "recovering" if d.recovery_until > now else "accumulating"
    d.cycle_id = int(d.cycle_id) + 1


def advance_drive_segment(d: DriveState, c: DriveDynamics, start: float, end: float, *,
                          context_factor: float = 1.0,
                          online_from: float | None = None) -> DriveState:
    """推进一段参数/情境不变的时间。

    online_from 之后的连续高位时间才计入 high_elapsed_s；离线被动积累
    仍按公式发生，但不补造已经执行的调节。
    """
    start, end = float(start), float(end)
    if end <= start:
        d.updated_at = max(float(d.updated_at), end)
        return d
    d = replace(d)
    online_from = start if online_from is None else clamp(float(online_from), start, end)
    pending_regulation = d.phase == "regulation_pending"
    cursor = start
    guard = 0
    while cursor < end - 1e-9:
        guard += 1
        if guard > 64:
            raise ValueError("drive_timeline_did_not_advance")
        recovering = cursor < float(d.recovery_until) - 1e-9
        crossing_boundary = False
        if recovering:
            stop = min(end, float(d.recovery_until))
        elif float(d.tension) < float(c.high_threshold):
            crossing = threshold_crossing_seconds(
                d.tension, c.high_threshold, c,
                context_factor=context_factor, in_recovery=False)
            stop = end if not math.isfinite(crossing) else min(end, cursor + crossing)
            crossing_boundary = math.isfinite(crossing) and (cursor + crossing) <= end + 1e-9
        else:
            stop = end
        if stop <= cursor + 1e-9:
            if crossing_boundary:
                d.tension = float(c.high_threshold)
                d.high_since, d.high_elapsed_s = cursor, 0.0
                cursor = min(end, cursor + 1e-9)
                continue
            raise ValueError("non_advancing_drive_boundary")

        was_high = float(d.tension) >= float(c.high_threshold) - 1e-12
        d.tension = passive_growth(d.tension, c, stop - cursor,
                                  context_factor=context_factor,
                                  in_recovery=recovering)
        online_overlap = max(0.0, stop - max(cursor, online_from))

        if recovering:
            _clear_high(d)
            d.phase = "recovering" if stop < float(d.recovery_until) - 1e-9 else "accumulating"
        elif was_high or crossing_boundary:
            if d.high_since is None:
                d.high_since = cursor if was_high else stop
            if online_overlap > 0:
                d.high_elapsed_s = float(d.high_elapsed_s) + online_overlap
            d.phase = "high"
        if float(d.tension) < float(c.high_threshold) - 1e-12:
            _clear_high(d)
            d.phase = "accumulating"
        if pending_regulation and float(d.tension) >= float(c.high_threshold) - 1e-12:
            d.phase = "regulation_pending"
        cursor = stop

    d.updated_at = end
    d.regulation_due = bool(
        c.regulation_enabled
        and d.phase in {"high", "regulation_pending"}
        and float(d.high_elapsed_s) >= float(c.high_dwell_hours) * 3600.0
    )
    return d


def _decay(value: float, half_life_hours: float, seconds: float) -> float:
    if float(seconds) <= 0 or float(half_life_hours) <= 0:
        return float(value)
    return float(value) * math.exp(-LOG2 * float(seconds) / (float(half_life_hours) * 3600.0))


def advance_to(state: DriveStateSet, until: float, config: DriveConfig | None = None,
               *, online_seconds: float | None = None,
               context_factors: dict[str, float] | None = None) -> DriveStateSet:
    """纯函数时间推进。调用方负责持久化。

    online_seconds 表示这段时间里程序实际运行、可累计高位时长的秒数。
    离线区间仍推进被动需要，但不累计 high_elapsed_s。
    """
    config = config or DriveConfig()
    until = float(until)
    if until <= float(state.updated_at):
        return state
    dt = max(0.0, until - float(state.updated_at))
    online = dt if online_seconds is None else clamp(float(online_seconds), 0.0, dt)
    offline = max(0.0, dt - online)
    context = dict(context_factors or {})
    out = DriveStateSet.from_dict(state.scope_id, state.to_dict(), config)
    for dim in DRIVE_DIMENSIONS:
        d = out.drives[dim]
        c = config.dynamics(dim)
        factor = float(context.get(dim, 1.0))
        cursor = float(state.updated_at)
        if offline > 0:
            d = advance_drive_segment(d, c, cursor, cursor + offline,
                                      context_factor=factor, online_from=None)
            cursor += offline
        if online > 0:
            d = advance_drive_segment(d, c, cursor, cursor + online,
                                      context_factor=factor, online_from=cursor)
        d.activation = clamp(_decay(d.activation,
                                    config.activation_half_life_hours.get(dim, 3.0), dt),
                             -0.25, 0.25)
        d.satiety = clamp01(_decay(d.satiety,
                                   config.satiety_half_life_hours.get(dim, 6.0), dt))
        if float(d.tension) < float(c.high_threshold) - 1e-12 and d.phase != "recovering":
            d.phase = "accumulating"
        out.drives[dim] = d
    out.updated_at = until
    return out


def view_at(state: DriveStateSet, now: float, config: DriveConfig | None = None,
            *, context_factors: dict[str, float] | None = None) -> dict[str, dict[str, Any]]:
    """只读视图：可以推进纯时间，但不结算满足、不执行调节、不写库。"""
    config = config or DriveConfig()
    advanced = advance_to(state, max(float(now), float(state.updated_at)), config,
                          online_seconds=0.0, context_factors=context_factors)
    return drive_view(advanced, config)


def drive_view(state: DriveStateSet, config: DriveConfig | None = None) -> dict[str, dict[str, Any]]:
    config = config or DriveConfig()
    out: dict[str, dict[str, Any]] = {}
    for dim in DRIVE_DIMENSIONS:
        d = state.drives.get(dim) or initial_drive(dim, config)
        c = config.dynamics(dim)
        pressure = clamp01(float(d.tension) + float(d.learned_offset) + float(d.activation))
        out[dim] = {
            "dimension": dim,
            "label": DRIVE_LABELS[dim],
            "mode": c.mode,
            "tension": round(float(d.tension), 6),
            "level": round(float(1.0 - d.tension), 6),
            "pressure": round(pressure, 6),
            "activation": round(float(d.activation), 6),
            "learned_offset": round(float(d.learned_offset), 6),
            "satiety": round(float(d.satiety), 6),
            "phase": d.phase,
            "high_since": d.high_since,
            "high_elapsed_s": round(float(d.high_elapsed_s), 3),
            "recovery_until": float(d.recovery_until),
            "regulation_due": bool(getattr(d, "regulation_due", False)),
            "cycle_id": int(d.cycle_id),
            "can_express": bool(float(d.tension) >= float(c.expression_threshold)),
            "preference": round(float(c.persona_preference), 4),
            "expression_threshold": float(c.expression_threshold),
            "high_threshold": float(c.high_threshold),
        }
    return out


def apply_one_evidence(state: DriveStateSet, evidence: DriveEvidence,
                       config: DriveConfig | None = None, *,
                       now: float | None = None) -> tuple[DriveStateSet, dict[str, Any]]:
    """应用一条已校验、已判重的证据；返回新状态与实际结算记录。

    调用方负责事务与去重。这个函数只保证数值规则与阶段转换。
    """
    config = config or DriveConfig()
    ev = evidence.normalized()
    at = float(now_ts() if now is None else now)
    out = DriveStateSet.from_dict(state.scope_id, state.to_dict(), config)
    d = out.drives[ev.dimension]
    c = config.dynamics(ev.dimension)
    before = float(d.tension)
    actual = 0.0

    if ev.kind == "activation_change":
        delta = clamp(float(ev.delta) * max(0.0, min(1.0, float(ev.confidence))),
                      -0.25, 0.25)
        old = float(d.activation)
        d.activation = clamp(old + delta, -0.25, 0.25)
        actual = float(d.activation) - old
    elif ev.kind in {"interaction_satisfaction", "verified_task_satisfaction"}:
        if ev.kind == "interaction_satisfaction":
            if not ev.confirmed or not ev.real_participant or not ev.relationship_allows:
                raise ValueError("interaction_satisfaction_requires_real_participant")
        gain = max(0.0, float(ev.delta if ev.delta else c.interaction_satisfaction_gain))
        gain = min(float(c.ceiling), gain * max(0.0, min(1.0, float(ev.confidence))))
        d.tension = max(0.0, before - gain)
        d.satiety = clamp01(float(d.satiety) + min(0.5, gain))
        _begin_recovery(d, c, at)
        actual = float(d.tension) - before
    elif ev.kind == "internal_regulation":
        if not ev.confirmed:
            raise ValueError("internal_regulation_requires_confirmed_plan")
        if int(ev.cycle_id) != int(d.cycle_id):
            raise ValueError("regulation_cycle_replaced")
        if not bool(getattr(d, "regulation_due", False)):
            raise ValueError("regulation_not_due")
        if at < float(d.recovery_until) - 1e-9:
            raise ValueError("regulation_still_recovering")
        relief = max(0.0, float(ev.delta if ev.delta else c.regulation_relief_gain))
        d.tension = max(0.0, before - relief)
        _begin_recovery(d, c, at)
        actual = float(d.tension) - before
    elif ev.kind == "preference_learning":
        # 慢学习由跨天证据结算函数处理；这里只排队，不直接改 learned_offset。
        actual = 0.0
    else:  # pragma: no cover - normalized() 已拦截
        raise ValueError(f"unsupported_drive_effect:{ev.kind}")

    d.updated_at = at
    out.updated_at = max(float(out.updated_at), at)
    settlement = {
        "dimension": ev.dimension,
        "effect_kind": ev.kind,
        "root_id": ev.root_id or ev.id,
        "cycle_id": int(ev.cycle_id),
        "before_value": round(before, 6),
        "after_value": round(float(d.tension), 6),
        "delta": round(actual, 6),
        "evidence_id": ev.id,
        "note": ev.note[:240],
    }
    return out, settlement


@dataclass
class ContactState:
    unanswered: bool = False
    quiet_now: bool = False
    last_attempt_at: dict[str, float] = field(default_factory=dict)
    attempts_in_window: dict[str, int] = field(default_factory=dict)
    global_attempts_in_window: int = 0
    explicit_refusal: set[str] = field(default_factory=set)
    relationship_allows: set[str] = field(default_factory=set)


def mark_contact_attempt(state: DriveStateSet, dimension: str, *,
                         at: float, contact_id: str = "",
                         config: DriveConfig | None = None) -> DriveStateSet:
    config = config or DriveConfig()
    out = DriveStateSet.from_dict(state.scope_id, state.to_dict(), config)
    dim = canonical_dimension(dimension)
    if not dim:
        raise ValueError(f"unknown_drive_dimension:{dimension}")
    d = out.drives[dim]
    d.last_contact_at = float(at)
    d.pending_contact_id = str(contact_id or "")
    out.updated_at = max(float(out.updated_at), float(at))
    return out


def may_propose_contact(view: dict[str, dict[str, Any]], dimension: str,
                        config: DriveConfig | None = None,
                        contact: ContactState | None = None, *,
                        now: float) -> bool:
    """`True` 只表示可以生成一个主动表达候选，不表示已经发送。"""
    config = config or DriveConfig()
    dim = canonical_dimension(dimension)
    if not dim:
        return False
    row = view.get(dim) or {}
    c = config.dynamics(dim)
    contact = contact or ContactState()
    if float(row.get("tension", 0.0)) < float(c.expression_threshold):
        return False
    if dim in contact.explicit_refusal:
        return False
    if contact.relationship_allows and dim not in contact.relationship_allows:
        return False
    if contact.unanswered or contact.quiet_now:
        return False
    last = float(contact.last_attempt_at.get(dim, 0.0))
    if now < last + float(c.contact_cooldown_hours) * 3600.0:
        return False
    if int(contact.attempts_in_window.get(dim, 0)) >= int(c.max_contact_attempts_per_window):
        return False
    if int(contact.global_attempts_in_window) >= int(config.global_contact_per_day):
        return False
    return True


def contact_candidates(view: dict[str, dict[str, Any]], config: DriveConfig | None = None,
                       contact: ContactState | None = None, *, now: float) -> list[str]:
    return [dim for dim in DRIVE_DIMENSIONS
            if may_propose_contact(view, dim, config, contact, now=now)]


def regulation_candidates(view: dict[str, dict[str, Any]]) -> list[str]:
    return [dim for dim in DRIVE_DIMENSIONS if bool((view.get(dim) or {}).get("regulation_due"))]


def summarize(view: dict[str, dict[str, Any]], scale: float = 10.0) -> dict[str, float]:
    """0–10 显示值；亲密与性欲始终是两项。"""
    return {row["label"]: round(float(row["pressure"]) * float(scale), 2)
            for row in view.values()}


def emotion_baseline_from_drives(view: dict[str, dict[str, Any]],
                                 base: dict[str, float] | None = None) -> dict[str, float]:
    """把驱力压力有界地投射到情绪基线；只用于显示/旧接口兼容。

    这是小幅调制，不是把情绪当成欲望的另一个副本。亲密与性欲分别影响
    不同的情绪通道：亲密更偏向依恋/委屈，性欲更偏向兴奋。
    """
    out = dict(base or {"joy": .20, "concern": .15, "frustration": .10,
                        "hurt": .10, "anticipation": .20})
    def press(dim: str) -> float:
        return float((view.get(dim) or {}).get("pressure", 0.0))
    out["concern"] = clamp01(out["concern"] + 0.10 * max(0.0, press("relatedness") - .45))
    out["hurt"] = clamp01(out["hurt"] + 0.06 * max(0.0, press("intimacy") - .50))
    out["joy"] = clamp01(out["joy"] + 0.08 * max(0.0, press("intimacy") - .40))
    out["joy"] = clamp01(out["joy"] + 0.04 * max(0.0, press("relatedness") - .50))
    out["anticipation"] = clamp01(out["anticipation"] + 0.10 * max(0.0, press("libido") - .45))
    out["anticipation"] = clamp01(out["anticipation"] + 0.05 * max(0.0, press("exploration") - .45))
    out["frustration"] = clamp01(out["frustration"] + 0.05 * max(0.0, press("competence") - .55))
    return out
