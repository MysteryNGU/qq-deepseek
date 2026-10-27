#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共用数据契约。字段名尽量贴设计文档第 4/5/6 节，方便逐条对照。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


EMOTIONS = ("joy", "concern", "frustration", "hurt", "anticipation")
EMOTION_ZH = {
    "joy": "愉悦",
    "concern": "不安",
    "frustration": "恼",
    "hurt": "委屈",
    "anticipation": "兴奋",
}

SOURCE_KINDS = {"user_report", "tool_observation", "self_output", "inference"}
MEMORY_KINDS = {
    "episode", "fact", "knowledge", "interpretation", "lesson", "diary",
}
MEMORY_STATUS = {
    "candidate", "active", "disputed", "superseded", "archived", "redacted",
}


def now_ts() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def clamp01(value: float) -> float:
    return clamp(value, 0.0, 1.0)


@dataclass(frozen=True)
class Scope:
    """一个操作可见的最小范围。

    conversation_id 必须由服务端生成，不能拿 participant_id 代替：
    同一个人在私聊和群聊里是两段不同的记忆。
    """

    agent_id: str
    conversation_id: str
    participant_id: str = ""
    audience_policy_id: str = ""

    @property
    def scope_id(self) -> str:
        return f"{self.agent_id}|{self.conversation_id}"

    @property
    def private_key(self) -> tuple[str, str]:
        return (self.agent_id, self.conversation_id)


@dataclass
class Event:
    id: str
    scope: Scope
    kind: str                      # message / action_result / observation / delivery
    source_kind: str               # user_report / tool_observation / self_output / inference
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: float = field(default_factory=now_ts)
    received_at: float = field(default_factory=now_ts)
    external_id: str | None = None
    ingest_seq: int = 0
    actor_id: str = ""
    source_event_ids: list[str] = field(default_factory=list)
    schema_version: int = 1
    redacted_at: float | None = None

    @property
    def text(self) -> str:
        return str(self.payload.get("text") or "")

    @property
    def role(self) -> str:
        return str(self.payload.get("role") or "")


@dataclass
class Turn:
    id: str
    scope: Scope
    origin: str                    # user_request / initiative / scheduled_commitment
    input_event_ids: list[str]
    status: str = "queued"
    generation: int = 1
    lease_owner: str | None = None
    lease_until: float | None = None
    context_revision: int = 1
    policy_epoch: int = 0
    forget_epoch: int = 0
    created_at: float = field(default_factory=now_ts)


@dataclass
class MemorySource:
    memory_id: str
    event_id: str
    relation: str = "supports"     # supports / contradicts / corrects / contextualizes
    quote_start: int | None = None
    quote_end: int | None = None


@dataclass
class Memory:
    id: str
    scope: Scope
    kind: str
    summary: str
    source_kind: str = "inferred"
    status: str = "candidate"
    subject_id: str = ""
    predicate: str = ""
    normalized_value: str = ""
    event_group_id: str = ""
    topic_tags: list[str] = field(default_factory=list)
    valid_from: float | None = None
    valid_until: float | None = None
    time_precision: str = "exact"
    recorded_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)
    version: int = 1
    salience: float = 0.5
    source_quality: float = 0.5
    uncertainty_reason: str = ""
    supersedes_id: str = ""
    derived_from: list[str] = field(default_factory=list)
    last_mentioned_at: float | None = None
    mention_count_window: int = 0
    retention_policy_id: str = "default"
    forget_epoch: int = 0
    sources: list[MemorySource] = field(default_factory=list)


@dataclass
class OpenLoop:
    id: str
    scope: Scope
    kind: str                      # request / commitment / shared_project / curiosity / study
    summary: str
    source_event_ids: list[str] = field(default_factory=list)
    expected_outcome: str = ""
    acceptance_checks: list[str] = field(default_factory=list)
    status: str = "proposed"
    next_step: str = ""
    checkpoint: str = ""
    waiting_for: str = ""
    due_at: float | None = None
    next_review_at: float | None = None
    retry_after: float | None = None
    permission_scope_id: str = ""
    result_evidence_ids: list[str] = field(default_factory=list)
    version: int = 1
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)


@dataclass
class Appraisal:
    id: str
    scope: Scope
    source_event_ids: list[str]
    event_type: str = "ordinary"
    relevance: float = 0.0
    goal_congruence: float = 0.0
    certainty: float = 0.5
    novelty: float = 0.0
    interpretation: str = ""
    quoted_or_hypothetical: bool = False
    negated: bool = False
    proposed_affect_delta: dict[str, float] = field(default_factory=dict)
    relation_evidence: dict[str, float] | None = None
    resolves_cause_ids: list[str] = field(default_factory=list)
    status: str = "valid"
    supersedes_appraisal_id: str | None = None
    # drive_v2：模型/规则提出的需要激活、满足与用户当下需要。数值由代码裁剪。
    user_need: str = "unclear"      # assistance/listening/quiet_company/sharing/boundary/unclear
    target: str = ""
    affected_need_ids: list[str] = field(default_factory=list)
    drive_evidence: list[dict[str, Any]] = field(default_factory=list)
    context_refs: list[str] = field(default_factory=list)
    subject_actor_id: str = ''
    meaning_kind: str = 'observation'
    recovery_half_life_seconds: dict[str, float] = field(default_factory=dict)
    persistence_reason: str = ''
    revision: int = 1
    based_on_state_version: int | None = None
    stimulus_event_id: str = ''
    relation_protocol: int | None = None
    based_on_relationship_version: int | None = None
    relation_impacts: list[dict[str, Any]] = field(default_factory=list)
    relation_context_changes: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AffectState:
    scope: Scope
    intensity: dict[str, float] = field(
        default_factory=lambda: {k: 0.2 for k in EMOTIONS})
    causes: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=now_ts)
    version: int = 1
    recovery_half_life_seconds: dict[str, float] = field(default_factory=dict)
    baseline: dict[str, float] = field(default_factory=dict)


@dataclass
class AffectChange:
    state: AffectState
    decay_delta: dict[str, float]
    stimulus_delta: dict[str, float]
    applied_stimulus_delta: dict[str, float]
    applied_delta: dict[str, float]
    source_appraisal_id: str
    proposed_delta: dict[str, float] = field(default_factory=dict)
    clipping_reasons: dict[str, list[str]] = field(default_factory=dict)
    drive_effects: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DesireEvidence:
    id: str
    scope: Scope
    kind: str                      # meaningful_gain / frustration / satisfaction / correction
    dimension: str
    supported_delta: float
    confidence: float = 0.5
    root_event_ids: list[str] = field(default_factory=list)
    appraisal_id: str = ""
    independent_experience_group: str = ""
    occurred_at: float = field(default_factory=now_ts)


@dataclass
class DesireState:
    scope: Scope
    baseline: dict[str, float] = field(default_factory=dict)
    learned_offset: dict[str, float] = field(default_factory=dict)
    activation: dict[str, float] = field(default_factory=dict)
    satiety: dict[str, float] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    last_window_id: str = ""
    daily_learning_used: float = 0.0
    experience_ids: list[str] = field(default_factory=list)
    pending_learning: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=now_ts)
    version: int = 1


@dataclass
class LegacyRelationshipState:
    scope: Scope
    relationship_id: str = ""
    familiarity: float = 0.3
    trust: float = 0.5
    comfort: float = 0.5
    preferred_address: str = ""
    conversation_preferences: dict[str, Any] = field(default_factory=dict)
    open_repairs: list[dict[str, Any]] = field(default_factory=list)
    last_meaningful_interaction_at: float = 0.0
    evidence_ids: list[str] = field(default_factory=list)
    experience_ids: list[str] = field(default_factory=list)
    daily_key: str = ""
    daily_used: dict[str, float] = field(default_factory=dict)
    version: int = 1
    updated_at: float = field(default_factory=now_ts)


@dataclass
class RelationshipState:
    """One current metrics map. Scalar properties are read-only consumers."""
    scope: Scope
    actor_id: str = ''
    metrics: dict[str, float] = field(default_factory=dict)
    schema_version: int = 2
    policy_version: str = ''
    semantics_version: str = 'relationship-semantics-2'
    metric_basis: dict[str, str] = field(default_factory=dict)
    daily_key: str = ''
    daily_used: dict[str, float] = field(default_factory=dict)
    last_journal_id: int | None = None
    updated_at: float = field(default_factory=now_ts)
    version: int = 0

    @property
    def familiarity(self):
        return self.metrics['familiarity']

    @property
    def trust(self):
        return self.metrics['trust']

    @property
    def comfort(self):
        return self.metrics['comfort']

    @property
    def bond(self):
        return self.metrics['bond']

    @property
    def relationship_id(self):
        return self.actor_id


@dataclass
class RelationEvidence:
    id: str
    scope: Scope
    kind: str                      # familiarity / trust / comfort / repair / boundary
    delta: float
    confidence: float = 0.5
    root_event_ids: list[str] = field(default_factory=list)
    independent_experience_group: str = ""
    note: str = ""
    occurred_at: float = field(default_factory=now_ts)


@dataclass
class MoodState:
    scope: Scope
    pleasure: float = 0.0          # -1..1
    activation: float = 0.35       # 0..1
    energy: float = 0.6            # 0..1
    updated_at: float = field(default_factory=now_ts)
    version: int = 1
