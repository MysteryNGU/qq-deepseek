#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自主学习线程（设计 16.1）。

状态机 + 持久化；真正搜索/读网页仍走现有 Executor 与出站检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import Scope, new_id, now_ts

STUDY_STATES = {"planned", "researching", "consolidating", "applying",
                "paused", "complete"}
STUDY_ALLOWED = {
    "planned": {"researching", "paused", "complete"},
    "researching": {"consolidating", "paused", "complete"},
    "consolidating": {"applying", "researching", "paused", "complete"},
    "applying": {"researching", "paused", "complete"},
    "paused": {"planned", "researching", "complete"},
    "complete": set(),
}


@dataclass
class StudyThread:
    id: str
    scope: Scope
    topic: str
    specific_question: str = ""
    origin_want_id: str = ""
    why_evidence_ids: list[str] = field(default_factory=list)
    learning_checks: list[str] = field(default_factory=list)
    known_card_ids: list[str] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    next_step: str = ""
    status: str = "planned"
    next_review_at: float | None = None
    budget: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)


def advance_study(thread: StudyThread, status: str, *, next_step: str | None = None,
                  review_at: float | None = None) -> StudyThread:
    if status == thread.status:
        return thread
    if status not in STUDY_ALLOWED.get(thread.status, set()):
        raise ValueError(f"学习线程状态不允许：{thread.status} -> {status}")
    thread.status = status
    if next_step is not None:
        thread.next_step = next_step[:300]
    if review_at is not None:
        thread.next_review_at = review_at
    thread.version += 1
    thread.updated_at = now_ts()
    return thread


def record_study(repo, thread: StudyThread, *, expected_epoch: int | None = None) -> str:
    with repo.tx():
        if (expected_epoch is not None
                and repo.forget_epoch(thread.scope.scope_id) != expected_epoch):
            return ""
        repo._exec(
            "INSERT OR REPLACE INTO study_threads(id,agent_id,conversation_id,topic,"
            "specific_question,origin_want_id,why_evidence_ids,learning_checks,"
            "known_card_ids,unresolved_questions,next_step,status,next_review_at,"
            "budget,version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (thread.id, thread.scope.agent_id, thread.scope.conversation_id,
             thread.topic, thread.specific_question, thread.origin_want_id,
             _json(thread.why_evidence_ids), _json(thread.learning_checks),
             _json(thread.known_card_ids), _json(thread.unresolved_questions),
             thread.next_step, thread.status, thread.next_review_at,
             _json(thread.budget), thread.version, thread.created_at,
             thread.updated_at),
        )
    return thread.id


def _json(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
