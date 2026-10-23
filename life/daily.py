#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日作业：日记、投影、清理的持久化入口（设计 6.6/11.3）。

首版只实现日记：按本地日期窗口取事件，写一条 interpretation 记忆。
作业键是 (scope, local_date, diary, version)，错过可补做一次，不会一天写多篇。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .contracts import Memory, Scope
from .runtime import LifeRuntime
from .text import one_line


def _day_bounds(local_date: str) -> tuple[float, float]:
    day = datetime.strptime(local_date, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def run_missing_diaries(runtime: LifeRuntime, scope: Scope, today: str,
                        days: int = 3) -> int:
    """补做最近几天漏掉的日记；running 卡住会按租约重排，failed 会重试。"""
    wrote = 0
    base = datetime.strptime(today, "%Y-%m-%d")
    for offset in range(max(1, days)):
        day = (base - timedelta(days=offset)).strftime("%Y-%m-%d")
        status = runtime.repo.daily_job_status(scope.scope_id, day, "diary", 1)
        if status == "done":
            continue
        if status == "failed":
            runtime.repo.requeue_daily_job(scope.scope_id, day, "diary", 1)
        # running 的作业由原 worker 持有；run_diary_job 的 CAS 只在租约过期时才会接手
        if run_diary_job(runtime, scope, day) is not None:
            wrote += 1
    return wrote


def run_diary_job(runtime: LifeRuntime, scope: Scope, local_date: str, *,
                  writer: Callable[[list], str] | None = None) -> Memory | None:
    runtime.repo.ensure_daily_job(scope.scope_id, local_date, "diary", 1)
    with runtime.repo.tx():
        epoch = runtime.repo.forget_epoch(scope.scope_id)
        lease_token = runtime.repo.claim_daily_job(scope.scope_id, local_date, "diary", 1)
        if not lease_token:
            return None
    try:
        start, end = _day_bounds(local_date)
        events = runtime.repo.events_between(scope, start, end, limit=400)
        if len(events) < 2:
            runtime.repo.finish_daily_job(scope.scope_id, local_date, "diary", 1,
                                          lease_token=lease_token)
            return None
        if writer is not None:
            summary = one_line(writer(events), 1200)
        else:
            parts = []
            for event in events[-40:]:
                who = "他" if event.source_kind == "user_report" else "我"
                text = one_line(event.text, 80)
                if text:
                    parts.append(f"{who}：{text}")
            summary = "今天：" + " / ".join(parts)
        # Fencing and output commit share one transaction; a replaced worker cannot
        # append another diary or finish the new worker's job.
        with runtime.repo.tx():
            if (runtime.repo.forget_epoch(scope.scope_id) != epoch
                    or not runtime.repo.daily_job_is_current(
                        scope.scope_id, local_date, "diary", 1, lease_token)):
                return None
            mem = runtime.memory.remember(
                scope, "diary", summary, [e.id for e in events],
                source_kind="inference", status="active", salience=0.55,
                source_quality=0.6,
                uncertainty_reason="日记是当时的解释，不是外部事实",
            )
            if mem.status == "redacted":
                runtime.repo.finish_daily_job(scope.scope_id, local_date, "diary", 1,
                                              error="source_removed", lease_token=lease_token)
                return None
            if not runtime.repo.finish_daily_job(scope.scope_id, local_date, "diary", 1,
                                                 lease_token=lease_token):
                raise RuntimeError("diary_lease_lost")
            return mem
    except Exception as exc:  # noqa: BLE001 - 作业失败要留痕，不阻塞聊天
        runtime.repo.finish_daily_job(
            scope.scope_id, local_date, "diary", 1, error=str(exc), lease_token=lease_token)
        raise
