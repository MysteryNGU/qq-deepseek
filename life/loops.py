#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenLoop 状态机（设计 8.2）。

超时不等于失败，失败不等于完成，暂停不等于丢弃；
承诺不能静默退休；succeeded 必须有证据。
"""

from __future__ import annotations

from dataclasses import replace

from .contracts import OpenLoop, now_ts

ALLOWED: dict[str, set[str]] = {
    "proposed": {"ready", "cancelled", "retired"},
    "ready": {"running", "waiting_permission", "waiting_dependency",
              "paused", "blocked", "cancelled", "retired"},
    "running": {"succeeded", "failed", "partial", "waiting_permission",
                "waiting_dependency", "paused", "outcome_unknown", "blocked",
                "cancelled"},
    "waiting_permission": {"ready", "cancelled", "blocked"},
    "waiting_dependency": {"ready", "cancelled", "blocked"},
    "paused": {"ready", "cancelled"},
    "outcome_unknown": {"succeeded", "failed", "ready", "unknown"},
    "blocked": {"ready", "cancelled", "retired"},
    "failed": {"ready", "cancelled"},
    "partial": {"ready", "succeeded", "cancelled"},
    "succeeded": set(),
    "cancelled": set(),
    "retired": set(),
}

COMMITMENT_KINDS = {"commitment", "request", "shared_project"}


def transition(loop: OpenLoop, new_status: str, *, now: float | None = None,
               evidence_ids: list[str] | None = None,
               next_step: str | None = None,
               checkpoint: str | None = None,
               waiting_for: str | None = None,
               next_review_at: float | None = None) -> OpenLoop:
    if new_status == loop.status:
        return loop
    if new_status not in ALLOWED.get(loop.status, set()):
        raise ValueError(f"不允许的状态迁移：{loop.status} -> {new_status}")
    if loop.kind in COMMITMENT_KINDS and new_status == "retired":
        raise ValueError("承诺不能静默退休；请显式 cancelled 或给出交代")
    if new_status == "succeeded" and not (evidence_ids or loop.result_evidence_ids):
        raise ValueError("succeeded 必须有证据，不能只凭模型说完成了")

    ts = float(now or now_ts())
    merged_evidence = list(dict.fromkeys(
        list(loop.result_evidence_ids) + list(evidence_ids or [])))
    return replace(
        loop,
        status=new_status,
        next_step=loop.next_step if next_step is None else next_step[:300],
        checkpoint=loop.checkpoint if checkpoint is None else checkpoint[:1000],
        waiting_for=loop.waiting_for if waiting_for is None else waiting_for[:200],
        next_review_at=(loop.next_review_at if next_review_at is None else next_review_at),
        result_evidence_ids=merged_evidence,
        version=loop.version + 1,
        updated_at=ts,
    )


def is_terminal(loop: OpenLoop) -> bool:
    return loop.status in {"succeeded", "cancelled", "retired"}
