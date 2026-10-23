#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力缺口与进化提案（设计 15.3/15.4 的队列与闸门）。

这一层只做"记录、去重、排队、权限判断"，不自己安装插件、不改代码。
真正的执行必须走现有 ActionExecutor/工坊和授权；没有可靠隔离时只生成候选。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import Scope, new_id, now_ts

GAP_STATES = {"observed", "investigating", "experimenting", "waiting",
              "resolved", "deferred"}
GAP_ALLOWED = {
    "observed": {"investigating", "waiting", "deferred", "resolved"},
    "investigating": {"experimenting", "waiting", "deferred", "resolved"},
    "experimenting": {"waiting", "deferred", "resolved"},
    "waiting": {"investigating", "experimenting", "deferred", "resolved"},
    "deferred": {"observed", "investigating"},
    "resolved": set(),
}
PROPOSAL_STATES = {"proposed", "prepared", "testing", "evaluated", "rejected",
                   "publishable", "published", "rolled_back"}
PROPOSAL_ALLOWED = {
    "proposed": {"prepared", "rejected"},
    "prepared": {"testing", "rejected"},
    "testing": {"evaluated", "rejected"},
    "evaluated": {"publishable", "rejected"},
    "publishable": {"published", "rejected"},
    "published": {"rolled_back"},
    "rolled_back": set(),
    "rejected": set(),
}
HIGH_RISK_EFFECTS = {
    "install_plugin", "modify_code", "publish_release", "network_write",
    "credential_add", "delete_data", "expand_permission",
}


@dataclass
class CapabilityGap:
    id: str
    scope: Scope
    normalized_need: str
    environment: dict[str, Any] = field(default_factory=dict)
    linked_open_loops: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    trigger_kind: str = "observed"
    expected_outcome: str = ""
    acceptance_checks: list[str] = field(default_factory=list)
    status: str = "observed"
    attempt_budget: int = 2
    attempts: int = 0
    next_review_at: float | None = None
    failure_signature: str = ""
    version: int = 1
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)


@dataclass
class EvolutionProposal:
    id: str
    gap_id: str
    scope: Scope
    kind: str                      # skill / plugin / code / strategy
    hypothesis: str = ""
    baseline_evidence_ids: list[str] = field(default_factory=list)
    expected_gain: str = ""
    acceptance_checks: list[str] = field(default_factory=list)
    requested_effects: list[str] = field(default_factory=list)
    experiment_budget: float = 0.0
    permission_scope_id: str = ""
    status: str = "proposed"
    candidate_artifact: str = ""
    evaluation_id: str = ""
    release_id: str = ""
    version: int = 1
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)


def advance_gap(gap: CapabilityGap, status: str) -> CapabilityGap:
    if status == gap.status:
        return gap
    if status not in GAP_ALLOWED.get(gap.status, set()):
        raise ValueError(f"缺口状态不允许：{gap.status} -> {status}")
    gap.status = status
    gap.version += 1
    gap.updated_at = now_ts()
    return gap


def advance_proposal(proposal: EvolutionProposal, status: str) -> EvolutionProposal:
    if status == proposal.status:
        return proposal
    if status not in PROPOSAL_ALLOWED.get(proposal.status, set()):
        raise ValueError(f"提案状态不允许：{proposal.status} -> {status}")
    proposal.status = status
    proposal.version += 1
    proposal.updated_at = now_ts()
    return proposal


def required_grant_missing(proposal: EvolutionProposal,
                           granted_effects: set[str] | None = None) -> list[str]:
    """返回需要额外授权的高风险效果；空列表表示可以按现有授权继续。"""
    granted = set(granted_effects or set())
    return sorted(set(proposal.requested_effects) & HIGH_RISK_EFFECTS - granted)


def can_auto_publish(proposal: EvolutionProposal,
                     granted_effects: set[str],
                     evaluation_passed: bool,
                     rollback_ready: bool) -> bool:
    """自动发布的最低门槛：授权覆盖、评估通过、可回退，三者缺一不可。"""
    return (not required_grant_missing(proposal, granted_effects)
            and evaluation_passed and rollback_ready
            and proposal.status in ("evaluated", "publishable"))


def record_gap(repo, gap: CapabilityGap) -> str:
    with repo.tx():
        repo._exec(
            "INSERT OR REPLACE INTO capability_gaps(id,agent_id,conversation_id,"
            "normalized_need,environment,linked_open_loops,evidence_ids,trigger_kind,"
            "expected_outcome,acceptance_checks,status,attempt_budget,attempts,"
            "next_review_at,failure_signature,version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (gap.id, gap.scope.agent_id, gap.scope.conversation_id,
             gap.normalized_need, _json(gap.environment), _json(gap.linked_open_loops),
             _json(gap.evidence_ids), gap.trigger_kind, gap.expected_outcome,
             _json(gap.acceptance_checks), gap.status, gap.attempt_budget,
             gap.attempts, gap.next_review_at, gap.failure_signature,
             gap.version, gap.created_at, gap.updated_at),
        )
    return gap.id


def record_proposal(repo, proposal: EvolutionProposal) -> str:
    with repo.tx():
        repo._exec(
            "INSERT OR REPLACE INTO evolution_proposals(id,gap_id,agent_id,conversation_id,"
            "kind,hypothesis,baseline_evidence,expected_gain,acceptance_checks,"
            "requested_effects,experiment_budget,permission_scope_id,status,"
            "candidate_artifact,evaluation_id,release_id,version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (proposal.id, proposal.gap_id, proposal.scope.agent_id,
             proposal.scope.conversation_id, proposal.kind, proposal.hypothesis,
             _json(proposal.baseline_evidence_ids), proposal.expected_gain,
             _json(proposal.acceptance_checks), _json(proposal.requested_effects),
             proposal.experiment_budget, proposal.permission_scope_id,
             proposal.status, proposal.candidate_artifact, proposal.evaluation_id,
             proposal.release_id, proposal.version, proposal.created_at,
             proposal.updated_at),
        )
    return proposal.id


def _json(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
