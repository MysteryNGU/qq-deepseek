#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动作契约与结果验证（设计 9.1 的最小实现）。

核心规则：
- "点击成功"不等于"消息已发"；"退出码 0"不等于"功能修好"；
- 结果不明一律 unknown，不能写成 succeeded；
- 同一 action + spec 只执行一次，重试读取已有 attempt。
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .contracts import Scope, new_id, now_ts


@dataclass
class ActionSpec:
    id: str
    scope: Scope
    kind: str
    recipient_or_resource: str = ""
    requested_effects: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    postconditions: list[str] = field(default_factory=list)
    permission_grant_id: str = ""
    idempotency_key: str = ""
    retry_policy: str = "no_auto_retry"
    plan_id: str = ""


@dataclass
class ActionResult:
    action_id: str
    status: str                      # succeeded / failed / partial / unknown
    observations: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    postconditions_passed: list[str] = field(default_factory=list)
    error_category: str = ""
    actual_cost: float = 0.0
    next_safe_step: str = ""


@dataclass
class ActionAttempt:
    id: str
    action_id: str
    scope: Scope
    spec_hash: str
    status: str = "prepared"
    result: ActionResult | None = None
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)


def spec_hash(spec: ActionSpec) -> str:
    raw = json.dumps({
        "id": spec.id, "kind": spec.kind,
        "scope": spec.scope.scope_id,
        "participant": spec.scope.participant_id,
        "audience": spec.scope.audience_policy_id,
        "plan_id": spec.plan_id, "grant_id": spec.permission_grant_id,
        "pre": list(spec.preconditions), "idempotency_key": spec.idempotency_key,
        "target": spec.recipient_or_resource,
        "effects": sorted(spec.requested_effects),
        "post": sorted(spec.postconditions),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def result_is_proven(spec: ActionSpec, result: ActionResult) -> bool:
    """只有 succeeded + 全部后置条件 + 证据，才算证明完成。"""
    if result.status != "succeeded":
        return False
    if not set(spec.postconditions).issubset(set(result.postconditions_passed)):
        return False
    if spec.postconditions and not result.evidence_ids:
        return False
    return True


async def execute_action(spec: ActionSpec, adapter: Callable[[ActionSpec], Any],
                         repo, *, granted_effects: set[str] | None = None,
                         policy=None, lease=None, jobs=None) -> ActionResult:
    """执行一次动作；未知/异常一律 unknown，绝不猜成功。"""
    restricted = set(spec.requested_effects) - set(granted_effects or set())
    if policy is not None:
        if spec.requested_effects and not policy.allows(
                spec.permission_grant_id, spec.scope, spec.plan_id,
                spec.requested_effects, [spec.recipient_or_resource]):
            restricted = set(spec.requested_effects)
        else:
            restricted = set()
    if restricted:
        return ActionResult(
            action_id=spec.id, status="failed",
            error_category="waiting_permission",
            next_safe_step=f"需要授权：{','.join(sorted(restricted))}",
        )

    if lease is not None and (jobs is None or not jobs.current(lease)):
        return ActionResult(spec.id, "unknown", error_category="job_lease_lost")
    budget = getattr(jobs, 'action_budget', None)
    if budget is not None and not budget.reserve(spec.id):
        return ActionResult(spec.id,'failed',error_category='budget_exhausted')
    with repo.tx():
        epoch = repo.forget_epoch(spec.scope.scope_id)
        attempt_id = repo.prepare_action_once(spec)
    rec = repo.get_action_attempt(attempt_id)
    if rec and rec["status"] in ("result_recorded", "projected"):
        return repo.action_result(attempt_id) or ActionResult(
            action_id=spec.id, status="unknown", error_category="already_attempted")
    # 中断在 dispatching：先归 unknown 对账，绝不由第二个 worker 重新执行
    if rec and rec["status"] in ("dispatching", "unknown"):
        return ActionResult(
            action_id=spec.id, status="unknown",
            error_category="interrupted_dispatching",
            next_safe_step="先核实外部结果，不要重复执行",
        )
    # 只有 CAS 从 prepared -> dispatching 成功的 worker 才能执行
    if not repo.mark_action_dispatching(attempt_id):
        return ActionResult(
            action_id=spec.id, status="unknown",
            error_category="lease_lost",
            next_safe_step="另一个 worker 已领取，先等待对账",
        )
    try:
        raw = adapter(spec)
        if inspect.isawaitable(raw):
            raw = await raw
        result = raw if isinstance(raw, ActionResult) else ActionResult(
            action_id=spec.id, status="unknown",
            observations=[str(raw)[:200]],
            error_category="adapter_returned_unstructured",
        )
    except Exception as exc:  # noqa: BLE001 - 传输/超时/取消都算 unknown
        result = ActionResult(
            action_id=spec.id, status="unknown",
            observations=[f"{type(exc).__name__}: {exc}"[:200]],
            error_category="transport_unknown",
            next_safe_step="先对账，不自动重发",
        )
    # 模型/适配器说 succeeded 不算；必须有后置条件和证据
    if result.status == "succeeded" and not result_is_proven(spec, result):
        result.status = "partial" if result.evidence_ids else "unknown"
        result.error_category = "unproven_success"
        result.next_safe_step = "补齐后置条件或证据后再判定成功"
    with repo.tx():
        if (repo.forget_epoch(spec.scope.scope_id) != epoch
                or lease is not None and (jobs is None or not jobs.current(lease))):
            return ActionResult(spec.id, "unknown", error_category="result_invalidated",
                                next_safe_step="核对已发生的动作，不重新执行")
        repo.record_action_result(attempt_id, result)
    return result
